/* =====================================================================
 * rig.test.js  -  the operator's screen, tested headlessly
 *
 * The engine has its own tests and the sheet has its own transcription.
 * This file tests the screen the operator actually stands at: that it
 * boots from whatever schedule it was handed, that the pedals say the
 * right three things on every screen, that the loop goes round, that a
 * turn boundary never interrupts a take, and that the events it would
 * send a backend name the right person.
 *
 * The rig is the app with no login and no task picker, so almost
 * everything here is really one question asked nine ways: given the
 * payload and the clock, does the rig need to ask the operator anything?
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { mountRig } = require("./test/dom.js");
const RE = require(path.resolve(__dirname, "../../packages/engine/rotation-engine.js"));
const ROSTER = require(path.resolve(__dirname, "../../packages/demo-roster/demo-roster.js"));

/* Runs a test with a freshly mounted rig and always tears it down, so
 * one test's frame loop or fading toast cannot reach the next. */
function withRig(opts, fn) {
  return async () => {
    const rig = await mountRig(opts);
    try { await fn(rig); } finally { rig.stop(); }
  };
}

function payloadFor(rigId, patch) {
  const plan = RE.buildPlan(Object.assign({}, ROSTER.defaults, { date: "2026-08-23" }), ROSTER.groups);
  return Object.assign(RE.rigPayload(plan, rigId), patch || {});
}

/* The demo clock starts at the top of the shift, so the operator on the
 * rig at boot is whoever holds the first turn. */
const FIRST = payloadFor("RIG-03").turns[0];
const SECOND = payloadFor("RIG-03").turns[1];

/* Walk from the checklist to a given screen the way an operator does. */
const toHandover = (rig) => { rig.press(2); rig.frames(1); };
const toRecording = (rig) => { toHandover(rig); rig.press(2); rig.frames(1); };

/* ------------------------------------------------------------ boot */

test("boots, and asks the page for nothing the page does not have",
  withRig({}, async (rig) => {
    rig.frames(2);
    // #v-timer and friends are drawn by the stage, so they are absent on
    // the screens that have no clock - rig.js guards for exactly that.
    const STAGE_DRAWN = ["v-timer", "v-take", "v-rec", "v-eff", "m-eff"];
    const unexplained = rig.missingIds.filter((id) => !STAGE_DRAWN.includes(id));
    assert.deepEqual(unexplained, [],
      "rig.js looked up ids that are not in index.html: " + unexplained.join(", "));
  }));

test("opens on the shift check, with the pushed rig in the rail",
  withRig({}, async (rig) => {
    rig.frames(2);
    assert.equal(rig.screen(), "checklist");
    assert.equal(rig.rail().rig, "RIG-03");
    assert.equal(rig.rail().task, payloadFor("RIG-03").task);
    assert.match(rig.stage(), /Check the rig/);
  }));

test("the rail reads the schedule, not a guess",
  withRig({ search: "?demo" }, async (rig) => {
    rig.frames(2);
    assert.equal(rig.rail().next, FIRST.relievedBy, "next up is the relief the payload names");
    assert.equal(rig.rail().then, FIRST.theyGoTo, "where they go has to travel in the payload");
  }));

test("the frame loop actually runs the clock",
  withRig({ search: "?demo" }, async (rig) => {
    rig.frames(1);
    const first = rig.rail().block;
    rig.frames(20);
    assert.notEqual(rig.rail().block, first, "the block countdown never moved");
  }));

/* ------------------------------------------------------------ the clock
 *
 * A deployed rig ran a 30x demo clock for most of this project's life,
 * because the first prototype ran fast "so a 45-minute stint is
 * watchable" and nothing revisited it. These pin the corrected default:
 * the floor is the default, the demo is the special case.
 */

test("by default the rig boots into the turn that is actually happening",
  withRig({ at: "10:37" }, async (rig) => {
    rig.frames(2);
    /* 10:37 falls inside the third turn. A rig on the wall clock knows
       that; a rig on the shift clock thinks the shift has just begun. */
    const now = payloadFor("RIG-03").turns.find((t) => t.from === "10:30");
    assert.equal(rig.rail().next, now.relievedBy,
      "the rig is not reading the wall clock - it thinks it is 08:00");
    assert.equal(rig.rail().then, now.theyGoTo);
  }));

test("with ?demo the shift clock comes back, for review",
  withRig({ at: "10:37", search: "?demo" }, async (rig) => {
    rig.frames(2);
    assert.equal(rig.rail().next, FIRST.relievedBy,
      "?demo should start the shift from the top, not follow the wall clock");
  }));

test("the demo clock runs faster than the floor clock",
  withRig({ at: "10:37", search: "?demo=60" }, async (rig) => {
    rig.frames(1);
    const before = rig.rail().block;
    rig.frames(30);
    assert.notEqual(rig.rail().block, before, "the demo clock did not move");
  }));

test("a rig says so when it is not running the ordinary thing",
  withRig({}, async (rig) => {
    rig.frames(1);
    // no server, no file - this schedule is one the rig made up
    assert.match(rig.$("rail-mode").textContent, /not pushed/,
      "a rig running an unpushed schedule looked identical to a correct one");
  }));

test("a rig on a pushed schedule and the wall clock says nothing extra",
  withRig({
    fetchImpl: (url) => (url.startsWith("/api/rigs/")
      ? Promise.resolve({ ok: true, json: async () => payloadFor("RIG-03") })
      : Promise.reject(new Error("no file"))),
  }, async (rig) => {
    rig.frames(1);
    assert.equal(rig.$("rail-mode").textContent, "",
      "the mode line should be silent when everything is ordinary");
  }));

test("a demo clock is always declared",
  withRig({ search: "?demo=30" }, async (rig) => {
    rig.frames(1);
    assert.match(rig.$("rail-mode").textContent, /demo clock 30/,
      "an accelerated clock must never be invisible");
  }));

/* ------------------------------------------------- where the schedule comes from */

test("a pushed payload on the page wins - a single-file build has nothing to fetch",
  withRig({ pushed: payloadFor("RIG-03", { task: "Baked in" }) }, async (rig) => {
    rig.frames(1);
    assert.equal(rig.rail().task, "Baked in");
  }));

test("otherwise the server's payload for this rig wins",
  withRig({
    fetchImpl: (url) => (url.startsWith("/api/rigs/")
      ? Promise.resolve({ ok: true, json: async () => payloadFor("RIG-03", { task: "From the server" }) })
      : Promise.reject(new Error("no file"))),
  }, async (rig) => {
    rig.frames(1);
    assert.equal(rig.rail().task, "From the server");
  }));

test("with no server, a co-located schedule.json still works - a plain static deploy",
  withRig({
    fetchImpl: (url) => (url.startsWith("/api/")
      ? Promise.resolve({ ok: false, status: 404 })
      : Promise.resolve({ ok: true, json: async () => payloadFor("RIG-03", { task: "From the file" }) })),
  }, async (rig) => {
    rig.frames(1);
    assert.equal(rig.rail().task, "From the file");
  }));

test("with nothing at all, the rig generates the same schedule the desk would",
  withRig({}, async (rig) => {
    rig.frames(1);
    // no fetch succeeds, so this is the local build - and it must agree
    // with the engine the desk runs. Assert against the turn actually in
    // progress: the first turn's relief happens to share a name with it,
    // so checking that one would pass on either clock and prove nothing.
    const now = payloadFor("RIG-03").turns.find((t) => t.from === "10:30");
    assert.equal(rig.rail().task, payloadFor("RIG-03").task);
    assert.equal(rig.rail().next, now.relievedBy);
    assert.equal(rig.rail().then, now.theyGoTo);
  }));

/* ----------------------------------------------------------- the loop
 *
 * Tests that jump the clock with H, or that expect the shift to start
 * from the top, mount with `?demo`. That is not a workaround: you
 * cannot fast-forward a wall clock, and time travel is a review
 * affordance. A rig on the floor has neither.
 */

test("the loop goes round: check, handover, record, review, reset",
  withRig({}, async (rig) => {
    rig.frames(1);
    assert.equal(rig.screen(), "checklist");
    rig.press(2); rig.frames(1);
    assert.equal(rig.screen(), "handover");
    rig.press(2); rig.frames(1);
    assert.equal(rig.screen(), "recording");
    rig.press(3); rig.frames(1);
    assert.equal(rig.screen(), "review");
    rig.press(2); rig.frames(1);
    assert.equal(rig.screen(), "resetting");
    rig.press(2); rig.frames(1);
    assert.equal(rig.screen(), "recording", "the next episode starts from the reset screen");
  }));

test("the middle pedal is inert while recording, so muscle memory cannot end a good take",
  withRig({}, async (rig) => {
    toRecording(rig);
    assert.deepEqual(rig.pedals(), ["Discard", "—", "Save"]);
    rig.press(2); rig.frames(1);
    assert.equal(rig.screen(), "recording", "the middle pedal ended a take");
  }));

test("a saved take is scored and counted; a discarded one is neither",
  withRig({}, async (rig) => {
    toRecording(rig);
    rig.frames(4);
    rig.press(3); rig.frames(1);          // save -> review
    rig.press(3); rig.frames(1);          // exemplary
    assert.equal(rig.logOf("episode_saved").length, 1);
    assert.match(rig.logOf("episode_saved")[0], /scored 5\/5/);

    rig.press(2); rig.frames(1);          // next episode
    rig.press(1); rig.frames(1);          // discard
    assert.equal(rig.logOf("episode_discarded").length, 1);
    assert.equal(rig.screen(), "resetting");
    assert.equal(rig.logOf("episode_saved").length, 1, "a discard must not save anything");
  }));

/* --------------------------------------------------------- the issue tree */

test("right is always 'other', three levels down, and never runs out of pedals",
  withRig({}, async (rig) => {
    toHandover(rig);
    rig.press(3); rig.frames(1);
    assert.deepEqual(rig.pedals(), ["Gripper broken", "Camera mount", "Other"]);
    rig.press(3); rig.frames(1);
    assert.deepEqual(rig.pedals(), ["Software", "Robot", "Other hardware"]);
    rig.press(3); rig.frames(1);
    assert.deepEqual(rig.pedals(), ["Gello", "Cable", "Other"]);
    rig.press(3); rig.frames(1);
    assert.equal(rig.screen(), "rig_down", "the deepest 'other' still has to land somewhere");
    assert.match(rig.stage(), /manager/i, "the unknown leaf is the one that needs a manager");
  }));

test("an issue found at handover is charged to the operator before, not the one arriving",
  withRig({}, async (rig) => {
    toHandover(rig);
    rig.press(3); rig.frames(1);
    rig.press(1); rig.frames(1);          // gripper broken -> rig down
    assert.equal(rig.screen(), "rig_down");
    assert.match(rig.logOf("rig_down")[0], /previous operator/);
  }));

test("an issue found mid-stint is charged to the rig, not to anyone",
  withRig({}, async (rig) => {
    toRecording(rig);
    rig.press(3); rig.frames(1);          // save
    rig.press(2); rig.frames(1);          // score -> resetting
    rig.press(3); rig.frames(1);          // hardware issue, from resetting
    rig.press(1); rig.frames(1);
    assert.equal(rig.screen(), "rig_down");
    assert.doesNotMatch(rig.logOf("rig_down")[0], /previous operator/);
    assert.match(rig.stage(), /charged to the rig/i);
  }));

test("rig down stops the operator's clock and Problem solved starts it again",
  withRig({}, async (rig) => {
    toHandover(rig);
    rig.press(3); rig.frames(1);
    rig.press(1); rig.frames(4);
    rig.press(2); rig.frames(1);          // problem solved
    assert.equal(rig.screen(), "resetting");
    assert.equal(rig.logOf("rig_up").length, 1);
  }));

/* ------------------------------------------------------------- faults */

test("a fault at the shift check is fixed and not charged",
  withRig({}, async (rig) => {
    rig.frames(1);
    rig.press(1); rig.frames(1);
    assert.equal(rig.screen(), "fault_class");
    rig.press(2); rig.frames(4);          // camera
    assert.equal(rig.screen(), "fault_fixing");
    rig.press(2); rig.frames(1);          // fixed
    assert.equal(rig.screen(), "checklist", "a fixed fault returns to the check");
    assert.match(rig.logOf("fault_closed")[0], /not charged/);
  }));

test("holding the right pedal withdraws the report and charges the time back",
  withRig({}, async (rig) => {
    rig.frames(1);
    rig.press(1); rig.frames(1);
    rig.press(1); rig.frames(2);          // gripper -> fixing
    assert.equal(rig.screen(), "fault_fixing");
    rig.press(3);                          // press and hold
    rig.frames(1, 400);
    assert.equal(rig.screen(), "fault_fixing", "a short press must not cancel");
    rig.frames(2, 400);                    // past the 900ms hold
    assert.equal(rig.screen(), "checklist");
    assert.match(rig.logOf("fault_cancelled")[0], /charged to operator/);
  }));

/* -------------------------------------------------------- the handover */

test("a turn boundary never interrupts a take",
  withRig({ search: "?demo" }, async (rig) => {
    toRecording(rig);
    rig.demoKey("h");                      // jump to the boundary
    rig.frames(3);
    assert.equal(rig.screen(), "recording", "the handover cut into an episode");
    assert.equal(rig.rail().block, "Handover due");
    rig.press(3); rig.frames(1);           // save
    assert.equal(rig.screen(), "review", "the take still has to be scored");
    rig.press(2); rig.frames(1);
    assert.equal(rig.screen(), "handover", "and only then does the rig change hands");
  }));

test("nothing tears the stage down mid-take, however long the take runs",
  withRig({ search: "?demo" }, async (rig) => {
    toRecording(rig);
    const rebuilds = rig.countRebuilds("stage");
    const before = rebuilds();

    /* Long enough to cross the 15-minute stub turn and the 45-minute
       turn after it - an operator who keeps recording through two
       handovers they have already deferred. */
    rig.frames(9000);

    assert.equal(rig.screen(), "recording", "the take was interrupted");
    assert.equal(rig.rail().block, "Handover due");
    assert.equal(rebuilds() - before, 1,
      "the stage was rebuilt " + (rebuilds() - before) + " times during one take; " +
      "only the first - flipping the rail to 'Handover due' - changes what it says, " +
      "and every rebuild takes the camera panes with it");
  }));

test("the stint is credited to the operator who worked it, not the one arriving",
  withRig({ search: "?demo" }, async (rig) => {
    toRecording(rig);
    rig.frames(4);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);           // one episode banked
    rig.demoKey("h");
    rig.frames(3);
    const stint = rig.logOf("stint_ended");
    assert.equal(stint.length, 1);
    assert.match(stint[0], new RegExp(FIRST.operator.name),
      "the stint that just ended belongs to " + FIRST.operator.name);
    assert.doesNotMatch(stint[0], new RegExp(SECOND.operator.name),
      "it must not be credited to the operator taking over");
  }));

test("the handover screen names the operator taking the rig, every time",
  withRig({ search: "?demo" }, async (rig) => {
    toHandover(rig);
    assert.match(rig.stage(), new RegExp(FIRST.operator.name));
    rig.demoKey("h");
    rig.frames(3);
    assert.equal(rig.screen(), "handover");
    assert.match(rig.stage(), new RegExp(SECOND.operator.name),
      "the rig changed hands but the screen kept the old name up");
    assert.equal(rig.rail().next, SECOND.relievedBy);
  }));

/* ------------------------------------------------------- every screen */

const SCREENS = ["standby", "checklist", "fault-class", "fault-fixing", "handover",
  "recording", "review", "resetting", "issue-menu", "rig-down"];

/* Standby is excluded on purpose: it is the one screen that *should* be
   left the moment the schedule says somebody is due, which is true at the
   10:37 these mount at. Its own reachability is asserted below, in the
   dead hours where it belongs. */
for (const id of SCREENS.filter((s) => s !== "standby")) {
  test("#" + id + " is reachable, and stays put once it is there",
    withRig({}, async (rig) => {
      rig.frames(1);
      rig.hashTo(id);
      const landed = rig.screen();
      rig.frames(3);
      assert.equal(landed, id.replace(/-/g, "_"), "#" + id + " did not open " + id);
      assert.equal(rig.screen(), landed, "#" + id + " drifted off the screen it opened");
      assert.notEqual(rig.rail().block, "Handover due",
        "#" + id + " arrived mid-handover, which never happened");
      // Opening a screen is not an event. A rig that logs a stint ending
      // because someone deep-linked into it would post that to a backend.
      assert.deepEqual(rig.logOf("stint_ended"), [],
        "#" + id + " ended a stint just by being opened");
    }));
}

test("a hash the app does not have starts the shift instead of a dead screen",
  withRig({ hash: "does-not-exist" }, async (rig) => {
    rig.frames(3);
    assert.equal(rig.screen(), "checklist");
    assert.deepEqual(rig.pedals(), ["Problem", "All good", "—"],
      "an unknown hash left the operator with nothing to press");
  }));

/* ------------------------------------------------------- the boot race */

test("the frame loop survives a payload that arrives after the first frame",
  withRig({
    settle: false,
    /* what a real rig does: fetch over the network, which the first
       frame routinely beats */
    fetchImpl: () => new Promise((_, reject) => {
      let n = 0;
      const step = () => (++n >= 10 ? reject(new Error("no server")) : setImmediate(step));
      setImmediate(step);
    }),
  }, async (rig) => {
    assert.equal(rig.frames(3), 3, "the loop stopped rescheduling before the payload landed");
    assert.deepEqual(rig.errors, [], "a frame threw while the payload was still in the air");
    await rig.settle();
    assert.equal(rig.frames(3), 3, "the loop died and the rig is frozen");
    assert.equal(rig.screen(), "checklist");
    assert.equal(rig.rail().rig, "RIG-03");
  }));

/* ------------------------------------------------------- the demo drawer */

test("the drawer offers every screen and every rig on the floor",
  withRig({}, async (rig) => {
    rig.frames(1);
    assert.deepEqual(
      rig.$("screens").querySelectorAll("button").map((b) => b.dataset.screen),
      SCREENS);
    assert.deepEqual(
      rig.$("rigs").querySelectorAll("button").map((b) => b.dataset.rig),
      ROSTER.allRigs);
  }));

test("switching rig reads the schedule as that rig, not as this one",
  withRig({ search: "?demo" }, async (rig) => {
    rig.frames(1);
    await global.switchRig("RIG-07");
    await rig.settle();
    rig.frames(1);
    assert.equal(rig.rail().rig, "RIG-07");
    assert.equal(rig.rail().task, payloadFor("RIG-07").task);
    assert.equal(rig.rail().next, payloadFor("RIG-07").turns[0].relievedBy);
  }));

test("restarting the shift clears the log and returns to the check",
  withRig({}, async (rig) => {
    toRecording(rig);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);
    assert.ok(rig.log().length > 1);
    rig.demoKey("r");
    rig.frames(1);
    assert.equal(rig.screen(), "checklist");
    assert.equal(rig.log().length, 1, "a restart starts a fresh shift");
  }));

/* ------------------------------------------------------------- standby
 *
 * A rig that boots when nothing is scheduled used to show a start-of-shift
 * checklist counting down beside a rail reading "End of shift" - two
 * contradictory claims on one screen. Making the clock honest exposed it;
 * Standby is the screen that was missing.
 */

/* 02:33 - after the Morning shift's schedule was pushed, long before it
   runs. The exact situation in the screenshot that prompted this. */
const DEAD_HOURS = { at: "02:33" };

test("with nothing scheduled, the rig says so instead of contradicting itself",
  withRig(DEAD_HOURS, async (rig) => {
    rig.frames(2);
    assert.equal(rig.screen(), "standby");
    assert.match(rig.stage(), /Morning · 08:00/, "it should name the shift it is waiting for");
    assert.match(rig.stage(), new RegExp(FIRST.operator.name), "and who is due first");
    assert.doesNotMatch(rig.stage(), /Check the rig/,
      "a start-of-shift checklist has no business running at 02:33");
  }));

test("standby clears the rail instead of leaving it contradicting the screen",
  withRig(DEAD_HOURS, async (rig) => {
    rig.frames(2);
    const rail = rig.rail();
    assert.equal(rail.block, "—", "there is no block running");
    assert.equal(rail.then, "—", "nobody is going anywhere");
    assert.equal(rail.next, FIRST.operator.name, "but who is on first is worth saying");
    assert.equal(rail.due, false);
  }));

test("standby still gives the operator something to press",
  withRig(DEAD_HOURS, async (rig) => {
    rig.frames(1);
    assert.deepEqual(rig.pedals(), ["—", "Check the rig", "—"]);
  }));

test("the rig can be checked before the shift, and returns to standby",
  withRig(DEAD_HOURS, async (rig) => {
    rig.frames(1);
    rig.press(2); rig.frames(1);
    assert.equal(rig.screen(), "checklist", "middle pedal starts the check early");
    rig.press(2); rig.frames(1);
    assert.equal(rig.screen(), "standby", "and it comes back - nobody is due yet");
    assert.match(rig.stage(), /all four passed/);
    assert.deepEqual(rig.pedals(), ["—", "Check again", "—"]);
    assert.equal(rig.logOf("shift_check").length, 2, "the early check is a real event");
  }));

test("inside the shift the rig still opens on the check, not standby",
  withRig({ at: "10:37" }, async (rig) => {
    rig.frames(2);
    assert.equal(rig.screen(), "checklist", "somebody is due at this rig right now");
  }));

test("#standby is reachable in the dead hours, and holds",
  withRig(DEAD_HOURS, async (rig) => {
    rig.frames(1);
    rig.hashTo("standby");
    rig.frames(3);
    assert.equal(rig.screen(), "standby");
  }));

test("standby does not survive the shift starting",
  withRig({ search: "?demo" }, async (rig) => {
    rig.frames(1);
    rig.hashTo("standby");
    rig.frames(3);
    assert.notEqual(rig.screen(), "standby",
      "somebody is due at this rig - standing on standby would hide that");
  }));
