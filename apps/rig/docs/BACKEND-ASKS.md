# What we need from the backend departments — v3

**v1** asked a department we had never spoken to for everything at once.
**v2** was the reply to their reply. **This version is written after
building it.**

That changes what this document is for. Almost nothing here is a request
for help any more - the service exists, in your convention, with 242
tests, and section 7 lists what is in the tree. What is left is a much
shorter list of things only you can answer, and one of them we have
narrowed from an open question to a choice between two implementations
that both already work.

Where we could not wait, we chose and said so - idempotency (5.1), the
token scheme (5.2), retention. Every one of those is a small change if
your answer differs, and cheaper now than at the handover, which is the
whole reason they are flagged rather than buried.

`BACKEND-PLAN.md` is still our plan; where it now disagrees with this
file, this file wins and the plan needs amending.

---

## What their reply settled

Three of our open questions were answered without our having asked them,
and one of our own decisions is dead. All four are good outcomes.

| Our section 0 decision | Their reply | Status |
|---|---|---|
| **Node, zero-dependency, for ingest** | FastAPI, SQLAlchemy 2 async, asyncpg, PostgreSQL 16+, Alembic — with layering enforced in CI by `lint-imports` | **Withdrawn.** It was on the veto list to die cheaply, and it has |
| **Per-rig token placed by Ansible** | **Ed25519, trust-on-first-use, for machine/station identity** | **Superseded, and theirs is better.** Machine identity is already a first-class concept in their system and is not a shared secret. We adopt it |
| Which cloud (section 3.1) | **Cloudflare R2** in prod, MinIO for local dev, via boto3 | **Answered** |
| Background jobs (section 0) | **taskiq + arq** | **Answered.** Our on-prem→cloud drain has a home |

Also noted and adopted without argument: one uniform shape for every
standalone worker — claim-and-process poll loop, shared
`install_signal_handlers()` / `is_shutdown_requested()`, argparse,
`logging.basicConfig`, exponential backoff as `backoff_base ** attempt`
rather than a library, and a **boto3 client per worker sized to its own
concurrency** rather than a shared global pool. Our uploader and drain
worker will look like every other worker they have.

---

## 1. ANSWERED — we build inside their codebase

Their message offered itself as *"a solid reference if you're building
something similar,"* which read two ways and blocked the whole server
side. **Decided: we build inside their tree.**

> **1.1 — Do we build inside your codebase as a service module, or
> alongside it in our own repo?**
> **→ Inside, as a service module.**

That was our preference and not out of politeness. Our five buckets were
named before we had ever seen their layout and they land on it almost
exactly: each is a bounded context with a model, a repository and a
schema, and two of them have real state transitions that belong in a
`lifecycle.py`. Building alongside would have meant maintaining a second,
weaker copy of an architecture they already enforce in CI.

### What the decision commits us to

- Ingest is a FastAPI sub-app at `services/rigs/`, mounted by their
  gateway, following their layering — `gateway` may import `services` and
  `core`; `services` may import `core` but never each other; `core` never
  imports upward. `lint-imports` enforces it in CI, so this is not a
  convention we can drift from.
- Our buckets become domains under `core/domains/`, each with
  `model.py`, `repository.py`, `schema.py`, and `lifecycle.py` where
  there are real state transitions.
- Workers follow their standalone shape exactly: claim-and-process poll
  loop, shared `install_signal_handlers()`, argparse,
  `logging.basicConfig`, exponential backoff as `backoff_base ** attempt`,
  a boto3 client per worker sized to its own concurrency.
- Postgres 16 with SQLAlchemy 2 async and Alembic migrations; R2 in
  production, MinIO locally; taskiq and arq for background work; pytest
  with `asyncio_mode = "auto"`.
- **`apps/server/server.js` becomes scaffolding.** It is not deleted on
  day one — it still serves the static tree for local development and
  the demo, and the desk's push points at it today. It stops being the
  ingest story, and it retires once the FastAPI routes carry the push.

### What we still need from you to start

> **1.2 — Five domains, or one `rigs` domain with five models?** Our
> buckets are listed in section 3. They are separate bounded contexts to
> us, but they are all one product surface, and you know better than we
> do where that line falls in your tree. This is now the only structural
> question left open.

Practical access, none of it blocking the contract work:

> **1.3 — Repository access and the contribution route.** Push rights or
> fork-and-PR? Which branch do we target, and who reviews a service we
> own but you host?
>
> **1.4 — A local environment that runs.** Postgres, MinIO and the
> gateway together — is there a compose file or a documented setup, or
> do we write one?
>
> **1.5 — Where do Alembic migrations live** for a new service, and who
> runs them in each environment?
>
> **1.6 — Is there a service template or a recent service** we should
> copy the shape of? Reading one you consider exemplary is worth more
> than any style guide.

---

## 2. What we built, in your convention

This was written as a proposal, so the answer to 1.1 could be concrete
rather than theoretical. It is now a description: the tree below exists,
with the tests in section 7 against it. It is here so you can object to
the shape before it lands in your repository rather than after.

```
services/rigs/
  app.py                 FastAPI sub-app, mounted by the gateway
  routes.py              ingest, cursor, schedule push, video
  workflows.py           episode + video reconciliation
  schema_rigs.py

core/domains/
  episodes/                    take, saved or discarded
  rig_shift_checks/            checklist pass, fault opened/closed/cancelled
  rig_downtime_events/         rig down → rig up          (lifecycle.py)
  rig_productivity_blocks/     one operator's turn at one rig
  sessions/                    operator at rig, turn start → end  (lifecycle.py)
```

The two with `lifecycle.py` are the two with genuine state transitions —
downtime opens and closes, a session starts and ends, and both can end in
more than one way.

**What the five carry** (unchanged from v1, and the reason the schema
question in section 5 is urgent):

| domain | one row per | carries |
|---|---|---|
| `episodes` | take, saved **or** discarded | episodeId, durationSecs, score, outcome, video path, bytes |
| `rig_shift_checks` | checklist pass, fault opened / closed / cancelled | subsystem, secondsCharged, outcome |
| `rig_downtime_events` | rig down → rig up | issue path, needsManager, downSecs, chargedTo |
| `rig_productivity_blocks` | stint | episodes, recordedSecs, assignedSecs, faultSecs, downSecs |
| `sessions` | operator at rig, turn start → turn end | operatorId, turnFrom, turnTo, endedBy |

`rig_productivity_blocks` stores the four seconds columns and **not** an
efficiency percentage, so the formula can be corrected later and every
past shift recomputes correctly. A stored percentage cannot be corrected
without re-running the floor.

---

## 3. Two things we need to protect

Neither is a request. Both are properties of our side that a helpful
person could break without realising, so they are written down before
anybody does.

### 3.1 The schedule engine must not be reimplemented in Python

`packages/engine/rotation-engine.js` is one file with no DOM and no
globals, and **both** the manager's desk and the rig load it. That is the
founding invariant of this codebase:

> The rig must never compute a different answer from the desk that
> scheduled it.

A Python port would be a third implementation and the first one that can
silently disagree. **The backend should store the pushed payload and
never recompute from it.** If the server needs to know who was on a rig at
a given minute, it should read what was pushed, not derive it.

### 3.2 The contract now spans two languages

`packages/schema/payload.js` is JavaScript, and it is already a contract
between three things — desk, server, rig. If ingest is Pydantic, the
event contract becomes a contract in two languages with nothing proving
they agree. That is exactly the drift this repo is built to prevent.

*Our proposal:* express the event schema as **JSON Schema**, generate or
validate both the Pydantic models and our JS validator from it, and keep
a directory of **fixture payloads that both test suites must accept.** If
either side stops accepting a fixture, CI says so.

> **3.3 — Does that fit how you already handle cross-language contracts,
> or do you have a pattern for this we should use instead?**

---

## 4. Video — one decision, and we have built both answers

This was written as an open question and you have not answered it. That
is probably our fault for asking an open question, so here it is as a
decision instead: **both implementations exist, both are tested, and we
need you to point at one.** Nothing is blocked on the answer; what is
blocked is knowing which one to provision for.

### What it costs, so the choice is about a real number

| | |
|---|---|
| one rig, sustained | 2.62 MB/s |
| one rig, one 8-hour shift | ~76 GB |
| twelve rigs, one shift | ~0.91 TB |
| the whole floor, three shifts | **2.72 TB/day** |
| sustained uplink just to keep up | **252 Mbps** |
| cold tier at our 90-day retention | **245 TB steady state** |

Retention is now decided at our end: 90 days, so the figure to provision
is 245 TB flat rather than the ~993 TB/year an unbounded archive would
reach. It climbs for ninety days and is level after that.

These are still derived from "three 1080p30 cameras at ~7 Mbps", which is
an assumption we wrote down. `GET /api/floor/video` reports the measured
bytes-per-second beside that assumption, so the table corrects itself the
moment a real episode lands. No real episode has landed yet - we have no
camera until the Tauri shell exists - so treat the table as a plan, not a
measurement, and expect it to move.

### The two implementations, both working

Everything about video sits behind one file,
`core/infrastructure/storage.py`. `upload_target()` decides which model is
in use and **nothing above it changes either way** - not the rig, not the
bookkeeping, not the drain, not the rule that lets a rig delete its own
copy. That is not a claim: the same end-to-end test runs against a local
directory and against a real MinIO bucket, and neither needs a
conditional anywhere else.

**A. Presigned PUT straight to the object store.** The rig asks us where
to put a take, we hand back a signed URL, the bytes never touch an
application worker. `ChecksumSHA256` is part of the signature, so the
store computes the digest as the bytes arrive and refuses a mismatch -
which matters, because with a presigned PUT we never see the bytes and
could not otherwise verify them.

**B. Streamed through the service.** `PUT /api/storage/{key}`. Simpler to
reason about and needs no presigning, and it means 2.72 TB/day crossing
application workers. We have it because our local stand-in has no
presigning, and we would not choose it for a floor.

We assume **A**, and will ship A unless you say otherwise.

### What we actually need from you

> **4.1 — A or B?** If A, we need a bucket, credentials, and confirmation
> that your storage layer permits `ChecksumSHA256` in a presigned PUT. If
> B, we need to know that streaming that volume through your gateway is
> acceptable to you, because it is not to us.

> **4.2 — May MinIO be a production spool, not just local dev?** You
> listed it as dev-only. We want it on the same switch as the rigs. The
> argument is the uplink: rigs push at wire speed and free their SSDs in
> minutes, and that box owns the one slow conversation with the cloud and
> can be hours behind without any rig noticing. Our drain worker already
> works this way and frees the spool only after the archive confirms what
> it holds.

> **4.3 — Does your storage layer already do resumable upload?** Ours does
> not, deliberately: it is the one piece we did not build because it is
> shaped differently under A than under B - S3 multipart versus a chunked
> protocol of our own. If you have a pattern, we will use it. If not, tell
> us and we will build the one that fits your answer to 4.1.

The checksum-on-arrival rule is no longer a question. We built it: the rig
deletes its local copy only after this service reads the object back out
of the store and verifies size and digest. It is what makes the rig SSD
self-managing, and it is tested against a real bucket.

### If we do not hear back

We ship **A**, against MinIO on the floor switch draining to R2, with no
resumable upload. All three are reversible - A/B is one file, the spool is
configuration, and resumable upload is additive. We would rather change
one file later than hold the floor waiting.

---

## 5. Still open, and yours

> **5.1 — Idempotency: built. Does it match your convention?** We could
> not wait, so we chose: a client-minted `eventId`, a per-rig `seq`, and a
> unique constraint on `(rigId, eventId)`. A resend reports
> `accepted: 0`, and `GET /cursor` tells a rig where it got to. The
> uploader retries blindly and the retry logic is twenty lines.
>
> If your convention differs it is a small change at our end and we would
> rather make it now than at the handover.
>
> **5.2 — We diverged from Ed25519 TOFU, and you should know before it
> is expensive.** You suggested trust-on-first-use. We shipped a per-rig
> bearer token placed by Ansible instead: simpler, no enrolment ceremony,
> and no question about what a re-imaged RIG-07 presents as. A token names
> exactly one rig and is refused for any other, so one compromised machine
> cannot attribute work across the floor.
>
> If TOFU is a requirement rather than a suggestion, tell us. It replaces
> one file and we would rather do it now.
>
> The original question still stands under either scheme: **who enrols a
> rig, and what happens when one is re-imaged?** For us that is "Ansible
> writes a token", but it is your provisioning story, not ours.
>
> **5.3 — The rigs have no person-level login, and we intend to keep it
> that way.** You mention JWT for *human operators*, which we read as the
> desk and admin surfaces, not the floor. Please confirm.
>
> Identity on the floor comes from the schedule: the desk knows who is at
> RIG-03 at 09:12, so the rig never asks. The machine authenticates; the
> person never does. This is the decision that let the app delete its
> login screen and its task picker.
>
> The honest cost: if someone stands at the wrong rig, both rigs
> mis-attribute and **nothing can detect it.** We think that is worth
> paying and would rather detect contradictions at the desk than put a
> login on the floor. If your security position requires per-person auth
> at the machine, that changes the product, not just the plumbing — and
> we need to know now.
>
> **5.4 — What is your event bus (`core/events/`) for, and should our
> domain events use it?** Ours are: episode saved, fault opened, rig down,
> stint ended. Some of them want to fan out to notifications.
>
> **5.5 — Monitoring, logging, on-call. We emit; where should it land?**
> We did not invent a dashboard. What exists is: structured logs carrying
> a request id taken from `X-Request-ID` when something upstream sets one,
> so our lines join yours; `GET /api/health` that runs a real query and
> answers 503 when the database is unreachable, and reports which of four
> cross-cutting switches are off; and seven alert kinds reconciled by a
> sweep, including two absences nothing can publish - a rig nobody can
> hear, and a turn nobody arrived for.
>
> One of those seven watches us rather than the floor: if the projection
> worker dies, the ledger keeps accepting and every board goes on
> answering with yesterday's episodes. `/api/floor/state` reports the lag.
>
> Where does a "RIG-07 has not been heard from in three minutes" page
> land, and in what format?

---

## 6. Not yours — so whose?

Your reply covered the platform. Three whole sections of v1 went
untouched, which we take to mean they belong to other departments. We
need pointing at them.

> **6.1 — Who owns RODA-RS?** It runs the teleop and owns the cameras and
> the act of recording. We have written our entire integration behind a
> six-call adapter trait and a mock, precisely because we have no docs for
> it. That adapter is the one file in our plan designed to be replaced —
> but nothing behind it can be real until we can talk to whoever owns it.
>
> **6.2 — Who owns the twelve rig machines and the floor network?** We
> need OS and specs, 2 TB NVMe per rig, SSH and sudo for provisioning,
> whether all twelve sit on the same switch, the site uplink in sustained
> Mbps, firewall rules to the ingest box — and **a site NTP source.**
> Twelve rigs disagreeing about the time makes the event stream unsortable
> and mis-attributes every handover. It is the smallest and most
> consequential item on this page.
>
> **6.3 — Who consumes the training data?** This one is urgent even
> though it feels far away, because it decides the event schema and the
> schema is what we are building now. Specifically: **what format does the
> training pipeline want?** We assume mp4 per camera because we assume
> that is what RODA-RS emits. If it wants LeRobot dataset format, MCAP,
> rosbag or HDF5, that is a conversion built once now or retrofitted
> across the archive later. Also: do they want the *discarded* takes -
> we currently never upload them, so they cost nothing and do not exist -
> do they need frame-level camera sync, and is operator identity PII here?
>
> **6.4 — Who owns the R2 bill?** The retention policy now exists at our
> end: 90 days, which is 245 TB steady state rather than the ~993 TB a
> year an unbounded archive reaches. It climbs for ninety days and is
> level after that. We picked a number so the system could not quietly
> fill a disk; whoever pays the bill should confirm or change it, and it
> is one setting.
>
> The original question, still yours: how long do episodes live, hot
> then cold then deleted — and are discarded takes kept at all?

Also worth asking whoever runs config management:

> **6.5 — Do you already run Puppet, Salt, Chef or MDM images?** We
> planned Ansible for the twelve machines. If you have a standard, we
> should use yours rather than adding a thirteenth tool.

---

## 7. What we built while waiting

This section used to say "Phase 0 splits, and half of it needs nobody",
with the server side blocked on your answer to 1.1. You answered 1.1 -
inside your codebase - and we built the rest. It is not a plan any more,
so here is what is actually in the tree, in your convention, ready to
lift.

**`core/` and `services/rigs/` lift as-is.** FastAPI, SQLAlchemy 2 async,
Alembic, `gateway -> services -> core` with the arrows enforced in CI by
import-linter. `local_gateway.py` is a dev harness and does not lift;
your gateway already exists.

| | |
|---|---|
| The ledger | append-only, unique on `(rigId, eventId)`, cursor protocol |
| Projection | five fact domains, claimed with `FOR UPDATE SKIP LOCKED` so more than one worker is safe |
| Replay | every fact is derived; wipe and rebuild is tested |
| Recovery | the whole database lost and rebuilt from envelopes alone, tested |
| The floor sweep | seven alert kinds, including absences nothing publishes |
| Video | both upload models, checksum-on-arrival, spool that frees itself, 90-day retention |
| Auth | per-rig bearer tokens, constant-time, one token names one rig |
| Limits | bounded uploads, optional per-rig rate limit, both reported by `/health` |
| Tests | 242 backend, 175 JavaScript, three layering contracts, one end-to-end |

**Measured rather than assumed.** `tools/benchmark` seeds a realistic
floor and times what runs continuously. The floor produces ~9,800 events
a day, about 0.11 per second sustained; projection runs ~400 a second.
The sweep takes 31.6 ms, 0.21% of its fifteen-second budget. Writing that
benchmark found a query inside a loop costing 190 ms of a 213 ms sweep,
which is the sort of thing that is invisible until somebody measures it.

**What is not built, and why.**

- **The camera.** No Tauri shell yet, so no real video has ever moved
  through the path. Every byte it has carried was synthetic. The sizing
  table in section 4 is a plan until that changes.
- **Resumable upload.** Deliberately: it is shaped differently under
  answer A than answer B to 4.1, and building both would waste one.

---

## What we need, by when

Everything not on this list is done or does not need you.

| When | What | Blocks |
|---|---|---|
| **Now** | 4.1 — presigned or through the gateway | Provisioning, and whether we build resumable upload |
| **Now** | 4.2 — MinIO as a production spool | The floor's network design |
| **Now** | 5.3 — confirm no person-level login on the floor | The product, not the plumbing |
| **Now** | 6.1 — repo access and the contribution route | Handing any of the above over |
| **This week** | 5.2 — is Ed25519 TOFU a requirement? | One file, cheap now and not later |
| **This week** | 6.3 — the training-data format | Whether our episode rows are the right shape |
| **Before rollout** | 6.2 — machines, network, NTP | Twelve-machine provisioning |
| **Before go-live** | 6.4 — who pays for 245 TB | Cost |

## What we will assume if we hear nothing

Presigned PUT straight to the store, MinIO on the floor switch draining
to R2, no resumable upload, per-rig bearer tokens, 90-day retention.

Every one of those is reversible: the upload model is one file, the spool
is configuration, resumable upload is additive, the token scheme is one
file, and retention is a number. We would rather change one file later
than hold twelve rigs waiting for an answer.

---

## One measurement still worth more than any answer here

**Record one real episode on real hardware and send us the numbers** —
duration, bytes per camera, container, codec, resolution, frame rate.

Every figure in section 4 is derived from "three 1080p30 cameras at ~7 Mbps," and
that is a guess. It sets the SSD size, the uplink requirement, the spool
disk and the R2 bill. Nothing else on this page would correct as much.

There is somewhere for it to go. `GET /api/floor/video` already reports
the measured bytes-per-second beside `planAssumedBytesPerSecond`, so the
first real episode to land corrects the table by itself and everyone can
see both numbers at once. Until then we are provisioning against an
assumption, and saying so.
