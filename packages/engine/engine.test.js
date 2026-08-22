/* Headless engine tests. The engine has no DOM and no globals, so it runs
 * under node with a shim window. The whole point of these tests is that
 * the desk and the rig cannot drift — if the engine changes shape, this
 * file is where it gets caught first. */

"use strict";

const { test } = require("node:test");
const assert   = require("node:assert/strict");

global.window = global;
require("./rotation-engine.js");
require("../demo-roster/demo-roster.js");

const RE     = window.RotationEngine;
const ROSTER = window.DEMO_ROSTER;

const demoCfg = Object.assign({ date: "2026-08-22" }, ROSTER.defaults);

test("demo defaults produce a clean fit", () => {
  const plan  = RE.buildPlan(demoCfg, ROSTER.groups);
  const audit = RE.auditPlan(plan);
  assert.equal(audit.level, "ok", audit.text + " / " + audit.note);
  assert.deepEqual(audit.work,  [360], "every op should work 6h");
  assert.deepEqual(audit.brk,   [60],  "every op should break 1h");
  assert.deepEqual(audit.think, [60],  "every op should think 1h");
});

test("hold mode opens with 1/2/3-block stubs (inherent, not a bug)", () => {
  const plan = RE.buildPlan(demoCfg, ROSTER.groups);
  const lens = RE.rigStintLengths(plan);
  // 15-min blocks: stubs are 15, 30, main run is 45. 3 distinct lengths.
  assert.deepEqual(lens, [15, 30, 45]);
});

test("rotate mode requires time-on-rig to divide the hour", () => {
  const good = RE.buildPlan({ ...demoCfg, mode: "rotate", blockMin: 20, stintBlocks: 3 }, ROSTER.groups);
  const bad  = RE.buildPlan({ ...demoCfg, mode: "rotate", blockMin: 20, stintBlocks: 2 }, ROSTER.groups);
  assert.equal(RE.auditPlan(good).level, "ok");
  assert.notEqual(RE.auditPlan(bad).level, "ok");
});

test("rigPayload gives one entry per turn and covers the shift end-to-end", () => {
  const plan    = RE.buildPlan(demoCfg, ROSTER.groups);
  const payload = RE.rigPayload(plan, "RIG-03");
  assert.ok(payload, "RIG-03 must be in the plan");
  assert.equal(payload.shift.start, "08:00");
  assert.equal(payload.shift.end,   "16:00");
  assert.equal(payload.turns[0].from,                       "08:00");
  assert.equal(payload.turns[payload.turns.length - 1].to,  "16:00");
  payload.turns.forEach(t => assert.ok(t.operator && t.operator.name, "every turn has an operator"));
});

test("whoIsOn returns the operator holding the rig at a given minute", () => {
  const plan    = RE.buildPlan(demoCfg, ROSTER.groups);
  const payload = RE.rigPayload(plan, "RIG-03");
  const at0900  = RE.whoIsOn(payload, 9 * 60);
  assert.ok(at0900, "someone must be on at 09:00");
  assert.equal(at0900.turn.from, "09:00");
  assert.equal(at0900.turn.operator.name, "Aleksandr Petrov");
  assert.equal(at0900.turn.theyGoTo, "Think", "outgoing operator's destination travels in the payload");
});
