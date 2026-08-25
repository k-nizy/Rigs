# Rigs — design notes for anyone opening this repo

This is the "how it all fits" doc. The README explains *what* the system
is; this file records the load-bearing decisions behind it, in the same
language the README uses so the two cannot drift.

## The system in one sentence

Two static web apps — **Desk** (the manager's) and **Rig** (one per
physical rig) — sharing one engine, so the schedule the desk hands out and
the one the rig enforces are literally the same code.

## The floor

16 operators, 12 rigs, 4 groups of (3 rigs + 4 operators). Each group
keeps one task for the whole shift so an operator works one skill all day.
Three 8-hour shifts (Morning 08:00–16:00, Day 16:00–00:00, Night
00:00–08:00), with the whole crew swapping at the boundary. Every operator
must end the shift with **6h work + 60min break + 60min think** — that is
the budget the audit checks against.

## Repository shape

```
packages/           imported, not deployed
  engine/               the schedule algorithm + headless tests
  demo-roster/          the example floor both apps open with
  schema/               payload shape, validated on every push

rotation-desk-v1/   deployed - manager's screen. the name is the version
apps/               deployed
  rig/                  operator's per-rig screen
  server/               push transport
```

If code is *imported by an app*, it lives under `packages/`. If code is
*shipped and run*, it is a top-level app folder.

The desk sits at the top level under its version name rather than in
`apps/` because the version *is* the contract: `rotation-desk-v1` draws
one format, and a change of format is a new folder, not an edit to this
one. That name does not get refactored away.

## The load-bearing invariant

> The rig must never compute a different answer from the desk that
> scheduled it.

That is why `packages/engine/rotation-engine.js` exists as one file with
no DOM and no globals — both apps load it, and it runs under node so the
tests in `packages/engine/engine.test.js` can pin its behaviour. If you
change the engine, the tests are the guardrail; if you change something
that affects the schedule and *don't* touch the engine, you have
introduced drift.

## The sheet is the format

The desk draws one thing: the reference sheet. Time down the side in
15-minute rows, `0:00` to `7:45`; the four operators across the top;
cells reading `Work RIG-01` / `Break` / `Think`; one sheet per group,
four of them two-up. The second tab is the identical schedule read
rig-first — `Rig 1 | Rig 2 | Rig 3` across the top, `Op 2 / M. Chen` in
the cells — which is the visualisation the sheet puts beside group A.

There is no grid-size control and no rotate/hold switch on the desk.
That is deliberate and is what `v1` in the folder name means:

```
15-minute blocks · hold rig · 3 rigs / 4 operators · 32 rows
one operator off per block, the slot walking 4, 3, 2, 1
four blocks of Break, then four of Think
6h work + 60 min break + 60 min think, per operator
```

The engine still implements `rotate` and other block sizes and they are
still tested — the desk never asks for them. If the format has to
change, that is `rotation-desk-v2`, not a setting.

`packages/engine/reference-sheet.test.js` is the guardrail: the sheet
transcribed by hand as data (which operator is on which rig in all 32
blocks, who is off and whether it is written Break or Think), asserted
against the engine. It also reads a pushed payload back through
`whoIsOn()` block by block, so the rig is checked against the same
transcription the desk is. If that file fails, the engine and the sheet
have parted company.

## The two rotation modes

**Hold rig** (the one the desk draws) — an operator keeps one rig until their break, and
the rig changes hands every third block. Fits the reference sheet
exactly. Necessarily produces 1/2/3-block "stub" turns at each end of the
shift because only one operator can be off per block; that is a property
of the simultaneous crew change, not a defect. Requires `nBlocks % 8 === 0`
so break and think come out equal.

**Rotate rigs** — an operator moves to the next rig each turn and takes
a whole turn off. Every handover is the same length and the last one
lands on the shift boundary. Works **iff** time-on-rig divides 60. Block
length is only the resolution of the grid — 15×4 and 20×3 are the same
schedule because both are 60-minute turns.

Off-runs alternate BREAK / THINK inside the engine, which is what keeps
the two budgets equal without a second rule.

## The payload — how a schedule reaches a rig

```
desk  →  rigPayload()  →  apps/rig/schedule.json  →  whoIsOn()  →  operator signed in
```

The desk emits one payload per rig. Its shape is:

```json
{
  "rigId": "RIG-03", "group": "A", "task": "...",
  "shift": { "label": "Morning", "date": "...", "start": "08:00", "end": "16:00" },
  "blockMinutes": 15, "rotation": "hold", "autoSignIn": true,
  "turns": [
    { "from": "08:15", "to": "09:00", "minutes": 45,
      "operator": { "id": "op-a4", "name": "Nadia Haddad" },
      "relievedBy": "Aleksandr Petrov",
      "theyGoTo": "Think" }
  ]
}
```

`theyGoTo` has to travel *in* the payload — where an outgoing operator
goes is a fact about *their day*, not derivable from *this* rig. That is
exactly what let the rig drop its login screen and task picker: given the
payload and the clock, there is nothing left to ask.

Once `apps/server/` exists, this JSON is what it will publish; that is
also why `packages/schema/` has a slot waiting.

## The rig, briefly

Single full-screen page, three foot pedals (keys `1`/`2`/`3`), nine
addressable screens. State machine:

```
checklist → handover → recording → review → resetting → (loop)
              ↑                                  ↓
              └─── issue-menu → rig-down ────────┘
              └─── fault-class → fault-fixing ───┘
```

- Middle pedal is "go" on every screen **except** Recording, where it is
  deliberately inert so muscle memory can't end a good take.
- Right pedal is always "other" — the issue tree is one rule instead of
  three menus. Right can require hold-to-confirm.
- Efficiency = `recordedSecs / (assignedSecs − faultSecs − downSecs)` with
  a warm-up so the first episode of a stint doesn't shout.
- A turn boundary never interrupts a take: `handoverDue` waits for the
  episode to land before rotating.
- The event log tags each event with its backend bucket (`episodes`,
  `rig_shift_checks`, `rig_downtime_events`, `rig_productivity_blocks`,
  `sessions`). These are filed as real envelopes, journalled to IndexedDB
  before the network is touched, and uploaded to `backend/`.
- The rig has no login and never will: a token placed on the machine
  authenticates it, and the operator authenticates nothing.

## The return arrow (built)

`backend/` is the other half: a FastAPI service on Postgres, laid out in
the platform team's convention so `core/` and `services/rigs/` lift into
their tree unmodified. Its own README covers it; three properties are
worth knowing here because nothing in a route list shows them.

**The ledger is the system.** `POST /api/rigs/:rigId/events` appends to an
append-only table and every other table is *derived* from it. Replay is
the property the design is arranged around.

**Sending the same events twice is safe.** Unique on `(rigId, eventId)`,
so a rig that loses its connection mid-batch retries blind and a resend
reports `accepted: 0`. `GET .../cursor` tells it where it got to.

**Measurements, not conclusions.** No percentage is stored. A productivity
block keeps four seconds columns and efficiency is computed at read time
from one definition, so correcting the formula corrects every shift ever
recorded.

The service stores the pushed payload opaque and reads it back. It never
derives a rotation - that is the founding invariant, and a Python
re-implementation would be a third answer and the first one that could
silently disagree.

## The push (built)

`apps/server/server.js` is a plain-node http server (zero deps) that
serves the static tree *and* carries the push:

```
POST /api/push                       body: { payloads: [...] }
GET  /api/rigs/:rigId/schedule.json  -> the payload for that rig
GET  /api/state                      -> { pushedAt, rigs: [...] }
```

The desk's "Push to floor" button sends all twelve payloads in one shot.
The server validates each against `packages/schema/payload.js` and stores
them atomically — a bad push is rejected in full so the floor never runs
half-updated. State is a plain JSON file (`apps/server/state.json`,
gitignored) so a restart survives.

The rig's `loadPayload()` fetches `/api/rigs/:rigId/schedule.json` first,
falls back to a co-located `schedule.json` (still supported for a plain
static deploy), and finally generates locally so the demo runs even with
no server.

## What the reference sheet does not ask for

The sheet defines the scope. It does not speak to:

- **Crew changeover at shift boundaries.** The engine hard-codes three
  8-hour shifts; the rig treats each boot as the start of a shift. No
  handover-window vs. cold-takeover decision has been made.
- **Accounts and permissions for people.** The rig authenticates as a
  machine and that is settled; who may read the desk, and whether anyone
  reviews the scores an operator gives their own takes, is not.

These are open questions to answer when the product is ready, not
implicit requirements to fill in.

## Working on this repo

- Run everything (server + push): `npm run serve`  →  `http://127.0.0.1:8765/`
- Static-only fallback (no push): `./serve.sh` (python)
- Rebuild the two single-file dists: `./build.sh` (or `npm run build`)
- Run the tests: `npm test`  (engine + the reference sheet + schema +
  server end-to-end + both screens, headless)
- The desk is at `/rotation-desk-v1/`, the rig at `/apps/rig/`

Before changing anything in `packages/engine/`, run the tests. Before
changing the payload shape, remember it is a contract between three
things (desk, server, rig) — update `packages/schema/payload.js` at the
same time or the server will start rejecting the desk's pushes.

Before changing what the desk *draws*, read
`rotation-desk-v1/README.md`. The format is fixed on purpose, and the
version lives in the folder name so it cannot be edited away by
accident.
