/* =====================================================================
 * fuzz.test.js  -  drive the rig badly and check nothing impossible
 *                  reaches the ledger
 *
 * Every other test here asks whether a path I thought of behaves. This
 * one asks the opposite question: over thousands of pedal presses in
 * orders nobody designed, does the rig ever file something the backend
 * would refuse, or something that is well formed and untrue?
 *
 * That second kind is what this is really for. A malformed envelope is
 * caught at the door by the schema and the rig sets the batch aside. An
 * envelope that validates but says an operator recorded more seconds
 * than they were assigned, or that two takes share one id, goes
 * straight into the training data and nothing downstream can tell.
 *
 * Seeded, so a failure is reproducible and this is a regression test
 * rather than a flake.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { mountRig } = require("./test/dom.js");
const { validate } = require(path.resolve(__dirname, "../../packages/schema/event.js"));

/* mulberry32. Small, fast, and the same sequence every run. */
function rng(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

async function hammer(seed, presses) {
  const rig = await mountRig({ search: "?demo=30" });
  global.fetch = async () => { throw new Error("no server"); };
  const rand = rng(seed);
  try {
    for (let i = 0; i < presses; i++) {
      const pedal = 1 + Math.floor(rand() * 3);
      rig.press(pedal);
      rig.frames(1 + Math.floor(rand() * 4), 200 + Math.floor(rand() * 900));
    }
    return { events: rig.events(), errors: rig.errors.slice(), screen: rig.screen() };
  } finally {
    rig.stop();
  }
}

const SEEDS = [1, 7, 42, 1337, 90210];

test("nothing the rig files is ever refused by its own schema", async () => {
  for (const seed of SEEDS) {
    const { events } = await hammer(seed, 400);
    assert.ok(events.length > 0, "seed " + seed + " produced no events at all");
    for (const e of events) {
      const v = validate(e);
      assert.ok(v.ok, "seed " + seed + ", " + e.event + ": " + (v.errors || []).join("; "));
    }
  }
});

test("the rig never throws, whatever order the pedals are pressed", async () => {
  for (const seed of SEEDS) {
    const { errors } = await hammer(seed, 400);
    assert.deepEqual(errors, [], "seed " + seed + " threw: " + errors.map(String).join(" | "));
  }
});

test("the sequence is strictly increasing and starts at zero", async () => {
  for (const seed of SEEDS) {
    const { events } = await hammer(seed, 300);
    assert.equal(events[0].seq, 0, "seed " + seed);
    for (let i = 1; i < events.length; i++) {
      assert.equal(events[i].seq, events[i - 1].seq + 1,
        "seed " + seed + ": seq jumped at " + i);
    }
  }
});

test("no two events share an id, and no two takes share an episode id", async () => {
  for (const seed of SEEDS) {
    const { events } = await hammer(seed, 400);
    const ids = events.map((e) => e.eventId);
    assert.equal(new Set(ids).size, ids.length, "seed " + seed + ": duplicate eventId");

    const takes = events.filter((e) => e.bucket === "episodes").map((e) => e.data.episodeId);
    assert.equal(new Set(takes).size, takes.length,
      "seed " + seed + ": two takes share an episodeId, so their video collides");
  }
});

test("every take is attributed, and can be joined to its video", async () => {
  for (const seed of SEEDS) {
    const { events } = await hammer(seed, 400);
    for (const e of events.filter((x) => x.bucket === "episodes")) {
      assert.ok(e.data.episodeId, "seed " + seed + ": a take with no id");
      assert.ok(e.operatorId, "seed " + seed + ": an unattributed take reached the ledger");
      assert.ok(e.turnFrom, "seed " + seed + ": a take with no turn");
      assert.ok(e.data.durationSecs >= 0, "seed " + seed + ": negative duration");
    }
  }
});

test("a stint never claims more work than the time it was given", async () => {
  /* Efficiency is recordedSecs over assigned minus fault minus down. If
     recorded exceeds chargeable, the ratio is above 1 and the operator
     recorded more than they were there for, which cannot happen. */
  for (const seed of SEEDS) {
    const { events } = await hammer(seed, 500);
    for (const e of events.filter((x) => x.event === "stint_ended")) {
      const d = e.data;
      for (const k of ["episodes", "recordedSecs", "assignedSecs", "faultSecs", "downSecs"]) {
        assert.ok(d[k] >= 0, "seed " + seed + ": negative " + k + " (" + d[k] + ")");
      }
      assert.ok(d.recordedSecs <= d.assignedSecs,
        "seed " + seed + ": recorded " + d.recordedSecs + "s in an assigned " +
        d.assignedSecs + "s stint");
      assert.ok(d.faultSecs + d.downSecs <= d.assignedSecs,
        "seed " + seed + ": fault and downtime exceed the stint itself");
    }
  }
});

test("every event names the shift it happened in", async () => {
  for (const seed of SEEDS) {
    const { events } = await hammer(seed, 300);
    for (const e of events) {
      assert.ok(e.shiftDate, "seed " + seed + ": " + e.event + " has no shift date");
      assert.ok(e.shiftLabel, "seed " + seed + ": " + e.event + " has no shift label");
      assert.ok(e.rigId, "seed " + seed + ": " + e.event + " has no rig");
      assert.ok(!Number.isNaN(Date.parse(e.at)), "seed " + seed + ": unparseable timestamp");
    }
  }
});

test("a fault always opens before it closes", async () => {
  for (const seed of SEEDS) {
    const { events } = await hammer(seed, 500);
    let open = 0;
    for (const e of events) {
      if (e.event === "fault_opened") open++;
      if (e.event === "fault_closed" || e.event === "fault_cancelled") {
        open--;
        assert.ok(open >= 0, "seed " + seed + ": a fault was closed that was never opened");
      }
    }
  }
});

test("the rig never comes up without having gone down", async () => {
  /* Asserted one way only, deliberately.
   *
   * The reverse - two rig_down events with no rig_up between them - does
   * happen, and the backend tolerates it on purpose: "a rig cannot be
   * doubly down, pressing through the issue tree twice is one outage".
   * So this asserts the invariant the system actually contracts rather
   * than the tidier one I assumed, which would have been a test failing
   * on correct behaviour.
   *
   * A rig_up with no outage open is different. That would close a
   * downtime row that does not exist, or none at all, and the duration
   * on the board would be invented. */
  for (const seed of SEEDS) {
    const { events } = await hammer(seed, 500);
    let down = false;
    for (const e of events.filter((x) => x.bucket === "rig_downtime_events")) {
      if (e.event === "rig_down") down = true;
      if (e.event === "rig_up") {
        assert.equal(down, true,
          "seed " + seed + ": the rig came up without having gone down");
        down = false;
      }
    }
  }
});

test("the rig always ends somewhere it can be driven from", async () => {
  /* Whatever sequence it has been through, an operator must arrive to a
     screen with at least one pedal that does something. A dead end on a
     floor is a rig nobody can start. */
  const drivable = new Set(["standby", "checklist", "handover", "recording",
    "review", "resetting", "issue_menu", "rig_down", "fault_class",
    "fault_fixing", "session_ended"]);
  for (const seed of SEEDS) {
    const { screen } = await hammer(seed, 400);
    assert.ok(drivable.has(screen), "seed " + seed + " ended on '" + screen + "'");
  }
});
