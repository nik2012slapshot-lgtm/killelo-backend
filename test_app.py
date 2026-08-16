"""Durchlauf gegen eine frische Datenbank - ohne laufenden Server.

Aufruf:  python test_app.py

Nutzt Flasks Test-Client, startet also nichts im Netz. Die Datenbank ist eine
Wegwerf-Datei, die am Anfang geloescht wird.

Achtung: Geprueft wird damit der SQLite-Weg. Der Postgres-Weg (DATABASE_URL
gesetzt) benutzt teils anderes SQL - siehe USE_PG in app.py - und laesst sich
nur gegen eine echte Postgres-Datenbank pruefen.
"""
import os
import sys
import tempfile
import time

DB = os.path.join(tempfile.gettempdir(), "killelo_test.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["KILLELO_DB"] = DB
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

client = app.app.test_client()
failed = []


def check(label, condition, extra=""):
    print(("  OK  " if condition else "FAIL  ") + label + ("  " + str(extra) if extra else ""))
    if not condition:
        failed.append(label)


# ---- Anmelden ------------------------------------------------------------
for i, name in enumerate(["Alpha", "Bravo", "Charlie"]):
    r = client.post("/register", json={"uuid": "u%d" % i, "name": name})
    check("register " + name, r.status_code == 200 and r.get_json()["ok"])

# ---- Kills melden --------------------------------------------------------
kill = {"killer_uuid": "u0", "killer_name": "Alpha",
        "victim_uuid": "u1", "victim_name": "Bravo"}
first = client.post("/report", json=kill).get_json()
check("report gibt gain und loss", first.get("gain", 0) > 0 and first.get("loss", 0) > 0, first)

again = client.post("/report", json=kill).get_json()
check("Doppelmeldung wird verworfen", again.get("deduped") is True, again)

check("Selbstmord wird abgelehnt",
      client.post("/report", json={"killer_uuid": "u0", "victim_uuid": "u0"}).status_code == 400)

for _ in range(3):
    client.post("/report", json={"killer_uuid": "u0", "killer_name": "Alpha",
                                 "victim_uuid": "u2", "victim_name": "Charlie"})
    time.sleep(0.02)

# ---- Rangliste lesen -----------------------------------------------------
board = client.get("/leaderboard?limit=10").get_json()
check("leaderboard zaehlt alle", board["total"] == 3, board["total"])
check("Platznummern lueckenlos", [p["pos"] for p in board["players"]] == [1, 2, 3],
      [p["pos"] for p in board["players"]])
check("Alpha fuehrt", board["players"][0]["name"] == "Alpha", board["players"][0]["name"])

stats = client.get("/stats").get_json()
check("stats zaehlt Spieler", stats["players"] == 3, stats)
check("stats zaehlt Kills", stats["kills"] >= 1, stats)

# ---- Suche ---------------------------------------------------------------
found = client.get("/leaderboard?q=rav").get_json()
check("Suche findet Teilstring",
      found["total"] == 1 and found["players"][0]["name"] == "Bravo", found["total"])
check("Suche behaelt den echten Platz", found["players"][0]["pos"] != 1,
      found["players"][0]["pos"])
check("Prozentzeichen sucht woertlich statt als Joker",
      client.get("/leaderboard?q=%25").get_json()["total"] == 0)
check("Suche ohne Treffer", client.get("/leaderboard?q=zzzz").get_json()["total"] == 0)

# ---- Blaettern -----------------------------------------------------------
page = client.get("/leaderboard?limit=2&offset=2").get_json()
check("Blaettern liefert den Rest",
      len(page["players"]) == 1 and page["players"][0]["pos"] == 3, page["players"])

# ---- Einzelabfrage und Seite --------------------------------------------
one = client.get("/player?name=alpha").get_json()  # Kleinschreibung mit Absicht
check("player findet ohne Ruecksicht auf Gross/Klein", one.get("name") == "Alpha", one)
check("player meldet Unbekannte mit 404", client.get("/player?name=niemand").status_code == 404)
check("Startseite laedt", client.get("/").status_code == 200)
check("health antwortet", client.get("/health").data == b"ok")

print("\n" + ("ALLES GRUEN" if not failed else "FEHLER: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
