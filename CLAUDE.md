# Rigs — design notes for anyone opening this repo

This is the "how it all fits" doc. The README explains *what* the system
is; this file records the load-bearing decisions behind it, in the same
language the README uses so the two cannot drift.

## The system in one sentence

Three static web apps — **Desk** (the manager's), **My Shift** (the
operator's own day) and **Rig** (one per physical rig). Desk and Rig share
one engine, so the schedule the desk hands out and the one the rig
enforces are literally the same code. My Shift computes no rotation; it
reads back what the desk already decided.

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

**This page is the station, not a panel beside one.** The screen at a rig
runs it and nothing else, the three foot pedals under the bench drive it,
and the arms and cameras are what it is driving. Anything the operator
does at a rig has to be reachable from here, which is the reason
calibration is an open question below rather than somebody else's
problem.

Single full-screen page, three foot pedals (keys `1`/`2`/`3`), nine
addressable screens. State machine:

```
standby ⇄ checklist → handover → recording → review → resetting → (loop)
                        ↑                                  ↓
                        └─── issue-menu → rig-down ────────┘
                        └─── fault-class → fault-fixing ───┘
```

Standby is both ends of the day, and the arrow goes both ways. The rig
leaves it when the schedule says somebody is due and returns to it when
the shift the desk pushed has run out — by comparison against the window
in the payload, never by working out when a shift ends.

- Middle pedal is "go" on every screen **except** Recording, where it is
  deliberately inert so muscle memory can't end a good take.
- Right pedal is always "other" — the issue tree is one rule instead of
  three menus. Right can require hold-to-confirm.
- Efficiency = `recordedSecs / (assignedSecs − faultSecs − downSecs)` with
  a warm-up so the first episode of a stint doesn't shout.
- A turn boundary never interrupts a take: `handoverDue` waits for the
  episode to land before rotating. The end of the shift is the same rule
  with a different flag — `restDue` — because a take is a take.
- A stint begins and ends in exactly one place each, `beginStint()` and
  `endStint()`, so every way out of one files the same block. There are
  two ways out: relieved, or the shift ended. Only the first used to
  exist, so the last stint of every shift was never filed at all.
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

**There is no sign-up page and there should not be.** That argument is
against people creating their own accounts; it is not against a manager
creating one for somebody else, which is a different thing and is where
this is going - see "Who a person is" below. The *first* manager is still
minted with `python -m tools.mint_account`, because a seeded default
would be a known password on every deployment. After that the desk is the
main path, and the command stays as the recovery route and the
developer's one - documented, supported, and not what anybody uses to
add a new hire.

**But everybody can change their own password**, from either screen, at
`POST /api/auth/password`. Minting is how an account *starts*; it is not
how a password gets *changed*, and while it was both, every password on
the floor travelled through a chat message at least once - somebody had
to ask a manager, and the manager had to read the new one back.

Three properties, each because the obvious version gets it wrong. The
current password is required even though the caller is signed in: a
cookie says this browser signed in once, not who is at the keyboard, and
screens here are left open on a floor. Every *other* session is revoked,
because the reason to change a password is usually that it might be
known. This one is not - it is re-issued instead, since a flow that signs
you out for using it is one people stop using, and they just proved the
current password.

The rule about what a password may be lives in
`core/domains/accounts/passwords.py` and nowhere else. It used to sit
inside `mint_account`'s interactive prompt, so `--password` set anything
at all: the check was on how the password was typed rather than on the
password.

**Off until configured, like everything else here.** With no accounts in
the database the desk opens exactly as it always did. The one switch that
defaults the other way is `SESSION_COOKIE_SECURE`, because a security
control whose default is the unsafe setting is one that ships unsafe.

Two failures in this area are the same shape and worth naming, because
the code has made both: reading a 401 as "this deployment has no
accounts", and reading a 500 as the same. Either one opens the door at
the moment nothing can be verified. Only an explicitly recognised signal
opens it; everything else keeps it shut.

## Who a person is, and who is on the floor

Decided here; only the first part is built. Read this before touching the
roster, the payload's `operator`, or anything that answers "who did this".

**Operator identity exists for the video.** That is the whole of it. The
question the floor has to answer months later is who recorded a
particular take, and nothing else in this system needs an operator's name
at all - the rig has no login and never will.

**A seat is not a person.** `rotation-engine.js` builds the id as `"op-" +
group + slot`, so `op-a4` names the fourth chair in group A, not whoever
is sitting in it. Assign Ben to Sara's seat for a day and the id is still
`op-a4`. It is a desk number: file work under it and two people's takes
land in one folder, and the natural report - group by `operator_id` -
credits the wrong person. That is why every event now carries the
operator's *name* beside the seat, which is what makes a cover day
readable, and it is why the id has to change next.

**A person gets an id that is theirs.** Minted once when a manager adds
them, never reused, travelling with them into every seat they ever work.
Then the id and the name always agree and "everything Ben recorded" is a
question with an answer. The seat keeps its own id, used for drawing the
sheet and nothing else.

**Created deliberately; assigned by picking.** Never by typing a name
into a schedule. One typo is a second person, their takes split across
two ids, and nobody finds out until somebody runs a report - silent,
permanent, the shape of failure this repository keeps designing against.
So the plan side of the desk has no free-text name field; it has a search
over people who already exist. Two *real* people with one name is a
different problem with a different answer: the desk says so and the
manager disambiguates. Names stay editable afterwards and ids do not, so
correcting a spelling never orphans a take.

**An email is optional.** It is what My Shift signs in with and nothing
else. An operator who never opens My Shift is still created, assigned,
recorded and reported on. Give an address and they are sent an invite -
which is the password reset flow doing the same job for somebody who has
no password yet, not a second mechanism.

**The roster moves server-side, and that retires machinery.** Today the
roster has no permanent home: it lives in the pushed payloads, and the
desk rebuilds it by reading twelve of them back and proving the
reconstruction turn by turn. That was the right fix for a compiled-in
file going stale - see "The roster travels the other way" below - but the
reason it exists is that there is no list of people anywhere. Once there
is one, the desk reads who is on the floor instead of reconstructing it,
and the read-back-and-prove path goes with it.

The objection to answer first is what the desk does with no service.
On a floor it does nothing either way: a desk that cannot reach the
service cannot push, and pushing is its whole job. The graceful-
degradation test is about a laptop with no service behind it - the demo -
and the compiled-in roster stays for exactly that and nothing else.

**The desk has to work off site.** Managers are on the floor most days,
but a schedule sometimes has to be set from somewhere else, so the
service is reachable from outside the floor network - which makes
`SESSION_COOKIE_SECURE` a requirement rather than a preference, and
HTTPS with it. Rig identity is unaffected: rigs are placed by the address
they call from, so they stay on the floor network, and a manager calling
from anywhere is told by `rig-config.js` that it is nobody, which is
correct because managers authenticate with a password instead.

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
static deploy), and then stops. It generates a schedule locally only on a
machine the service never identified — a laptop, or a static deploy with
nothing behind it — because that is the demo.

**A machine that knows it is RIG-07 never invents one.** It used to, out
of `packages/demo-roster`, and the window check did not catch it: an
expired sheet is refused because its window has closed, but a generated
sheet is stamped with today and always covers now. So a service
restarting behind a web server that is still up — a deployment, from the
rig's side — put demonstration names in front of an operator and queued
their takes under `op-a3` for upload. With nothing pushed the rig now
stands by and keeps asking. Same trade as everything else here: idle is
loud and recoverable, misfiled work is silent and permanent.

## The roster travels the other way

A push replaces the whole day on all twelve rigs. It is not a merge, and
that made the roster the one thing in this system that could go
*backwards*.

The roster lived only in `packages/demo-roster/`, compiled into the desk.
A manager correcting a name corrected it in their own browser tab and
nowhere else: the floor got the fix, every other desk still had the file,
and the next person to push sent the file's version back over it. The
correction disappeared with no trace anywhere, and every recording from
then on was filed under whoever the file said.

So the desk stops treating its own file as the truth. On opening it reads
the floor and rebuilds the roster from the payloads — which carry the
group, the task and every operator, and under hold rig name three of a
group's four operators in rig order in the first block, the fourth being
the one who is off.

**It is proved rather than trusted.** What comes back is rebuilt into a
schedule and compared, rig by rig and turn by turn, against the schedule
the floor is actually running. Agreement makes the recovery correct by
demonstration. Disagreement keeps the file and says so out loud, because
a roster the desk cannot rebuild is worse than a stale one — it would go
to twelve rigs under a manager's name. Same instinct as the rig refusing
an expired sheet, and as rejecting a bad push in full.

The floor is never adopted over an edit already made on the screen.
Arriving late and overwriting a half-typed name is the same silent
clobber, only faster.

**And a push says so when it would replace one it never read.** The desk
already knows when the floor was last pushed. It asks again on the way
out; if the floor has moved since this screen read it, the first press
refuses and names the time, and the button becomes "Push anyway" so a
manager who meant it can still do it. Reading the floor back answers the
refusal instead. Not a lock — locking twelve rigs behind whoever opened a
tab first is a much bigger promise, and this closes almost all of the
window for a few lines.

What is left: two managers editing in the same few minutes still ends
with the later push winning. The window shrinks from all day to the
minutes between opening the desk and pressing the button.

## Running a floor day to day

A payload covers **one shift**, and a push covers **one calendar day** -
midnight to midnight, three shifts, twelve rigs, thirty six sheets. So
the rule for whoever is managing the floor is one line:

> Push once a day, **before the first shift starts**. Push again whenever
> the roster changes — before the crew it affects walks in.

Any desk will do, and it no longer matters which one — a desk opens on
the roster the floor is running, not on the file it shipped with. See
"The roster travels the other way" above.

Timing does not affect coverage: a push made at nine in the morning and
one made at four in the afternoon both cover the whole of that day,
including the hours already gone. What it does affect is content -
whatever is on the desk when the button is pressed is what the floor
runs, and it reaches every rig within thirty seconds.

**Which is why "before" is the rule and not a preference.** Coverage is
retrospective; attribution is not. An operator standing at a rig is
recorded as whoever the *last* push named, from the moment they start.
Push a correction at 09:20 for a crew that started at 09:00 and those
twenty minutes are already filed under the person who did not work them
- and there is no correction mechanism in the ledger, so they stay that
way. The push lands on every rig within thirty seconds; everything
before it is the part nobody can fix.

So a cover is a roster change like any other. Sara is off, Ben is
covering: the manager assigns Ben at the desk and pushes, and the rig
files Ben because the sheet says Ben. The rig is never asked who is
standing at it - see "Who signs in, and who does not" - and the roster
read-back above is what stops that correction being reverted by the next
desk to push.

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

The rest of that screen has to agree with the badge, and for a while it
did not. Everything else on Live was measured as minutes since the top of
the shift *modulo a day*, which carries no date - so a Morning sheet from
yesterday read at 10:37 came out **running**, with live countdowns beside
a badge saying nothing was scheduled, and a Day sheet that ended at
midnight was announced at 02:19 as starting in 13h 44m. `liveState()` now
asks `shiftWindow()` the same question the badge asks, and says the shift
has ended when it has.

## What the reference sheet does not ask for

The sheet defines the scope. It does not speak to:

- **Crew changeover at shift boundaries.** The engine hard-codes three
  8-hour shifts, and the rig now comes to rest at the end of the one it
  was pushed rather than running past it. What is still undecided is the
  *changeover itself*: no handover-window vs. cold-takeover decision has
  been made, and a rig treats every boot as the start of a shift.
- **Whether anyone reviews the scores an operator gives their own
  takes.** Who may read which screen is now settled and built - see "Who
  signs in, and who does not" above - but nobody checks the marking.
- **Where calibrating the arms belongs.** The station has two teleop
  arms, they drift, and nothing in the loop makes room for aligning them.
  Written out below, because the answer decides a column and a formula
  rather than a screen.

These are open questions to answer when the product is ready, not
implicit requirements to fill in.

### Calibration, and why the answer decides more than a screen

Two teleop arms, a marked work surface and several cameras. The arms
drift, and bringing what an arm believes back into line with what the
cameras see is a real task somebody performs, for real minutes, on a real
shift. None of the nine screens is that task.

**It is not the checklist.** The checklist asks whether the rig is fit to
work. Calibration is what *makes* it fit. Folding one into the other
gives a checklist that can take ten minutes and fail halfway, which is a
different thing wearing the same name.

**The decision is which bucket the time falls in**, because efficiency is

```
recordedSecs / (assignedSecs - faultSecs - downSecs)
```

and the two subtractions are there for one reason: a fault and a
breakdown are not the operator's doing, so the operator is not charged
for them. Calibration is the same kind of time - required, unskippable,
and not a failure of the person doing it - which is what makes this a
question rather than an oversight. Three answers, and they are not
equally good:

**Work.** Counted in assigned time like anything else. Simplest, and
honest if calibration is quick and occasional. But at ten minutes on
every eight-hour shift it removes about two per cent from every score on
the floor, permanently, for doing as instructed. A measure that punishes
required work is one people stop reading, and a measure people stop
reading stops being worth collecting.

**Downtime.** Subtracted like a fault: `downSecs` grows, the denominator
shrinks, nobody is charged. It matches how the formula already treats
what is outside the operator's control, and it costs nothing to build.
What it loses is meaning: `rig_downtime_events` currently says *the rig
could not work*, and calibration is the rig being made ready. Filing them
together makes every downtime report answer a blurrier question than it
does today.

**Its own bucket.** A `calibrationSecs` column beside the other four,
subtracted like them but countable on its own. Most work - a column, a
migration, a projection change, and a term in the read-time formula - and
the most honest. It also answers something the other two cannot: how much
of the floor's time goes into calibration at all, which is exactly the
number that says whether automating it is worth anything. Note that this
is cheap in one specific way: efficiency is computed at read time from
one definition, so adding a term corrects every shift ever recorded
rather than only the ones after it.

**What the floor says.** Asked directly, and the answers are not what the
question assumed:

- It happens **when a problem occurs**, not on a timer - and it can
  happen in the middle of an episode.
- **Both the manager and the operator** do it. There is no separate
  technician who is not on the roster.
- It **produces a result worth keeping**.
- How long it takes is not known until the hardware is on the bench.
- Which bucket the time falls in is not decided.

**So calibration is a fault, not a ritual.** Something goes wrong, work
stops, whoever is there fixes it, and a record is left behind. That is
the shape the state machine already has a path for - `issue-menu →
fault-class → fault-fixing` - and a drifted arm is precisely a rig that
cannot produce good work until somebody makes it able to. It is not a
checklist step, and it is not a shift-start ritual; the first draft of
this section assumed both and was wrong on each.

If that reading holds, two things get easier rather than harder. The
efficiency question may answer itself: `faultSecs` is already subtracted,
so calibration time would be treated correctly with no formula change and
no new column. And the record worth keeping is already a shape this
system has - `rig_shift_checks` holds `fault_opened`,
`fault_reclassified` and `fault_closed`, each carrying a subsystem - so
calibration may be a fault *class* rather than a fact table of its own.
Both of those want confirming against the real hardware before anybody
builds them.

**The take in progress looked like an open question and is not.** It
seemed to need a decision - does the rig discard a spoiled take on its
own when calibration begins, or ask first? - and the answer is neither,
because the pedals on Recording are `Discard`, inert, `Save`, and there
is no route to the issue tree from that screen at all. An operator who
notices a drifted arm mid-take discards it, which is the honest act
regardless since the footage was already bad, and only then reports the
problem from Handover or Resetting. The rig never has to guess, the
recording screen keeps three unambiguous pedals, and
`episode_discarded` is filed by the person who knew.

**So what calibration needs is a node in `ISSUE_TREE`.** Not a screen,
not a column, not a change to the formula. And it is a *Gello* matter
specifically: GELLO is the leader arm the operator holds, calibration is
bringing it back into line with the follower, and `gello_problem` is
already in the tree. Worth deciding at the same time whether it sets
`needsManager` - both the manager and the operator calibrate, so probably
not, but that flag is easier to set correctly than to correct later.

**Which exposes something about the tree's shape.** Gello sits at the
bottom of it:

```
Hardware issue → Other → Other hardware → Gello        four presses
                                        → Calibration  five
```

while `Gripper broken` and `Camera mount` are one press each. If
calibration is among the commonest reasons work stops - and "whenever the
problem occurs, sometimes mid-episode" suggests it is - then the tree is
ordered backwards from how it is used, and the operator pays for that
with their feet, mid-shift, every time.

The tree's rule is sound and should stay: two specific choices and an
Other on every level, so the right pedal is always "deeper" and there is
one rule rather than three menus. What is not established is the
*ordering* within that rule, which was guessed before anybody had run a
shift. Nobody knows the real frequencies yet, so this is not a change to
make now - it is a measurement to take once the hardware is on the bench.
The events are already there to take it from: every `fault_opened`
carries its subsystem, so a month of them says exactly which two belong
at the top.

**And whichever is chosen, say so on the screen.** The operator watches
this number. If calibration is charged to them they should be told, and
if it is not they should see that too - otherwise the first person to
spend fifteen minutes on a stubborn arm learns only that their score
fell, and the lesson they take is about the score rather than the arm.

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

**A suite run stays inside its own schema, `public` included.** Each run
takes a private schema in `rigs_test`, and the connection that gets it
names that schema and *nothing else* — `search_path` is built in one
place, `database.schema_connect_args()`, which both the fixtures and the
app under test use.

The single name is the whole point. SQLAlchemy emits unqualified table
names, so Postgres resolves each by walking the path in order: with
`run_X,public` on it, `CREATE TABLE` landed in the run schema while
`DROP TABLE` walked past the still-empty schema on the first test and
found the one in `public` instead. The suite dropped tables it had never
created, which meant running a service against `rigs_test` — the way to
do local end-to-end work without minting an account — was quietly
incompatible with running the suite beside it. It looked like the
service breaking.

Before changing anything in `packages/engine/`, run the tests. Before
changing the payload shape, remember it is a contract between three
things (desk, server, rig) — update `packages/schema/payload.js` at the
same time or the server will start rejecting the desk's pushes.

Before changing what the desk *draws*, read
`rotation-desk-v1/README.md`. The format is fixed on purpose, and the
version lives in the folder name so it cannot be edited away by
accident.
