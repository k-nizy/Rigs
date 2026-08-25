# The rig backend — plan

Scope: **the rig side only.** The desk is finished for what the reference
sheet asks of it — it builds a schedule and pushes it. Nothing below
changes the desk, the engine, or the rotation.

This is the plan for the other direction. Today the arrow runs one way:

```
desk  →  server  →  rig                    (the schedule, built)
```

Everything here is the return arrow:

```
rig   →  server  →  cloud                  (episodes and events, to build)
```

`apps/rig/README.md` describes the app as it stands. This file describes
what it becomes when there is a real robot behind it.

---

## The one-line summary

**RODA-RS** runs the teleop. **Tauri** wraps the rig page into one binary
per machine and supervises RODA-RS beneath it. **`apps/server`** grows an
ingest side to catch what comes back. **Ansible** makes all twelve
machines identical. The rig writes to its own SSD first and uploads
second, so the floor keeps working when the network does not.

---

## The invariant this plan is built around

The repo already has one:

> The rig must never compute a different answer from the desk that
> scheduled it.

That is why the engine is a shared file. The backend needs the same rule
pointed the other way:

> **The server must never disagree with the rig about what happened.**

Which gives the same remedy — one shared definition, imported by both
sides, not re-typed on each. `packages/schema/payload.js` is the contract
for the schedule going out. It gets a sibling, `packages/schema/event.js`,
as the contract for events coming back, validated by the rig before it
journals and by the server before it stores. Same shape, same discipline,
same test file pattern.

There is a second half to that rule, and it is the one that is easy to get
wrong. **The rig sends measurements, never conclusions.** Today the event
log emits this:

```js
emit("stint_ended", "rig_productivity_blocks",
     operator() + " · " + S.episode + " episodes · " + pct(efficiency()) + " efficiency");
```

`"74% efficiency"` is a conclusion, and once it is stored it is the only
copy of the truth. What ships instead is the four numbers the ratio was
made of — `recordedSecs`, `assignedSecs`, `faultSecs`, `downSecs` — and the
backend computes the ratio, from one definition, at read time. The reason
is not tidiness: it means the efficiency formula can be corrected in six
months and every shift already recorded recomputes correctly. A stored
percentage cannot be corrected without re-running the floor.

---

## The four layers

```
┌─────────────────────────────────────────────────────────────┐
│  Tauri app  (one binary per rig, autostarts, full screen)   │
│                                                             │
│   ┌───────────────────────┐    ┌────────────────────────┐   │
│   │  webview              │    │  supervisor (Rust)     │   │
│   │  the rig page, as-is  │◄──►│  RODA-RS child process │   │
│   │  pedals, screens, log │    │  disk, journal, ids    │   │
│   └───────────────────────┘    └───────────┬────────────┘   │
└────────────────────────────────────────────┼────────────────┘
                                             │
                    /var/lib/rig/  episodes/ │ journal.ndjson
                                             │
                                    ┌────────▼────────┐
                                    │  uploader       │  drains the queue,
                                    │  (own process)  │  retries forever
                                    └────────┬────────┘
                                             │  LAN
                                    ┌────────▼────────┐
                                    │  apps/server    │  push + ingest + spool
                                    └────────┬────────┘
                                             │  trickle
                                    ┌────────▼────────┐
                                    │  cloud archive  │
                                    └─────────────────┘

  Ansible provisions every box in this picture, identically, from one inventory.
```

### 1. RODA-RS — the teleop layer

RODA-RS owns the robot, the cameras, and the act of recording an episode.
The rig page owns *when* — which is what the pedals have always decided —
and RODA-RS owns *how*.

There are no docs for it in this repo and none were available, so **this
plan does not invent its API.** Every call to it goes through one adapter
with one interface, and nothing else in the plan knows RODA-RS exists:

```rust
trait Roda {
    fn open(rig_id: &str, cameras: &[Camera]) -> Result<Session>;
    fn start_episode(&mut self, episode_id: Uuid) -> Result<()>;
    fn stop_episode(&mut self) -> Result<Recorded>;   // path, bytes, secs, cameras
    fn discard_episode(&mut self) -> Result<()>;      // stop and unlink
    fn health(&self) -> Health;                       // cameras, arms, disk
    fn close(self) -> Result<()>;
}
```

Six calls. When the real API arrives — CLI, socket, HTTP, library, it does
not matter which — one file changes and the rest of the tree does not
notice. That is the whole point of writing it down before knowing.

Ships alongside it: **`MockRoda`**, which satisfies the same trait, writes
a real file of plausible size to the real path, and returns real numbers.
It exists so the entire pipeline below — journal, uploader, ingest, spool,
archive — is built and tested end to end **before any hardware arrives**,
and hardware day is a swap rather than an integration.

### 2. Tauri — one binary per rig

The rig page is deliberately a browser page and stays one. Tauri hosts it
in a webview and gives it exactly the four things a browser cannot do:

- **spawn and supervise RODA-RS** — restart it if it dies, and surface
  that as a real `rig_down` rather than a frozen screen
- **write to the SSD** — the journal and the episode queue
- **own the pedals as raw HID** — today they are the keys `1`/`2`/`3`;
  on a rig they are a pedal board, and reading them below the browser
  means no focus loss, no stray keystroke, no browser chrome to escape to
- **know which rig it is** — see below

The webview loads the same `assets/rig.js` that runs in a browser today.
Where that file currently calls `emit()` into an array, it calls a Tauri
command that journals to disk — and when the Tauri bridge is absent (a
plain browser, the demo, the single-file dist) it falls back to the array.
Same pattern the schedule fetch already uses: server, then file, then
local. The demo must keep working; it is how the app gets reviewed.

**Rig identity comes from the machine, not from a person.** Ansible writes
`/etc/rig/id` when it provisions the host; Tauri reads it and hands it to
the page. Nobody ever types "RIG-07". This is the same decision the app
already made when it deleted its login screen — if the system knows, do
not ask.

### 3. The service — Python, in the platform team's tree

> **Superseded.** This section was written as "grow `apps/server` in
> Node", and that is what the decision below said. It changed when the
> platform team's codebase structure arrived: the return arrow is now
> `backend/`, a FastAPI service laid out in their convention
> (`gateway → services → core`, arrows enforced in CI by import-linter)
> so that `core/` and `services/rigs/` lift into their tree unmodified.
>
> `apps/server` still exists and still serves the desk's push for a
> plain static deploy. It is no longer where ingest is going.
>
> Everything below this line — the routes, the cursor, idempotency,
> all-or-nothing batches — was implemented exactly as written. Only the
> language and the address changed.

The routes, as built:

```
POST /api/rigs/:rigId/events               { events: [...] }   → { accepted, seq }
POST /api/rigs/:rigId/episodes/:episodeId  video bytes, resumable
GET  /api/rigs/:rigId/cursor               → { seq }  the last event it holds
```

`cursor` is the piece that makes the rig's life simple: on reconnect it
asks what the server already has and resends only what follows. No
bookkeeping negotiation, no lost tail.

Every event carries a client-minted `eventId` (uuid) and a per-rig `seq`.
The server dedupes on `(rigId, eventId)`, which makes ingest **idempotent**
— so the uploader can retry blindly, forever, without ever needing to know
whether its last attempt landed. That single property is what lets the
retry logic be twenty lines instead of two hundred.

Validation is all-or-nothing per batch, exactly as `/api/push` already
does it, and for the same reason: a half-accepted batch is worse than a
rejected one.

### 4. Ansible — twelve machines that are the same machine

Two playbooks and one inventory:

```
ops/ansible/
  inventory.ini          RIG-01..RIG-12 → hostnames, and the ingest box
  rig.yml                cameras/GPU drivers, RODA-RS, the Tauri app,
                         /etc/rig/id, pedal udev rules, the SSD mount,
                         NTP, log rotation, autologin + kiosk,
                         systemd: rig-app, roda-rs, rig-uploader
  server.yml             node, apps/server, systemd, reverse proxy,
                         the ingest spool disk, cloud credentials
  roles/…
```

Twelve rigs is exactly the number where hand-configuration works right up
until the day it silently does not — one machine on a different driver
version, producing subtly different video, discovered a month later.
Ansible's value here is not the install; it is that drift becomes visible
and fixable in one command.

---

## The data path, in detail

### Episode identity

The single most important join in the system: the video file on the SSD
and the `episode_saved` event must name the same thing.

The rig mints a uuid **at pedal-press**, before RODA-RS is told anything,
and passes it down. RODA-RS names its output directory by it. The event
carries it. No lookup table, no reconciliation, no possibility of an
orphaned take.

If RODA-RS insists on minting its own session id, the adapter records both
and the event carries `rodaSessionId` alongside — but the rig's id remains
the primary key, because the rig's id exists even for an episode RODA-RS
failed to start, and *that* is a row worth having.

### The journal

Every event is appended to `/var/lib/rig/journal.ndjson` and flushed to
disk **before** the UI advances. Disk first, network second, always. The
uploader is a separate process reading that file from a checkpoint; if it
dies, crashes, or is stopped for a week, not one event is lost, because it
was never the thing holding them.

This is also what makes an existing promise true. The rig already tells the
operator, on the session-ended screen:

> Downtime and episodes are queued for upload.

That sentence was aspirational when this was written. It is now true in
the browser: the rig journals to IndexedDB before it touches the network,
reads back on boot whatever the last one did not finish sending, and
forgets a row only when the server says it holds it. Video goes with it -
bytes are the one thing here with no second copy anywhere until a take is
confirmed.

One honest gap remains, and it is the reason the disk journal above is
still the target. A browser cannot write synchronously, so "flushed
before the UI advances" becomes "handed to the store before the network
is touched". A hard power cut in the few milliseconds before that
transaction commits can still lose the last event. Every failure short of
that - a reload, a crash, a closed lid, a discarded background tab - is
covered. Closing the last window needs a synchronous write, which needs
Tauri.

### Timestamps

`emit()` currently stamps events with `clock(S.t)` — `MM:SS` since the rig
booted, on a demo clock that can run at 8×. Useful on screen, useless in a
database. Persisted events gain real identity:

```json
{
  "eventId": "…", "seq": 4417,
  "at": "2026-08-22T09:14:02.881Z",
  "rigId": "RIG-03", "shiftDate": "2026-08-22", "shiftLabel": "Morning",
  "turnFrom": "09:00", "operatorId": "op-a4",
  "event": "episode_saved", "bucket": "episodes",
  "data": { "episodeId": "…", "durationSecs": 92, "score": 4, "bytes": 214000000 }
}
```

The on-screen `MM:SS` log stays exactly as it is — it is for the operator,
not the backend, and it is good.

Two clock notes worth writing down now rather than debugging later:
**durations come from a monotonic source**, never from subtracting two
wall-clock readings, so an NTP correction mid-episode cannot produce a
negative take; and **NTP is an Ansible responsibility**, because twelve
rigs disagreeing about the time makes the whole event stream unsortable.

### The five buckets become five tables

`emit()` already names them. They were chosen well and they do not change:

| bucket | one row per | carries |
|---|---|---|
| `episodes` | take, saved **or** discarded | episodeId, durationSecs, score, outcome, video path, bytes |
| `rig_shift_checks` | checklist pass, fault opened/closed/cancelled | subsystem, secondsCharged, outcome |
| `rig_downtime_events` | rig down → rig up | issue path through the tree, needsManager, downSecs, chargedTo |
| `rig_productivity_blocks` | stint (one operator's turn at one rig) | episodes, recordedSecs, assignedSecs, faultSecs, downSecs |
| `sessions` | operator at rig, turn start → turn end | operatorId, turnFrom, turnTo, endedBy |

Note that `rig_productivity_blocks` stores the four seconds columns and
**not** the efficiency percentage — for the reason given at the top.

Note also `chargedTo` on downtime. The app already makes this distinction
in the UI — *"Found at handover — charged to the previous operator, not
you"* — and it is a fact about a person's shift, so it must survive into
the data or the desk can never show it.

### Video, end to end

```
RODA-RS writes  →  /var/lib/rig/episodes/<episodeId>/{front,wrist-l,overhead}.mp4
uploader        →  POST to the on-prem box, resumable, checksum on arrival
on-prem         →  spool disk, then trickle to cloud archive
rig             →  unlinks the local copy only after the server confirms the checksum
```

The rig deleting its own copy only on confirmed checksum is the rule that
makes the SSD self-managing. Everything else is a retry.

**On-prem hop first, then cloud** — recommended, and the sizing is why.
Taking three 1080p30 cameras at ~7 Mbps each:

| | |
|---|---|
| one rig, one hour | ~9 GB |
| one rig, one 8h shift | ~72 GB |
| twelve rigs, one shift | ~0.9 TB |
| the whole floor, three shifts | **~2.6 TB/day** |

2.6 TB/day is ~240 Mbps sustained, every hour of every day, to keep up.
That is comfortable on a LAN and painful on a typical site uplink — which
is the entire argument. The rigs push to a box on the same switch at wire
speed and free their SSDs in minutes; that box owns the one slow
conversation with the cloud and can be behind by hours without any rig
noticing.

**These numbers are an estimate and they are load-bearing.** They decide
the SSD size, the uplink, and the cloud bill. The first thing to do when
RODA-RS is available is measure one real episode and correct this table —
a different codec or resolution moves every row.

On the estimate as it stands: a rig SSD should hold **at least three
shifts** of buffer (~220 GB) so a weekend outage is an inconvenience
instead of a stoppage. 2 TB is the sensible buy.

---

## Phasing

Ordered so that **nothing is blocked on hardware until it has to be**, and
each phase is worth having on its own.

### Phase 0 — make the events real *(no hardware, no new toolchain)*

- `packages/schema/event.js` + `event.test.js`, alongside the payload schema
- `emit()` gains real identity: `eventId`, `seq`, ISO `at`, rig, shift, turn,
  operator, and structured `data` instead of a prose sentence
- measurements not conclusions — the four seconds columns replace `pct()`
- `POST /api/rigs/:rigId/events`, `GET …/cursor` on `apps/server`, idempotent
- end-to-end test in the existing `node --test` suite: emit a shift, ingest
  it, read the buckets back, resend it all and prove nothing duplicates

The rig stays a browser page throughout. At the end of Phase 0 the demo is
already producing real, queryable data.

### Phase 1 — the shell *(no hardware)*

- Tauri project under `apps/rig/desktop/`, hosting the existing page unchanged
- `MockRoda` behind the adapter trait
- the disk journal, and `emit()` routed through it with browser fallback
- the uploader as its own process, with the checksum-then-unlink rule
- the on-prem spool on `apps/server`

At the end of Phase 1 every line of the pipeline has run, repeatedly, with
fake video. What is missing is only the camera.

### Phase 2 — real RODA-RS *(needs the API, and a rig)*

- replace `MockRoda` with the real adapter
- reconcile ids if RODA-RS mints its own
- measure a real episode; **correct the sizing table above**
- map RODA-RS health onto the existing fault and rig-down screens, so a
  camera dropping out raises the screen the operator already knows

Only the adapter file should change. If anything else has to, the boundary
was drawn in the wrong place and it is worth fixing then rather than
absorbing.

### Phase 3 — the floor *(needs twelve machines)*

- `ops/ansible/` and the two playbooks
- `/etc/rig/id` provisioning, pedal HID rules, kiosk autostart, NTP
- cloud archive and lifecycle policy
- one rig first, then eleven

---

## Decisions this plan takes, and why

**~~Node for the backend, growing `apps/server`.~~ Superseded: Python,
in the platform team's tree.** The original argument was one language
across desk, server, schema and tests, and it was a good one. What
outweighed it was where this code has to end up: the platform team runs
FastAPI, SQLAlchemy and Alembic in a layered tree they enforce in CI, and
a Node service would have had to be rewritten at the handover by people
who did not write it.

So `backend/` is Python and is laid out in their convention from the
first commit. The cost is a second language and a duplicated event
envelope — `packages/schema/event.js` and
`core/domains/rig_events/schema.py`. That cost is paid down deliberately:
`packages/schema/fixtures/` is loaded by both suites, so the two halves
cannot drift without CI saying which one broke.

The one thing that did **not** move is the rotation. It is computed in
exactly one shared JavaScript file, and the service stores the pushed
payload opaque and reads it back rather than re-deriving it. A Python
re-implementation would be a third answer and the first one that could
silently disagree.

**Tauri as the wrapper rather than a rewrite of the rig app.** The rig UI
is finished and good, and it was designed under a real constraint — read
from two metres away, three pedals, no colour-coding. Rewriting it native
would cost that for nothing. Tauri keeps the page and adds only the
capabilities the page cannot have.

**Offline-first, unconditionally.** The floor cannot stop because a switch
did. Journal to disk, upload from disk, idempotent ingest, resend from a
cursor.

**One adapter for RODA-RS, mocked from day one.** Without docs, the honest
options were to guess an API or to isolate the unknown. Isolating it also
happens to be what makes Phases 0 and 1 startable today.

---

## Open, and deliberately not filled in

- **The RODA-RS API itself.** The adapter is a TODO by design. It is the
  one file in this plan written to be replaced.
- ~~**Auth.**~~ **Settled and built.** A per-rig token placed by Ansible
  and checked on every rig-facing route: the *machine* authenticates, the
  person never does, and the rig keeps its central idea of a screen with
  no login. A token names one rig and is refused for any other, so one
  compromised machine cannot attribute work across the floor. Unset, the
  service is open and says so at startup and in `/api/health`, which is
  what a deploy check reads.

  Desk auth is deliberately **not** settled here. The desk is expected to
  sit behind the platform team's gateway, which already owns who is
  allowed in. `DESK_TOKEN` exists so a deployment without that in front
  of it can still close the hole, and it is off by default.
- **Retention.** Still open, and now the only thing standing between the
  system and a disk that fills. The mechanism is built, tested and off:
  `VIDEO_KEEP_DAYS=0` keeps everything for ever. What is missing is the
  number, and it is a cost decision - at this plan's own sizing, ~2.7 TB
  a day, ~82 TB a month, roughly a petabyte a year.

  Two parts of it are already answered. Discarded takes are never
  uploaded at all, so they cost nothing. And the on-prem spool is not a
  retention question: it releases its copy as soon as the archive can
  account for it, because a spool that never frees is not a spool.
- **Review and QA of scores.** The operator scores their own take 3/4/5.
  Nothing yet says whether anyone checks.
- **Crew changeover at the shift boundary.** Already open in `CLAUDE.md`,
  and ingest does not change it. Still to answer: handover window, or cold
  takeover.

These are questions to answer when the product is ready for them, not
requirements to quietly infer — same rule the rest of the repo follows.
