# Teleop Floor

Two apps and one schedule between them. Sixteen operators over twelve rigs,
in four groups of three rigs and four operators. Each group keeps one task
for the whole shift so an operator works one skill all day.

## Run

```sh
npm run serve           # http://127.0.0.1:8765/
```

That starts `apps/server/`, which serves the whole tree *and* carries the
push. `/rotation-desk-v1/` is the manager's screen, `/apps/rig/` is one
rig's screen. `./serve.sh` is still there as a plain static server (no
push, python only) for cases where node is not available. No dependencies
on either path. `./build.sh` (or `npm run build`) regenerates the two
single-file distributions.

## Layout

```
index.html                     landing page linking the two apps
serve.sh                       serve the whole tree
build.sh                       rebuild both dist/ files
package.json                   just for `npm test` - no runtime deps

packages/                      code that is imported, not deployed
  engine/rotation-engine.js    THE SCHEDULE. no DOM, no globals, runs under node too
  engine/engine.test.js        headless assertions on the engine
  engine/reference-sheet.test.js  the sheet transcribed cell by cell
  demo-roster/demo-roster.js   the example floor both apps open with
  schema/payload.js            the shape the desk pushes and the rig consumes
  schema/schema.test.js        every demo payload has to validate

rotation-desk-v1/              the manager's app - the name is the version
  README.md                    the format, and what is deliberately fixed
  index.html
  assets/desk.css
  assets/desk.js               roster state, the sheets, "Push to floor"
  dist/rotation-desk.html      single-file build, for publishing
  tools/make-single-file.py

apps/                          things that are deployed
  rig/                         the operator's app, one per rig
    index.html
    assets/rig.css
    assets/rig.js              screen state machine, pedal map, event log
    schedule.json              fallback payload for a plain static deploy
    dist/rig.html              single-file build, push baked in
    manifest.webmanifest       installs full-screen landscape on the rig display
    tools/make-icons.py
    tools/make-single-file.py
  server/                      the push transport
    server.js                  plain-node http, serves the tree and the push
    server.test.js             end-to-end: push a full floor, read each rig back
    state.json                 current pushed state, regenerated on every push
```

## Why the engine is shared

`packages/engine/rotation-engine.js` is loaded by both apps. The rig must
never compute a different answer from the desk that scheduled it, so there
is exactly one implementation and neither app owns it.

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
buildPlan(cfg, groups)     -> plan
auditPlan(plan)            -> { level, text, note, checks }
rigPayload(plan, rigId)    -> what one rig receives
whoIsOn(payload, minute)   -> { turn, minutesLeft }
cleanStints(blockMin)      -> times on rig that divide the hour
cleanBlocks(options)       -> grid sizes that give equal break and think
```

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
  "operator": { "id": "op-a4", "name": "Nadia Haddad" },
  "relievedBy": "Aleksandr Petrov",
  "theyGoTo": "Think" }
```

`theyGoTo` has to travel in the payload: where an outgoing operator goes is
a fact about *their* day, not about this rig, and the rig cannot derive it.

### Endpoints

```
POST /api/push                       body: { payloads: [...] }
GET  /api/rigs/:rigId/schedule.json  -> the payload for that rig
GET  /api/state                      -> { pushedAt, rigs: [...] }
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

Everything the reference sheet calls for is now wired up: the desk
generates a schedule, pushes it to the floor, and each rig reads its own
payload and signs the right operator in when their turn starts.

The sheet does not call for anything else. In particular it does not
speak to episode persistence, offline recovery, authentication, or crew
changeover at the shift boundary - those are open questions to raise
when the product is ready to answer them, not implicit requirements.
