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

const SCREENS = ["no-identity", "standby", "checklist", "fault-class", "fault-fixing",
  "handover", "recording", "review", "resetting", "issue-menu", "rig-down"];

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

test("the rail stays cleared through an early check",
  withRig(DEAD_HOURS, async (rig) => {
    rig.frames(1);
    rig.press(2); rig.frames(2);            // start the check early
    assert.equal(rig.screen(), "checklist");
    assert.equal(rig.rail().block, "—",
      "the rail snapped back to the live values the moment the pedal was pressed");
    assert.equal(rig.rail().then, "—");
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

/* A stylesheet guard, like the desk's. The camera panes hold
   aspect-ratio 16/10; letting the flex column shrink their grid box
   below that left them overflowing it and painting over the "Episode"
   label beneath. The stub has no layout engine and cannot see this -
   only a browser could - so this asserts the rule that fixes it. */
test("the camera grid cannot be shrunk below the panes it holds",
  async () => {
    const css = require("node:fs").readFileSync(
      require("node:path").join(__dirname, "assets/rig.css"), "utf8");
    const rule = /\.cams\s*\{[^}]*\}/.exec(css);
    assert.ok(rule, ".cams rule missing");
    assert.match(rule[0], /flex:\s*0\s+0\s+auto/,
      "a shrinkable .cams lets the panes overflow onto the metrics row");
  });

/* ================================================================ events
 *
 * What the rig sends back, as opposed to the sentence it shows the
 * operator. The drawer log is for the person standing at the rig and is
 * unchanged; these assert the parallel machine-readable half against
 * packages/schema, which is the same validator the ingest service will
 * generate its models from.
 */

const EventSchema = require(path.resolve(__dirname, "../../packages/schema/event.js"));

const invalid = (evs) => evs
  .map((e) => ({ e, r: EventSchema.validate(e) }))
  .filter((x) => !x.r.ok)
  .map((x) => x.e.event + ": " + x.r.errors.join("; "));

test("every event the rig emits validates against the shared contract",
  withRig({ search: "?demo" }, async (rig) => {
    // walk the whole loop, so this covers most of the buckets at once
    rig.frames(1);
    rig.press(2); rig.frames(1);            // check passed -> handover
    rig.press(2); rig.frames(2);            // start recording
    rig.press(3); rig.frames(1);            // save
    rig.press(2); rig.frames(1);            // score 4 -> resetting
    rig.press(2); rig.frames(1);            // next episode
    rig.press(1); rig.frames(1);            // discard
    rig.press(3); rig.frames(1);            // hardware issue
    rig.press(1); rig.frames(2);            // gripper broken -> rig down
    rig.press(2); rig.frames(1);            // problem solved

    const evs = rig.events();
    assert.ok(evs.length >= 6, "expected a spread of events, got " + evs.length);
    assert.deepEqual(invalid(evs), [], "the rig emitted events the backend would reject");
  }));

test("stint_ended carries the four seconds columns, not a percentage",
  withRig({ search: "?demo" }, async (rig) => {
    toRecording(rig);
    rig.frames(4);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);
    rig.demoKey("h");
    rig.frames(3);

    const stint = rig.eventsOf("stint_ended");
    assert.equal(stint.length, 1);
    const d = stint[0].data;
    for (const k of ["episodes", "recordedSecs", "assignedSecs", "faultSecs", "downSecs"]) {
      assert.equal(typeof d[k], "number", k + " must be a number, not a formatted string");
      assert.ok(d[k] >= 0, k + " must not be negative");
    }
    assert.equal(d.efficiency, undefined,
      "a stored percentage cannot be corrected without re-running the floor");
    assert.deepEqual(invalid(stint), []);
  }));

test("an episode keeps one id from the pedal press that started it",
  withRig({ search: "?demo" }, async (rig) => {
    toRecording(rig);
    rig.frames(2);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);            // scored -> saved

    const saved = rig.eventsOf("episode_saved");
    assert.equal(saved.length, 1);
    assert.match(saved[0].data.episodeId, /^[0-9a-f-]{36}$/i,
      "the id names the video directory too, so it has to be real");
    assert.equal(typeof saved[0].data.durationSecs, "number");
    assert.ok([3, 4, 5].includes(saved[0].data.score));
  }));

test("every envelope carries the schedule it happened under",
  withRig({ search: "?demo" }, async (rig) => {
    rig.frames(1);
    rig.press(2); rig.frames(1);
    const e = rig.events()[0];
    const p = payloadFor("RIG-03");
    assert.equal(e.rigId, "RIG-03");
    assert.equal(e.shiftDate, p.shift.date, "shiftDate comes from the payload, not the rig's own clock");
    assert.equal(e.shiftLabel, p.shift.label);
    assert.equal(e.operatorId, FIRST.operator.id, "the rig never asks who anyone is - the payload says");
    assert.equal(e.operatorName, FIRST.operator.name,
      "the id is a seat; the name is the person who sat in it that day");
    assert.equal(e.turnFrom, FIRST.from);
  }));

test("ids are unique and seq is monotonic, so a batch can be resent blindly",
  withRig({ search: "?demo" }, async (rig) => {
    rig.frames(1);
    rig.press(2); rig.frames(1);
    rig.press(2); rig.frames(1);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    const evs = rig.events();
    const ids = evs.map((e) => e.eventId);
    assert.equal(new Set(ids).size, ids.length, "two events share an id - ingest would drop one");
    const seqs = evs.map((e) => e.seq);
    assert.deepEqual(seqs, seqs.slice().sort((a, b) => a - b), "seq is the cursor; it has to only go up");
  }));

test("downtime records whether it was charged to the rig or the operator before",
  withRig({ search: "?demo" }, async (rig) => {
    toHandover(rig);
    rig.press(3); rig.frames(1);
    rig.press(1); rig.frames(2);            // found at handover

    const down = rig.eventsOf("rig_down");
    assert.equal(down.length, 1);
    assert.equal(down[0].data.chargedTo, "previous_operator");
    assert.equal(down[0].data.needsManager, false);
    assert.equal(down[0].data.issue, "Gripper broken");
    assert.deepEqual(invalid(down), []);
  }));

test("a rig on standby still reports, with no turn and no operator",
  withRig(DEAD_HOURS, async (rig) => {
    rig.frames(1);
    const e = rig.events()[0];
    assert.equal(e.turnFrom, null, "nothing is scheduled, so there is no turn");
    assert.equal(e.operatorId, null);
    assert.deepEqual(invalid([e]), [], "standby events still have to validate");
  }));

/* ============================================================== uploading
 *
 * The seam between the rig and the backend. The whole design is one idea:
 * the rig keeps events until the server says it has them, and never stops
 * trying. Ingest dedupes on (rigId, eventId), so re-sending is free and
 * asking "did that land?" is unnecessary.
 */

/* A server that accepts everything, and records what it was sent. */
function acceptingServer(state) {
  return async (url, opts) => {
    if (url.includes("/cursor")) {
      return { ok: true, json: async () => ({ rigId: "RIG-03", seq: state.cursor ?? -1 }) };
    }
    if (url.includes("/events")) {
      const body = JSON.parse(opts.body);
      state.received.push(...body.events);
      state.batches = (state.batches || 0) + 1;
      return { ok: true, json: async () => ({ accepted: body.events.length }) };
    }
    return { ok: false, status: 404 };
  };
}

test("filed events reach the server",
  withRig({ search: "?demo" }, async (rig) => {
    const state = { received: [], cursor: -1 };
    global.fetch = acceptingServer(state);

    rig.frames(1);
    rig.press(2); rig.frames(1);
    await rig.upload();

    assert.ok(state.received.length >= 2, "nothing was uploaded");
    assert.equal(rig.outbox().queued, 0, "the outbox should be empty once acknowledged");
    assert.deepEqual(
      state.received.map((e) => e.event).slice(0, 2),
      ["shift_check", "shift_check"]);
  }));

test("events are held, not lost, while the server is unreachable",
  withRig({ search: "?demo" }, async (rig) => {
    global.fetch = async () => { throw new Error("no network"); };

    rig.frames(1);
    rig.press(2); rig.frames(1);
    await rig.upload();

    const box = rig.outbox();
    assert.ok(box.queued >= 2, "a rig with no network must keep its events");
    assert.equal(box.state, "offline");
    assert.ok(box.failures >= 1);
  }));

test("the queue drains when the network comes back",
  withRig({ search: "?demo" }, async (rig) => {
    global.fetch = async () => { throw new Error("no network"); };
    rig.frames(1);
    rig.press(2); rig.frames(1);
    await rig.upload();
    const held = rig.outbox().queued;
    assert.ok(held >= 2);

    const state = { received: [], cursor: -1 };
    global.fetch = acceptingServer(state);
    await rig.upload();

    assert.equal(rig.outbox().queued, 0, "the backlog should clear in one flush");
    assert.equal(state.received.length, held, "every held event should arrive");
  }));

test("a rig carries on from the sequence the server already holds",
  withRig({ search: "?demo", fetchImpl: async (url) => {
    if (url.includes("/cursor")) return { ok: true, json: async () => ({ rigId: "RIG-03", seq: 416 }) };
    if (url.includes("/api/rigs/")) return { ok: false, status: 404 };
    return { ok: false, status: 404 };
  } }, async (rig) => {
    /* A restarted rig counting from zero again would send seq backwards,
       and seq is the cursor the server reads. */
    rig.frames(1);
    const first = rig.events()[0];
    assert.ok(first.seq > 416,
      `seq restarted at ${first.seq}; the server already holds 416`);
  }));

test("a batch the server refuses is set aside, not retried forever",
  withRig({ search: "?demo" }, async (rig) => {
    global.fetch = async (url) => {
      if (url.includes("/cursor")) return { ok: true, json: async () => ({ seq: -1 }) };
      return { ok: false, status: 422 };
    };

    rig.frames(1);
    rig.press(2); rig.frames(1);
    await rig.upload();

    const box = rig.outbox();
    assert.equal(box.queued, 0, "a poison batch must not block the outbox forever");
    assert.ok(box.rejected >= 2, "the events are set aside, not dropped");
    assert.equal(box.state, "rejected");
    assert.ok(rig.log().some((l) => l.includes("refused")),
      "a rig filing events its own schema rejects should say so out loud");
  }));

test("a rig that cannot reach the server says so on the wall",
  withRig({}, async (rig) => {
    global.fetch = async () => { throw new Error("no network"); };
    rig.frames(1);
    await rig.upload();
    assert.match(rig.$("rail-mode").textContent, /queued/,
      "an operator should be able to see that nobody has their events");
  }));

test("uploading does not interrupt the operator",
  withRig({ search: "?demo" }, async (rig) => {
    global.fetch = async () => { throw new Error("no network"); };
    toRecording(rig);
    const before = rig.screen();
    await rig.upload();
    rig.frames(3);
    assert.equal(rig.screen(), before, "a failed upload changed what the operator sees");
    assert.deepEqual(rig.errors, []);
  }));


/* =====================================================================
 * the video path
 *
 * There is no camera behind a browser tab, so these attach a recorder of
 * their own. What is being tested is not the bytes - it is the rule that
 * the rig only lets go of a take after the server has verified what
 * landed, and that everything before that step is a retry.
 * ===================================================================== */

const TAKE = new Blob([new Uint8Array(2048).fill(7)]);

/* A server that answers the three steps, and counts what it was asked. */
function fakeStore(opts) {
  const o = opts || {};
  const seen = { presign: 0, put: 0, complete: 0, bytes: 0 };
  global.fetch = async (url, init) => {
    const u = String(url);
    if (u.includes("video:presign")) {
      seen.presign += 1;
      if (o.notProjected) return { ok: false, status: 404, json: async () => ({}) };
      const camera = JSON.parse(init.body).camera;
      return { ok: true, status: 200, json: async () => ({
        key: "RIG-03/ep/" + camera + ".mp4",
        url: "/api/storage/RIG-03/ep/" + camera + ".mp4",
        method: "PUT",
      }) };
    }
    if (u.includes("/api/storage/")) {
      seen.put += 1;
      seen.bytes = init.body.byteLength;
      if (o.putFails) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => ({ bytes: seen.bytes }) };
    }
    if (u.includes("video:complete")) {
      seen.complete += 1;
      if (o.mismatch) return { ok: false, status: 409, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => ({
        key: "RIG-03/ep/front.mp4", safeToDelete: true,
      }) };
    }
    if (u.includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  return seen;
}

/* checklist -> handover -> recording -> review -> scored, which is the
   only route by which a take is ever saved. */
function saveATake(rig) {
  rig.press(2); rig.frames(1);      // check passed
  rig.press(2); rig.frames(30);     // start, record
  rig.press(3); rig.frames(1);      // save
  rig.press(2); rig.frames(1);      // scored
}

test("with no recorder attached, a saved take queues no video",
  withRig({ search: "?demo" }, async (rig) => {
    fakeStore();
    saveATake(rig);
    await rig.uploadVideo();
    assert.equal(rig.video().queued, 0,
      "a page with no camera must not invent bytes to send");
  }));

test("a saved take queues one upload per camera",
  withRig({ search: "?demo" }, async (rig) => {
    fakeStore();
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    assert.equal(rig.video().queued, 3, "three panes, three videos");
  }));

test("the camera name becomes a key that can be a filename",
  withRig({ search: "?demo" }, async (rig) => {
    const asked = [];
    rig.setVideoSource((episodeId, camera) => { asked.push(camera); return TAKE; });
    fakeStore();
    saveATake(rig);
    assert.deepEqual(asked, ["front", "wrist-l", "overhead"],
      '"Wrist L" is a label on a screen, not a path in a store');
  }));

test("a take is released only after the server says what landed is right",
  withRig({ search: "?demo" }, async (rig) => {
    const seen = fakeStore();
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    assert.equal(rig.video().queued, 3);

    await rig.uploadVideo();
    assert.equal(seen.presign, 1, "asked where to put it");
    assert.equal(seen.put, 1, "put the bytes");
    assert.equal(seen.complete, 1, "reported the checksum");
    assert.equal(seen.bytes, 2048, "sent the whole take");
    assert.equal(rig.video().queued, 2, "the confirmed camera was released");
    assert.equal(rig.video().sent.length, 1);
  }));

test("a checksum the server refuses keeps the bytes on the rig",
  withRig({ search: "?demo" }, async (rig) => {
    fakeStore({ mismatch: true });
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    await rig.uploadVideo();

    const v = rig.video();
    assert.equal(v.queued, 3, "a refused upload must not drop the only good copy");
    assert.equal(v.sent.length, 0);
    assert.match(v.error, /409/);
  }));

test("an upload that never reaches the store keeps the bytes on the rig",
  withRig({ search: "?demo" }, async (rig) => {
    fakeStore({ putFails: true });
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    await rig.uploadVideo();
    assert.equal(rig.video().queued, 3);
    assert.equal(rig.video().state, "offline");
  }));

test("an episode not projected yet is waited for, not given up on",
  withRig({ search: "?demo" }, async (rig) => {
    /* The rig saves a take and reaches the upload in the same second;
       projection runs on its own timer. A 404 here means "not yet". */
    fakeStore({ notProjected: true });
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    await rig.uploadVideo();

    const v = rig.video();
    assert.equal(v.state, "waiting", "a take about to exist was treated as a failure");
    assert.equal(v.queued, 3, "nothing was dropped");
  }));

test("uploading video does not interrupt the operator",
  withRig({ search: "?demo" }, async (rig) => {
    global.fetch = async () => { throw new Error("no network"); };
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    const before = rig.screen();
    await rig.uploadVideo(2);
    rig.frames(3);
    assert.equal(rig.screen(), before, "a failed video upload changed the screen");
    assert.deepEqual(rig.errors, []);
  }));
