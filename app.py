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
import sqlite3
import time
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
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
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


def ensure_schema():
    """Legt das Schema an, sobald die Datenbank erreichbar ist.

    Beim Start darf das noch scheitern: kostenlose Postgres-Anbieter fahren die
    Datenbank bei Leerlauf herunter, die erste Verbindung laeuft dann in einen
    Zeitfehler. Frueher waere damit der ganze Dienst gestorben - jetzt wird es
    beim naechsten Aufruf einfach noch einmal versucht.
    """
    if _schema_ready:
        return
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - Grund wird geloggt, Dienst laeuft weiter
        app.logger.warning("Schema noch nicht bereit: %s", exc)


@app.before_request
def _before(*_args):
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
    return jsonify(ok=True)


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
        return jsonify(ok=True, deduped=True)
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
    return jsonify(ok=True, gain=gain, loss=loss)


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
               ROW_NUMBER() OVER (
                   ORDER BY (kills + deaths >= ?) DESC, elo DESC, name ASC
               ) AS pos
        FROM players WHERE is_mod_user = 1
    """
    args = [MIN_MATCHES_RANKED]
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


ensure_schema()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
