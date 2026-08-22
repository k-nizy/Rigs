# Teleop Floor

Two apps and one schedule between them. Sixteen operators over twelve rigs,
in four groups of three rigs and four operators. Each group keeps one task
for the whole shift so an operator works one skill all day.

## Run

```sh
./serve.sh              # http://127.0.0.1:8765/
```

`/desk/` is the manager's screen, `/rig/` is one rig's screen. No build step
and no dependencies beyond the Google Fonts stylesheet. `./build.sh`
regenerates the two single-file distributions.

## Layout

```
index.html                     landing page linking the two apps
serve.sh                       serve the whole tree
build.sh                       rebuild both dist/ files

shared/
  rotation-engine.js           THE SCHEDULE. no DOM, no globals, runs under node too
  demo-roster.js               the example floor both apps open with

desk/                          the manager's app
  index.html
  assets/desk.css
  assets/desk.js               roster state and rendering only
  dist/rotation-desk.html      single-file build, for publishing
  tools/make-single-file.py

rig/                           the operator's app, one per rig
  index.html
  assets/rig.css
  assets/rig.js                screen state machine, pedal map, event log
  schedule.json                what the desk pushed to this rig
  dist/rig.html                single-file build, push baked in
  manifest.webmanifest         installs full-screen landscape on the rig display
  tools/make-icons.py
  tools/make-single-file.py
```

## Why the engine is shared

`shared/rotation-engine.js` is loaded by both apps. The rig must never
compute a different answer from the desk that scheduled it, so there is
exactly one implementation and neither app owns it.

It is also plain enough to run headlessly, which is how it gets tested:

```js
global.window = global;
require("./shared/rotation-engine.js");
const plan = window.RotationEngine.buildPlan(cfg, groups);
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
desk  ->  rigPayload()  ->  rig/schedule.json  ->  whoIsOn()  ->  operator signed in
```

The desk emits one payload per rig: its id, its group's task, and every turn
in the shift with who holds it, who relieves them, and where the outgoing
operator goes.

```json
{ "from": "08:15", "to": "09:00", "minutes": 45,
  "operator": { "id": "op-a4", "name": "Nadia Haddad" },
  "relievedBy": "Aleksandr Petrov",
  "theyGoTo": "Think" }
```

The rig calls `whoIsOn(payload, now)` on a timer. Given the payload and the
clock there is nothing left to ask the operator, which is what removed the
login screen and the task picker.

`theyGoTo` has to travel in the payload: where an outgoing operator goes is
a fact about *their* day, not about this rig, and the rig cannot derive it.

## The two rotations

**Hold rig** (default) - an operator keeps one rig until their break, and
the rig changes hands every third block. This reproduces the reference sheet
exactly. It cannot produce equal-length turns when the whole crew changes at
once: at block 0 three operators take three rigs but only one can be off per
block, so the opening turns are forced to 1, 2 and 3 blocks. Those short
turns at each end are inherent, not a defect.

Hold needs an even number of off-turns per operator, so break and think come
out equal: `blocks % 8 == 0`. At 15 minutes that is 32 blocks, four breaks
and four thinks. 40-minute blocks give 12 and break it.

**Rotate rigs** - an operator moves to the next rig each turn and takes a
whole turn off. Every handover is the same length and the last one lands
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

## Not wired up

- No persistence or push transport. `rig/schedule.json` is written by hand
  today; the desk's "Copy for this rig" is the manual version of the push.
- The rig's event log names the bucket each event belongs in but nothing is
  stored. A reload starts a fresh shift.
- Crew changeover at the shift boundary is unmodelled: whether there is a
  handover window or the incoming operator takes the rig cold.
