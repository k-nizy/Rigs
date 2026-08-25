/* End-to-end smoke test for the push server. Starts a fresh process on
 * a random port, pushes a full floor, pulls each rig back, checks the
 * shape. Runs against a scratch state file so it does not clobber a
 * developer's local server state. */

"use strict";

const { test, after, before } = require("node:test");
const assert                  = require("node:assert/strict");
const { spawn }               = require("node:child_process");
const fs                      = require("node:fs");
const path                    = require("node:path");
const os                      = require("node:os");

global.window = global;
require("../../packages/engine/rotation-engine.js");
require("../../packages/demo-roster/demo-roster.js");

const RE     = window.RotationEngine;
const ROSTER = window.DEMO_ROSTER;

const PORT      = 8710 + Math.floor(Math.random() * 200);
const HOST      = "127.0.0.1";
const BASE      = `http://${HOST}:${PORT}`;
const scratch   = fs.mkdtempSync(path.join(os.tmpdir(), "rigs-server-"));
const stateFile = path.join(scratch, "state.json");

let proc;

before(async () => {
  proc = spawn(process.execPath, [path.join(__dirname, "server.js")], {
    env: { ...process.env, PORT: String(PORT), HOST, STATE_FILE: stateFile },
    stdio: ["ignore", "pipe", "pipe"],
  });
  // Wait until we can reach /api/state, up to 3s.
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(BASE + "/api/state");
      if (r.ok) return;
    } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 50));
  }
  throw new Error("server did not start on " + BASE);
});

after(() => { if (proc) proc.kill(); });

test("empty server serves 404 for a rig with no push", async () => {
  const r = await fetch(BASE + "/api/rigs/RIG-01/schedule.json");
  assert.equal(r.status, 404);
});

test("push accepts a full floor and each rig can read its own", async () => {
  const cfg      = Object.assign({ date: "2026-08-22" }, ROSTER.defaults);
  const plan     = RE.buildPlan(cfg, ROSTER.groups);
  const payloads = ROSTER.allRigs.map(r => RE.rigPayload(plan, r));

  const push = await fetch(BASE + "/api/push", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body:    JSON.stringify({ payloads }),
  });
  assert.equal(push.status, 200);
  const body = await push.json();
  assert.equal(body.ok, true);
  assert.equal(body.count, ROSTER.allRigs.length);

  for (const rigId of ROSTER.allRigs) {
    const r = await fetch(BASE + "/api/rigs/" + rigId + "/schedule.json");
    assert.equal(r.status, 200);
    const p = await r.json();
    assert.equal(p.rigId, rigId);
    assert.ok(Array.isArray(p.turns) && p.turns.length > 0);
  }
});

test("push rejects a malformed payload without touching the floor", async () => {
  const bad = { rigId: "RIG-XX" };   // missing everything else
  const r = await fetch(BASE + "/api/push", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body:    JSON.stringify({ payloads: [bad] }),
  });
  assert.equal(r.status, 422);
  const body = await r.json();
  assert.ok(body.problems && body.problems.length === 1);
});

/* ------------------------------------------------- a push carries the day

   The desk pushes every shift of the day, not just the one on screen,
   because a payload covers one shift and a rig holding a finished one
   drops to Standby at the boundary.

   The server kept ONE payload per rig, so 36 payloads collapsed into 12
   slots and the last written silently won. At half four in the afternoon
   the whole floor - and the desk's own Live view - was being handed the
   Night shift. Every payload was valid; it was simply the wrong one, so
   nothing reported an error anywhere. */

function wholeDay(date) {
  const out = [];
  RE.SHIFTS.forEach(sh => {
    const cfg  = Object.assign({}, ROSTER.defaults, { date, shift: sh.id });
    const plan = RE.buildPlan(cfg, ROSTER.groups);
    ROSTER.allRigs.forEach(r => { const one = RE.rigPayload(plan, r); if (one) out.push(one); });
  });
  return out;
}

const today = () => new Date().toLocaleDateString("en-CA");

test("a push may carry every shift of the day", async () => {
  const payloads = wholeDay(today());
  assert.equal(payloads.length, ROSTER.allRigs.length * RE.SHIFTS.length);

  const push = await fetch(BASE + "/api/push", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body:    JSON.stringify({ payloads }),
  });
  assert.equal(push.status, 200);
  assert.equal((await push.json()).count, payloads.length,
    "the server reported storing fewer than it was sent");
});

test("every rig is served the shift that is actually running", async () => {
  /* Time-independent, deliberately: rather than asserting a label that
     depends on when the suite runs, it asserts the served payload's own
     window contains now. That is exactly what was false before - Night
     served in the afternoon - and it holds at any hour of the day. */
  await fetch(BASE + "/api/push", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body:    JSON.stringify({ payloads: wholeDay(today()) }),
  });

  const now = Date.now();
  for (const rigId of ROSTER.allRigs) {
    const p = await (await fetch(BASE + "/api/rigs/" + rigId + "/schedule.json")).json();
    const w = RE.shiftWindow(p);
    assert.ok(w, rigId + " was served a payload with no window");
    assert.ok(now >= w.start && now < w.end,
      rigId + " was served the " + p.shift.label + " shift (" + p.shift.start + "-" +
      p.shift.end + "), which is not running at " +
      new Date(now).toISOString() + " - the floor is on the wrong schedule");
  }
});

test("the shift served is one of the three that were pushed", async () => {
  const p = await (await fetch(BASE + "/api/rigs/" + ROSTER.allRigs[0] + "/schedule.json")).json();
  assert.ok(RE.SHIFTS.some(s => s.label === p.shift.label), "unknown shift " + p.shift.label);
  assert.ok(Array.isArray(p.turns) && p.turns.length > 0, "the served shift has no turns");
});

test("a rig still gets its own schedule and nobody else's", async () => {
  for (const rigId of ROSTER.allRigs) {
    const p = await (await fetch(BASE + "/api/rigs/" + rigId + "/schedule.json")).json();
    assert.equal(p.rigId, rigId,
      "every episode from " + rigId + " would be filed under " + p.rigId);
  }
});
