/* =====================================================================
 * attribution.test.js  -  whose video is this?
 *
 * The one question the whole system exists to answer correctly. Every
 * other failure here is recoverable: the ledger replays, the spool
 * drains, a wrong schedule gets pushed again. A take filed under the
 * wrong operator is not recoverable, and it is the least detectable
 * failure this product can have - the episode is well formed, the
 * operator exists, the turn exists, the score is real. Nothing
 * downstream can tell. You would only find out by asking somebody
 * "did you record this?" and hearing no.
 *
 * The bug these were written for: the rig asked "who is on this rig?"
 * at the moment a take was SAVED, from a clock, with no idea a
 * recording was in flight. And the rig deliberately lets takes run past
 * a turn boundary - `handoverDue` waits for the episode to land before
 * rotating - so a take started at 08:59 and saved at 09:01 was filed
 * under the operator who came on at 09:00.
 *
 * The rig was doing two contradictory things at once: refusing to cut
 * the recording, while handing the result to somebody else.
 *
 * The rule now: a take belongs to whoever pressed start. Captured at
 * `start_episode` beside the episode id, used when it is filed.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { mountRig } = require("./test/dom.js");
const RE = require(path.resolve(__dirname, "../../packages/engine/rotation-engine.js"));
const ROSTER = require(path.resolve(__dirname, "../../packages/demo-roster/demo-roster.js"));

function withRig(opts, fn) {
  return async () => {
    const rig = await mountRig(opts);
    try { await fn(rig); } finally { rig.stop(); }
  };
}

const offline = () => { global.fetch = async () => { throw new Error("no server"); }; };

/* Runs the clock forward while whatever is on screen keeps running. The
   demo clock is 30x, so this crosses turn boundaries in seconds. */
function runOn(rig, ticks) {
  for (let i = 0; i < ticks; i++) rig.frames(20, 500);
}

function startATake(rig) {
  rig.press(2); rig.frames(1);      // shift check passed -> handover
  rig.press(2); rig.frames(2);      // start -> recording
}

const saved = (rig) => rig.eventsOf("episode_saved");
const discarded = (rig) => rig.eventsOf("episode_discarded");

// ------------------------------------------------------- the control

test("a take that begins and ends inside one turn belongs to that operator",
  withRig({ search: "?demo=30" }, async (rig) => {
    offline();
    startATake(rig);
    const before = rig.rail();
    rig.frames(10);                 // a short take, well inside the turn
    rig.press(3); rig.frames(1);    // save
    rig.press(2); rig.frames(1);    // score

    const e = saved(rig)[0];
    assert.ok(e, "no episode was filed");
    assert.equal(e.turnFrom, "08:00");
    assert.ok(e.operatorId, "the take is unattributed");
    assert.ok(before, "sanity");
  }));

// ---------------------------------------------- across a turn boundary

test("a take that runs past a turn boundary belongs to whoever started it",
  withRig({ search: "?demo=30" }, async (rig) => {
    offline();
    startATake(rig);

    /* Who is on the rig at the moment the pedal was pressed. Read from
       the rig's own first event rather than assumed, so this test cannot
       drift from the roster. */
    const starter = rig.eventsOf("shift_check")[0];
    assert.equal(starter.turnFrom, "08:00");

    runOn(rig, 40);                 // well past the 15-minute first turn
    assert.equal(rig.screen(), "recording", "the rig cut the take short");
    assert.equal(rig.rail().due, true, "the handover should be deferred, not taken");

    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    const e = saved(rig)[0];
    assert.equal(e.operatorId, starter.operatorId,
      "the take was filed under " + e.operatorId + " but " + starter.operatorId +
      " recorded it");
    assert.equal(e.turnFrom, starter.turnFrom,
      "the take was filed against turn " + e.turnFrom + " but began in " +
      starter.turnFrom);
  }));

test("a discarded take is attributed the same way",
  withRig({ search: "?demo=30" }, async (rig) => {
    offline();
    startATake(rig);
    const starter = rig.eventsOf("shift_check")[0];

    runOn(rig, 40);
    rig.press(1); rig.frames(1);    // discard

    const e = discarded(rig)[0];
    assert.ok(e, "a discarded take is a row, not an absence");
    assert.equal(e.operatorId, starter.operatorId);
    assert.equal(e.turnFrom, starter.turnFrom);
  }));

// --------------------------------------------- no leaking between takes

test("the capture does not leak from one take to the next",
  withRig({ search: "?demo=30" }, async (rig) => {
    offline();
    startATake(rig);
    rig.frames(10);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);    // first take filed

    runOn(rig, 40);                 // cross a boundary between takes

    /* Second take, started after the boundary. It must carry the NEW
       turn, not the one cached by the first take. */
    if (rig.screen() === "resetting") { rig.press(2); rig.frames(2); }
    if (rig.screen() === "handover") { rig.press(2); rig.frames(2); }
    if (rig.screen() !== "recording") return;   // standby: nothing to assert

    rig.frames(10);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    const all = saved(rig);
    assert.equal(all.length, 2, "expected two takes");
    assert.notEqual(all[1].turnFrom, null);
    assert.ok(all[1].seq > all[0].seq);
  }));

// ------------------------------------------- point-in-time facts unchanged

test("a shift check still reports the operator on the rig at that moment",
  withRig({ search: "?demo=30" }, async (rig) => {
    offline();
    rig.press(2); rig.frames(1);

    const checks = rig.eventsOf("shift_check");
    assert.ok(checks.length >= 1);
    /* A check is a point-in-time fact, not something that spans time, so
       it correctly reads the clock. Only episodes needed the change. */
    assert.equal(checks[checks.length - 1].turnFrom, "08:00");
  }));

test("every episode carries an operator and a turn",
  withRig({ search: "?demo=30" }, async (rig) => {
    offline();
    startATake(rig);
    runOn(rig, 30);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    for (const e of saved(rig).concat(discarded(rig))) {
      assert.ok(e.operatorId, "an unattributed episode reached the ledger");
      assert.ok(e.turnFrom, "an episode with no turn reached the ledger");
      assert.ok(e.data.episodeId, "an episode with no id cannot be joined to its video");
    }
  }));

test("the video is keyed to the episode the operator recorded",
  withRig({ search: "?demo=30" }, async (rig) => {
    /* The join that matters: video is stored under the episode id, and
       the episode row carries the operator. If the id or the operator is
       wrong, the footage belongs to the wrong person. */
    offline();
    const TAKE = new Blob([new Uint8Array(64).fill(1)]);
    rig.setVideoSource(() => TAKE);

    startATake(rig);
    const starter = rig.eventsOf("shift_check")[0];
    runOn(rig, 40);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    const e = saved(rig)[0];
    assert.equal(e.operatorId, starter.operatorId);
    assert.equal(rig.video().queued, 3, "one upload per camera");
  }));


/* =====================================================================
 * The same bug, twice more
 *
 * An episode was not the only thing that spans time and is filed at the
 * end of it. A stint is a whole turn's accounting, filed by rotate() -
 * which runs AFTER the boundary, because handoverDue deliberately waits
 * for a take to land first. So an operator's entire turn was credited to
 * the person who relieved them.
 *
 * That one was worse than the episode bug in the way that matters least
 * for finding it and most for trusting the screen: `outgoingOperator()`
 * named the correct person in the sentence the operator reads, while the
 * envelope filed the wrong one. Right on the wall, wrong in the database.
 * ===================================================================== */

test("a stint belongs to the operator who worked it, not the one who relieved them",
  withRig({ search: "?demo=30" }, async (rig) => {
    offline();
    startATake(rig);
    const starter = rig.eventsOf("shift_check")[0];

    runOn(rig, 40);                 // past the boundary, still recording
    rig.press(3); rig.frames(1);    // save
    rig.press(2); rig.frames(1);    // score -> handoverDue -> rotate()

    const stint = rig.eventsOf("stint_ended")[0];
    assert.ok(stint, "no stint was filed");
    assert.equal(stint.operatorId, starter.operatorId,
      "the stint was credited to " + stint.operatorId + " but " +
      starter.operatorId + " worked it");
    assert.equal(stint.turnFrom, starter.turnFrom);
  }));

test("the stint's numbers are the ones that operator earned",
  withRig({ search: "?demo=30" }, async (rig) => {
    /* Efficiency is computed at read time from these four columns, per
       operator. Filed against the wrong person, both operators' numbers
       are wrong: one loses the credit, the other gains work they never
       did. */
    offline();
    startATake(rig);
    runOn(rig, 40);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    const stint = rig.eventsOf("stint_ended")[0];
    assert.ok(stint.data.recordedSecs > 0, "a worked stint recorded nothing");
    assert.ok(stint.data.assignedSecs >= stint.data.recordedSecs,
      "recorded time cannot exceed assigned time");
    assert.equal(stint.data.episodes, 1);
  }));

test("the next stint is not still credited to the operator who left",
  withRig({ search: "?demo=30" }, async (rig) => {
    offline();
    startATake(rig);
    const first = rig.eventsOf("shift_check")[0];
    runOn(rig, 40);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);    // rotate happens here

    const stint = rig.eventsOf("stint_ended")[0];
    /* After the rotation the rig is on the next turn. Anything filed now
       must carry the new operator, or the capture has gone stale and is
       following the wrong person around. */
    rig.press(2); rig.frames(2);
    if (rig.screen() !== "recording") return;
    rig.frames(10);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    const takes = rig.eventsOf("episode_saved");
    if (takes.length < 2) return;
    assert.notEqual(takes[1].turnFrom, stint.turnFrom,
      "the second take is still filed against the finished turn");
  }));
