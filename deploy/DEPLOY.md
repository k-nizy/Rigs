# Deploying the pilot

What this gets you: **twelve rigs filing real events into a real ledger,
a desk that pushes schedules and reads a live floor board, alerts for the
things nothing publishes, and a tested way to get it all back if the
database is lost.**

What it does **not** get you, and you should decide with both eyes open:

- **No video.** The rig is a browser page with no camera, so
  `setVideoSource` is null and nothing is ever recorded or uploaded. The
  whole video path exists and is tested; it has never carried a real
  byte. This is a floor-management system until the Tauri shell lands.
- **A rig runs one shift and stops.** The desk pushes only when somebody
  clicks *Push to floor*, and the rig reads its schedule only at boot.
  At each changeover somebody clicks push and the twelve kiosks are
  reloaded. That is 3 pushes and 36 reloads a day.

Both are rig-side. Neither is fixed by anything below.

---

## What you need

| | |
|---|---|
| One server | Linux, PostgreSQL 18, Python 3.11+, nginx |
| Twelve rig machines | anything that runs a modern browser, three pedals mapped to keys `1` `2` `3` |
| One desk machine | a browser |
| A network | the rigs and the server on the same switch |

Postgres 18 is what this has been developed and tested against. Nothing
knowingly depends on 18 specifically, but nothing has been run on less.

---

## 1. The database

```sh
sudo -u postgres psql -c "CREATE ROLE rigs LOGIN PASSWORD 'something-long';"
sudo -u postgres psql -c "CREATE DATABASE rigs OWNER rigs;"
```

Do **not** create a test database on this server. `TEST_DATABASE_URL` is
for developer machines; the suite drops and recreates every table it can
see. Preflight fails if the two ever point at the same database, and it
compares host and database name rather than the URL string, because two
URLs differing only by password are the same database.

## 2. The code

```sh
sudo useradd --system --home /srv/rigs rigs
sudo git clone <this repo> /srv/rigs
cd /srv/rigs/backend
python3 -m venv .venv
.venv/bin/pip install -e .
sudo mkdir -p /srv/rigs/spool && sudo chown rigs:rigs /srv/rigs/spool
```

## 3. Credentials

One token per rig, never shared. A token names exactly one rig and is
refused for any other - that is what stops one compromised machine
attributing work across the floor, and it is worth nothing if all twelve
carry the same string.

```sh
cd /srv/rigs/backend
.venv/bin/python -m tools.mint_tokens --desk --out /tmp/rig-secrets
```

It prints the `RIG_TOKENS` line for the server and writes one
`rig-config.js` per rig. Both come from one run so they cannot drift.

## 4. Configuration

`/etc/rigs/rigs.env`, owned by root, mode 600 - it holds the database
password and twelve tokens.

```sh
DATABASE_URL=postgresql+asyncpg://rigs:something-long@127.0.0.1:5432/rigs

RIG_TOKENS={"RIG-01":"...","RIG-02":"..."}      # from step 3
DESK_TOKEN=...                                   # from step 3
RIG_RATE_LIMIT_PER_MIN=120

STORAGE_LOCAL_ROOT=/srv/rigs/spool
VIDEO_KEEP_DAYS=90
VIDEO_PENDING_AFTER_DAYS=7
```

`backend/.env.example` documents every setting and why it has the value
it has. For MinIO as the spool, set `STORAGE_ENDPOINT` and its keys -
that is ask 4.2 and not yet answered, so the local directory is the
default.

## 5. Migrate, then check

```sh
cd /srv/rigs/backend
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m tools.preflight --strict
```

Preflight is the whole point of this section. It checks the database is
reachable, the migrations are at head, every table the models expect
exists, the spool can be written **and read back with a checksum**, all
three workers import, and that nothing is left open that should not be on
a floor. Exit codes: `0` ready, `1` broken, `2` open with `--strict`.

Do not skip it. Every failure it names is five minutes to fix here and an
hour to find later, because the symptom always appears somewhere else -
rigs refused, a board that never updates, a disk that quietly fills.

## 6. Run it

```sh
sudo cp /srv/rigs/deploy/systemd/*.service /srv/rigs/deploy/systemd/*.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rigs.target
systemctl status 'rigs-*'
```

Four processes: the API and three workers. They are separate on purpose -
a worker that dies must not take the API with it, and the API restarting
must not lose a worker's place. `backend/local_gateway.py` runs them
in-process for development and **does not deploy**; it also serves the
static apps, which nginx does here.

The API unit runs preflight before it starts, so it refuses to come up
against an unmigrated database rather than failing at the first request
that touches a missing column.

## 7. nginx

```sh
sudo cp /srv/rigs/deploy/nginx.conf /etc/nginx/sites-available/rigs
sudo ln -s /etc/nginx/sites-available/rigs /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

One origin for everything, and the URL layout mirrors the repository
exactly. That is deliberate: both pages load the engine by relative path
and only resolve correctly from `/apps/rig/` and `/rotation-desk-v1/`.
Tidier URLs break it, at deploy time, on a floor.

Same origin also matters for the token: the rig attaches it only to
same-origin calls, so a presigned upload URL pointing at an object store
never receives a floor credential.

## 8. Each rig machine

Copy that rig's config - **only its own**:

```sh
scp /tmp/rig-secrets/RIG-07/rig-config.js rig07:/srv/rigs/apps/rig/rig-config.js
```

Then a kiosk browser pointed at the server:

```sh
chromium --kiosk --noerrdialogs --disable-infobars \
         --autoplay-policy=no-user-gesture-required \
         http://<server>/apps/rig/
```

Without `rig-config.js` a rig has no identity and falls back to the
default, so **every machine asks for the same rig's schedule and files
every episode under that one id**. Nothing downstream can detect it -
from the server's side twelve rigs reporting as one is indistinguishable
from one very busy rig. Check each machine after provisioning.

Delete `/tmp/rig-secrets` when Ansible has them.

## 9. Prove it works

```sh
curl -s http://<server>/api/health
```

```json
{"ok":true,"rigAuth":"on","deskAuth":"on","floorReads":"open","rigRateLimit":"120/min"}
```

`"rigAuth":"on"` is the one to look at. If it says `off`, the floor is
running unauthenticated and any caller may file events for any rig.

Then, on the desk at `http://<server>/rotation-desk-v1/`: press **Push to
floor**, and confirm each rig picks up its own schedule and shows its own
id in the top-left. Work one take on one rig and watch it arrive:

```sh
curl -s http://<server>/api/floor/state | jq '.backend'
# {"unprojectedEvents": 0, "projectionLagSecs": 0.0}
```

---

## Running it

**Three numbers worth a dashboard.**

| | |
|---|---|
| `backend.projectionLagSecs` on `/api/floor/state` | if this climbs, facts have stopped and every board is showing yesterday |
| `spool.bytes` on `/api/floor/video` | should oscillate, never climb. A climbing spool ends with a full disk, and that fails *backwards*: `confirm()` starts refusing, so rigs correctly keep their copies and the rig SSDs fill too |
| open alerts on `/api/floor/alerts` | seven kinds, including the two absences nothing publishes |

**Logs.** Journald, per unit. Every line carries a request id taken from
`X-Request-ID` when nginx sets one, so a line in the service joins a line
in nginx. No token, password or connection string is ever logged.

**Backups.** The ledger is the system; everything else is derived.

```sql
COPY (SELECT envelope FROM rig_events ORDER BY rig_id, seq)
  TO '/backup/rig_events.jsonl';
```

Restoring is re-POSTing those envelopes through the ordinary ingest
route, in batches, grouped by rig. It inherits ingest's idempotency, so
it can be interrupted, resumed, or run twice by two people at once - all
three are tested in `backend/tests/test_recovery.py`.

Two things to know before you need it. Schedules are **not** in the
ledger, so a restore returns correct facts and an empty board until the
desk pushes again. And at ~400 events/second, rebuilding the facts after
restoring a year of ledger takes about **two and a half hours** - the
restore is fast, the projection behind it is not.

**Capacity.** 12 rigs produce ~9,800 events/day, about 0.11/second.
Projection runs ~400/second and the sweep uses 0.21% of its 15-second
budget, both measured by `python -m tools.benchmark`. Events are not
going to be the problem. Video would be, at 2.72 TB/day and 245 TB steady
state at 90-day retention - but no video moves until there is a camera.

---

## Rolling back

```sh
sudo systemctl stop rigs.target
cd /srv/rigs && sudo -u rigs git checkout <previous>
cd backend && .venv/bin/python -m alembic downgrade -1     # only if needed
.venv/bin/python -m tools.preflight
sudo systemctl start rigs.target
```

Every migration has a tested `downgrade`. Two build their indexes
`CONCURRENTLY`, which cannot run inside a transaction - if one fails part
way Postgres leaves an `INVALID` index behind, and the fix is to drop it
and run again rather than assume it is usable.

**Never point the test suite at this database.** It drops every table it
can see. Preflight checks for it; that check is the only thing between a
tired evening and an empty ledger.
