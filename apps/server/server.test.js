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

/* ====================================================================
 * What must not be served
 *
 * This server used to hand out the whole checkout. It only refused paths
 * that escaped the repository, so everything inside it was fair game -
 * including `backend/.env`, which carries the database password, and
 * `.git/config`. `deploy/nginx.conf` says nothing under `backend/` should
 * be served and this contradicted it, and the seven tests here only ever
 * checked what *is* served.
 *
 * It binds to 127.0.0.1 by default, so it took `HOST=0.0.0.0` for it to
 * matter. That is the obvious thing to type to show somebody the demo on
 * the office network.
 * ==================================================================== */

const forbidden = async (p) => (await fetch(BASE + p)).status;

test("the repository is not a web root", async () => {
  for (const p of ["/backend/.env",
                   "/backend/.env.example",
                   "/.git/config",
                   "/backend/core/workflows/floor.py",
                   "/apps/server/state.json",
                   "/package.json",
                   "/CLAUDE.md"]) {
    assert.equal(await forbidden(p), 404, p + " was served");
  }
});

test("a traversal back through an allowed folder is not a way in", async () => {
  /* The reason the check runs after the path is resolved rather than on
     what arrived: this starts with an allowed folder and lands two
     directories away from it. */
  for (const p of ["/apps/../backend/.env",
                   "/packages/../backend/.env",
                   "/apps/rig/../../backend/.env",
                   "/apps/%2e%2e/backend/.env"]) {
    const status = await forbidden(p);
    assert.ok(status === 404 || status === 403, p + " returned " + status);
  }
});

test("nothing outside the repository is reachable either", async () => {
  for (const p of ["/../../../Windows/win.ini", "/../../etc/passwd"]) {
    const status = await forbidden(p);
    assert.ok(status === 404 || status === 403, p + " returned " + status);
  }
});

test("the three folders the apps actually need are still served", async () => {
  for (const p of ["/",
                   "/rotation-desk-v1/",
                   "/apps/rig/",
                   "/packages/engine/rotation-engine.js",
                   "/packages/brand/brand.css",
                   "/apps/rig/assets/rig.js",
                   "/rotation-desk-v1/assets/desk.css"]) {
    const r = await fetch(BASE + p);
    assert.equal(r.status, 200, p + " should be served but returned " + r.status);
  }
});

test("the landing page is served at the root and nowhere else", async () => {
  /* nginx has `location = /` for the landing page and `location /` returning
     404, so /index.html is not a URL there. Matching it here is the point:
     a dev server that serves more than the deployment does is a dev server
     that hides the difference. */
  const root = await fetch(BASE + "/");
  assert.equal(root.status, 200);
  assert.match(await root.text(), /Teleop Stations/);
  assert.equal(await forbidden("/index.html"), 404);
});

test("what is served matches the folders nginx names", async () => {
  /* One list in two places. If they drift, the deployment serves
     something the dev server does not, or the other way round, and the
     first anybody knows is on a floor.

     This checks the folders nginx serves. The one path it must *not*
     serve - rig-config.js, which has to reach the service or every rig
     is the same rig - is checked in backend/tests/test_identity.py,
     next to the rule it protects. */
  const conf = fs.readFileSync(
    path.join(__dirname, "../../deploy/nginx.conf"), "utf8");
  for (const folder of ["/apps/rig/", "/packages/", "/rotation-desk-v1/"]) {
    assert.ok(conf.includes("location " + folder),
      "nginx.conf no longer serves " + folder + ", but this server does");
  }
  assert.ok(conf.includes("location / { return 404; }"),
    "nginx.conf stopped refusing everything else");
});

// ------------------------------------------------- the roster, server-side

/* The assignment rides on the push. The demo floor keeps it in the log
   line and serves it back, so a desk against this server reads who is
   on the floor the same way it does against the real one. */
const aFloor = () => {
  const plan = RE.buildPlan(Object.assign({ date: "2026-08-22" }, ROSTER.defaults), ROSTER.groups);
  return ROSTER.allRigs.map(r => RE.rigPayload(plan, r));
};
const postPush = body => fetch(BASE + "/api/push", {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
});

test("a push may carry the roster, and the server serves the latest one back", async () => {
  const roster = [{ key: "A", task: "Box transfer", rigs: ["RIG-01", "RIG-02", "RIG-03"],
                    ops: ["Mei Chen", "Ben Carter", "Tomas Rivera", "Nadia Haddad"] }];
  const r = await postPush({ payloads: aFloor(), roster });
  assert.equal(r.status, 200, await r.text());
  const got = await fetch(BASE + "/api/roster");
  assert.equal(got.status, 200);
  const body = await got.json();
  assert.deepEqual(body.roster, roster);
  assert.ok(body.pushedAt);
});

test("a push without a roster still serves no roster, not an error", async () => {
  const r = await postPush({ payloads: aFloor() });
  assert.equal(r.status, 200, await r.text());
  const body = await (await fetch(BASE + "/api/roster")).json();
  assert.equal(body.roster, null);
  assert.ok(body.pushedAt, "a floor that has been pushed still says when");
});

test("a roster that is not a list of groups is refused whole", async () => {
  const before = await (await fetch(BASE + "/api/state")).json();
  const r = await postPush({ payloads: aFloor(), roster: { not: "a list" } });
  assert.equal(r.status, 422);
  assert.deepEqual(await (await fetch(BASE + "/api/state")).json(), before,
    "a refused push touched the floor");
});
