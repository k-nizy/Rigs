/* =====================================================================
 * clock.test.js  -  the rig's clock has to agree with the wall
 *
 * Every number this system reports about a person's day is measured by
 * one clock inside the rig, so if that clock is slow, every one of them
 * is wrong together and nothing downstream can tell.
 *
 * The bug these were written for: elapsed time was accumulated one
 * animation frame at a time, and each frame was allowed to contribute at
 * most a quarter of a second - at any speed, including 1x. So any stall
 * longer than 250ms was silently dropped and never recovered. A take
 * held for three seconds was filed as ZERO seconds long. It was found by
 * recording a take in a browser tab and reading the row back out of
 * Postgres, not by any test.
 *
 * Why the suite missed it, which is the more useful lesson: the tests
 * drive the clock themselves, and they all drove it in small steps well
 * under the cap, where the cap is invisible. The fuzz test did produce
 * gaps big enough to trip it - it just never asserted anything the loss
 * would break, because it checks ratios and invariants, and a clock that
 * is uniformly slow keeps every ratio intact.
 *
 * So this file asserts the one thing none of them did: that the seconds
 * the rig reports are the seconds that actually passed.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");

const { mountRig } = require("./test/dom.js");

/* `?demo=1` is the deployed configuration's clock - one second of real
   time is one second of rig time - with the test harness still in charge
   of when frames happen. A real rig runs at 1x too. */
const REAL_TIME = { search: "?demo=1" };

function withRig(opts, fn) {
  return async () => {
    const rig = await mountRig(opts);
    try { await fn(rig); } finally { rig.stop(); }
  };
}

const toRecording = (rig) => {
  rig.press(2); rig.frames(1);        // checklist passed
  rig.press(2); rig.frames(2);        // start
};

const fileIt = (rig) => {
  rig.press(3); rig.frames(1);        // save
  rig.press(2); rig.frames(1);        // score
};

// ------------------------------------------------------------- the bug

test("a take is measured in the seconds that actually passed",
  withRig(REAL_TIME, async (rig) => {
    toRecording(rig);
    assert.equal(rig.screen(), "recording", "never got into a take");

    rig.frames(1, 5000);              // the browser stalls for five seconds
    fileIt(rig);

    const e = rig.eventsOf("episode_saved")[0];
    assert.ok(e, "no take was filed");
    assert.ok(e.data.durationSecs >= 5,
      "a take that ran five seconds was filed as " + e.data.durationSecs +
      "s, so the clock swallowed the stall");
  }));

test("many small stalls lose nothing between them",
  withRig(REAL_TIME, async (rig) => {
    /* The realistic shape on a floor. A rig is not backgrounded, it just
       drops frames while RODA-RS is busy on the same GPU - a third of a
       second here, half a second there, all day. */
    toRecording(rig);
    for (let i = 0; i < 10; i++) rig.frames(1, 400);   // 4s in 400ms steps
    fileIt(rig);

    const e = rig.eventsOf("episode_saved")[0];
    assert.ok(e.data.durationSecs >= 4,
      "ten 400ms frames should be four seconds, got " + e.data.durationSecs + "s");
  }));

test("a stint is credited the time it was actually given",
  withRig(REAL_TIME, async (rig) => {
    /* assignedSecs is the denominator of efficiency. If it is short, an
       operator's day looks shorter than it was. */
    toRecording(rig);
    rig.frames(1, 3000);
    fileIt(rig);
    rig.frames(1, 3000);

    const stints = rig.eventsOf("stint_ended");
    if (!stints.length) return;                        // no rotation yet, fine
    const d = stints[0].data;
    assert.ok(d.assignedSecs >= 6,
      "the stint spanned at least six seconds of stalls but was credited " +
      d.assignedSecs + "s");
  }));

// ------------------------------------------- the invariants still hold

test("recorded time never exceeds the time assigned",
  withRig(REAL_TIME, async (rig) => {
    /* Uncapping the clock must not let recorded time run past the stint
       that contains it - that would put efficiency above 1. */
    toRecording(rig);
    rig.frames(1, 8000);
    fileIt(rig);
    rig.frames(3, 1000);

    for (const e of rig.eventsOf("stint_ended")) {
      assert.ok(e.data.recordedSecs <= e.data.assignedSecs,
        "recorded " + e.data.recordedSecs + "s inside an assigned " +
        e.data.assignedSecs + "s stint");
      assert.ok(e.data.faultSecs + e.data.downSecs <= e.data.assignedSecs,
        "fault and downtime together exceed the stint");
    }
  }));

test("no duration is ever negative, whatever the frame gap",
  withRig(REAL_TIME, async (rig) => {
    toRecording(rig);
    rig.frames(1, 60000);             // a full minute between two frames
    fileIt(rig);

    for (const e of rig.events()) {
      const d = e.data || {};
      for (const k of ["durationSecs", "recordedSecs", "assignedSecs", "faultSecs", "downSecs"]) {
        if (typeof d[k] === "number") {
          assert.ok(d[k] >= 0, e.event + "." + k + " is " + d[k]);
          assert.ok(Number.isFinite(d[k]), e.event + "." + k + " is not finite");
        }
      }
    }
    assert.deepEqual(rig.errors, []);
  }));

// --------------------------------------- the demo keeps its safety rail

test("the sped-up demo still refuses to leap on one frame",
  withRig({ search: "?demo=30" }, async (rig) => {
    /* The cap exists for a reason and the reason is still true: at 30x a
       backgrounded tab would come back and jump the shift half an hour on
       a single frame. Uncapping 1x must not uncap the demo. */
    toRecording(rig);
    rig.frames(1, 10000);             // ten real seconds at 30x = five minutes
    fileIt(rig);

    const e = rig.eventsOf("episode_saved")[0];
    assert.ok(e, "no take was filed");
    assert.ok(e.data.durationSecs < 60,
      "one frame advanced the demo by " + e.data.durationSecs +
      "s, so the simulation can leap");
  }));
