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

backend/            deployed - the return arrow. FastAPI on Postgres
deploy/             nginx, systemd, and the deploy guide
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
engine too, but for two clock helpers — `minutesOnFloor()` and
`shiftWindow()`, both of which read the window the desk wrote — and
takes its turns from `/api/me/shift`, which hands back what the desk
pushed and derives nothing. It draws the gaps between turns rather than
being sent them, because each turn already carries `theyGoTo`. A screen
that recomputed the rotation in order to show it would be exactly the
third answer this invariant exists to prevent.

It shows the whole of a person's scheduled shift, before it starts and
after it ends, not only while it runs. The route picks the shift the way
`in_force` picks a rig's - the one naming this person that covers now,
else the one about to start, else the one that ended last, bounded a
day either side - and the page counts down to the first turn or says
the day is done. Which side of the shift *now* is on cannot be read from
a minute-of-day (23:00 the night before a Night shift is the same minute
as 23:00 the night after it), so the page measures from the dated
window, through the engine, and not from the wall clock.

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
  "blockMinutes": 15, "rotation": "hold",
  "turns": [
    { "from": "08:15", "to": "09:00", "minutes": 45,
      "operator": { "id": "op-a4", "name": "Nadia Haddad", "personId": "..." },
      "relievedBy": "Aleksandr Petrov",
      "theyGoTo": "Think" }
  ]
}
```

`personId` is the seat's occupant as an id that is theirs, and it is
optional: absent from a laptop demo and from any floor without a people
table, present only when the desk assigned by picking. The engine adds
the key only when a roster entry carries one, so a roster of plain names
pushes byte for byte what it always pushed.

`theyGoTo` has to travel *in* the payload — where an outgoing operator
goes is a fact about *their day*, not derivable from *this* rig. That is
exactly what let the rig drop its login screen and task picker: given the
payload and the clock, there is nothing left to ask.

This JSON is what `apps/server/` publishes, and `packages/schema/` is
what it is validated against on the way through - see "The push" below.

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

**And a forgotten one is emailed back, where a floor has a relay.**
`POST /api/auth/reset/request` sends a link, `POST /api/auth/reset`
spends it. Changing a password you know is a different problem from one
you have forgotten - there is no session to lean on - so the only thing
this can ever prove is that whoever asked can read the account's mailbox.

The case that decided it is the night shift. Night runs 00:00-08:00, and
an operator locked out at two in the morning with no manager in the
building would otherwise wait until the crew changes. A manager-issued
code needs a manager on site; this does not.

The properties that follow from a token being the account for as long as
it lives: it is a row, so it can be withdrawn; single-use, spent by an
`UPDATE ... WHERE used_at IS NULL` so the database decides a race rather
than an if-statement with a gap in it; short-lived; and asking again
voids the earlier ones, or somebody who asked three times has three
working ways in sitting in a mailbox. Setting a password by *either*
route voids outstanding links and every session.

**And it takes the same time, which is the half that got missed.** The
wording was identical from the first version; the latency was not. An
address with no account cost one SELECT and came back in 16ms, while a
real one minted a token, wrote two rows, committed and then held the
request open for an entire SMTP conversation - 977ms, with every real
request slower than every invented one. Identical wording and a
sixty-fold difference in latency is an oracle with a polite error
message, and a better one than the login route's, because it needs no
credential and no guessing.

So the route does nothing at all: it checks the throttle, hands the work
to a background task and returns. There is deliberately no database
session on it, because acquiring one is work and work is what leaks.
`tests/test_password_reset.py` asserts that structure rather than the
clock, since a timing test in CI is a flaky test.

**The request route answers the same way whatever happened** - unknown
address, disabled account, relay that refused the mail. It is reachable
by anybody who can load the sign-in page and it takes an email, so a
version that said "no such account" would be a quicker way to enumerate
the floor's staff than the login route the dummy hash exists to protect.
The cost is a person who mistypes their address waiting for nothing,
which is why the message says *if*.

**The token rides in the fragment, not the query string.** A browser
never sends the part after `#` to a server, and that is the entire
reason it is there. As `?reset=TOKEN` it travelled in the request line,
and `deploy/nginx.conf` sets `access_log off` on `/healthz` and nothing
else - so every reset link would have been written into the access log
in the clear, still valid for `password_reset_minutes`. Stripping it
from the address bar afterwards, which both pages do, does not cover
that: it deals with history and referrers, and the GET has already
happened.

Both halves have to move together - the service emits `#reset=` and
`resetTokenInUrl` reads `location.hash`. A reader that accepted either
form would let the logged one come back unnoticed, so it accepts only
the fragment, and the tests assert the link's *shape* rather than only
pulling a token out of it. Splitting on `reset=` matches both forms and
would have passed either way.

**The link is built from `PUBLIC_BASE_URL`, never from the request.** The
service sits behind nginx and `Host` is a header the caller writes, so a
link derived from it would let somebody request a reset with a host of
their choosing and have the floor mail the victim a link pointing at it.

**What turning it on costs, stated plainly:** it makes the address on an
account a credential. Those addresses are typed once at `mint_account`
time and nothing has ever verified one, so a typo is a reset link posted
to a stranger. `tools.preflight` warns about it whenever the relay is
configured, and that warning is deliberately absent when it is not -
a warning on every deployment is one nobody reads.

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

Decided here, and being built in order - the table itself is in, and
nothing reads it yet; the end of this section says what reads it next.
Read this before touching the roster, the payload's `operator`, or
anything that answers "who did this".

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
operator's *name* beside the seat, which is what keeps a day readable
when the people in a seat change - and they change any day - and it is
why the id has to change next.

**And nobody sits in one seat for long.** The manager assigns whoever
the schedule needs, group by group, as often as they like. The freedom is
correct and should stay - it is how a floor actually runs - but it does
mean `op-a4` is a proxy for nothing at all. If people stayed in group A
for months it would at least be a rough stand-in for a person; reshuffled
freely it is only ever the name of a chair.

**Be clear about what that does and does not cost, because it is easy to
overstate.** The take itself is fine: the manager assigns Ben, the rig
shows Ben, the episode is filed with Ben's name, and "who recorded this
video" is answered by reading the row. That works today. What the seat id
costs is narrower and worth naming exactly - correcting a spelling later
does not reach the takes already filed, since the name was copied in as
each one happened; and a report grouped by `operator_id` will silently
merge people, which is a habit to avoid rather than damage already done.
Both are fixed by the same thing, both come free with a people table, and
neither is a reason to hurry.

**A person gets an id that is theirs.** Minted once when a manager adds
them, never reused, travelling with them into every seat they ever work.
Then the id and the name always agree and "everything Ben recorded" is a
question with an answer. The seat keeps its own id, used for drawing the
sheet and nothing else.

**The person reaches the episode row itself.** Decided: an episode names
the person who recorded it, directly, and not only through a projection
built from it afterwards. The QC platform is what asks - it watches a
take and checks the marking was honest, so "who recorded this" has to be
answerable from the row it is looking at.

The seat id stays on the row beside it. Which chair the work came from is
a real fact about the schedule and worth keeping; it is simply not the
answer to who did it.

**Created deliberately; assigned by picking.** Never by typing a name
into a schedule. One typo is a second person, their takes split across
two ids, and nobody finds out until somebody runs a report - silent,
permanent, the shape of failure this repository keeps designing against.
So the plan side of the desk has no free-text name field; it has a search
over people who already exist. Two *real* people with one name is a
different problem with a different answer: the desk says so and the
manager disambiguates. Names stay editable afterwards and ids do not, so
correcting a spelling never orphans a take.

**How the desk says so, and how the manager disambiguates.** The picker
shows an email beside a name where there is one - free information, and
what My Shift signs in with anyway. Where two people still cannot be
told apart it says so and offers to **rename** one of them, inline. That
is the remedy the paragraph above already licenses rather than a new
mechanism: names stay editable and ids do not, so renaming to tell two
people apart cannot orphan a take, and it fixes the data instead of
teaching every screen to cope with it forever.

No id fragment on screen. `Ben Carter - 4f2a` asks a manager to read a
checksum, and ids here are for machines. Two humans who cannot tell two
rows apart are looking at a name that is doing its job badly, and the
thing to fix is the name.

**It flags; it does not refuse.** The argument for refusing is real -
picking the wrong Ben is silent and permanent, and the ledger has no
correction mechanism, which is the same reasoning that puts a rig into
Standby rather than let it guess. What decided it the other way is that
a rig in Standby costs idle minutes, while a desk that will not schedule
a shift until somebody does data admin costs the shift itself, at 07:55,
and is a desk people learn to work around. The rename sitting one click
inside the picker is what keeps the mistake cheap to avoid. The residual
is stated rather than designed away: a manager can pick the wrong person
of two who share a name, and nothing downstream will notice.

**An email is optional.** It is what My Shift signs in with and nothing
else. An operator who never opens My Shift is still created, assigned,
recorded and reported on. Give an address and they are sent an invite -
which is the password reset flow doing the same job for somebody who has
no password yet, not a second mechanism.

**The manager's push decides who is where. Nothing else does.** This is
the rule the rest of this section hangs from, and it is simpler than
the first drafts made it sound. People are assigned freely, every day
and every shift - Mei on Monday morning, Mei on Tuesday night, a
different seat each time - and the people working a shift can change
at any time. None of that is a special case. There is one fact, the
push; a person's screen follows *that person* wherever the push puts
them; and every take is filed to the person who did it.

**So an account names its person, and the seat is never on the
account.** An operator account used to hold `op-a2` - a chair - set
once when the account was minted, and `/me/shift` answered "how did I
do" by asking what that *chair* did. The chair a person works changes
by the manager's decision every day, so that stored chair was stale by
the next morning, and the morning somebody else was put in it, the
first person's own screen showed the second person's work as theirs. A
leak, not a reporting error, and the seat-is-not-a-person failure this
whole section is about, arriving through the door marked
authentication.

Now the account carries `person_id` and nothing about where that
person sits. Where they sit is today's push - the one fact - and the
rig, the desk and My Shift all read it. "How did I do" asks what *this
person* recorded, whatever seat or shift the push gave them. There is
no arithmetic to get wrong because nothing is derived: there is a
person, and there is a push. Observed live: push a person into seat 2
and their screen shows seat 2 across three rigs; push again with them
in seat 4 and it shows seat 4; the account never changed.

**And the operator sees where they are working, on purpose - and
what for.** Every turn on My Shift carries its rig, because the rig is
what "where am I" means to somebody standing on the floor, and the
rig's task with it, read from that rig's payload, so the card says what
you are doing on the rig you are on and changes it when you move. A
group keeps one task for the whole shift today, so it reads the same on
all three rigs; the day a push gives two rigs two tasks, the screen is
already right. The table names the task on every turn, so what the
next turns are for is read down the day, not only what this one is.
The seat label rides in the same data for the sheet's sake and is not
what a person needs to read.

Built: `accounts.person_id`, migration `6492d4e8117c`, `mint_account
operator --person <id>`, and `/me/shift`, `/me/scores` and the session
all answering by person. `operator_id` stays on old rows, unread, so
nothing is destroyed and the downgrade is clean.

**A floor that already has operator accounts upgrades, and links.** The
first version of that migration was green in CI and could not be
applied to any floor with an operator on it: a CHECK added the ordinary
way is checked against every existing row, and every existing operator
had a seat and no person. Its downgrade did not restore the old rule,
so the one-step rollback DEPLOY.md documents could not go forward
again. Both were caught by running the real thing against a database
with rows in it, which CI never had. Now the new rule is added `NOT
VALID` - enforced on every row written from then on, tolerant of the
rows that predate it - and the downgrade restores the old rule the same
way. Existing accounts keep signing in and are given their person by a
manager with `mint_account link`, which keeps the password; they are
never re-minted and never invented from the account's name. Until
linked, such an account sees an empty day.

**What that costs on the day it lands, stated rather than hidden.**
"My day" is matched on the person the push named in each turn, and
never on a name - a name is only a string, and inferring identity from
one is the failure this section exists to prevent. So a push that names
no people gives a person-linked operator an empty day: My Shift says
"no shift for you right now", which is true of the sheet and misleading
about the person. The push that produces one is a roster of plain names
- a floor that has not yet created its people, or a desk that read a
plain-name roster back from the floor and pushed it untouched, since
the picker resolves a name to a person only when that name is changed.
It ends with the first push that names people. And the screen says
*why* it is empty, because three floors produce an empty day and they
are not the same to the person looking at it. `/me/shift` reports two
facts beside the day - whether anything was pushed, and whether any of
it names people - and My Shift reads back the one that applies: nothing
pushed (the rig says Standby for the same reason), a schedule that names
no people yet, or one that names people and not this person. All three
end with a manager, and the line says so. A service too old to report
them gets the first line, which is the one it always had.

The old rule that an operator account *must* name a seat is retired
with it. It was written when a seat was the best stand-in for a person
available, and a seat stored on the account beside a person is two
answers to one question that can disagree - which is the failure, not a
safeguard. The credential itself is unchanged: email and password. What
changed is what the account points at once you are in.

**An operator sees their own numbers and nobody else's.** Decided, and
restored here after being dropped from the open list by mistake - the
route's own comment had said it was open, and it was. A shared board
motivates some floors and turns others into a leaderboard people
resent; that is a call about how this floor is run, and the private
default is the one that can be widened later, whereas a floor that has
already seen each other's scores cannot be un-shown them. The floor-wide
view stays the manager's.

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

**What is built, and the order the rest lands in.** Each piece lands on
its own, so each can be reverted on its own.

- **The table** - built. `core/domains/people/` and migration
  `d137b5150ea3`. A person has an id minted once and never reused, a name
  that stays editable, an email that is optional and unique only when
  given, and `disabled_at` in place of deletion. `people` and `accounts`
  do not import each other, and `.importlinter` enforces it. Nothing
  reads the table.
- **The episode row** - built, backend first. `personId` is accepted on
  the envelope and stored on the ledger and all five fact rows, beside
  the seat id, because the QC platform asks "who recorded this" of the
  row it is looking at. The rig sends it now, in the release after the
  server learned it - per DEPLOY.md that order is not optional, because
  a rig drops a refused batch rather than retrying it. It rides from the
  turn captured when the take *began*, the same as the seat and the
  name, so a take that runs past a turn boundary still names whoever
  pressed start; and it is absent rather than null from a sheet with no
  people, so a floor without a people table files byte for byte what it
  always did. The JSON schema, the JS validator and the fixtures moved
  together, and the Python side accepts the fixture that carries it.
  There is deliberately no foreign key to `people` - a restore re-POSTs
  the ledger, people are outside it like schedules, and a key would turn
  a missing row into lost events.
- **The desk picks** - the service half is built. `POST /api/people`,
  `GET /api/people?q=`, `GET /api/people/{id}`, `PATCH /api/people/{id}`
  and `POST /api/people/{id}/disable`, all manager-only with the same
  open-until-configured fallback as the push, all in `test_roles.py`'s
  per-role sweep. Two people with one name come back as two rows. The
  payload half is built too: a roster entry may be `{ name, personId }`
  and the engine puts `operator.personId` on every turn beside the seat,
  only when the entry carries one, so the sheet is drawn identically
  either way. The picker is built too: a roster card resolves a typed
  name to a person on change, offers to add a name nobody has, and flags
  two people who share one - never guessing. The card is a plain text
  box against a service that predates `/api/people` (the same absent-
  route exception `session.js` makes), and read-only with no service at
  all, since a desk that cannot push has nowhere for an edit to go.
  Typing never mints a person; only the add button does.
- **The roster reads server-side** - built, and the read-back-and-prove
  path is gone. The assignment had no home but the pushed payloads; it
  rides on the push now, as `schedule_pushes.roster`, in the same row
  and transaction as the schedules it produced, so the two cannot
  disagree and there is nothing left to prove. `GET /api/roster` answers
  the newest push - with or without a roster, so an older roster is
  never served over a newer push. The desk reads it on opening and sends
  `GROUPS` with every push; the demo floor in `apps/server` keeps it in
  the log line and serves it the same way. Optional on both servers, so
  a desk that predates the field still pushes. Stored opaque, like the
  payload: the service is a courier, and an opinion about slots would be
  a third answer.
- **Invites**, through the password reset flow, for anybody given an
  address.

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

*Retired. The mechanism below is gone from the code: the roster rides on
the push now, and the desk reads it rather than reconstructing it - see
"Who a person is, and who is on the floor" above. What survives from
this section is the second half, the refusal to push over a floor that
moved since the screen read it, which was never about reconstruction and
still stands. The rest is kept as the record of why the read-back was
built and what it cost, because the failure it answered is real and the
answer moved rather than disappeared.*

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

The people working a shift can change at any time, and that is not a
special case: the manager assigns whoever is working at the desk and
pushes, and the rig files whoever the sheet names. The rig is never asked who is
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

## The changeover is cold, and the rig waits to be told

Decided. There is no handover window across a shift boundary. The rig
comes to rest at the end of the shift it was pushed and does not start
the next one on its own: it stands by, keeps asking, and goes to work the
moment a manager pushes the day.

This adds no code - it is the behaviour already built, and
`apps/rig/no-schedule.test.js` already pins it: "it keeps asking, and
goes to work the moment one is pushed". What was missing was the reason.
A handover window would have to say who is charged for the overlap
minutes when the *whole* crew swaps at once, and there is no honest
answer to that. One crew is on the clock or the other is; inventing a
window where both are puts minutes into `assignedSecs` that nobody
worked, and every efficiency score in the overlap is wrong by
construction.

The cost is the one this repository keeps choosing. A floor whose manager
forgets to push has twelve rigs in Standby, which is loud, cheap and
recoverable - a rig with nothing to run asks again every few seconds, so
it starts working seconds after somebody pushes. The alternative is a rig
carrying on under yesterday's sheet, filing every take against the wrong
operator, on the wrong shift, on the wrong day, which is silent and
permanent. Same trade as refusing an expired sheet, and as rejecting a
bad push in full.

A rig still treats every boot as the start of a shift, and that stays
correct: what makes a boot safe is the window check against the sheet it
loads, not any knowledge of what happened before it.

## Who checks the marking, and who does not

Decided, and the answer is mostly that it is not this system's job.

An operator scores their own take. The decision is that a manager may
**see** those scores and may not change them. The seeing half is built:
`GET /api/floor/scores` gives one row per person for a shift - takes
recorded, saved and discarded, how many scored 3, 4 and 5, and the mean
- and Live draws it beside the board. There is a GET and nothing else,
and the panel has nothing to press; the route's test asserts every
write verb is absent. Grouped by *person*, never by seat: grouping by
`operator_id` credits two people who sat in one seat on different days
to one row with the wrong average for both, which is the merge the person id was
built to end. A take filed before the rig sent a person falls back to
the seat and name it carries, and the row says which seat, so a chair
is never mistaken for a person. The not-changing half is not built and
is not going to be. The asymmetry is the point: surfacing an outlier is worth having and
costs a projection and no new event type, whereas letting one person
overwrite another's mark needs a screen, an event, and a settled answer
to who may re-mark whose work.

The real review happens elsewhere. There is a **QC platform** - people
whose job is to watch the video and check it was scored honestly - and it
is a separate system, not another screen here. That boundary is the
point. This service stores measurements and not conclusions, which is why
no percentage is written down and efficiency is computed at read time
from one definition. An adjudicated score is a conclusion, and the moment
one is stored, correcting the rule behind it stops correcting the past.

Which makes the operator id matter more here than the score does. QC asks
"who recorded this, and is their marking sound" - a question about one
person across months. `op-a4` is a chair, so it cannot answer it, and a
report grouped by it merges two people in silence. See "Who a person is,
and who is on the floor".

## What the reference sheet does not ask for

The sheet defines the scope. It does not speak to:

- **Where calibrating the arms belongs.** The station has two teleop
  arms, they drift, and nothing in the loop makes room for aligning them.
  Written out below, because the answer decides a column and a formula
  rather than a screen.

That is the one still open. Crew changeover and who reviews an operator's
own scores were both on this list and are answered in the two sections
immediately above; whether an operator sees anyone else's numbers was
on it too, was dropped from here by mistake while it was still open,
and is answered under "Who a person is". This is an open question to answer when the product is
ready, not an implicit requirement to fill in.

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

`.github/workflows/ci.yml` runs all four on every push, plus the
things nobody runs by hand: that the committed dists still match their
sources; that the migrations apply to an empty database, come back
down, and still agree with the models; and, since `6492d4e8117c`, that
they apply over a floor that already has rows in it and survive the
one-step rollback DEPLOY.md documents - `tools.rehearse_upgrade`, which
pins a revision, seeds the floor as it was then, and carries it to
head. The empty-database check matters because the test suite builds
its schema from the models while a deployment builds it from the
migrations — a green suite on its own cannot tell you those two have
not drifted apart. The rehearsal matters because an empty database is
the one thing a floor never is: a rule that refuses existing rows, or a
downgrade that forgets to put one back, is green on empty and fails on
the day it is needed.

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
