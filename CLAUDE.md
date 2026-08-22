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
packages/    imported, not deployed
  engine/          the schedule algorithm + headless tests
  demo-roster/     the example floor both apps open with
  schema/          payload + event shapes (empty until server lands)

apps/        deployed
  desk/            manager's screen
  rig/             operator's per-rig screen
  server/          push transport + event ingestion (empty until built)
```

`packages/` vs `apps/` is the only structural rule: if code is *imported
by an app*, it lives under `packages/`. If code is *shipped and run*, it
lives under `apps/`.

## The load-bearing invariant

> The rig must never compute a different answer from the desk that
> scheduled it.

That is why `packages/engine/rotation-engine.js` exists as one file with
no DOM and no globals — both apps load it, and it runs under node so the
tests in `packages/engine/engine.test.js` can pin its behaviour. If you
change the engine, the tests are the guardrail; if you change something
that affects the schedule and *don't* touch the engine, you have
introduced drift.

## The two rotation modes

**Hold rig** (default) — an operator keeps one rig until their break, and
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
  `sessions`). Nothing is persisted yet.

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

- **Persistence of the rig event log.** Buckets are named in `emit()`
  (`episodes`, `rig_shift_checks`, `rig_downtime_events`,
  `rig_productivity_blocks`, `sessions`) but nothing writes them.
- **Crew changeover at shift boundaries.** The engine hard-codes three
  8-hour shifts; the rig treats each boot as the start of a shift. No
  handover-window vs. cold-takeover decision has been made.
- **Auth, accounts, permissions.** The rig has no login by design; the
  desk has no gate.

These are open questions to answer when the product is ready, not
implicit requirements to fill in.

## Working on this repo

- Run everything (server + push): `npm run serve`  →  `http://127.0.0.1:8765/`
- Static-only fallback (no push): `./serve.sh` (python)
- Rebuild the two single-file dists: `./build.sh` (or `npm run build`)
- Run the tests: `npm test`  (engine + schema + server end-to-end)

Before changing anything in `packages/engine/`, run the tests. Before
changing the payload shape, remember it is a contract between three
things (desk, server, rig) — update `packages/schema/payload.js` at the
same time or the server will start rejecting the desk's pushes.
