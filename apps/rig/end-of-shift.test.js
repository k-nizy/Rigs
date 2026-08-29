/* =====================================================================
 * end-of-shift.test.js  -  the rig comes to rest when its shift ends
 *
 * The rig has always had one half of the standby seam: `tick()` moves a
 * rig INTO work the moment the schedule says somebody is due. It never
 * had the other half. When the shift ended - the payload's window
 * closing, or the last turn simply running out - nothing happened at
 * all. The rig stayed on whatever screen it was on, pedals live, and
 * carried on.
 *
 * Three things go wrong from there, and none of them are visible to
 * anyone standing at the rig:
 *
 *   the last stint of every shift is never filed. rotate() is what
 *   emits `stint_ended`, and rotate() only runs when there is a next
 *   turn to rotate INTO. The final turn of every shift, every day,
 *   twelve rigs, was simply missing from rig_productivity_blocks.
 *
 *   episodes recorded past the end are unattributed. `current()` is
 *   null, so `envelope()` files them with turnFrom and operatorId both
 *   null - work belonging to nobody, which is the one thing the ledger
 *   has no mechanism to correct.
 *
 *   the rig looks busy. Twelve rigs sitting on Handover after 16:00 is
 *   indistinguishable, from the desk, from twelve rigs being worked.
 *
 * The other half of the same seam, which resting makes routine: leaving
 * Standby has to START a stint. Nothing did, so the stint that began
 * when the shift did carried the standby hours in its assigned time and
 * was credited to whoever the clock named at the NEXT boundary.
 *
 * These drive the wall clock rather than the demo clock, because the
 * thing under test is a rig standing on the floor reading the window the
 * desk wrote - and that is the only clock that can end a shift.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { mountRig } = require("./test/dom.js");
const RE = require(path.resolve(__dirname, "../../packages/engine/rotation-engine.js"));
const ROSTER = require(path.resolve(__dirname, "../../packages/demo-roster/demo-roster.js"));

/* The harness builds its instants with `new Date(2026, 7, 23, hh, mm)` -
   wall-clock time on the machine running the tests. Stamping the payload
   with that same zone is what makes "15:50" in the sheet and "15:50" on
   the harness clock the same minute, on any machine and in CI. */
const HERE = Intl.DateTimeFormat().resolvedOptions().timeZone;

function morning(rigId) {
  const plan = RE.buildPlan(
    Object.assign({}, ROSTER.defaults, { date: "2026-08-23", shift: "morning" }),
    ROSTER.groups);
  const p = RE.rigPayload(plan, rigId || "RIG-03");
  p.shift.tz = HERE;
  return p;
}

const PUSHED = morning();
const LAST = PUSHED.turns[PUSHED.turns.length - 1];   // 15:45 - 16:00

function withRig(opts, fn) {
  return async () => {
    const rig = await mountRig(Object.assign({ search: "", pushed: morning() }, opts));
    try { await fn(rig); } finally { rig.stop(); }
  };
}

/* Checklist -> handover -> recording, the way an operator walks it. */
const toHandover = (rig) => { rig.press(2); rig.frames(2); };
const toRecording = (rig) => { toHandover(rig); rig.press(2); rig.frames(2); };
const fileTake = (rig) => { rig.press(3); rig.frames(1); rig.press(2); rig.frames(2); };

/* Inside the last turn of the Morning shift, with the operator working. */
const WORKING = { at: "15:50" };

// ------------------------------------------------- the rig comes to rest

test("a rig whose shift has ended stops working and says so",
  withRig(WORKING, async (rig) => {
    toHandover(rig);
    assert.equal(rig.screen(), "handover", "sanity: somebody is due at 15:50");

    rig.clockTo("16:05");
    rig.frames(3);

    assert.equal(rig.screen(), "standby",
      "the shift ended at 16:00 and the rig carried on holding a live screen");
  }));

test("the pedals stop offering work once the shift has ended",
  withRig(WORKING, async (rig) => {
    toHandover(rig);
    rig.clockTo("16:05");
    rig.frames(3);
    assert.deepEqual(rig.pedals(), ["—", "Check the rig", "—"],
      "Start was still under the operator's foot after the shift ended");
  }));

test("the last stint of the shift is filed, under the operator who worked it",
  withRig(WORKING, async (rig) => {
    toRecording(rig);
    rig.frames(20);
    fileTake(rig);

    const before = rig.eventsOf("stint_ended").length;
    rig.clockTo("16:05");
    rig.frames(3);

    const filed = rig.eventsOf("stint_ended");
    assert.equal(filed.length, before + 1,
      "the final stint of the shift was never filed - rotate() is the only "
      + "thing that emits one, and there is no turn left to rotate into");
    assert.equal(filed[filed.length - 1].operatorId, LAST.operator.id,
      "filed under the wrong operator");
  }));

test("nothing recorded on this rig belongs to nobody",
  withRig(WORKING, async (rig) => {
    toRecording(rig);
    rig.frames(20);
    fileTake(rig);

    rig.clockTo("16:05");
    rig.frames(3);

    // Whatever an operator can still press after the shift has ended, no
    // episode may come out of it unattributed.
    rig.press(2); rig.frames(2);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(2);

    const orphans = rig.events().filter(
      (e) => e.bucket === "episodes" && !e.operatorId);
    assert.deepEqual(orphans.map((e) => e.event), [],
      "episodes were filed with operatorId null - work belonging to nobody, "
      + "which the ledger has no mechanism to correct");
  }));

// ------------------------------------------------ but never mid-episode

test("the end of the shift never cuts a take short",
  withRig({ at: "15:55" }, async (rig) => {
    toRecording(rig);
    assert.equal(rig.screen(), "recording", "sanity");

    rig.clockTo("16:05");
    rig.frames(5);

    assert.equal(rig.screen(), "recording",
      "the shift ending ended somebody's take - only a pedal may do that");
  }));

test("and the rig rests as soon as the take lands",
  withRig({ at: "15:55" }, async (rig) => {
    toRecording(rig);
    rig.frames(20);
    rig.clockTo("16:05");
    rig.frames(5);

    fileTake(rig);
    rig.frames(2);

    assert.equal(rig.screen(), "standby",
      "the take landed and the rig went back to work on a shift that is over");
  }));

test("a take that ran past the end is still the operator's own",
  withRig({ at: "15:55" }, async (rig) => {
    toRecording(rig);
    rig.frames(20);
    rig.clockTo("16:05");
    rig.frames(5);
    fileTake(rig);

    const saved = rig.eventsOf("episode_saved");
    assert.equal(saved.length, 1, "sanity: one take");
    assert.equal(saved[0].operatorId, LAST.operator.id,
      "the take was made before 16:00, by the operator the sheet named");
  }));

// ------------------------------- a rig that is down is not a rig at rest

test("a rig standing down at the end of the shift stays down",
  withRig(WORKING, async (rig) => {
    toHandover(rig);
    rig.press(3); rig.frames(1);          // hardware issue
    rig.press(1); rig.frames(1);          // "Gripper broken", a leaf
    assert.equal(rig.screen(), "rig_down", "sanity: the rig is down");

    rig.clockTo("16:05");
    rig.frames(3);

    assert.equal(rig.screen(), "rig_down",
      "a rig that is down stays down until a human clears it, and a clock is not a human");
  }));

// ------------------------------------------- and the other half: waking

/* A rig switched on before its shift - which is every rig on a floor
   where somebody sweeps the machines before the crew arrives - boots
   into Standby with `turnKey` null. When the shift starts, `tick()`
   moves it into work and then, in the SAME frame, sees a turn whose
   `from` does not match that null key and treats it as a boundary. It
   rotates.

   Rotating is how a stint ends, so the rig files one for a shift that
   has not started yet: nought episodes, nought recorded, and every
   second of standby in the assigned time. Nobody was at the rig. The
   block lands on the first operator of the shift, because `stintWho` is
   null and `envelope()` falls back to the clock - and it lands as a
   0% efficiency score against their name before they have touched a
   pedal.

   Then it goes to Handover, and the start-of-shift check never runs. */

test("the shift starting does not file a stint nobody worked",
  withRig({ at: "07:50" }, async (rig) => {
    rig.frames(2);
    assert.equal(rig.screen(), "standby", "sanity: nobody is due at 07:50");
    rig.frames(30, 20000);                // ten real minutes of standing about

    rig.clockTo("08:05");
    rig.frames(3);

    assert.deepEqual(rig.eventsOf("stint_ended"), [],
      "the shift starting filed a stint for the standby before it - a "
      + "0% block against the first operator, for time nobody was at the rig");
  }));

test("a rig switched on early still runs its shift check",
  withRig({ at: "07:50" }, async (rig) => {
    rig.frames(2);
    rig.clockTo("08:05");
    rig.frames(3);

    assert.equal(rig.screen(), "checklist",
      "the check was skipped: waking rotated, and rotate() ends on Handover");
  }));

test("the first stint of the shift is not charged for the standby before it",
  withRig({ at: "07:50" }, async (rig) => {
    rig.frames(2);
    rig.frames(30, 20000);

    rig.clockTo("08:05");
    rig.frames(3);
    assert.notEqual(rig.screen(), "standby", "sanity: the shift has started");

    const assigned = rig.stint().assignedSecs;
    assert.ok(assigned < 60,
      "the first stint of the shift was charged " + assigned + "s of assigned "
      + "time, which is the standby it sat through. Every operator who starts "
      + "a shift on a rig that was already switched on reads as behind.");
  }));

test("the first stint of the shift belongs to the operator who worked it",
  withRig({ at: "07:50" }, async (rig) => {
    rig.frames(2);
    rig.clockTo("08:05");
    rig.frames(3);

    const first = PUSHED.turns.find((t) => t.from === "08:00");
    toRecording(rig);
    rig.frames(20);
    fileTake(rig);

    // Over the boundary into the next turn, which is what files the stint.
    rig.clockTo("08:20");
    rig.frames(3);

    const filed = rig.eventsOf("stint_ended");
    assert.equal(filed.length, 1, "sanity: one stint closed");
    assert.equal(filed[0].operatorId, first.operator.id,
      "the first stint of the shift was credited to whoever the clock named "
      + "at the boundary, not to the operator who actually worked it");
  }));
