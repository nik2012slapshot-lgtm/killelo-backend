# Universal Kill-ELO — Backend & Website

Flask service for the **shared, mod-users-only** leaderboard, plus the public
web page that shows it. Only mod clients register and report kills, so the
leaderboard contains only mod users. ELO is computed here (authoritative), so
every client shows the same numbers.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Then open <http://localhost:8000/> — the leaderboard page. `/health` returns `ok`.

```bash
python test_app.py
```

runs 20 checks against a throwaway database. No server needed.

## Endpoints

| Method | Path | Body / Query | Purpose |
| --- | --- | --- | --- |
| GET | `/` | – | the leaderboard web page |
| POST | `/register` | `{uuid, name}` | mark a player as a mod user |
| POST | `/report` | `{killer_uuid, killer_name, victim_uuid, victim_name}` | apply a kill (deduplicated) |
| GET | `/leaderboard` | `?limit=25&offset=0&q=name` | ranked mod users, searchable |
| GET | `/stats` | – | player count, kills tracked, top elo |
| GET | `/player` | `?uuid=...` or `?name=...` | one player's stats |
| GET | `/health` | – | returns `ok` |

`/leaderboard` returns `{players, total, offset, limit}`. Every player carries
its **global** `pos` — searching and paging never renumber the ranking. Players
with fewer than 3 recorded fights are `provisional` and sort to the bottom, the
same rule the mod uses (`EloManager.MIN_MATCHES_FOR_RANKING`).

Optional auth: set env var `KILLELO_API_KEY`; clients must then send header
`X-Api-Key: <same value>`. Only the two POST routes check it — the page and
the read endpoints stay public.

## Storage: SQLite or Postgres

| `DATABASE_URL` | Storage | Use when |
| --- | --- | --- |
| unset | SQLite file (`KILLELO_DB`, default `killelo.db`) | local, or a host with a real disk |
| `postgresql://…` | Postgres | free hosts, whose disks are wiped on every restart |

The same code serves both; the handful of dialect differences are marked with
`USE_PG` in `app.py`. Switching is only an environment variable — no code change.

## Free hosting, step by step

Free web hosts do **not** keep files. A SQLite database there is empty again
after every restart, and free instances restart often. So the data goes into a
free Postgres database, which is kept separately from the web service.

**1 — Database (Neon, free, no expiry).** Sign up at <https://neon.com>, create
a project, and copy the connection string (`postgresql://…`).

**2 — Web service (Render, free).** Put this folder in a GitHub repo, then at
<https://render.com> → **New → Web Service** → connect the repo:

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4`
- **Environment variable:** `DATABASE_URL` = the string from step 1

Deploy, then open the URL Render gives you. The tables are created on first
request.

### What the free tier costs you

- The service **sleeps after ~15 min idle**; the first request after that takes
  ~30 seconds. Every following one is instant.
- Neon's free database also idles, so the first query after a pause is slow.
  The service no longer dies when the database is briefly unreachable — it
  retries on the next request.
- Neon free: 0.5 GB. A leaderboard row is a few dozen bytes, so this is not a
  limit you will reach.

## Note

The mod does **not** talk to this backend yet — no version of it makes any HTTP
request. Until that is added, the page shows an empty leaderboard.
