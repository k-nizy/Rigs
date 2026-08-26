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


/* ================================================================= zone

   A second clock question, and the one that decides whose name is on the
   work: not how much time has passed, but what time it is where the rigs
   actually are.

   Every "HH:MM" in a payload is wall-clock time on the floor, and the
   desk writes the zone in beside them. The backend was caught reading
   those against its own clock and was fixed. The rig was doing exactly
   the same thing and nobody noticed, because a rig normally stands on the
   floor it serves and the two clocks agree.

   They stop agreeing when a rig is imaged in the wrong zone - which is a
   provisioning slip, not an exotic scenario. What it gets wrong is which
   operator it believes is sitting at it. */

const RE_ENG = require("../../packages/engine/rotation-engine.js");
const ROSTER_ENG = require("../../packages/demo-roster/demo-roster.js");

function morningFor(tz) {
  const plan = RE_ENG.buildPlan(
    Object.assign({}, ROSTER_ENG.defaults, { date: "2026-08-23", shift: "morning" }),
    ROSTER_ENG.groups);
  const p = RE_ENG.rigPayload(plan, "RIG-03");
  p.shift.tz = tz;
  return p;
}

/* The harness clock stands at 10:37 local on 2026-08-23. On a floor one
   hour further east it is 11:37, which is a different turn with a
   different operator - so the two readings cannot be mistaken for each
   other. The instant is written out here because the harness only fakes
   the clock while a rig is mounted, and these expectations are worked out
   before one is. */
const HARNESS_NOW = new Date(2026, 7, 23, 10, 37).getTime();
const HERE  = "Etc/GMT-2";    // UTC+2, the same as the test machine
const EAST  = "Etc/GMT-3";    // UTC+3, one hour ahead

test("the rig reads the turn from the floor's clock, not its own", async () => {
  const here = morningFor(HERE);
  const east = morningFor(EAST);

  const atHere = RE_ENG.whoIsOn(here, RE_ENG.minutesOnFloor(here, HARNESS_NOW));
  const atEast = RE_ENG.whoIsOn(east, RE_ENG.minutesOnFloor(east, HARNESS_NOW));
  assert.notEqual(atHere.turn.from, atEast.turn.from,
    "sanity: the two floors must be in different turns or this proves nothing");

  const rig = await mountRig({ search: "", pushed: east });
  try {
    const filed = rig.events().filter(e => e.turnFrom);
    assert.ok(filed.length, "the rig filed nothing to check");
    assert.equal(filed[0].turnFrom, atEast.turn.from,
      "the rig filed against turn " + filed[0].turnFrom + ", which is what its OWN "
      + "clock says. The floor is in " + atEast.turn.from + " - a different operator.");
  } finally { rig.stop(); }
});

test("a rig standing on the floor it serves is unaffected", async () => {
  /* The normal case, and the reason this went unnoticed. */
  const here = morningFor(HERE);
  const on = RE_ENG.whoIsOn(here, RE_ENG.minutesOnFloor(here, HARNESS_NOW));

  const rig = await mountRig({ search: "", pushed: here });
  try {
    const filed = rig.events().filter(e => e.turnFrom);
    assert.equal(filed[0].turnFrom, on.turn.from);
  } finally { rig.stop(); }
});

test("a payload with no zone still works, the old way", async () => {
  /* Pushes made before the zone travelled, and the local demo. */
  const p = morningFor(HERE);
  delete p.shift.tz;
  const rig = await mountRig({ search: "", pushed: p });
  try {
    assert.notEqual(rig.screen(), "standby", "a zoneless payload stopped the rig");
    assert.deepEqual(rig.errors, []);
  } finally { rig.stop(); }
});
