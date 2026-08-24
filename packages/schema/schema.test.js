/* Schema tests. The payload shape is the contract between the desk,
 * the server and the rig - if this file breaks, one of them has drifted. */

"use strict";

const { test } = require("node:test");
const assert   = require("node:assert/strict");

global.window = global;
require("../engine/rotation-engine.js");
require("../demo-roster/demo-roster.js");
const { validate } = require("./payload.js");

const RE     = window.RotationEngine;
const ROSTER = window.DEMO_ROSTER;

test("every rig in the demo plan produces a valid payload", () => {
  const cfg  = Object.assign({ date: "2026-08-22" }, ROSTER.defaults);
  const plan = RE.buildPlan(cfg, ROSTER.groups);
  ROSTER.allRigs.forEach(rigId => {
    const p = RE.rigPayload(plan, rigId);
    const v = validate(p);
    assert.ok(v.ok, rigId + " failed validation: " + v.errors.join("; "));
  });
});

test("validate rejects a missing rigId", () => {
  const v = validate({ group: "A", task: "x", shift: { label: "M", date: "d", start: "08:00", end: "16:00" },
                       blockMinutes: 15, rotation: "hold", turns: [] });
  assert.equal(v.ok, false);
  assert.ok(v.errors.some(e => e.includes("rigId")));
});

test("validate rejects a bad HH:MM", () => {
  const v = validate({ rigId: "R", group: "A", task: "x",
                       shift: { label: "M", date: "d", start: "8:00", end: "16:00" },
                       blockMinutes: 15, rotation: "hold", turns: [] });
  assert.equal(v.ok, false);
  assert.ok(v.errors.some(e => e.includes("shift.start")));
});

test("validate rejects a bad rotation", () => {
  const v = validate({ rigId: "R", group: "A", task: "x",
                       shift: { label: "M", date: "d", start: "08:00", end: "16:00" },
                       blockMinutes: 15, rotation: "spin", turns: [] });
  assert.equal(v.ok, false);
  assert.ok(v.errors.some(e => e.includes("rotation")));
});

/* The zone. Added after the backend read a payload's "00:15" as 00:15 UTC
 * while the floor meant 00:15 local, and quietly decided no turn was in
 * progress on any of the twelve rigs. */

test("the payload says which zone its wall-clock times are in", () => {
  const cfg  = Object.assign({ date: "2026-08-22" }, ROSTER.defaults);
  const plan = RE.buildPlan(cfg, ROSTER.groups);
  const p    = RE.rigPayload(plan, "RIG-03");
  assert.equal(typeof p.shift.tz, "string");
  assert.ok(p.shift.tz.length > 0, "the zone is empty");
});

test("the zone can be pinned, so a desk can schedule a floor it is not standing on", () => {
  const cfg  = Object.assign({ date: "2026-08-22", tz: "Africa/Nairobi" }, ROSTER.defaults);
  const plan = RE.buildPlan(cfg, ROSTER.groups);
  assert.equal(RE.rigPayload(plan, "RIG-03").shift.tz, "Africa/Nairobi");
});

test("validate rejects a payload with no zone", () => {
  const v = validate({ rigId: "R", group: "A", task: "x",
                       shift: { label: "M", date: "d", start: "08:00", end: "16:00" },
                       blockMinutes: 15, rotation: "hold", turns: [] });
  assert.equal(v.ok, false);
  assert.ok(v.errors.some(e => e.includes("shift.tz")),
            "a payload whose times belong to no zone has to be refused: " + v.errors.join("; "));
});
