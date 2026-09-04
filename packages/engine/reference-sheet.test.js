/* =====================================================================
 * reference-sheet.test.js
 *
 * The sheet, transcribed by hand, and the assertion that the engine
 * still draws it.
 *
 * Rotation Desk v1 is locked to one format: 15-minute blocks, hold-rig,
 * three rigs and four operators to a group, 32 rows from 0:00 to 7:45.
 * The desk has no control that can leave that format - but the engine
 * underneath it can still be edited, and an edit that quietly changes
 * who stands where would leave the floor reading a sheet nobody
 * recognises. So the sheet lives here as data, not as a description.
 *
 * Two halves are transcribed, because the sheet has two halves:
 *
 *   SHEET_RIGS  the rig-side table - which operator is on Rig 1, Rig 2
 *               and Rig 3 in each of the 32 blocks
 *   SHEET_OFF   the operator-side table's odd column out - who is off
 *               in each block, and whether the sheet writes it Break or
 *               Think
 *
 * Between them they pin every cell. If this file fails, the engine and
 * the sheet have parted company; fix the engine, or agree a v2.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");

global.window = global;
require("./rotation-engine.js");
const RE = window.RotationEngine;

/* Which operator (1-4) holds Rig 1, Rig 2, Rig 3 - one row per block. */
const SHEET_RIGS = [
  [1, 2, 3],  // 0:00
  [1, 2, 4],  // 0:15
  [1, 3, 4],  // 0:30
  [2, 3, 4],  // 0:45
  [2, 3, 1],  // 1:00
  [2, 4, 1],  // 1:15
  [3, 4, 1],  // 1:30
  [3, 4, 2],  // 1:45
  [3, 1, 2],  // 2:00
  [4, 1, 2],  // 2:15
  [4, 1, 3],  // 2:30
  [4, 2, 3],  // 2:45
  [1, 2, 3],  // 3:00
  [1, 2, 4],  // 3:15
  [1, 3, 4],  // 3:30
  [2, 3, 4],  // 3:45
  [2, 3, 1],  // 4:00
  [2, 4, 1],  // 4:15
  [3, 4, 1],  // 4:30
  [3, 4, 2],  // 4:45
  [3, 1, 2],  // 5:00
  [4, 1, 2],  // 5:15
  [4, 1, 3],  // 5:30
  [4, 2, 3],  // 5:45
  [1, 2, 3],  // 6:00
  [1, 2, 4],  // 6:15
  [1, 3, 4],  // 6:30
  [2, 3, 4],  // 6:45
  [2, 3, 1],  // 7:00
  [2, 4, 1],  // 7:15
  [3, 4, 1],  // 7:30
  [3, 4, 2],  // 7:45
];

/* Who is off, and what the sheet calls it. One operator off per block,
 * the slot walking 4, 3, 2, 1; four blocks of Break then four of Think,
 * which is what makes the two budgets come out equal. */
const SHEET_OFF = [
  [4, "BREAK"], [3, "BREAK"], [2, "BREAK"], [1, "BREAK"],
  [4, "THINK"], [3, "THINK"], [2, "THINK"], [1, "THINK"],
  [4, "BREAK"], [3, "BREAK"], [2, "BREAK"], [1, "BREAK"],
  [4, "THINK"], [3, "THINK"], [2, "THINK"], [1, "THINK"],
  [4, "BREAK"], [3, "BREAK"], [2, "BREAK"], [1, "BREAK"],
  [4, "THINK"], [3, "THINK"], [2, "THINK"], [1, "THINK"],
  [4, "BREAK"], [3, "BREAK"], [2, "BREAK"], [1, "BREAK"],
  [4, "THINK"], [3, "THINK"], [2, "THINK"], [1, "THINK"],
];

/* The locked format, spelled out the way the desk holds it. */
const V1 = { shift: "morning", date: "2026-01-01", blockMin: 15, stintBlocks: 3, mode: "hold" };

const GROUP = {
  key: "A",
  task: "Box transfer - bin to conveyor",
  rigs: ["Rig 1", "Rig 2", "Rig 3"],
  ops: ["Op 1", "Op 2", "Op 3", "Op 4"],
};

const plan = RE.buildPlan(V1, [GROUP]);
const g = plan.groups[0];

test("v1 is the shape the sheet is drawn in", () => {
  assert.equal(plan.blockMin, 15, "the sheet is on a 15-minute grid");
  assert.equal(plan.nBlocks, SHEET_RIGS.length, "32 rows, 0:00 to 7:45");
  assert.equal(plan.mode, "hold", "an operator holds one rig until they step off");
  assert.equal(g.rigs.length, 3);
  assert.equal(g.ops.length, 4);
});

test("every rig gets the operator the sheet gives it, in all 32 blocks", () => {
  for (let b = 0; b < plan.nBlocks; b++) {
    const got = g.rigs.map((_, ri) => RE.holderAt(g, ri, b) + 1);
    assert.deepEqual(got, SHEET_RIGS[b],
      "block " + b + " (" + RE.hhmm(RE.blockStart(plan, b)) + ") reads "
      + got.join("/") + ", the sheet says " + SHEET_RIGS[b].join("/"));
  }
});

test("the right operator is off, and it is called what the sheet calls it", () => {
  for (let b = 0; b < plan.nBlocks; b++) {
    const [op, label] = SHEET_OFF[b];
    const offRow = g.rows.findIndex(r => r[b] === RE.BREAK || r[b] === RE.THINK);
    assert.equal(offRow + 1, op, "block " + b + ": the sheet has Op " + op + " off");
    assert.equal(g.rows[offRow][b], label, "block " + b + ": the sheet writes " + label);
  }
});

test("nobody is on two rigs at once and no rig stands empty", () => {
  for (let b = 0; b < plan.nBlocks; b++) {
    const onRigs = g.rows.map(r => r[b]).filter(c => typeof c === "number");
    assert.equal(onRigs.length, 3, "block " + b + ": three of the four are on a rig");
    assert.equal(new Set(onRigs).size, 3, "block " + b + ": on three different rigs");
  }
});

test("the sheet's own margin: 6h work, 60 min break, 60 min think, each", () => {
  g.totals.forEach((t, i) => {
    assert.equal(t.work, 360, g.ops[i] + " works 6 hours");
    assert.equal(t.brk, 60, g.ops[i] + " breaks for 60 minutes");
    assert.equal(t.think, 60, g.ops[i] + " thinks for 60 minutes");
  });
});

test("what a rig is handed says the same thing as the sheet", () => {
  const payload = RE.rigPayload(plan, "Rig 1");
  assert.equal(payload.blockMinutes, 15);
  assert.equal(payload.rotation, "hold");

  // Read the payload back the way the rig platform does - by the clock -
  // and check it against the sheet block by block. This is the whole
  // point of the push: the rig must not be able to answer differently.
  for (let b = 0; b < plan.nBlocks; b++) {
    const at = RE.blockStart(plan, b);
    const on = RE.whoIsOn(payload, at);
    assert.ok(on, "somebody is on Rig 1 at " + RE.hhmm(at));
    assert.equal(on.turn.operator.name, "Op " + SHEET_RIGS[b][0],
      RE.hhmm(at) + " on Rig 1");
  }
});
