# Teleop Floor

Three apps and one schedule between them. Sixteen operators over twelve
rigs, in four groups of three rigs and four operators. Each group keeps one
task for the whole shift so an operator works one skill all day.

The desk builds the schedule, the rig enforces it, and My Shift shows an
operator their own day out of it.

## Run

```sh
npm run serve           # http://127.0.0.1:8765/
```

That starts `apps/server/`, which serves the whole tree *and* carries the
push. `/rotation-desk-v1/` is the manager's screen, `/apps/my-shift/` is
an operator's own day, and `/apps/rig/` is one rig's screen.
`./serve.sh` is still there as a plain static server (no
push, python only) for cases where node is not available. No dependencies
on either path. `./build.sh` (or `npm run build`) regenerates the two
single-file distributions.

## Layout

```
index.html                     landing page linking the three apps
serve.sh                       serve the whole tree - python, static only
build.sh                       rebuild both dist/ files
package.json                   just for `npm test` - no runtime deps

packages/                      code that is imported, not deployed
  engine/rotation-engine.js    THE SCHEDULE. no DOM, no globals, runs under node too
  engine/*.test.js             engine, reference sheet, rotate, in-force
  demo-roster/demo-roster.js   the example floor the apps open with
  schema/payload.js            the shape the desk pushes and the rig consumes
  schema/*.test.js             the payload shape, and the event shape
  session/session.js           who is signed in - desk and my-shift share it
  brand/                       the mark and the palette, inlined at build

rotation-desk-v1/              the manager's app - the name is the version
  README.md                    the format, and what is deliberately fixed
  assets/desk.js               roster state, the sheets, "Push to floor"
  *.test.js                    the sheets, the sign-in gate, the roster, passwords
  dist/rotation-desk.html      single-file build, for publishing
  tools/make-single-file.py

apps/                          things that are deployed
  my-shift/                    an operator's own day, read only
    assets/my-shift.js         the countdown, the timeline, no controls
    *.test.js                  the screen, and changing a password
  rig/                         the operator's app, one per rig
    assets/rig.js              screen state machine, pedal map, event log
    schedule.json              fallback payload for a plain static deploy
    dist/rig.html              single-file build
    desktop/                   the shell the rig runs in, and its uploader
    docs/                      rig-side notes and the backend asks
    *.test.js                  thirteen - the clock, the journal, resync, faults
    manifest.webmanifest       installs full-screen landscape on the rig display
  server/                      the push transport
    server.js                  plain-node http, serves the tree and the push
    pushes.jsonl               append-only, one line per accepted push (gitignored)
    state.json                 a cache of the log's last line (gitignored)
    server.test.js             end-to-end: push a full floor, read each rig back
    store.test.js              the log survives a crash, a torn write, a wrong day

backend/                       the return arrow - FastAPI on Postgres
  core/                        domains and infrastructure, laid out to lift
  services/rigs/               routes, auth, people, mail
  alembic/versions/            the migrations
  tests/                       ledger, projection, floor, video, auth, reset
  tools/                       mint_account, preflight, benchmark

deploy/                        nginx, systemd, and DEPLOY.md
```

## Why the engine is shared

`packages/engine/rotation-engine.js` is loaded by the desk and the rig.
The rig must never compute a different answer from the desk that scheduled
it, so there is exactly one implementation and neither app owns it.

My Shift is deliberately outside that. It loads the engine for two clock
helpers and takes its turns from `/api/me/shift`, which hands back what
the desk pushed - the whole of the operator's shift, before it starts and
after it ends, not only while it runs. It computes no rotation, and a
screen that recomputed one in order to draw it would be the third answer
the invariant exists to prevent.

It is also plain enough to run headlessly, which is how it gets tested:

```js
global.window = global;
require("./packages/engine/rotation-engine.js");
const plan = window.RotationEngine.buildPlan(cfg, groups);
```

Run the tests:

```sh
npm test
```

### API

```
buildPlan(cfg, groups)         -> plan
auditPlan(plan)                -> { level, text, note, checks }
rigPayload(plan, rigId)        -> what one rig receives
whoIsOn(payload, minutes)      -> { turn, minutesLeft }, or null
holderAt(group, rigIndex, b)   -> who is on that rig in that block
rigStintLengths(plan)          -> the turn lengths a rig sees
```

Four more decide whether a sheet may be believed at all, which is the
mechanism behind a rig refusing an expired one:

```
shiftWindow(payload)           -> the half-open [start, end) it covers, or null
coversAt(payload, at)          -> whether it covers that instant
inForce(payloads, at)          -> the one whose window contains `at`, or null
minutesOnFloor(payload, at)    -> the clock helper My Shift borrows
```

`inForce` deliberately does not fall back. What to show when no shift is
running is the caller's decision, and choosing one here would let a
finished schedule look like a live one.

`cfg` is `{ shift, date, blockMin, stintBlocks, mode }`, mode `"hold"` or
`"rotate"`.

## How a schedule reaches a rig

```
desk  ->  POST /api/push  ->  server  ->  GET /api/rigs/:id/schedule.json  ->  whoIsOn()
```

The desk builds one payload per rig with `RE.rigPayload(plan, rigId)`, then
sends the whole floor in a single POST. The server validates every payload
against `packages/schema/payload.js` before accepting *any* of them - a bad
push is rejected at the door, so the floor never runs half-updated.

Each rig fetches its own payload from
`/api/rigs/:rigId/schedule.json`. The rig calls `whoIsOn(payload, now)` on a
timer. Given the payload and the clock there is nothing left to ask the
operator, which is what removed the login screen and the task picker.

A payload looks like this:

```json
{ "from": "08:15", "to": "09:00", "minutes": 45,
  "operator": { "id": "op-a4", "name": "Nadia Haddad", "personId": "..." },
  "relievedBy": "Aleksandr Petrov",
  "theyGoTo": "Think" }
```

`theyGoTo` has to travel in the payload: where an outgoing operator goes is
a fact about *their* day, not about this rig, and the rig cannot derive it.

### Endpoints

`apps/server/` carries the push and nothing else:

```
POST /api/push                       body: { payloads: [...] }
GET  /api/rigs/:rigId/schedule.json  -> the payload for that rig
GET  /api/state                      -> { pushedAt, rigs: [...] }
```

`backend/` is the other half, and the routes the screens actually sign in
against live there - `npm run serve` has none of them, which is why the
desk and My Shift open unlocked on it:

```
POST /api/rigs/:rigId/events         the ledger - append-only, resend-safe
GET  /api/rigs/:rigId/events/cursor  where a rig got to
GET  /api/me/shift                   an operator's own shift, derived from nothing
POST /api/auth/login, /logout, /password, /reset
GET  /api/health                     every switch that can be off
```

## The desk is locked to the sheet

`rotation-desk-v1/` draws exactly one format and offers no control that
can leave it: 15-minute blocks, hold-rig, three rigs and four operators
to a group, 32 rows from 0:00 to 7:45. Time runs down the side and the
operators run across the top, which is the reference sheet's own layout;
the second tab is the same schedule read rig-first.

The engine still knows the other rotations - the desk simply never asks.
`packages/engine/reference-sheet.test.js` holds the sheet transcribed by
hand and asserts the engine still draws it, so an engine change that
moves somebody fails the build instead of reaching the floor. See
`rotation-desk-v1/README.md`.

## The two rotations

**Hold rig** (what the desk uses) - an operator keeps one rig until their break, and
the rig changes hands every third block. This reproduces the reference sheet
exactly. It cannot produce equal-length turns when the whole crew changes at
once: at block 0 three operators take three rigs but only one can be off per
block, so the opening turns are forced to 1, 2 and 3 blocks. Those short
turns at each end are inherent, not a defect.

Hold needs an even number of off-turns per operator, so break and think come
out equal: `blocks % 8 == 0`. At 15 minutes that is 32 blocks, four breaks
and four thinks. 40-minute blocks give 12 and break it.

**Rotate rigs** (in the engine, not offered by the desk) - an operator
moves to the next rig each turn and takes a whole turn off. Every handover is the same length and the last one lands
exactly on the shift boundary, at the cost of moving rig every turn. Rotate
works if and only if the time on rig divides an hour:

```
turn x 8  divides  480     <=>     turn divides 60
```

Block length is only the resolution of the grid. 15 min x 4 and 20 min x 3
are the same schedule, because both are 60-minute turns.

## Shifts

Morning 08:00-16:00, Day 16:00-00:00, Night 00:00-08:00. All eight hours,
covering the day continuously, with the whole crew changing at the boundary.
Every operator gets 6 hours of work, 60 minutes of break and 60 of thinking
time.

## Scope

The reference sheet is wired up end to end: the desk builds a schedule,
pushes it to the floor, and each rig reads its own payload and runs the
right operator's turn from the clock. The rig has no login - given the
payload and the time there is nothing left to ask.

Three things this section used to list as open are now built. Episodes
persist, in an append-only ledger every other table is derived from.
Offline recovery works, because a rig journals to IndexedDB before the
network is touched and a resend is safe on `(rigId, eventId)`.
Authentication exists for the two screens that answer "whose" - the desk
and My Shift - as revocable session rows rather than signed tokens.

Crew changeover is settled too, and settled as *cold*: a rig comes to
rest at the end of the shift it was pushed and waits for a manager to
push the next day, rather than carrying on under yesterday's sheet.

What is genuinely still open is where calibrating the arms belongs, and
it is waiting on hardware rather than on a decision. `CLAUDE.md` carries
the reasoning for all of it.
