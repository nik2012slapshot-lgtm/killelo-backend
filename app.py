"""
Universal Kill-ELO — central backend.

A tiny Flask + SQLite service that keeps a SHARED, mod-users-only leaderboard.
Only clients running the mod report kills and register themselves, so the
leaderboard naturally contains only mod users. ELO is computed server-side
(authoritative), so every client sees the same numbers.

Endpoints:
  POST /register     {uuid, name}                         -> mark a player as a mod user
  POST /report       {killer_uuid, killer_name,
                      victim_uuid, victim_name}            -> apply a kill (deduplicated)
  GET  /leaderboard?limit=10                               -> top mod users by ELO
  GET  /player?uuid=...  (or ?name=...)                    -> one player's stats
  GET  /health                                             -> "ok"

Optional shared secret: set env var KILLELO_API_KEY; clients must then send
header  X-Api-Key: <same value>.
"""
import math
import os
import re
import json
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing

from flask import Flask, jsonify, render_template, request, g

DB_PATH = os.environ.get("KILLELO_DB", "killelo.db")
API_KEY = os.environ.get("KILLELO_API_KEY", "")  # empty = no auth
DEDUP_WINDOW_S = 10.0

# Ist DATABASE_URL gesetzt, laeuft alles auf Postgres, sonst auf SQLite.
# Grund: kostenlose Hoster (Render & Co.) haben kein bleibendes Dateisystem -
# eine SQLite-Datei dort waere nach jedem Neustart leer. Lokal und auf Hostern
# mit echter Festplatte bleibt SQLite die einfachere Wahl, deshalb kann das
# Backend beides.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
# Wie lange auf die Datenbank gewartet wird, bevor aufgegeben wird.
DB_CONNECT_TIMEOUT_S = 10
# Nach einem Fehlschlag so lange nicht erneut versuchen, das Schema anzulegen -
# sonst laeuft jede einzelne Anfrage in denselben Zeitfehler.
SCHEMA_RETRY_S = 30.0

# ---- ELO rules (mirrors the mod) -----------------------------------------
K = 50.0
MIN_CHANGE = 5
MAX_CHANGE = 50
FARM_MULT = [1.0, 0.5, 0.25, 0.0]
FARM_RESET_S = 24 * 60 * 60
FARM_VICTIM_KILLS_RESET = 3
NEW_PLAYER_KILLS = 10
NEW_PLAYER_AGE_S = 2 * 60 * 60
START_ELO = 1000
# Gleiche Schwelle wie EloManager.MIN_MATCHES_FOR_RANKING in der Mod: darunter
# ist die Wertung noch vorlaeufig und rutscht ans Ende der Liste.
MIN_MATCHES_RANKED = 3

app = Flask(__name__)


def connect():
    if USE_PG:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise RuntimeError(
                "DATABASE_URL ist gesetzt, aber psycopg fehlt - "
                "'pip install psycopg[binary]' ausfuehren."
            )
        # connect_timeout ist Pflicht, keine Feinheit: ohne ihn wartet psycopg
        # unbegrenzt. Ist die Datenbank nicht erreichbar, haengt damit der
        # einzige Arbeitsprozess fest und der ganze Dienst antwortet auf nichts
        # mehr - nach aussen sieht das aus wie ein Server, der stumm bleibt.
        return psycopg.connect(DATABASE_URL, row_factory=dict_row,
                               connect_timeout=DB_CONNECT_TIMEOUT_S)
    d = sqlite3.connect(DB_PATH)
    d.row_factory = sqlite3.Row
    return d


def sql(text):
    """Uebersetzt die SQLite-Schreibweise in die von Postgres.

    Beide Treiber koennen Platzhalter, nur mit anderem Zeichen. Alles Weitere
    ist bewusst so geschrieben, dass es in beiden Dialekten gilt - bis auf die
    wenigen Stellen, die unten einzeln unterschieden werden.
    """
    return text.replace("?", "%s") if USE_PG else text


def ex(d, text, args=()):
    return d.execute(sql(text), tuple(args))


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    # Zeitstempel sind Unix-Sekunden (~1.7 Milliarden). Postgres' REAL hat dafuer
    # zu wenige Stellen, deshalb dort DOUBLE PRECISION.
    ts = "DOUBLE PRECISION" if USE_PG else "REAL"
    schema = [
        """
        CREATE TABLE IF NOT EXISTS players (
            uuid TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            elo INTEGER NOT NULL DEFAULT 1000,
            kills INTEGER NOT NULL DEFAULT 0,
            deaths INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0,
            is_mod_user INTEGER NOT NULL DEFAULT 0,
            first_seen {ts} NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS farm (
            killer TEXT NOT NULL,
            victim TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            reset_at {ts} NOT NULL DEFAULT 0,
            victim_kills_snapshot INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (killer, victim)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS dedup (
            key TEXT PRIMARY KEY,
            ts {ts} NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS players_board ON players (is_mod_user, elo DESC)",
    ]
    with closing(connect()) as d:
        for statement in schema:
            d.execute(statement.format(ts=ts))
        d.commit()
    global _schema_ready
    _schema_ready = True


_schema_ready = False
_schema_next_try = 0.0


def ensure_schema():
    """Legt das Schema an, sobald die Datenbank erreichbar ist.

    Beim Start darf das scheitern: kostenlose Postgres-Anbieter fahren die
    Datenbank bei Leerlauf herunter, die erste Verbindung laeuft dann in einen
    Zeitfehler. Der Dienst stirbt daran nicht, sondern versucht es spaeter noch
    einmal - aber mit Abstand, sonst wartet jede Anfrage aufs Neue.
    """
    global _schema_next_try
    if _schema_ready or time.time() < _schema_next_try:
        return
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - Grund wird geloggt, Dienst laeuft weiter
        _schema_next_try = time.time() + SCHEMA_RETRY_S
        app.logger.warning("Schema noch nicht bereit: %s", exc)


@app.before_request
def _before(*_args):
    # /health muss ohne Datenbank antworten. Genau dann will man es lesen:
    # wenn etwas klemmt und die Frage lautet, ob ueberhaupt jemand zu Hause ist.
    if request.path == "/health":
        return
    ensure_schema()


def require_key():
    if API_KEY and request.headers.get("X-Api-Key", "") != API_KEY:
        return False
    return True


def get_player(d, uuid, name=""):
    row = ex(d, "SELECT * FROM players WHERE uuid = ?", (uuid,)).fetchone()
    if row is None:
        ex(
            d,
            "INSERT INTO players (uuid, name, elo, first_seen) VALUES (?, ?, ?, ?)",
            (uuid, name, START_ELO, time.time()),
        )
        row = ex(d, "SELECT * FROM players WHERE uuid = ?", (uuid,)).fetchone()
    elif name and row["name"] != name:
        ex(d, "UPDATE players SET name = ? WHERE uuid = ?", (name, uuid))
    return dict(row)


def expected(self_elo, opp_elo):
    return 1.0 / (1.0 + math.pow(10.0, (opp_elo - self_elo) / 400.0))


def base_change(winner_elo, loser_elo):
    change = K * (1.0 - expected(winner_elo, loser_elo))
    return max(MIN_CHANGE, min(MAX_CHANGE, round(change)))


def streak_multiplier(streak):
    if streak >= 20:
        return 1.30
    if streak >= 10:
        return 1.20
    if streak >= 5:
        return 1.10
    return 1.0


def upsert(d, table, columns, keys, values):
    """Einfuegen oder ueberschreiben - in beiden Dialekten.

    SQLite kennt dafuer INSERT OR REPLACE, Postgres nur ON CONFLICT.
    """
    placeholders = ", ".join(["?"] * len(columns))
    if USE_PG:
        updates = ", ".join(
            c + " = EXCLUDED." + c for c in columns if c not in keys
        )
        text = (
            "INSERT INTO " + table + " (" + ", ".join(columns) + ") VALUES (" + placeholders + ")"
            " ON CONFLICT (" + ", ".join(keys) + ") DO UPDATE SET " + updates
        )
    else:
        text = (
            "INSERT OR REPLACE INTO " + table + " (" + ", ".join(columns) + ")"
            " VALUES (" + placeholders + ")"
        )
    ex(d, text, values)


def farm_multiplier(d, killer_uuid, victim_uuid, victim_kills):
    now = time.time()
    row = ex(
        d, "SELECT * FROM farm WHERE killer = ? AND victim = ?", (killer_uuid, victim_uuid)
    ).fetchone()
    reset = row is None or now > row["reset_at"] or victim_kills >= row["victim_kills_snapshot"] + FARM_VICTIM_KILLS_RESET
    if reset:
        count = 0
        upsert(
            d,
            "farm",
            ["killer", "victim", "count", "reset_at", "victim_kills_snapshot"],
            ["killer", "victim"],
            (killer_uuid, victim_uuid, 1, now + FARM_RESET_S, victim_kills),
        )
    else:
        count = row["count"]
        ex(
            d,
            "UPDATE farm SET count = count + 1 WHERE killer = ? AND victim = ?",
            (killer_uuid, victim_uuid),
        )
    return FARM_MULT[min(count, len(FARM_MULT) - 1)]


def is_protected(player):
    age = time.time() - (player["first_seen"] or time.time())
    return player["kills"] < NEW_PLAYER_KILLS and age < NEW_PLAYER_AGE_S


@app.post("/register")
def register():
    if not require_key():
        return jsonify(error="unauthorized"), 401
    data = request.get_json(force=True, silent=True) or {}
    uuid = (data.get("uuid") or "").strip()
    name = (data.get("name") or "").strip()
    if not uuid:
        return jsonify(error="missing uuid"), 400
    d = db()
    get_player(d, uuid, name)
    ex(d, "UPDATE players SET is_mod_user = 1 WHERE uuid = ?", (uuid,))
    d.commit()
    # Der gespeicherte Stand geht mit zurueck. Die Mod rechnet lokal mit
    # allen Kaempfen, die sie im Chat sieht, meldet aber nur die eigenen -
    # dadurch bewertet sie fremde Spieler anders als die Rangliste und kommt
    # auf einen anderen Wert. Fuer den eigenen Spieler ist die Rangliste
    # massgeblich, sonst zeigen HUD und Website verschiedene Zahlen.
    row = get_player(d, uuid, name)
    return jsonify(ok=True, elo=row["elo"], kills=row["kills"],
                   deaths=row["deaths"], streak=row["streak"])


@app.post("/report")
def report():
    if not require_key():
        return jsonify(error="unauthorized"), 401
    data = request.get_json(force=True, silent=True) or {}
    ku, kn = (data.get("killer_uuid") or "").strip(), (data.get("killer_name") or "").strip()
    vu, vn = (data.get("victim_uuid") or "").strip(), (data.get("victim_name") or "").strip()
    if not ku or not vu or ku == vu:
        return jsonify(error="bad killer/victim"), 400

    d = db()
    now = time.time()
    # Deduplicate: same kill reported by several clients within the window.
    key = ku + "|" + vu
    ex(d, "DELETE FROM dedup WHERE ts < ?", (now - DEDUP_WINDOW_S,))
    if ex(d, "SELECT 1 FROM dedup WHERE key = ?", (key,)).fetchone():
        d.commit()
        k_row = ex(d, "SELECT elo, kills, deaths FROM players WHERE uuid = ?", (ku,)).fetchone()
        v_row = ex(d, "SELECT elo, kills, deaths FROM players WHERE uuid = ?", (vu,)).fetchone()
        antwort = {"ok": True, "deduped": True}
        if k_row:
            antwort.update(killer_uuid=ku, killer_elo=k_row["elo"],
                           killer_kills=k_row["kills"], killer_deaths=k_row["deaths"])
        if v_row:
            antwort.update(victim_uuid=vu, victim_elo=v_row["elo"],
                           victim_kills=v_row["kills"], victim_deaths=v_row["deaths"])
        return jsonify(antwort)
    upsert(d, "dedup", ["key", "ts"], ["key"], (key, now))

    killer = get_player(d, ku, kn)
    victim = get_player(d, vu, vn)

    base = base_change(killer["elo"], victim["elo"])
    mult = farm_multiplier(d, ku, vu, victim["kills"])
    new_streak = killer["streak"] + 1
    gain = round(base * mult * streak_multiplier(new_streak))
    loss = round(base * mult * (0.5 if is_protected(victim) else 1.0))

    ex(
        d,
        "UPDATE players SET elo = elo + ?, kills = kills + 1, streak = ? WHERE uuid = ?",
        (gain, new_streak, ku),
    )
    # MAX() nimmt in Postgres nur eine Spalte; das Gegenstueck heisst GREATEST.
    floor_at_zero = "GREATEST" if USE_PG else "MAX"
    ex(
        d,
        "UPDATE players SET elo = " + floor_at_zero + "(0, elo - ?),"
        " deaths = deaths + 1, streak = 0 WHERE uuid = ?",
        (loss, vu),
    )
    d.commit()
    # Nach dem Kampf beide Staende zurueckmelden, damit die Mod ihren
    # eigenen Wert nachziehen kann (siehe Erklaerung in /register).
    k_neu = ex(d, "SELECT elo, kills, deaths, streak FROM players WHERE uuid = ?", (ku,)).fetchone()
    v_neu = ex(d, "SELECT elo, kills, deaths, streak FROM players WHERE uuid = ?", (vu,)).fetchone()
    return jsonify(ok=True, gain=gain, loss=loss,
                   killer_uuid=ku, killer_elo=k_neu["elo"],
                   killer_kills=k_neu["kills"], killer_deaths=k_neu["deaths"],
                   victim_uuid=vu, victim_elo=v_neu["elo"],
                   victim_kills=v_neu["kills"], victim_deaths=v_neu["deaths"])


def player_json(row):
    return {
        "uuid": row["uuid"],
        "name": row["name"],
        "elo": row["elo"],
        "kills": row["kills"],
        "deaths": row["deaths"],
        "streak": row["streak"],
    }


def is_provisional(row):
    return (row["kills"] + row["deaths"]) < MIN_MATCHES_RANKED


@app.get("/leaderboard")
def leaderboard():
    limit = max(1, min(100, int(request.args.get("limit", 10))))
    offset = max(0, int(request.args.get("offset", 0)))
    query = (request.args.get("q") or "").strip()[:32]

    d = db()
    # Die Platznummer muss die *ganze* Rangliste meinen, nicht nur die
    # gefilterte Seite - deshalb wird sie in einer Unterabfrage vergeben und
    # erst danach gesucht und geblaettert.
    ranked = """
        SELECT uuid, name, elo, kills, deaths, streak,
               ROW_NUMBER() OVER (ORDER BY elo DESC, name ASC) AS pos
        FROM players WHERE is_mod_user = 1
    """
    args = []
    where = ""
    if query:
        # SQLite vergleicht mit LIKE bei ASCII ohnehin ohne Ruecksicht auf
        # Gross-/Kleinschreibung, Postgres braucht dafuer ILIKE.
        like = "ILIKE" if USE_PG else "LIKE"
        where = " WHERE name " + like + " ? ESCAPE '\\'"
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append("%" + escaped + "%")

    # Der Alias hinter der Unterabfrage ist in Postgres Pflicht.
    board = "(" + ranked + ") AS board"
    total = ex(d, "SELECT COUNT(*) AS n FROM " + board + where, args).fetchone()["n"]
    rows = ex(
        d,
        "SELECT * FROM " + board + where + " ORDER BY pos LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()

    return jsonify(
        players=[
            dict(player_json(r), pos=r["pos"], provisional=is_provisional(r)) for r in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@app.get("/stats")
def stats():
    """Kennzahlen fuer die Kopfzeile der Website."""
    d = db()
    row = ex(
        d,
        "SELECT COUNT(*) AS players, COALESCE(SUM(kills), 0) AS kills,"
        " COALESCE(MAX(elo), 0) AS top FROM players WHERE is_mod_user = 1",
    ).fetchone()
    return jsonify(players=row["players"], kills=row["kills"], top_elo=row["top"])


# ---------------------------------------------------------------------------
# Downloadzahlen von Modrinth und CurseForge
# ---------------------------------------------------------------------------
# Beide Seiten zaehlen getrennt, und keine der beiden Zahlen allein sagt, wie
# oft die Mod wirklich geholt wurde. Auf der Website steht deshalb die Summe.
#
# CurseForge braucht fuer die eigene Schnittstelle einen Schluessel. cfwidget
# ist ein oeffentlicher Dienst, der genau diese Zahl ohne Anmeldung liefert.
#
# Zwei Dinge sind hier wichtig, sonst faellt die ganze Seite aus:
#   * Ein kurzer Timeout. Es laeuft nur ein Arbeitsprozess - haengt der an
#     einer fremden Schnittstelle, antwortet die Seite gar nicht mehr.
#   * Ein Zwischenspeicher. Sonst fragen wir bei jedem Besucher erneut nach,
#     und die Zahl aendert sich ohnehin nur langsam.
MODRINTH_URL = "https://api.modrinth.com/v2/project/elo-system"
CURSEFORGE_URL = "https://api.cfwidget.com/minecraft/mc-mods/elo-system"
DL_TIMEOUT_S = 4
DL_CACHE_S = 1800  # 30 Minuten

_dl_cache = {"zeit": 0.0, "wert": None}


def _hole_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "killelo-leaderboard/1.0 (+https://killelo-backend.onrender.com)"})
    with urllib.request.urlopen(req, timeout=DL_TIMEOUT_S) as antwort:
        return json.loads(antwort.read().decode("utf-8"))


@app.get("/downloads")
def downloads():
    """Downloads beider Plattformen, zwischengespeichert."""
    jetzt = time.time()
    alt = _dl_cache["wert"]
    if alt is not None and jetzt - _dl_cache["zeit"] < DL_CACHE_S:
        return jsonify(alt)

    modrinth = curseforge = None
    try:
        modrinth = int(_hole_json(MODRINTH_URL)["downloads"])
    except Exception as exc:
        app.logger.warning("Modrinth-Zahl nicht erreichbar: %s", exc)
    try:
        curseforge = int(_hole_json(CURSEFORGE_URL)["downloads"]["total"])
    except Exception as exc:
        app.logger.warning("CurseForge-Zahl nicht erreichbar: %s", exc)

    # Faellt eine Seite aus, ist ihr letzter bekannter Wert besser als eine
    # Null - sonst faellt die Gesamtzahl auf der Website sichtbar ein.
    if alt:
        if modrinth is None:
            modrinth = alt.get("modrinth", 0)
        if curseforge is None:
            curseforge = alt.get("curseforge", 0)
    modrinth = modrinth or 0
    curseforge = curseforge or 0

    wert = {"modrinth": modrinth, "curseforge": curseforge,
            "total": modrinth + curseforge}
    # Nur merken, wenn ueberhaupt etwas ankam. Sonst bleibt der alte Stand
    # stehen und wir versuchen es beim naechsten Besucher gleich wieder.
    if wert["total"] > 0:
        _dl_cache["wert"] = wert
        _dl_cache["zeit"] = jetzt
    return jsonify(wert)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/player")
def player():
    d = db()
    uuid = request.args.get("uuid")
    name = request.args.get("name")
    if uuid:
        row = ex(d, "SELECT * FROM players WHERE uuid = ?", (uuid,)).fetchone()
    elif name:
        # COLLATE NOCASE kennt nur SQLite; LOWER() gilt in beiden.
        row = ex(d, "SELECT * FROM players WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
    else:
        return jsonify(error="need uuid or name"), 400
    if row is None:
        return jsonify(error="not found"), 404
    return jsonify(player_json(row))


@app.get("/health")
def health():
    return "ok"


def redact(text):
    """Entfernt Zugangsdaten aus einer Fehlermeldung.

    Postgres-Treiber schreiben die Verbindungsangaben gern in den Fehlertext,
    und dieser Endpunkt ist oeffentlich - das Passwort darf da nicht landen.
    """
    text = re.sub(r"://[^@\s]+@", "://***@", text)
    text = re.sub(r"password=\S+", "password=***", text)
    return text[:300]


@app.get("/health/db")
def health_db():
    """Sagt, ob die Datenbank erreichbar ist - und warum nicht.

    Absichtlich getrennt von /health: jenes muss auch dann antworten, wenn die
    Datenbank weg ist. Dieses hier ist zum Nachsehen, wenn etwas klemmt.
    """
    info = {
        "storage": "postgres" if USE_PG else "sqlite",
        "database_url_set": bool(DATABASE_URL),
        "schema_ready": _schema_ready,
    }
    started = time.time()
    try:
        ex(db(), "SELECT 1").fetchone()
        info["ok"] = True
    except Exception as exc:  # noqa: BLE001 - der Grund ist hier der Zweck
        info["ok"] = False
        info["error_type"] = type(exc).__name__
        info["error"] = redact(str(exc))
    info["took_ms"] = int((time.time() - started) * 1000)
    return jsonify(info), 200 if info.get("ok") else 503


# Beim Import wird bewusst *nicht* verbunden. Sonst haengt der Arbeitsprozess
# schon beim Start an einer Datenbank, die vielleicht gerade hochfaehrt - der
# Port ist dann offen, aber niemand antwortet. Das Schema entsteht bei der
# ersten Anfrage (siehe _before).

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
