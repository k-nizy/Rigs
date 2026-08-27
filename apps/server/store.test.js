/* ====================================================================
 * How the floor's state survives - a crash, a bad write, and a push
 * that replaced the wrong day.
 *
 * `state.json` is the only record of what the desk told the floor, and
 * it is written the way a scratch file is written:
 *
 *     fs.writeFileSync(STATE, JSON.stringify(store, null, 2));
 *
 * Two things follow from that, and they are separate bugs.
 *
 * The write is not atomic. `writeFileSync` truncates the file to zero
 * before it writes a byte, so a crash, a kill signal or a full disk in
 * that window leaves a partial file - and the partial file is the only
 * copy. It is read back at boot with `JSON.parse`, which throws into a
 * `catch` that logs to stderr and *carries on with an empty store*. A
 * server nobody is watching then answers 404 for all twelve rigs, and
 * twelve rigs drop to Standby having been told nothing is scheduled.
 * That is indistinguishable, from the floor, from a manager who forgot
 * to push. Nothing raises its hand.
 *
 * And the store is replaced whole on every push - `store = next` - with
 * no record of what it replaced. So "the desk pushed the wrong roster
 * at 06:00, what was the floor running before that?" has no answer, and
 * neither does "put it back". The backend half of this system is built
 * on exactly the opposite instinct: the ledger is the system, every
 * other table is derived from it, and replay is the property the design
 * is arranged around. The push store is the one piece of state here
 * that is a mutable cell rather than a log.
 *
 * The fence at the bottom of this file matters as much as the fixes. A
 * push log gives every push an identity, and identity is a door this
 * design keeps shut on purpose: the rig resolves its schedule by the
 * window the desk wrote and by nothing else. If a push id ever reaches
 * the rig, there are two ways to answer "which schedule is real", and
 * the founding invariant is that there is exactly one.
 * ==================================================================== */

"use strict";

const { test }   = require("node:test");
const assert     = require("node:assert/strict");
const { spawn }  = require("node:child_process");
const fs         = require("node:fs");
const path       = require("node:path");
const os         = require("node:os");

global.window = global;
require("../../packages/engine/rotation-engine.js");
require("../../packages/demo-roster/demo-roster.js");

const RE     = window.RotationEngine;
const ROSTER = window.DEMO_ROSTER;

const SERVER = path.join(__dirname, "server.js");
const HOST   = "127.0.0.1";

let nextPort = 8950 + Math.floor(Math.random() * 150);

/* Each test gets its own scratch directory, its own port and its own
   process, because these are tests about what is on disk at boot. */
function scratchDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "rigs-store-"));
}

function spawnServer(stateFile, port) {
  return spawn(process.execPath, [SERVER], {
    env: { ...process.env, PORT: String(port), HOST, STATE_FILE: stateFile },
    stdio: ["ignore", "pipe", "pipe"],
  });
}

/* Resolves { ok: true } once the server answers, or { ok: false, code,
   stderr } if it exits first. Both are legitimate outcomes here - one
   of these tests is about a server that should decline to come up. */
function settle(proc, base, ms = 4000) {
  return new Promise(resolve => {
    let stderr = "", done = false;
    const finish = v => { if (!done) { done = true; resolve(v); } };

    proc.stderr.on("data", d => { stderr += d; });
    proc.on("exit", code => finish({ ok: false, code, stderr }));

    const deadline = Date.now() + ms;
    (async function poll() {
      while (!done && Date.now() < deadline) {
        try { if ((await fetch(base + "/api/state")).ok) return finish({ ok: true, stderr }); }
        catch { /* not up yet */ }
        await new Promise(r => setTimeout(r, 50));
      }
      finish({ ok: false, code: null, stderr, timedOut: true });
    })();
  });
}

const today = () => new Date().toLocaleDateString("en-CA");

function wholeDay(date, roster) {
  const groups = roster || ROSTER.groups;
  const out = [];
  RE.SHIFTS.forEach(sh => {
    const cfg  = Object.assign({}, ROSTER.defaults, { date, shift: sh.id });
    const plan = RE.buildPlan(cfg, groups);
    ROSTER.allRigs.forEach(r => { const one = RE.rigPayload(plan, r); if (one) out.push(one); });
  });
  return out;
}

/* A second roster that differs in a way a person would notice on the
   floor: group A is running a different task. Used to tell "the push
   that is live" apart from "the push it replaced". */
function otherRoster() {
  const groups = JSON.parse(JSON.stringify(ROSTER.groups));
  groups[0].task = "Sorting - loose parts to trays";
  return groups;
}

async function push(base, payloads) {
  const r = await fetch(base + "/api/push", {
    method:  "POST",
    headers: { "content-type": "application/json" },
    body:    JSON.stringify({ payloads }),
  });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}

/* ------------------------------------------------------------------ 1 */

test("a half-written state file does not come up as an empty floor", async () => {
  const dir   = scratchDir();
  const state = path.join(dir, "state.json");

  /* Exactly what a kill signal partway through writeFileSync leaves:
     valid JSON up to the point the process died, and nothing after. */
  const port = nextPort++;
  const base = `http://${HOST}:${port}`;
  const good = JSON.stringify({ pushedAt: new Date().toISOString(), rigs: {} }, null, 2);
  fs.writeFileSync(state, good.slice(0, Math.floor(good.length / 2)));

  const proc = spawnServer(state, port);
  const out  = await settle(proc, base);
  proc.kill();

  assert.equal(out.ok, false,
    "the server came up on a corrupt state file. Every rig will be told " +
    "nothing is scheduled, which on the floor is indistinguishable from " +
    "a manager who forgot to push - refuse to start instead");
  assert.notEqual(out.code, 0, "it should exit non-zero, not quietly succeed");
  assert.match(out.stderr, /state/i, "stderr should name the file that is unreadable");
});

/* ------------------------------------------------------------------ 2 */

test("a push that replaces the floor keeps what it replaced", async () => {
  const dir   = scratchDir();
  const state = path.join(dir, "state.json");
  const log   = path.join(dir, "pushes.jsonl");
  const port  = nextPort++;
  const base  = `http://${HOST}:${port}`;

  const proc = spawnServer(state, port);
  assert.ok((await settle(proc, base)).ok, "server did not start");

  const first  = wholeDay(today());
  const second = wholeDay(today(), otherRoster());

  assert.equal((await push(base, first)).status, 200);
  assert.equal((await push(base, second)).status, 200);
  proc.kill();

  assert.ok(fs.existsSync(log),
    "there is no record of the superseded push. `store = next` replaces " +
    "the floor whole, so 'the desk pushed the wrong roster, put it back' " +
    "has no answer");

  const lines = fs.readFileSync(log, "utf8").trim().split("\n").filter(Boolean);
  assert.equal(lines.length, 2, "one line per accepted push, oldest first");

  const entries = lines.map(l => JSON.parse(l));
  entries.forEach((e, i) => {
    assert.ok(e.pushedAt, "entry " + i + " has no pushedAt");
    assert.ok(Array.isArray(e.payloads) && e.payloads.length === first.length,
      "entry " + i + " does not carry the payloads it accepted");
  });

  /* The point of keeping it: the replaced roster is still readable, in
     full, and is the one the floor was running before. */
  const wasLive = entries[0].payloads.find(p => p.rigId === ROSTER.allRigs[0]);
  const isLive  = entries[1].payloads.find(p => p.rigId === ROSTER.allRigs[0]);
  assert.notEqual(wasLive.task, isLive.task,
    "the two pushes should be distinguishable - the fixture is wrong");
  assert.equal(wasLive.task, first[0].task, "the superseded push did not survive intact");
});

/* ------------------------------------------------------------------ 3 */

test("the floor rebuilds from the log when state.json is lost", async () => {
  const dir   = scratchDir();
  const state = path.join(dir, "state.json");
  const port  = nextPort++;
  const base  = `http://${HOST}:${port}`;

  const one = spawnServer(state, port);
  assert.ok((await settle(one, base)).ok, "server did not start");
  assert.equal((await push(base, wholeDay(today()))).status, 200);
  one.kill();
  await new Promise(r => setTimeout(r, 200));

  /* state.json is a cache of the last line of the log, not the record
     itself. Losing it should cost nothing. */
  fs.rmSync(state, { force: true });

  const port2 = nextPort++;
  const base2 = `http://${HOST}:${port2}`;
  const two   = spawnServer(state, port2);
  const out   = await settle(two, base2);
  assert.ok(out.ok, "server did not restart: " + out.stderr);

  const st = await (await fetch(base2 + "/api/state")).json();
  two.kill();

  assert.equal(st.rigs.length, ROSTER.allRigs.length,
    "the floor did not come back. state.json is holding the only copy, so " +
    "deleting it loses the schedule for all twelve rigs");
});

/* ------------------------------------------------------ the fence

   These two must pass today and must still pass afterwards. A push log
   is history for people and for recovery; it is not a new input to the
   thing that decides which schedule a rig runs. That decision stays
   where it is - the window the desk wrote, compared against now. */

test("a rig is still handed a plain payload, with no push identity on it", async () => {
  const dir   = scratchDir();
  const state = path.join(dir, "state.json");
  const port  = nextPort++;
  const base  = `http://${HOST}:${port}`;

  const proc = spawnServer(state, port);
  assert.ok((await settle(proc, base)).ok, "server did not start");
  assert.equal((await push(base, wholeDay(today()))).status, 200);

  const served = await (await fetch(base + "/api/rigs/" + ROSTER.allRigs[0] + "/schedule.json")).json();
  proc.kill();

  const { validate } = require("../../packages/schema/payload.js");
  assert.equal(validate(served).ok, true, "the served payload no longer validates");

  for (const key of ["pushId", "pushedAt", "pushIndex", "generation"]) {
    assert.equal(key in served, false,
      "the rig was handed `" + key + "`. Once a rig can name a push, there " +
      "are two ways to answer which schedule is real, and the invariant is " +
      "that there is exactly one");
  }
});

test("which shift is served still depends on the clock, not on push order", async () => {
  const dir   = scratchDir();
  const state = path.join(dir, "state.json");
  const port  = nextPort++;
  const base  = `http://${HOST}:${port}`;

  const proc = spawnServer(state, port);
  assert.ok((await settle(proc, base)).ok, "server did not start");
  assert.equal((await push(base, wholeDay(today()))).status, 200);

  const now = Date.now();
  for (const rigId of ROSTER.allRigs) {
    const p = await (await fetch(base + "/api/rigs/" + rigId + "/schedule.json")).json();
    const w = RE.shiftWindow(p);
    assert.ok(w && now >= w.start && now < w.end,
      rigId + " was served the " + p.shift.label + " shift, which is not running now");
  }
  proc.kill();
});
