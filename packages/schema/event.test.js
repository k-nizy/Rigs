/* =====================================================================
 * event.test.js
 *
 * The return arrow's contract, tested the way the payload's is.
 *
 * The most important test in this file is the last group: every file in
 * fixtures/ has to validate. Those fixtures are the only thing that can
 * prove the JavaScript validator and the ingest service's Pydantic
 * models still agree, once the two sides are written in different
 * languages. If either stops accepting a fixture, CI says so on the side
 * that broke.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { validate, BUCKETS } = require("./event.js");

const FIXTURES = path.join(__dirname, "fixtures");
const SCHEMA = JSON.parse(fs.readFileSync(path.join(__dirname, "event.schema.json"), "utf8"));

/* A known-good envelope, so each test can break exactly one thing. */
function good(over) {
  return Object.assign({
    eventId: "00000000-0000-4000-8000-000000000001",
    seq: 4417,
    at: "2026-08-24T09:14:02.881Z",
    rigId: "RIG-03",
    shiftDate: "2026-08-24",
    shiftLabel: "Morning",
    turnFrom: "09:00",
    operatorId: "op-a4",
    operatorName: "Nadia Haddad",
    bucket: "episodes",
    event: "episode_saved",
    data: { episodeId: "11111111-1111-4111-8111-111111111111", durationSecs: 92, score: 4 },
  }, over || {});
}

const why = (ev) => validate(ev).errors.join("; ");

/* ------------------------------------------------------------ identity */

test("a well-formed event validates", () => {
  assert.deepEqual(validate(good()), { ok: true, errors: [] });
});

test("eventId has to be a uuid, because ingest dedupes on it", () => {
  assert.match(why(good({ eventId: "4417" })), /eventId must be a uuid/);
  assert.match(why(good({ eventId: undefined })), /eventId must be a uuid/);
});

test("seq has to be a whole number, because it is the uploader's cursor", () => {
  assert.match(why(good({ seq: -1 })), /seq must be/);
  assert.match(why(good({ seq: 1.5 })), /seq must be/);
  assert.match(why(good({ seq: "4417" })), /seq must be/);
  assert.ok(validate(good({ seq: 0 })).ok, "seq 0 is the first event of a rig's life");
});

test("at has to carry a zone - a timestamp without one is unsortable across rigs", () => {
  assert.ok(validate(good({ at: "2026-08-24T09:14:02.881Z" })).ok);
  assert.ok(validate(good({ at: "2026-08-24T09:14:02+02:00" })).ok);
  assert.match(why(good({ at: "2026-08-24T09:14:02" })), /at must be/);
  assert.match(why(good({ at: "09:14" })), /at must be/, "MM:SS since boot is for the operator, not the database");
});

/* --------------------------------------------- where and when it happened */

test("the schedule's own fields are required", () => {
  assert.match(why(good({ rigId: "" })), /rigId/);
  assert.match(why(good({ shiftDate: "24-08-2026" })), /shiftDate must be YYYY-MM-DD/);
  assert.match(why(good({ shiftLabel: undefined })), /shiftLabel/);
});

test("turn and operator may be null, because a rig on standby still reports", () => {
  assert.ok(validate(good({ turnFrom: null, operatorId: null })).ok);
  assert.match(why(good({ turnFrom: "9:00" })), /turnFrom must be HH:MM/);
});

/* The id is a seat - rotation-engine builds it as "op-" + group + slot -
   so the same string is a different person on a cover day, and no table
   downstream keeps a name to tell them apart. */
test("an event carries the operator's name, not only the seat they sat in", () => {
  assert.ok(validate(good()).ok);
  assert.match(why(good({ operatorName: 42 })), /operatorName must be a string or null/);
});

/* The shape of every event already queued on a rig the day this ships.
   A rig does not retry a refused batch - it drops it from the outbox and
   forgets it from the journal - so a demanded field is destroyed work. */
test("an event with no operator name at all is still a valid event", () => {
  const ev = good();
  delete ev.operatorName;
  assert.ok(validate(ev).ok, why(ev));
  assert.ok(validate(good({ operatorName: null })).ok, "and null is fine on standby");
});

/* ------------------------------------------------------- bucket and event */

test("an event has to belong to the bucket it claims", () => {
  assert.match(why(good({ bucket: "sessions" })),
    /episode_saved.*does not belong in bucket.*sessions/);
});

test("the bucket has to be one the far side has a table for", () => {
  assert.match(why(good({ bucket: "episodes_v2" })), /is not one of/);
});

test("every event named in the map belongs to exactly one bucket", () => {
  const seen = new Map();
  for (const [bucket, events] of Object.entries(BUCKETS)) {
    for (const ev of events) {
      assert.ok(!seen.has(ev), ev + " appears in both " + seen.get(ev) + " and " + bucket);
      seen.set(ev, bucket);
    }
  }
});

/* --------------------------------------------- measurements, not conclusions */

test("stint_ended carries the four seconds columns", () => {
  const stint = {
    bucket: "rig_productivity_blocks", event: "stint_ended",
    data: { episodes: 14, recordedSecs: 2140, assignedSecs: 2700, faultSecs: 0, downSecs: 180 },
  };
  assert.ok(validate(good(stint)).ok, why(good(stint)));

  for (const missing of ["recordedSecs", "assignedSecs", "faultSecs", "downSecs"]) {
    const d = Object.assign({}, stint.data);
    delete d[missing];
    assert.match(why(good(Object.assign({}, stint, { data: d }))), new RegExp("data\\." + missing),
      missing + " is one of the four the ratio is made of - it cannot be optional");
  }
});

test("an efficiency percentage is not a thing an event may carry instead", () => {
  /* The rig used to emit "74% efficiency" as a sentence. Stored, that is
     the only copy of the truth and cannot be corrected without re-running
     the floor. The four numbers can be, so they are what ships. */
  const conclusion = {
    bucket: "rig_productivity_blocks", event: "stint_ended",
    data: { episodes: 14, efficiency: "74%" },
  };
  const r = validate(good(conclusion));
  assert.equal(r.ok, false, "a stint with a percentage and no measurements must be rejected");
  assert.match(r.errors.join("; "), /recordedSecs/);
});

test("seconds are never negative and never a formatted string", () => {
  const mk = (v) => good({
    bucket: "rig_productivity_blocks", event: "stint_ended",
    data: { episodes: 1, recordedSecs: v, assignedSecs: 60, faultSecs: 0, downSecs: 0 },
  });
  assert.match(why(mk(-1)), /recordedSecs/);
  assert.match(why(mk("35:40")), /recordedSecs/);
  assert.ok(validate(mk(0)).ok, "zero recorded seconds is a real outcome");
});

test("a score is 3, 4 or 5 - the three pedals on the review screen", () => {
  for (const s of [3, 4, 5]) assert.ok(validate(good({ data: Object.assign({}, good().data, { score: s }) })).ok);
  for (const s of [2, 6, "4", null]) {
    assert.match(why(good({ data: Object.assign({}, good().data, { score: s }) })), /score must be 3, 4 or 5/);
  }
});

test("downtime records who it was charged to, because the app already says so on the wall", () => {
  const down = {
    bucket: "rig_downtime_events", event: "rig_down",
    data: { issue: "Gripper broken", needsManager: false, chargedTo: "previous_operator" },
  };
  assert.ok(validate(good(down)).ok, why(good(down)));
  const d = Object.assign({}, down.data); delete d.chargedTo;
  assert.match(why(good(Object.assign({}, down, { data: d }))), /chargedTo/);
});

test("unknown fields in data are allowed through, so the rig can ship before the server reads", () => {
  const d = Object.assign({}, good().data, { cameraCount: 3, rodaSessionId: "abc" });
  assert.ok(validate(good({ data: d })).ok);
});

/* ------------------------------------------------------------- fixtures
 *
 * The cross-language guarantee. Both sides must accept every file here.
 */

const files = fs.readdirSync(FIXTURES).filter((f) => f.endsWith(".json"));

test("there are fixtures, and they cover every event the floor can produce", () => {
  assert.ok(files.length > 0, "fixtures/ is empty");
  const covered = new Set(files.map((f) => JSON.parse(fs.readFileSync(path.join(FIXTURES, f), "utf8")).event));
  const all = Object.values(BUCKETS).flat();
  const missing = all.filter((e) => !covered.has(e));
  assert.deepEqual(missing, [],
    "no fixture covers: " + missing.join(", ") + " - an event with no fixture is one the two sides can silently disagree about");
});

for (const f of files) {
  test("fixture " + f + " validates", () => {
    const doc = JSON.parse(fs.readFileSync(path.join(FIXTURES, f), "utf8"));
    const r = validate(doc);
    assert.deepEqual(r.errors, [], f + " no longer validates: " + r.errors.join("; "));
  });
}

/* ------------------------------------------------- the two specs agree */

test("the JSON Schema and this validator name the same buckets and events", () => {
  const schemaBuckets = SCHEMA.properties.bucket.enum.slice().sort();
  assert.deepEqual(schemaBuckets, Object.keys(BUCKETS).sort(),
    "event.schema.json and event.js disagree about the buckets");

  const schemaEvents = SCHEMA.properties.event.enum.slice().sort();
  assert.deepEqual(schemaEvents, Object.values(BUCKETS).flat().sort(),
    "event.schema.json and event.js disagree about the events");
});

test("every fixture only uses fields the schema declares", () => {
  const allowed = new Set(Object.keys(SCHEMA.properties));
  for (const f of files) {
    const doc = JSON.parse(fs.readFileSync(path.join(FIXTURES, f), "utf8"));
    const extra = Object.keys(doc).filter((k) => !allowed.has(k));
    assert.deepEqual(extra, [], f + " carries fields the schema does not declare: " + extra.join(", "));
  }
});
