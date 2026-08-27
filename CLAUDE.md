# Rigs — design notes for anyone opening this repo

This is the "how it all fits" doc. The README explains *what* the system
is; this file records the load-bearing decisions behind it, in the same
language the README uses so the two cannot drift.

## The system in one sentence

Three static web apps — **Desk** (the manager's), **My Shift** (the
operator's own day) and **Rig** (one per physical rig). Desk and Rig share
one engine, so the schedule the desk hands out and the one the rig
enforces are literally the same code. My Shift computes nothing; it reads
back what the desk already decided.

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
  demo-roster/          the example floor the apps open with
  schema/               payload shape, validated on every push
  session/              who is signed in - desk and my-shift share it
  brand/                the mark and the palette, inlined at build

rotation-desk-v1/   deployed - manager's screen. the name is the version
apps/               deployed
  my-shift/             operator's own day, read only
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
no DOM and no globals — the desk and the rig both load it, and it runs
under node so the tests in `packages/engine/engine.test.js` can pin its
behaviour. If you change the engine, the tests are the guardrail; if you
change something that affects the schedule and *don't* touch the engine,
you have introduced drift.

**My Shift is not a third answer, and must not become one.** It loads the
engine too, but for one call — `minutesOnFloor()`, a clock helper — and
takes its turns from `/api/me/shift`, which hands back what the desk
pushed and derives nothing. It draws the gaps between turns rather than
being sent them, because each turn already carries `theyGoTo`. A screen
that recomputed the rotation in order to show it would be exactly the
third answer this invariant exists to prevent.

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

`packages/engine/rotate.test.js` is where "still tested" is made true.
It asks rotate the same questions the reference sheet asks of hold: the
6h + 60 + 60 budget, every rig manned by exactly one person in every
block, one turn length with no stubs, an operator who actually moves and
sees all three rigs, and a payload that reads back the same answer. It
also pins the claim above about block size — 15×4 and 20×3 come out as
the same schedule, asserted rather than assumed.

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
- The rig has no login and never will: a token authenticates the machine,
  and the operator authenticates nothing.
- **The machine does not choose which rig it is; the service tells it.**
  `index.html` loads `rig-config.js` by a relative path and the kiosk
  loads the page from the server, so a per-machine file placed on the rig
  is never read - the browser asks the server for its copy. That is how
  twelve machines came to load one blank file and all became the same
  rig, and it is invisible from every angle: from the service's side,
  twelve rigs reporting as one is exactly what one very busy rig looks
  like. So the service answers that path per caller, from `RIG_ADDRESSES`.
- A rig the floor cannot place **refuses to work**. It says it has no
  identity, names the address it called from, offers no pedal, and throws
  away anything it filed before it found out. Same trade as Standby for
  an expired sheet: idle time is loud, cheap and recoverable, and work
  filed under the wrong rig is silent, permanent, and uncorrectable.

## Who signs in, and who does not

Three screens, three different answers, and the differences are the
design rather than an inconsistency.

**The rig authenticates as a machine.** A token names one rig, the
operator standing at it types nothing, and that is settled. It is the
same instinct as the rig not choosing its own id: if the system knows, do
not ask.

**The desk and My Shift authenticate a person.** They have to, because
the question they answer is *whose*. A manager may push a schedule to
twelve rigs; an operator may read their own day and nothing else. The
desk boots locked, asks `/api/auth/session`, and shows one of four
things: the sign-in card, a refusal naming the operator, the desk, or -
with no accounts or no service - the desk exactly as it was before any of
this existed.

An operator who reaches the desk is shown a refusal, **not** a sign-in
box. They are already signed in; their password is not the problem, and
offering it invites them to think they typed it wrong.

**Sessions are rows, not signed tokens.** A row can be revoked; a JWT
cannot be withdrawn before it expires. Being able to end somebody's
access on the day they leave is the whole argument for having people here
instead of one shared secret.

**There is no sign-up page and there should not be.** A floor has two or
three managers and sixteen operators on a roster somebody already
maintains. Accounts are minted with `python -m tools.mint_account`, which
is also how the first manager exists at all - a seeded default would be a
known password on every deployment.

**Off until configured, like everything else here.** With no accounts in
the database the desk opens exactly as it always did. The one switch that
defaults the other way is `SESSION_COOKIE_SECURE`, because a security
control whose default is the unsafe setting is one that ships unsafe.

Two failures in this area are the same shape and worth naming, because
the code has made both: reading a 401 as "this deployment has no
accounts", and reading a 500 as the same. Either one opens the door at
the moment nothing can be verified. Only an explicitly recognised signal
opens it; everything else keeps it shut.

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
half-updated.

What the floor was pushed is kept the way the backend keeps everything
else: `apps/server/pushes.jsonl` is appended to, one line per accepted
push, and `apps/server/state.json` is a cache derived from its last line
(both gitignored). So a restart survives, losing the cache costs
nothing, and a push that replaced the wrong day can still be read back —
`store = next` used to overwrite the only copy. The cache is written
beside itself and renamed over, because truncating it in place is what
left a half-written file to be found at the next boot; and a state the
log cannot account for makes the server refuse to start rather than come
up empty, since twelve rigs in Standby look exactly like a manager who
forgot to push.

The log is history, for people and for recovery. It is deliberately not
an input to `pick()`: which schedule a rig runs is decided by the window
the desk wrote against the current time and by nothing else, so a push
never acquires an identity a rig could pin to. That would be a second
answer to which schedule is real.

The rig's `loadPayload()` fetches `/api/rigs/:rigId/schedule.json` first,
falls back to a co-located `schedule.json` (still supported for a plain
static deploy), and finally generates locally so the demo runs even with
no server.

## Running a floor day to day

A payload covers **one shift**, and a push covers **one calendar day** -
midnight to midnight, three shifts, twelve rigs, thirty six sheets. So
the rule for whoever is managing the floor is one line:

> Push once a day. Any time that day. Push again whenever the roster
> changes.

Timing does not affect coverage: a push made at nine in the morning and
one made at four in the afternoon both cover the whole of that day,
including the hours already gone. What it does affect is content -
whatever is on the desk when the button is pressed is what the floor
runs, and it reaches every rig within thirty seconds.

The part that catches people is that **a Night shift belongs to the date
it starts on**. Night runs 00:00-08:00, so the night that *follows*
Tuesday is not on Tuesday's sheet; it starts at 00:00 on Wednesday and
lives on Wednesday's. A floor that is only ever pushed in the morning
therefore has no schedule for the night crew who arrive at midnight.

When that happens the rigs **stay put**. They show Standby and refuse to
start a take. That is deliberate and it is the important decision in this
whole area, so it is worth being explicit about why.

`whoIsOn()` matches on the time of day and nothing else, which means an
expired sheet still cheerfully names somebody at half past midnight - a
different person, on a shift that ended a day earlier. A rig that
believed it would file every take under the wrong operator, against the
wrong shift, on the wrong day, and *nothing downstream could tell*: the
episode is well formed, the operator exists, the score is real. So the
rig checks the window the desk wrote before trusting the sheet, and
stops when it does not cover now.

The trade is deliberate. Standby costs idle time, which is loud, cheap
and recoverable - a rig with nothing to run asks for a schedule every ten
seconds, so it starts working seconds after somebody pushes. A misfiled
take costs provenance, which is silent, permanent and poisons the
training data. There is no correction mechanism in the ledger. Refuse
rather than guess, which is the same instinct as rejecting a bad push in
full and storing measurements rather than percentages.

Because the floor depends on a person remembering, the desk has to be
honest about it. When nothing it holds covers the current minute the Live
badge reads **"Nothing scheduled for now"** in the warning colour rather
than "On the floor", so the one screen a manager would check to find out
cannot quietly reassure them.

## What the reference sheet does not ask for

The sheet defines the scope. It does not speak to:

- **Crew changeover at shift boundaries.** The engine hard-codes three
  8-hour shifts; the rig treats each boot as the start of a shift. No
  handover-window vs. cold-takeover decision has been made.
- **Whether anyone reviews the scores an operator gives their own
  takes.** Who may read which screen is now settled and built - see "Who
  signs in, and who does not" above - but nobody checks the marking.

These are open questions to answer when the product is ready, not
implicit requirements to fill in.

## Working on this repo

- Run everything (server + push): `npm run serve`  →  `http://127.0.0.1:8765/`
- Static-only fallback (no push): `./serve.sh` (python)
- Rebuild the two single-file dists: `./build.sh` (or `npm run build`)
- Run the tests: `npm test`  (engine + the reference sheet + rotate +
  schema + server end-to-end + all three screens, headless)
- The desk is at `/rotation-desk-v1/`, My Shift at `/apps/my-shift/`, the
  rig at `/apps/rig/`

`npm run serve` is the static tree and the push, and has no `/api/auth/*`
routes - so the desk and My Shift open unlocked there, which is the
deliberate "a server too old for this route" path rather than a fault. To
see a sign-in you need `local_gateway` and an account in the database.

`npm test` is the JavaScript half and nothing else. The rest has to be
run where it lives:

```
npm test                                       the screens, engine, schema
cd backend && pytest                           ledger, projection, floor, video
cd backend && lint-imports                     the three layering contracts
cd backend && node tests/e2e_rig_to_floor.js   the seam, against a live service
```

`.github/workflows/ci.yml` runs all four on every push, plus the two
things nobody runs by hand: that the committed dists still match their
sources, and that the migrations apply to an empty database, come back
down, and still agree with the models. That last one matters because the
test suite builds its schema from the models while a deployment builds
it from the migrations — a green suite on its own cannot tell you those
two have not drifted apart.

Before changing anything in `packages/engine/`, run the tests. Before
changing the payload shape, remember it is a contract between three
things (desk, server, rig) — update `packages/schema/payload.js` at the
same time or the server will start rejecting the desk's pushes.

Before changing what the desk *draws*, read
`rotation-desk-v1/README.md`. The format is fixed on purpose, and the
version lives in the folder name so it cannot be edited away by
accident.
