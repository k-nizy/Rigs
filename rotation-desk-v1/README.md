# Rotation Desk v1

The manager's screen, locked to the reference sheet.

This folder keeps its name. `rotation-desk-v1` is the deliverable, and
the format below is what v1 *means* — if the format changes, that is a
v2, not an edit to this one.

## Two modes, because a manager has two jobs

They happen at different times of day, so they are not on screen at the
same time.

**Live** — where the desk opens. Every rig, who is on it, the countdown
to their handover, where they go next and who takes over; the fourth
operator in each group with what they are off for and when they are
back; and one line at the bottom saying what happens next on the whole
floor. Nothing to fill in, nothing to read across.

```
10:37   Morning · 08:00-16:00 · 5h 23m left        [On the floor · pushed 09:58]

GROUP A   Box transfer - bin to conveyor
  RIG-01  Nadia Haddad                                     22:38
          ▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░
          → THINK 11:00                       A. Petrov takes over

  RIG-02  Aleksandr Petrov                                  7:38
          ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░
          → BREAK 10:45                         M. Chen takes over

  RIG-03  Tomas Rivera                                     37:38
          ▓▓▓░░░░░░░░░░░░░░░░░░░░░░
          → THINK 11:15                       N. Haddad takes over

  BREAK   Mei Chen                      → RIG-02 10:45      7:38

Next handover in 7:38 · RIG-02 · Aleksandr Petrov goes to break, Mei Chen takes over
```

**Every row says where that person is going and when** — including the
fourth operator, the one not on a rig. That last line is the one the
board used to leave out: it said "back in 8 min" without ever saying
Mei Chen walks to RIG-02. A destination is one of three things, and it
is painted the way the sheet paints them — grey for a break, the amber
hatch for a think block, the group's own colour for a rig — so the board
and the sheet do not have to be learned twice.

Note that Mei Chen's `7:38` and RIG-02's `7:38` are the same number,
because they are the same instant: she takes that rig at 10:45. Every
countdown on the page is one subtraction — whole minutes to the moment,
less the seconds already spent in the current minute — so no two of them
can drift apart. They used to: the off row counted in whole minutes and
read a minute long against the rig beside it.

Live shows the schedule the floor was actually **pushed**, not the one
being typed in Plan — the badge says which. Edit a name without pushing
and Live keeps showing what the rigs are really running. With no server
at all it falls back to the plan on screen and says so.

Before the shift starts it shows the opening line-up with each turn's
length instead of a countdown, under a banner saying when the shift
begins. It never counts down to something that is not happening.

**Plan** — shift and date, the four groups, one line of confirmation,
one button. The sheet sits at the bottom, as reference.

## The format

Names down the side, time across the top. One table for the whole floor,
banded by group, so the four groups are read down a single column of
time instead of four tables that have to be lined up by eye.

```
Operator              08:00  08:15  08:30  08:45  09:00  09:15  09:30  09:45  10:00 ...

GROUP A               Box transfer - bin to conveyor      RIG-01  RIG-02  RIG-03
OP 1  Aleksandr Petrov  6h  |RIG-01 RIG-01 RIG-01 |Break  |RIG-03 RIG-03 RIG-03 |Think
OP 2  Mei Chen          6h  |RIG-02 RIG-02 |Break  |RIG-01 RIG-01 RIG-01 |Think  |RIG-03
OP 3  Tomas Rivera      6h  |RIG-03 |Break  |RIG-02 RIG-02 RIG-02 |Think  |RIG-01 RIG-01
OP 4  Nadia Haddad      6h  |Break  |RIG-03 RIG-03 RIG-03 |Think  |RIG-02 RIG-02 RIG-02

GROUP B               Cable routing - harness to clip     RIG-04  RIG-05  RIG-06
...
```

The bar down a cell's left edge is a handover: a new value starts there.
The name column is sticky, so a row never loses its label however far
right you scroll.

The second tab is the same table with rigs down the side — the
visualisation the reference sheet puts beside group A, for every group:

```
Rig                   08:00      08:15      08:30      08:45      09:00 ...

RIG-01  Rig 1        |A. Petrov  A. Petrov  A. Petrov |M. Chen    M. Chen
RIG-02  Rig 2        |M. Chen    M. Chen   |T. Rivera  T. Rivera  T. Rivera
RIG-03  Rig 3        |T. Rivera |N. Haddad  N. Haddad  N. Haddad |A. Petrov
```

Whichever shift is picked, that is the only shift on the page: the
columns run from its start to its end, and a line above the grid says
which one in words.

## What is fixed, and why there is no control for it

| | |
|---|---|
| Grid | 15-minute blocks, 32 rows to an 8-hour shift |
| Rotation | hold rig — an operator keeps one rig until they step off |
| Group | 3 rigs, 4 operators, one task for the whole shift |
| Off | one operator off per block, the slot walking 4, 3, 2, 1 |
| Labels | four blocks of Break, then four of Think, alternating |
| Budget | 6h work + 60 min break + 60 min think, per operator |

The engine can still rotate rigs and run other grids — `mode: "rotate"`
and the other block sizes are all still in
`packages/engine/rotation-engine.js` and still tested. The desk simply
never asks for them. A desk that can wander off the format is a desk
that can hand the floor a sheet nobody recognises.

`packages/engine/reference-sheet.test.js` holds the sheet transcribed by
hand, cell by cell, and asserts the engine still draws it. That test is
the contract; it is what stops a future engine change from quietly
moving somebody.

## What a manager actually does

**Before the shift starts** - not once it is running - in **Plan**:

1. Pick the shift (Morning / Day / Night) and the date.
2. Check the four operator names and the one task for each group, and
   correct whatever has changed. Rig ids are folded away — they change
   about once a year.
3. Glance at the one-line check: *Everything checks out — 360 min work
   · 60 break · 60 think, each*. It opens itself if anything is wrong.
4. **Push to floor.** All twelve payloads go in one request; the server
   validates every one and rejects the lot if any fails, so the floor
   never runs half-updated.

Before, rather than during, because a push covers the hours already gone
but cannot re-file the work done in them. A crew that starts at 09:00
against a sheet corrected at 09:20 has twenty minutes of takes recorded
against the wrong person, permanently. The same goes for a cover: if
somebody is off and another operator is standing in, assign them here and
push before they start.

For the rest of the shift, in **Live**: nothing. It is a board to be
read, not operated.

Nothing else on either screen is an input.

### The names it opens with are the floor's, not this file's

The roster starts as a file compiled into the page, and for a while that
was the only place it lived — which meant a correction made here never
left this browser tab. The floor got it; every other desk still had the
file; the next person to push sent the file's version back over it, and
the correction was gone with nothing recording that it had ever existed.

So a push carries the roster on screen along with the schedules it
built, and the desk reads that roster back on opening — from the
service, not reconstructed from twelve payloads. For a while it did
reconstruct: rebuild from the payloads, redraw, compare turn by turn,
refuse on disagreement. That machinery is gone. The roster and the
payloads are one act, stored in one row and one transaction, so there
is nothing left to prove. Anything you have already typed on the screen
still wins over the floor.

**And pushing over a floor somebody else has changed asks first.** If the
floor was pushed after you opened this screen, the first press refuses
and names the time; the button becomes *Push anyway* if you meant it, and
**Refresh** answers it properly by reading the floor back. It is not a
lock: two managers editing within the same few minutes still ends with
the later push winning.

## Against the brief

| Asked for | Where it is |
|---|---|
| Autogenerate in groups of 3 rigs / 4 operators | `buildPlan()`, four groups, the sheet format |
| Managers assign operators and tasks to a shift | the Groups cards |
| Task name and operator names fill the placeholders | the group band and the row labels |
| Rig ids fill too | every cell of the operator grid, and the rig grid's row labels |
| Each group keeps one task all day | one task field per group, no per-block task |
| Breaks total 60, thinking totals 60, work is 6h | the one-line check, and each row's hours in its label |
| Push the schedule to each rig's platform app | `POST /api/push` → `apps/server/` |
| The rig knows who is assigned when | `whoIsOn(payload, clock)` in the shared engine |
| No login screen, no task picker on the rig | the payload carries both, so the rig asks nothing |
| Timer top right: time left, who replaces them, what is next | `minutesLeft`, `relievedBy`, `theyGoTo` in the payload; drawn by `apps/rig/`, and by the desk's Live board for all twelve at once |

## The files

```
index.html          the page
assets/desk.css     tokens, live board, plan form, grid, print rules
assets/desk.js      floor state, the board, the grid, the push
desk.test.js        the screen, tested headlessly - runs on npm test
test/dom.js         just enough DOM to run desk.js under node
dist/               single-file build for publishing - generated, never edit
tools/              the build script
```

The schedule itself is not here. It is in
`../packages/engine/rotation-engine.js`, which the rig loads too, so the
rig cannot compute a different answer from the desk that scheduled it.

## The screen is tested too

`desk.test.js` runs the real `desk.js` against a stub DOM, so the parts
a browser would normally have to prove get proved on every `npm test`:

- both modes are exclusive — never both showing, never neither
- picking a shift shows **that shift and nothing else**: one button
  pressed, the label naming it, and all 32 columns inside its hours
- the grid really is names down the side and time across the top, with
  16 operator rows and 12 rig rows
- **every cell** of both grids equals what the engine says, and the
  handover bar falls exactly where a name changes
- Live puts the right people on the right rigs at a pinned clock, counts
  down to the right second, goes amber at five minutes and red at one
- every row carries a destination and a time, the off operator included,
  and the off operator's countdown **equals** the countdown of the rig
  they are walking to — the two numbers cannot drift apart again
- each destination is painted for what it is, and the rig chip is named
  `to-rig` so it cannot pick up the `.rig` row's padding and border
- before the shift it says so instead of counting down; in the last turn
  it promises nobody a relief and the bottom line stops calling it a
  handover
- the push sends twelve payloads that all pass `packages/schema`, and a
  rejected or unreachable push says so rather than pretending

The stub reads its element ids out of `index.html`, so it cannot drift
from the page, and any id `desk.js` asks for that the page does not have
fails the suite instead of failing silently in a browser.

## Run it

```sh
npm run serve      # from the repo root -> http://127.0.0.1:8765/rotation-desk-v1/
npm test           # engine + reference sheet + schema + server + this screen
npm run build      # regenerate dist/rotation-desk.html
```

`Print sheets` puts the operator grid and the rig grid on their own
landscape pages for a wall.
