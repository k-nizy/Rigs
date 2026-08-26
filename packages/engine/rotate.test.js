/* =====================================================================
 * rotate.test.js
 *
 * The other rotation mode, held to the same standard as the sheet.
 *
 * `rotation-desk-v1` draws hold-rig and nothing else, and
 * reference-sheet.test.js pins that format cell by cell. Rotate had one
 * test, and it only asserted that a bad turn length is refused - nothing
 * checked that a good one produces a schedule anybody could work. So the
 * engine carried a second mode that CLAUDE.md called "still tested" while
 * an edit could have broken it in silence.
 *
 * This is the rotate equivalent: the same six questions the sheet asks of
 * hold, asked of rotate.
 *
 *   Does every operator end the shift on the budget?
 *   Is every rig manned, by exactly one person, in every block?
 *   Is every turn the same length, ending on the shift boundary?
 *   Does an operator actually move, and cover the whole group?
 *   Does the off-time split into equal Break and Think?
 *   Does a rig handed this schedule read back the same answer?
 *
 * Rotate is not drawn by the desk today. It is tested because the engine
 * still implements it, and untested code that ships is a promise nobody
 * has checked.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

global.window = global;
require("./rotation-engine.js");
const RE = window.RotationEngine;

/* Rotate's own condition: time on a rig has to divide the hour. 15x4 is
   a 60-minute turn, which does. */
const CFG = {
  shift: "morning", date: "2026-01-01",
  blockMin: 15, stintBlocks: 4, mode: "rotate",
};

const GROUP = {
  key: "A",
  task: "Box transfer - bin to conveyor",
  rigs: ["RIG-01", "RIG-02", "RIG-03"],
  ops: ["Op 1", "Op 2", "Op 3", "Op 4"],
};

const plan = RE.buildPlan(CFG, [GROUP]);
const g = plan.groups[0];

const STINT_BLOCKS = CFG.stintBlocks;
const N_STINTS = plan.nBlocks / STINT_BLOCKS;

/* What operator `i` is doing in stint `s`: a rig index, or BREAK/THINK. */
const cellAt = (i, s) => g.rows[i][s * STINT_BLOCKS];
const isOff = (c) => c === RE.BREAK || c === RE.THINK;

test("the rotate format is a 60-minute turn, eight of them, and it audits clean", () => {
  assert.equal(plan.mode, "rotate");
  assert.equal(plan.blockMin, 15);
  assert.equal(plan.nBlocks, 32, "eight hours on a 15-minute grid");
  assert.equal(RE.stintMinutes(CFG), 60, "an hour on a rig, which divides the hour");
  assert.equal(N_STINTS, 8, "eight turns in the shift");

  const audit = RE.auditPlan(plan);
  assert.equal(audit.level, "ok", audit.text + " / " + audit.note);
});

test("every operator ends the shift on the budget: 6h work, 60 break, 60 think", () => {
  // The same margin the sheet is checked against. It is the promise the
  // floor makes to the people working it, and it does not depend on
  // which mode the desk chose.
  g.totals.forEach((t, i) => {
    assert.equal(t.work, 360, g.ops[i] + " works 6 hours");
    assert.equal(t.brk, 60, g.ops[i] + " breaks for 60 minutes");
    assert.equal(t.think, 60, g.ops[i] + " thinks for 60 minutes");
  });
});

test("nobody is on two rigs at once and no rig stands empty", () => {
  for (let b = 0; b < plan.nBlocks; b++) {
    const onRigs = g.rows.map((r) => r[b]).filter((c) => typeof c === "number" && c >= 0);
    assert.equal(onRigs.length, 3,
      "block " + b + " (" + RE.hhmm(RE.blockStart(plan, b)) + "): three of the four are on a rig");
    assert.equal(new Set(onRigs).size, 3, "block " + b + ": on three different rigs");
  }
});

test("every turn on a rig is the same length - no stubs at either end", () => {
  // This is the difference from hold, and the reason rotate exists at
  // all. Hold necessarily produces 15/30/45-minute stubs where the shift
  // begins and ends, because only one operator can be off per block.
  // Rotate hands over on the turn boundary, so there is one length.
  assert.deepEqual(RE.rigStintLengths(plan), [60],
    "one turn length across every rig, start to finish");
});

test("the last turn lands on the shift boundary, so nothing dangles", () => {
  for (let ri = 0; ri < g.rigs.length; ri++) {
    const last = RE.holderAt(g, ri, plan.nBlocks - 1);
    const firstOfLastTurn = RE.holderAt(g, ri, plan.nBlocks - STINT_BLOCKS);
    assert.equal(last, firstOfLastTurn,
      g.rigs[ri] + ": the final turn runs whole rather than being cut off");
  }
  assert.equal(plan.nBlocks % STINT_BLOCKS, 0, "the shift is a whole number of turns");
});

test("an operator takes a whole turn off, never part of one", () => {
  for (let i = 0; i < g.ops.length; i++) {
    for (let s = 0; s < N_STINTS; s++) {
      const cells = g.rows[i].slice(s * STINT_BLOCKS, (s + 1) * STINT_BLOCKS);
      const offs = cells.filter(isOff).length;
      assert.ok(offs === 0 || offs === STINT_BLOCKS,
        g.ops[i] + " turn " + s + ": off for " + offs + " of " + STINT_BLOCKS
        + " blocks - a turn off is the whole turn");
    }
  }
});

test("the two off-turns are one Break and one Think, in that order", () => {
  // Off-runs alternate inside the engine, which is what keeps the two
  // budgets equal without a second rule. With four operators and eight
  // turns each is off exactly twice, so the alternation has to land on
  // one of each - if it ever lands on two Breaks the budget test above
  // fails too, and this says which rule broke.
  g.rows.forEach((row, i) => {
    const offLabels = [];
    for (let s = 0; s < N_STINTS; s++) {
      const c = cellAt(i, s);
      if (isOff(c)) offLabels.push(c);
    }
    assert.deepEqual(offLabels, [RE.BREAK, RE.THINK],
      g.ops[i] + " is off twice: once on Break, once on Think");
  });
});

test("an operator moves rig every turn, and covers all three", () => {
  // The defining behaviour of the mode. Hold keeps one rig until the
  // break; rotate moves on every turn, so a stationary operator here
  // would be hold wearing rotate's name.
  g.rows.forEach((row, i) => {
    const worked = [];
    let previous = null;
    for (let s = 0; s < N_STINTS; s++) {
      const c = cellAt(i, s);
      if (isOff(c)) { previous = null; continue; }
      assert.notEqual(c, previous,
        g.ops[i] + " stayed on " + g.rigs[c] + " across turn " + s);
      worked.push(c);
      previous = c;
    }
    assert.equal(new Set(worked).size, g.rigs.length,
      g.ops[i] + " should see all three rigs, saw " + new Set(worked).size);
  });
});

test("block length is only the grid: 15x4 and 20x3 are the same schedule", () => {
  // CLAUDE.md says so, and it is the reason the desk has no grid-size
  // control. Both are 60-minute turns; the finer grid just draws the
  // same thing on more rows.
  const COARSE = { ...CFG, blockMin: 20, stintBlocks: 3 };
  const coarse = RE.buildPlan(COARSE, [GROUP]);
  const cg = coarse.groups[0];

  assert.equal(RE.stintMinutes(COARSE), RE.stintMinutes(CFG), "both are 60-minute turns");
  assert.equal(coarse.nBlocks / COARSE.stintBlocks, N_STINTS, "and both are eight of them");
  assert.deepEqual(cg.totals, g.totals, "the same budget out of both grids");
  assert.deepEqual(RE.rigStintLengths(coarse), RE.rigStintLengths(plan));

  // Same rig, same operator, at the same minute of the shift - compared
  // per turn, because that is the unit the two grids share.
  for (let ri = 0; ri < GROUP.rigs.length; ri++) {
    for (let s = 0; s < N_STINTS; s++) {
      assert.equal(
        RE.holderAt(g, ri, s * STINT_BLOCKS),
        RE.holderAt(cg, ri, s * COARSE.stintBlocks),
        "turn " + s + " on " + GROUP.rigs[ri] + " differs between the two grids");
    }
  }
});

test("a turn that does not divide the hour is refused, and the budget is why", () => {
  // The existing check asserted only that the audit stops being "ok".
  // What it did not say is what going ahead would cost, which is the
  // part worth pinning: the four operators stop being equal.
  const bad = RE.buildPlan({ ...CFG, blockMin: 15, stintBlocks: 3 }, [GROUP]);
  assert.equal(RE.stintMinutes({ ...CFG, stintBlocks: 3 }), 45, "45 does not divide 60");

  const audit = RE.auditPlan(bad);
  assert.notEqual(audit.level, "ok", "a 45-minute turn should not pass");

  const totals = bad.groups[0].totals;
  const equal = totals.every((t) => t.work === totals[0].work
    && t.brk === totals[0].brk && t.think === totals[0].think);
  assert.ok(!equal,
    "the cost of ignoring the rule: " + JSON.stringify(totals));
});

test("what a rig is handed says the same thing as the plan", () => {
  // The founding invariant, in rotate's terms: read the payload back the
  // way the rig platform does - by the clock - and it has to name the
  // operator the plan put there. If these disagree the rig and the desk
  // have parted company, which is the one thing this system may not do.
  const payload = RE.rigPayload(plan, "RIG-01");
  assert.ok(payload, "RIG-01 must be in the plan");
  assert.equal(payload.blockMinutes, 15);
  assert.equal(payload.rotation, "rotate");

  const ri = GROUP.rigs.indexOf("RIG-01");
  for (let b = 0; b < plan.nBlocks; b++) {
    const at = RE.blockStart(plan, b);
    const on = RE.whoIsOn(payload, at);
    assert.ok(on, "somebody is on RIG-01 at " + RE.hhmm(at));
    assert.equal(on.turn.operator.name, g.ops[RE.holderAt(g, ri, b)],
      RE.hhmm(at) + " on RIG-01");
  }
});

test("the turns handed to a rig tile the shift with no gap and no overlap", () => {
  // A gap is a stretch with nobody on the rig; an overlap is two people
  // holding it at once. Both are silent, and both are what a same-length
  // turn is supposed to make impossible.
  for (const rigId of GROUP.rigs) {
    const turns = RE.rigPayload(plan, rigId).turns;
    assert.equal(turns.length, N_STINTS, rigId + " should have one turn per stint");
    assert.equal(turns[0].from, "08:00", rigId + " starts when the shift does");
    assert.equal(turns[turns.length - 1].to, "16:00", rigId + " ends when the shift does");

    turns.forEach((t, i) => {
      assert.equal(t.minutes, 60, rigId + " turn " + i + " is an hour");
      if (i > 0) {
        assert.equal(turns[i - 1].to, t.from,
          rigId + ": " + turns[i - 1].to + " -> " + t.from + " is not a clean handover");
      }
    });
  }
});
