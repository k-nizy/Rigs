# What we need from the backend departments — v2

**Revised after the platform team's reply describing their codebase.**
The first version of this file asked a department we had never spoken to
for everything at once. Their reply settled more than we asked and opened
one question bigger than any of them. This version is the reply to the
reply.

`BACKEND-PLAN.md` is still our plan; where it now disagrees with this
file, this file wins and the plan needs amending.

---

## What their reply settled

Three of our open questions were answered without our having asked them,
and one of our own decisions is dead. All four are good outcomes.

| Our §0 decision | Their reply | Status |
|---|---|---|
| **Node, zero-dependency, for ingest** | FastAPI, SQLAlchemy 2 async, asyncpg, PostgreSQL 16+, Alembic — with layering enforced in CI by `lint-imports` | **Withdrawn.** It was on the veto list to die cheaply, and it has |
| **Per-rig token placed by Ansible** | **Ed25519, trust-on-first-use, for machine/station identity** | **Superseded, and theirs is better.** Machine identity is already a first-class concept in their system and is not a shared secret. We adopt it |
| Which cloud (§3.1) | **Cloudflare R2** in prod, MinIO for local dev, via boto3 | **Answered** |
| Background jobs (§0) | **taskiq + arq** | **Answered.** Our on-prem→cloud drain has a home |

Also noted and adopted without argument: one uniform shape for every
standalone worker — claim-and-process poll loop, shared
`install_signal_handlers()` / `is_shutdown_requested()`, argparse,
`logging.basicConfig`, exponential backoff as `backoff_base ** attempt`
rather than a library, and a **boto3 client per worker sized to its own
concurrency** rather than a shared global pool. Our uploader and drain
worker will look like every other worker they have.

---

## 1. The one question that decides everything

Their message offers itself as *"a solid reference if you're building
something similar."* That sentence has two readings and we cannot start
the server side until we know which:

**A — we build inside their codebase.** Ingest becomes a service module
under `services/`, our five buckets become five domains under
`core/domains/`, and `apps/server` is scaffolding we delete.

**B — we build alongside, borrowing the conventions.** `apps/server`
stays ours, shaped like theirs.

> **1.1 — Do we build inside your codebase as a service module, or
> alongside it in our own repo?**

**Our preference is A**, and not out of politeness. Our five buckets were
named before we had ever seen their layout and they land on it almost
exactly: each is a bounded context with a model, a repository and a
schema, and two of them have real state transitions that belong in a
`lifecycle.py`. Building B would mean maintaining a second, weaker copy
of an architecture they already enforce in CI.

If A, one follow-up on their own convention:

> **1.2 — Five domains, or one `rigs` domain with five models?** Our five
> buckets are listed in §3. They are separate bounded contexts to us, but
> they are all one product surface, and you know better than we do where
> that line falls in your tree.

---

## 2. What we would build, in your convention

Written out so the answer to 1.1 can be concrete rather than
theoretical.

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
question in §5 is urgent):

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

## 4. The gap in your reply — video

This is the substantive omission, and it is the highest-volume path in
the entire system.

The reply describes a request/response API and a boto3 client. It does
not describe how bulk bytes move. Our sizing, which is still an estimate:

| | |
|---|---|
| one rig, one 8-hour shift | ~72 GB |
| twelve rigs, one shift | ~0.9 TB |
| the whole floor, three shifts | **~2.6 TB/day** |
| sustained rate just to keep up | **~240 Mbps** |
| a year, if nothing is deleted | ~950 TB |

> **4.1 — How do 2.6 TB/day actually reach R2?** Presigned URLs direct
> from the rigs, multipart through the gateway, or something else?
> Streaming that volume through FastAPI workers is our concern; if you
> have a pattern for large-object upload we should be using it.
>
> **4.2 — Can rigs upload to an on-prem MinIO on the same switch, and
> drain to R2 from there?** You listed MinIO as local dev only. We want
> it as a production spool. The argument is the site uplink: rigs push to
> a box on the same switch at wire speed and free their SSDs in minutes,
> and that box owns the one slow conversation with the cloud and can be
> hours behind without any rig noticing.
>
> **4.3 — Is the resumable-upload and checksum-on-arrival behaviour
> something your storage layer already does?** Our rig deletes its local
> copy only after the server confirms a checksum. That rule is what makes
> the rig SSD self-managing; everything else is retry.

---

## 5. Still open, and yours

> **5.1 — What is your convention for idempotency keys?** Every event
> carries a client-minted `eventId` and a per-rig `seq`. We need dedupe on
> `(rigId, eventId)` so the uploader can retry blindly, forever, without
> knowing whether its last attempt landed. That single property is what
> keeps the retry logic twenty lines instead of two hundred.
>
> **5.2 — Ed25519 TOFU: who enrols a rig, and what happens when one is
> re-imaged?** Trust-on-first-use has to have a first use. Is enrolment a
> human action, an Ansible action, or automatic on first contact — and
> does a re-imaged RIG-07 present as a new station or the same one?
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
> **5.5 — Monitoring, logging, on-call.** What stack, and where should a
> "rig has not been heard from in N minutes" alert land? We would rather
> emit into what you run than invent a dashboard.

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
> across 950 TB later. Also: do they want the *discarded* takes, do they
> need frame-level camera sync, and is operator identity PII here?
>
> **6.4 — Who owns the R2 bill and the retention policy?** At full tilt
> storage is the dominant cost of the whole system, and it is set by a
> retention policy that does not exist. How long do episodes live, hot
> then cold then deleted — and are discarded takes kept at all?

Also worth asking whoever runs config management:

> **6.5 — Do you already run Puppet, Salt, Chef or MDM images?** We
> planned Ansible for the twelve machines. If you have a standard, we
> should use yours rather than adding a thirteenth tool.

---

## 7. What we are doing meanwhile

Phase 0 splits, and half of it needs nobody.

**0a — the rig side. Started, and unaffected by any answer above.**

- real event identity in `emit()`: `eventId`, per-rig `seq`, ISO
  timestamp, rig, shift date and label, turn, operator
- **measurements, not conclusions** — the four seconds columns replace the
  `"74% efficiency"` string the rig emits today, so the ratio is computed
  at read time from one definition and can be corrected later
- the on-screen `MM:SS` log stays exactly as it is; it is for the
  operator, not the database, and it is good

**0b — the contract. Started, and useful under either answer to 1.1.**

- the event schema as JSON Schema, with fixtures both sides must accept
- the five-domain mapping in §2, as a concrete proposal rather than a
  conversation

**0c — the server side. Blocked on 1.1**, and only on 1.1.

---

## What we need, by when

| When | What | Blocks |
|---|---|---|
| **First** | §1.1 — inside your codebase, or alongside? | The entire server side |
| **This week** | §6.3 — the training-data format | The event schema, which we are writing now |
| **This week** | §5.3 — confirm no person-level login on the floor | The product, not just the plumbing |
| **This week** | §6.1–6.4 — who do we talk to? | Everything not on your desk |
| **Soon** | §4.1–4.3 — how video moves | All sizing, and the uploader |
| **Soon** | §5.1 — idempotency convention | Blind retry, and a simple uploader |
| **Before rollout** | §6.2 — machines, network, NTP | Twelve-machine provisioning |
| **Before go-live** | §6.4 — retention and the bill | Cost, and how long we can run |

## What we will assume if we hear nothing

We build 0a and 0b as described, and Phase 1 behind `MockRoda`: the disk
journal, the uploader, the on-prem spool, the whole pipeline end to end
with fake video. None of that is wasted under either answer to 1.1.

It stops where it needs a camera, a machine we do not own, or a bucket
somebody pays for.

---

## One measurement still worth more than any answer here

**Record one real episode on real hardware and send us the numbers** —
duration, bytes per camera, container, codec, resolution, frame rate.

Every figure in §4 is derived from "three 1080p30 cameras at ~7 Mbps," and
that is a guess. It sets the SSD size, the uplink requirement, the spool
disk and the R2 bill. Nothing else on this page would correct as much.
