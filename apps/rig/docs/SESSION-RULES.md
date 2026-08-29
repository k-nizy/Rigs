# The session — what must not break, what breaks anyway, and who gets told

Scope: **the rig side, one operator's turn at one rig.** Nothing here
changes the desk, the engine, or the rotation.

`../README.md` describes the app as it stands. `BACKEND-PLAN.md` describes
where its events are going. This file is the third question, and the one
neither of those answers: the schedule says who should be at RIG-03 at
09:12, but it says nothing about what happens when that person is seven
minutes late, when the browser reloads mid-take, or when a gripper fails
with thirty seconds of good demonstration already recorded.

Same rule as the rest of the repo: these are **questions with
recommendations, not requirements quietly filled in.** Where something is
already true of the code it says so and points at the line. Where a
decision is yours, it is marked **OPEN** and left alone.

---

## Part 1 — The invariants

Eight things that must hold for a session to mean anything. For each:
what enforces it today, and where the hole is.

### I1. A take in progress is never ended by the system

Only the operator ends an episode — Save or Discard, right pedal or left.
Nothing else may.

*Enforced.* The middle pedal is inert during Recording (`pedals()`), and
a turn boundary sets `handoverDue` rather than rotating (`tick()`), so
the rig changes hands only once the episode has landed. Both are pinned
by tests, as is the weaker property that nothing rebuilds the stage
mid-take — a rebuild takes the camera panes with it and reads, to the
operator, as an interruption.

*Hole:* a page reload ends a take, silently and completely. See **F10**.

### I2. Every second of a stint lands in exactly one bucket

Recorded, or reset-and-idle, or fault, or downtime. Never two, never
none. This is the whole basis of the efficiency number.

*Partly enforced.* `faultSecs` and `downSecs` accrue frame by frame in
`tick()`, and `efficiency()` subtracts them from the assigned time. The
arithmetic is sound.

*Hole:* the buckets are only correct if the *phase* is correct, and the
phase is correct only if the operator keeps pressing pedals. An operator
who walks away leaves the rig accruing "assigned" time in whatever screen
they left it on. See **F5**.

### I3. Every episode and every second belongs to exactly one operator

No episode may be unattributed, and none may be attributed to two people.

*Enforced, newly.* `rotate()` used to credit the *incoming* operator with
the outgoing one's stint — it reads `current()`, which has already moved
past the boundary. It now reads the turn `turnKey` still holds
(`outgoingOperator()`), and a test fails if that regresses.

*Hole:* attribution is only as good as the assumption that the person at
the rig is the person on the schedule. With no login, nothing checks. See
**F4**.

### I4. The operator always has a way forward

Three pedals, and at least one of them does something, on every screen.

*Enforced, newly.* An unrecognised `#hash` used to leave the rig on an
undefined phase — blank stage, three dead pedals, no recovery but a
reload. `boot()` now only seeds a screen the app actually has.

### I5. Nothing is discarded except by a press

A crash, a reload or a network drop must never be indistinguishable from
an operator choosing to throw a take away.

*Not enforced.* Today they are exactly indistinguishable: both produce no
`episode_saved` event, because nothing is written until the operator
scores. See **F10**.

### I6. A rig that is down stays down until a human clears it

No timeout, no auto-retry, nothing that quietly puts an operator back on
a broken rig.

*Enforced.* `rig_down` is left only by the middle pedal ("Problem
solved") or the left ("End session"), and `tick()` explicitly refuses to
rotate out of `rig_down`.

### I7. The rig and the desk never disagree about who is on

The repo's founding invariant, pointed at the session.

*Enforced at boot, and only at boot.* Both sides run the same engine on
the same payload, so they cannot compute different answers — but the rig
reads the payload **once**, in `start()`. There is no polling and no
refresh. A schedule pushed at 10:00 never reaches a rig that booted at
08:00. See **F12**.

### I8. The break/think budget is never silently eaten

Every operator ends the shift with 6h work + 60min break + 60min think.
That is what the audit checks, and a handover that runs long comes out of
somebody's hour.

*Not enforced, and not currently even visible.* The rig knows when a turn
overruns; nothing accumulates it, and nothing tells the desk. See **F2**.

### I9. A rig works only inside the shift it was pushed

The window the desk wrote is the whole of the rig's authority to work.
Outside it there is nobody at this rig, and the rig must say so rather
than keep a live screen up.

*Enforced, newly.* `tick()` had one half of this from the start —
Standby ends when the schedule says somebody is due — and nothing at all
for the other. A shift that ended simply left the rig where it was, with
live pedals, and three things followed that nobody at the rig could see:
the last stint of every shift was never filed, because `rotate()` is what
emits one and the final turn has no turn to rotate into; episodes
recorded past the end filed with `operatorId` null, because `envelope()`
had no turn to read; and twelve rigs sitting on Handover after 16:00 look,
from the desk, exactly like twelve rigs being worked.

`rest()` closes it, by comparison against the window the desk wrote and
never by working out when a shift ends — the same rule the rig already
followed going the other way. It files the stint on the way out and
leaves the next crew owing their own check.

It rests from the working loop only (`RESTS_FROM`). A rig in a fault
report or standing down is dealing with the machine rather than the
schedule, and **I6** says a rig that is down stays down until a human
clears it — a clock is not a human. An early check is excluded too: it
runs outside the shift deliberately.

**I1 holds through it.** A take is never cut short by the shift ending
any more than by a turn boundary; `restDue` waits for the episode to land,
exactly as `handoverDue` does.

*Hole:* a rig that is down when its shift ends stays down, and its stint
stays unfiled, until somebody clears it. That is the trade **I6** asks
for and it is the right way round, but it means downtime spanning a crew
change is charged to one stint rather than split at the boundary.

---

## Part 2 — The failures we should expect

Grouped by where they come from. Each says what happens *today*, because
in most cases something already happens and it is just not the right
thing.

### A. The changeover

This is the richest source of failure on the floor, because it is the one
moment the schedule assumes is instantaneous and never is.

**F1 — the slow changeover.** *(likely; happens every shift)*
A finishes at 08:45. B is still walking, or finishing a conversation, or
was never told. The rig rotates on the clock whether or not anyone is
standing at it, and B's stint clock starts at the boundary.

Measured, on the current build:

| B arrives | B records | B's efficiency reads |
|-----------|-----------|----------------------|
| on time   | 20:00     | 100% |
| 3 min late| 20:00     | 87%  |
| 7 min late| 20:00     | 74%  |
| 15 min late| 20:00    | 57%  |

At fifteen minutes B is below the 60% floor and the rig starts telling
them they are behind — for time they may not have lost. **The penalty
lands entirely on the arriving operator, and the rig cannot tell a late
operator from a slow one.**

*Recommendation:* the gap between a rotation and the first press of Start
is its own bucket — call it changeover time — excluded from the
denominator exactly as fault and downtime are, and reported separately so
a rig that always changes hands slowly is visible as a fact about the
floor rather than a slur on twelve individuals. **OPEN:** whether there
is a grace window (say, 2 minutes) that is nobody's fault, and who wears
the rest.

**F2 — the handover that runs long.** *(likely)*
A is mid-take at the boundary. The rig correctly waits (**I1**), so A's
turn overruns by however long the episode has left. That time is charged
to A, which is right — they were working — but it comes out of A's break,
and nothing anywhere records that it did. Over a shift these accumulate
into a broken **I8** that no report will show.

*Recommendation:* emit the overrun as a measurement on `stint_ended`, and
let the desk decide whether it owes A the minutes back.

**F3 — the no-show.** *(occasional, high cost)*
B never arrives. The rig sits on Handover for a whole 45-minute turn.
Nothing detects it, nothing tells anyone, and the loss is invisible until
someone reads the numbers the next day.

*Recommendation:* the strongest candidate for the first real
notification. See Part 3.

**F4 — the wrong rig.** *(occasional; a direct consequence of no login)*
B is scheduled to RIG-03 and stands at RIG-02. Both rigs now attribute
work to the wrong person, and **neither can tell** — the rig has no login
by design, so "who is here" is an assumption, not an observation.

This is the honest cost of the app's best idea. It is worth paying, but
it should be paid knowingly: every number the floor produces is correct
*given that people stood where the schedule said*.

*Recommendation:* do not add a login. Detect it instead — if the desk
ever sees two rigs whose activity contradicts the schedule, that is a
question for a human, not a prompt for the operator. **OPEN.**

**F5 — the ghost.** *(likely)*
A walks away without pressing anything. The rig sits on Recording,
`phaseSecs()` climbing, an episode nominally in progress and nobody
there. Every second lands in A's assigned time (**I2**), and if RODA-RS
is really recording, it is recording an empty workspace.

*Recommendation:* no take should be able to run past a plausible maximum.
**OPEN:** what that maximum is — it is a fact about the task, not about
the app, and the four groups have four different tasks.

### B. The take

**F6 — forgot to press Save.** *(likely)*
The demonstration ended two minutes ago; the pedal has not been pressed.
Same shape as **F5** and the same remedy.

**F7 — self-scoring has no check.** *(certain, low cost per event)*
The operator scores their own take 3/4/5 and nothing reviews it. Already
listed as open in `BACKEND-PLAN.md`; noted here because it is the one
number on the rig that is an *opinion*, and it will be read later as
though it were a measurement.

**F8 — the reset that never ends.** *(likely)*
Past 45 seconds the timer turns red (`RESET_URGENT_SECS`). That is the
whole mechanism. A genuinely broken workspace and an operator having a
conversation look identical.

*Recommendation:* leave it. The red timer is the right amount of pressure
and the difference is what a manager is for — but the reset duration
should travel to the backend so the distribution is visible.

**F9 — the bad take that gets saved.** *(likely)*
The gripper slips at second 40 of a 60-second demonstration. The operator
should discard; discarding costs them efficiency, so there is a standing
incentive not to. Worth knowing the incentive exists: **the efficiency
number quietly argues against throwing away bad data.**

*Recommendation:* count a discard as recorded time for efficiency
purposes, so discarding a bad take is free. It changes the number from
"time you spent recording" to "time you spent demonstrating", which is
closer to what the floor actually wants. **OPEN.**

### C. The machine

**F10 — reload, crash, or kiosk restart.** *(likely; the most severe on
this list)*
Everything is lost. Episode count, recorded seconds, fault and downtime
totals, the whole stint. The rig boots back to the checklist as though a
new shift had started — `boot()` treats every boot as the start of one.

This breaks **I5** outright: a crash and a deliberate discard are the
same event as far as any future backend can tell. It also means an
operator can reset their own efficiency by pressing F5.

*Recommendation:* the disk journal in Phase 1 of `BACKEND-PLAN.md` is
exactly the fix, and this failure is the argument for doing it early. On
boot, if a journal exists for this rig and this shift, resume the stint
rather than starting a new one. **OPEN:** whether an in-flight *episode*
resumes or is marked interrupted — probably the latter; the video is the
authority, not the page.

**F11 — the network drops.** *(likely)*
`loadPayload()` falls back cleanly: server, then a co-located
`schedule.json`, then a locally generated schedule. The fallback is
correct code and a silent hazard — a locally generated schedule is built
from `DEMO_ROSTER`, so if the desk has changed the roster, the rig now
runs a schedule **nobody pushed** and looks entirely normal doing it.

*Recommendation:* the rig should show which of the three it is running.
One line of rail text, in the same words as everything else. A rig
running an unpushed schedule is not an error, but it is not a fact to
hide either.

**F12 — the desk pushes mid-shift and nothing happens.** *(likely)*
There is no polling. `loadPayload()` is called from `start()` and from
the demo rig-picker, and nowhere else. A rig that booted at 08:00 holds
that payload until it is reloaded.

This breaks **I7** for the entire remainder of a shift, silently, and it
is the failure most likely to be discovered at the worst moment — the
desk reschedules around an absence and the floor does not move.

*Recommendation:* poll `/api/rigs/:rigId/schedule.json` on a slow
interval and adopt a new payload **at the next turn boundary, never
mid-stint** — changing who owns the rig underneath a running take would
break **I1** and **I3** at once. **OPEN:** what a rig does if the new
schedule removes it from the shift entirely.

**F13 — the clock is wrong.** *(occasional, silent, corrupts everything)*
`whoIsOn()` is a pure function of the payload and the time. A rig three
minutes fast hands over three minutes early and mis-attributes every
boundary. With twelve machines this is a question of when.

*Recommendation:* NTP is already in the Ansible phase of
`BACKEND-PLAN.md`. Add the cheap check: the rig knows the server's time
from any response it gets, and a rig more than a few seconds out should
say so rather than quietly being wrong.

**F14 — the rig ships in demo mode.** *(certain, if nobody notices)*
`live` defaults to `false` and `speed` to `30`. Out of the box a
deployed rig runs the accelerated demo clock off `S.t`, not the wall
clock, and the only way to change either is the demo drawer. The README
says "on a real rig `speed` is `1`" — but nothing sets it.

*Recommendation:* this one is not a policy question, it is a missing
switch. A deployed rig should follow the wall clock unless something
explicitly puts it in demo mode, rather than the other way round.

**F15 — two rigs think they are the same rig.** *(occasional)*
`RIG_ID` defaults to `"RIG-03"` until a payload says otherwise. A
misprovisioned machine, or one that falls all the way through
`loadPayload()`'s fallbacks, is RIG-03. If two of them do it, two
machines file episodes as the same rig.

*Recommendation:* settled. The service answers `rig-config.js` per
caller from `RIG_ADDRESSES`, and a rig the floor cannot place refuses to
work rather than taking the default.

### D. The floor

**F16 — an operator is absent for the whole shift.** *(likely)*
The schedule assumes four operators per group. With three, the rotation
is wrong from the first block and there is no way for the floor to tell
the desk. Every rig in that group runs a schedule that cannot be
followed.

**F17 — a rig is down for the whole shift.** *(likely)*
Three rigs become two, and the schedule still walks four operators
through three. `rig_down` is a *session* state; nothing makes a rig
unavailable for the *shift*.

Both are the same shape: **the schedule is built once and the floor
changes underneath it.** Both are desk-side, both need **F12** working
first, and both are properly **OPEN** — they touch the rotation, which is
the one thing this repo does not let you change casually.

---

## Part 3 — Notifications

### The two constraints that shape all of them

**The rig does not blink.** From `../README.md`: the handover and fault
states are carried by the words and the pedals, not by a flashing
colour. Anything added here is a sentence in the same voice, in the same
place, or it does not belong on the wall. Nothing may cover the pedals.

**The rig has no login.** So a notification *from* a rig identifies the
rig, never a person. This turns out to be an advantage: the desk already
holds the schedule, so it can resolve rig plus time into a name without
the rig ever knowing one. The rig reports what happened; the desk knows
who it happened to.

### Where they go

Three audiences, and only one of them needs anything built:

- **The operator**, on the wall. Words in the rail or the stage.
- **The desk**, on the Live board. This already exists — a card per rig,
  operator, time left, where they go next. It is the natural inbox and
  needs no new concept, only new fields.
- **A technician**, for `rig_down` and the manager-needed leaf of the
  issue tree. There is no queue today and no route to one.

### What exists today

On the rig: "Handover due" in the rail; the reset timer turning red past
45 seconds; the efficiency figure marked when it falls under 60%, warmed
up for five minutes so the first episode of a stint does not shout. All
three are words or a number changing weight. None of them leave the rig.

**Nothing the rig knows reaches anybody.** That is the gap.

### Proposed, in the order I would add them

| # | Trigger | Who is told | How it reads |
|---|---------|-------------|--------------|
| N1 | Rig on Handover, nobody has pressed Start, past a grace window | Desk | The rig's Live card says how long it has been waiting — **F3, F1** |
| N2 | `rig_down` raised | Desk, and a technician queue | Rig, issue class, and whether a manager was asked for — **I6** |
| N3 | Same fault class on the same rig N shifts running | Desk | "This rig has had three camera-mount faults this week" — the point of logging faults at all |
| N4 | Turn overran the boundary | Desk | Minutes owed back, on `stint_ended` — **F2, I8** |
| N5 | Rig running an unpushed schedule | Operator **and** desk | One line of rail text — **F11** |
| N6 | Rig has not been seen for N minutes | Desk | Absence of heartbeat, not presence of an event — the one that catches crashes and power cuts, **F10** |
| N7 | Take running past a plausible maximum | Operator only | A sentence on the Recording screen. **Never** an interruption, never a pedal change — **F5, F6, I1** |

N7 is the only one that touches the operator's screen mid-task, and it is
deliberately the weakest: a sentence appears, nothing else changes, and
the middle pedal stays inert. If it ever grows into something that can
end a take, it has broken **I1** and should be removed instead.

**OPEN:** whether the desk should be able to send anything *to* a rig —
"come to the desk", "stop after this take". It is easy to build on top of
the push transport that already exists, and it is a genuinely different
product decision: it turns a rig from a thing that reads a schedule into
a thing that can be interrupted.

---

## What I would do first

1. **F14** — make a deployed rig follow the wall clock. It is a missing
   switch, not a design question, and everything else is measured
   against that clock.
2. **F12** — poll for the schedule, adopt at a turn boundary. Restores
   **I7** for the whole shift.
3. **F10** — the disk journal from Phase 1, so a reload stops being a
   silent discard. Restores **I5**.
4. **F1** — changeover as its own bucket. Small, and it stops the
   efficiency number lying about the person who arrived.
5. **N1 and N6** — the two notifications that catch a floor going quiet.

The first four are all measurements, and all of them want
`packages/schema/event.js` from Phase 0 to exist first — which is another
argument for starting there.

---

## The questions only you can answer

Collected from the **OPEN** marks above, because these are policy, not
engineering:

- Who wears a slow changeover, and is there a grace window that is
  nobody's fault?
- Should a discarded take count as recorded time, so throwing away bad
  data is free?
- What is the longest a take can plausibly run? (Four groups, four
  tasks, possibly four answers.)
- After a crash, does the stint resume, or start fresh with the loss
  recorded?
- If a mid-shift push removes a rig from the schedule, what does that rig
  do with the operator standing at it?
- Can the desk send anything to a rig, or does the arrow stay one-way?
- Does anyone check the self-scores?
