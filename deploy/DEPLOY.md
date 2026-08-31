# Deploying the pilot

What this gets you: **twelve rigs filing real events into a real ledger,
a desk that pushes schedules and reads a live floor board, a screen where
an operator sees their own day, alerts for the things nothing publishes,
and a tested way to get it all back if the database is lost.**

Three screens, and only two of them need a person to sign in. The rig
authenticates as a machine and nobody standing at it types anything. The
desk is for managers, My Shift is for operators, and both refuse the other
- so `mint_account` is not optional if you want either of them used.

What it does **not** get you, and you should decide with both eyes open:

- **No video.** The rig is a browser page with no camera, so
  `setVideoSource` is null and nothing is ever recorded or uploaded. The
  whole video path exists and is tested; it has never carried a real
  byte. This is a floor-management system until the Tauri shell lands.
- **Somebody has to push, once a day, before the first shift starts.** A
  push covers a whole calendar day - midnight to midnight, three shifts,
  twelve rigs - and the rigs pick it up by themselves, so the daily cost
  is one click and no reloads. But nothing pushes on its own. Miss a day
  and the rigs show Standby and refuse to start a take rather than run
  yesterday's sheet, which is deliberate: an expired sheet still
  cheerfully names somebody at half past midnight, and a take filed under
  the wrong operator is silent and permanent where idle time is loud and
  recoverable.

  **Before, not during**, and that is the rule rather than a preference.
  A push covers the hours already gone but cannot re-file the work done
  in them. A crew that starts at 09:00 against a sheet corrected at 09:20
  has twenty minutes of takes recorded against whoever the previous push
  named, permanently. The same goes for a cover: if somebody is off and
  another operator stands in, assign them at the desk and push before
  they start - the rig is never asked who is standing at it.

  Watch for the Night shift. It runs 00:00-08:00 and therefore belongs
  to the date it *starts* on, so the night that follows Tuesday is on
  Wednesday's sheet. A floor only ever pushed in the morning has nothing
  for the crew who arrive at midnight. The desk's Live badge reads
  "Nothing scheduled for now" when that happens.

The first is rig-side and is not fixed by anything below. The second is
a habit, and the desk is built to make a missed one visible.

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
.venv/bin/python -m tools.mint_tokens --desk --addresses
```

It prints the `RIG_TOKENS` line for the server and a `RIG_ADDRESSES`
skeleton with the same twelve rigs in it, for you to fill in with the
address each machine calls from. Nothing is written to the rig: the
service hands each machine its own identity, and step 8 says why.

## 4. Configuration

`/etc/rigs/rigs.env`, owned by root, mode 600 - it holds the database
password and twelve tokens.

```sh
DATABASE_URL=postgresql+asyncpg://rigs:something-long@127.0.0.1:5432/rigs

RIG_TOKENS={"RIG-01":"...","RIG-02":"..."}      # from step 3
RIG_ADDRESSES={"RIG-01":"10.0.0.11","RIG-02":"10.0.0.12"}
DESK_TOKEN=...                                   # from step 3
RIG_RATE_LIMIT_PER_MIN=120

STORAGE_LOCAL_ROOT=/srv/rigs/spool
VIDEO_KEEP_DAYS=90
VIDEO_PENDING_AFTER_DAYS=7
```

`RIG_ADDRESSES` is which machine is which. It has to name the same
twelve rigs as `RIG_TOKENS`: a rig with a token and no address can never
be told who it is, and one with an address and no token is handed one
every call refuses. Preflight compares them, because both failures show
up somewhere else entirely.

That means the twelve rigs need fixed addresses - DHCP reservations are
enough. What it buys is that a machine which is not at RIG-07's address
cannot obtain RIG-07's token. What it does not buy is anything against
somebody who can already take that address; it decides who is handed a
credential, and the credential is what authenticates.

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

## 5a. The people who sign in

Nothing above creates an account, and with none in the database both the
desk and My Shift open to anybody who can reach them. That is deliberate -
it is the same off-until-configured rule as `RIG_TOKENS` - but on a floor
it is the wrong end of it.

```sh
cd /srv/rigs/backend
.venv/bin/python -m tools.mint_account manager  --email r.osei@verlet.co  --name "Ruth Osei"
.venv/bin/python -m tools.mint_account operator --email m.chen@verlet.co  --name "Mei Chen" --operator-id op-a2
.venv/bin/python -m tools.mint_account list
```

With no `--password` one is generated and printed. That is the better
default: strong, used once, changed by the person. It is printed to a
terminal, which is not a safe place to leave it - hand it over and clear
the scrollback.

Three things worth knowing before you run it:

- **A manager names no operator; an operator must name one.** The
  `--operator-id` is the id on the sheet (`op-a2`), and My Shift has
  nothing to look itself up by without it. The database enforces this,
  so a wrong one is refused rather than discovered later by a screen
  showing somebody else's day.
- **There is no sign-up page and there should not be.** A floor has two
  or three managers and sixteen operators on a roster somebody already
  maintains. This is also how the first manager exists at all - a seeded
  default account would be a known password on every deployment.
- **Disable, never delete.** `mint_account disable` ends every session
  that person has open and keeps the name, so a later audit row still
  resolves to somebody.

Check it took: `/api/health` reports `personAuth` once anybody has an
account, and `SESSION_COOKIE_SECURE` must be true behind HTTPS or the
session cookie travels in the clear.

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

Nothing is copied to the rig **while the kiosk is a browser**. A kiosk
browser, pointed at the server:

```sh
chromium --kiosk --noerrdialogs --disable-infobars \
         --autoplay-policy=no-user-gesture-required \
         http://<server>/apps/rig/
```

The machine is identified by the address it calls from, which is what
step 4 put in `RIG_ADDRESSES`. Give it a fixed one first - a DHCP
reservation is enough.

**This used to say to `scp` a per-rig `rig-config.js` onto each machine,
and that never worked.** The page loads `rig-config.js` with a relative
path and the kiosk loads the page *from the server*, so the browser asks
the server for its copy and the file on the rig is never read. Following
the old instructions gave twelve machines one blank config, every one of
them fell back to the same hard-coded default, and every episode on the
floor would have been filed under one rig id. Nothing downstream could
have caught it: from the service's side, twelve rigs reporting as one is
exactly what one very busy rig looks like.

So the server decides. `/apps/rig/rig-config.js` is proxied to the
service, which answers per caller from `RIG_ADDRESSES` and hands back
that rig's id and its own token - never another's.

### When the Tauri shell replaces the browser

Not yet - `apps/rig/desktop/` builds and is tested, and this floor still
runs the chromium kiosk above. When it does land, two things go on each
rig and neither of them comes from `deploy/systemd/`, which is the
server's directory and is copied to the server wholesale:

```sh
sudo useradd --system --create-home --shell /usr/sbin/nologin rig
sudo install -m755 rig-desktop rig-uploader /usr/bin/
sudo install -d -m750 /etc/rigs
sudo tee /etc/rigs/rig.env <<'ENV'
RIG_URL=http://<server>/apps/rig/
ENV
sudo cp /srv/rigs/deploy/systemd-rig/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rig-uploader
```

That env file carries the one deployment fact both of them read, and the
unit will refuse to start without it - which is the right way round, since
a rig with no floor address has nowhere to send anything.

The same address on all twelve machines - it is not an identity. Which
rig this is still comes from the address the request arrives on, exactly
as it does for the browser kiosk above.

The uploader is a separate process on purpose. Uploading used to happen
inside the page, so a reload or a crashed webview stopped it - the work
stayed safe on disk and nothing was carrying it. This one has no window:
`ldd` on it names no GTK and no WebKit, so it keeps running when the
graphical session does not. It writes only `/var/lib/rig`, which its
unit creates with `StateDirectory`.

A machine the floor cannot place is told it is nobody, and the rig then
**refuses to work**: it shows "This rig has no identity", names the
address it called from, and offers no pedal to press. That is the same
trade as Standby for an expired sheet. Idle time is loud, cheap and
recoverable; work filed under the wrong rig is silent and permanent, and
there is no correction mechanism in the ledger.

Check each machine after provisioning by reading the id in the top-left.
That is now a real check rather than a formality, because a wrong answer
is visible on the screen instead of invisible everywhere.

Delete `/tmp/rig-secrets` once the tokens are in `/etc/rigs/rigs.env`.

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
