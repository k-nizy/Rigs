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

test("every rig's turns tile its shift exactly, in all three shifts", () => {
  /* The existing coverage test checks RIG-03 on the Morning shift, and
     only that the first turn starts when the shift does and the last one
     ends when it ends. That leaves the middle unexamined: a gap or an
     overlap between two turns would satisfy it completely.

     A gap is a stretch of the shift with nobody on the rig, which the
     rig reads as Standby in the middle of a working shift. An overlap is
     two operators holding one rig at the same minute, and whichever the
     engine happens to return first gets the recordings. Both are silent.

     Counting turns is not the check - a rig gets 11 or 12 depending on
     where its stub turns fall, which is a property of only one operator
     being off per block, not a defect. What must hold is that they TILE:
     start to start, end to end, touching, adding to eight hours. */
  for (const shift of RE.SHIFTS) {
    const cfg  = Object.assign({}, ROSTER.defaults, { date: "2026-08-22", shift: shift.id });
    const plan = RE.buildPlan(cfg, ROSTER.groups);

    for (const rig of ROSTER.allRigs) {
      const p = RE.rigPayload(plan, rig);
      assert.ok(p, shift.label + " has no sheet for " + rig);
      const where = shift.label + " " + rig;
      const t = p.turns;

      assert.equal(t[0].from, p.shift.start, where + " starts late");
      assert.equal(t[t.length - 1].to, p.shift.end, where + " ends early");

      for (let i = 1; i < t.length; i++) {
        assert.equal(t[i].from, t[i - 1].to,
          where + ": turn " + i + " begins at " + t[i].from + " but the one before it "
          + "ended at " + t[i - 1].to + " - " +
          (t[i].from > t[i - 1].to ? "nobody holds the rig in between"
                                   : "two operators hold it at once"));
      }

      const total = t.reduce((a, x) => a + x.minutes, 0);
      assert.equal(total, RE.SHIFT_MINUTES,
        where + " covers " + total + " minutes of a " + RE.SHIFT_MINUTES + " minute shift");

      t.forEach(x => assert.ok(x.operator && x.operator.name,
        where + " has a turn with nobody on it"));
    }
  }
});

test("every operator's day comes out to the budget, in all three shifts", () => {
  /* 6h work, 60 min break, 60 min think. It is the reason the sheet has
     the shape it has, and it was only ever asserted for the Morning
     shift - the one the demo opens on. Day and Night are built by the
     same code, but "built by the same code" is exactly the assumption
     that has been wrong twice in this system already. */
  for (const shift of RE.SHIFTS) {
    const cfg   = Object.assign({}, ROSTER.defaults, { date: "2026-08-22", shift: shift.id });
    const audit = RE.auditPlan(RE.buildPlan(cfg, ROSTER.groups));

    assert.equal(audit.level, "ok", shift.label + ": " + audit.text + " - " + audit.note);
    /* One distinct value each, or somebody's day differs from somebody
       else's and the floor is not the floor the sheet describes. */
    assert.deepEqual(audit.work,  [360], shift.label + " work minutes");
    assert.deepEqual(audit.brk,   [60],  shift.label + " break minutes");
    assert.deepEqual(audit.think, [60],  shift.label + " think minutes");
  }
});
