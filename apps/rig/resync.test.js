/* =====================================================================
 * resync.test.js  -  a rig that keeps itself in step
 *
 * A payload covers one shift. The rig read its schedule once at boot and
 * never again, so at 16:00 the Morning payload it was still holding ran
 * out, whoIsOn() found nothing, and it dropped to Standby. Twelve rigs,
 * three times a day, until somebody walked round reloading browsers.
 * That was blocker 2, and it is the reason this file exists.
 *
 * What is being tested is mostly what the rig REFUSES to do. Picking up
 * a new schedule is easy; picking one up at the wrong moment costs an
 * operator their take, or strands them on a dead screen mid-shift.
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

function payloadFor(rigId, shift, patch) {
  const plan = RE.buildPlan(
    Object.assign({}, ROSTER.defaults, { date: "2026-08-23", shift: shift || "morning" }),
    ROSTER.groups);
  return Object.assign(RE.rigPayload(plan, rigId), patch || {});
}

/* A server that hands back whatever it is currently holding. */
function serving(payload) {
  const state = { payload, asked: 0 };
  global.fetch = async (url) => {
    const u = String(url);
    if (u.includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    if (u.includes("/schedule")) {
      state.asked++;
      if (!state.payload) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => state.payload };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  return state;
}

const renamed = (p, name) => {
  const copy = JSON.parse(JSON.stringify(p));
  copy.turns.forEach((t) => { t.operator = { id: "op-zz", name }; });
  return copy;
};

// ------------------------------------------------------ it picks one up

test("a corrected roster reaches a rig without anybody reloading it",
  withRig({ search: "?demo" }, async (rig) => {
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();

    server.payload = renamed(server.payload, "Corrected Person");
    await rig.resync();

    assert.match(rig.rail().next + " " + rig.log().join(" "), /Corrected Person/,
      "the new roster never reached the screen");
  }));

test("the operator is told, rather than the name changing by itself",
  withRig({ search: "?demo" }, async (rig) => {
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();

    server.payload = renamed(server.payload, "Corrected Person");
    await rig.resync();

    assert.ok(rig.log().some((l) => l.includes("Schedule updated")),
      "the schedule changed under somebody and the screen said nothing");
  }));

test("the same schedule again changes nothing",
  withRig({ search: "?demo" }, async (rig) => {
    serving(payloadFor("RIG-03"));
    await rig.settle();
    const before = rig.log().length;

    await rig.resync();
    await rig.resync();

    assert.equal(rig.log().length, before,
      "an unchanged schedule was announced as an update");
  }));

// --------------------------------------------------- what it refuses to do

test("it does not swap a schedule out from under a take",
  withRig({ search: "?demo" }, async (rig) => {
    /* The rig already refuses to cut a recording at a turn boundary. A
       schedule arriving mid-take is the same situation: swapping would
       file the take under whoever the new payload says is on. */
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();

    rig.press(2); rig.frames(1);          // check passed
    rig.press(2); rig.frames(4);          // recording
    assert.equal(rig.screen(), "recording");

    server.payload = renamed(server.payload, "Should Not Appear");
    await rig.resync();

    assert.equal(rig.screen(), "recording", "the take was interrupted");
    assert.ok(!rig.log().some((l) => l.includes("Should Not Appear")),
      "the schedule was swapped mid-take");
  }));

test("it does not strand a working operator on a dead screen",
  withRig({ search: "?demo" }, async (rig) => {
    /* The groups were restructured, or this rig was dropped while
       somebody fixed something. Applying that mid-shift would put a
       working operator on Standby. A stale schedule that works beats a
       fresh one that stops the rig. */
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();
    rig.press(2); rig.frames(1);
    const screenBefore = rig.screen();

    const empty = JSON.parse(JSON.stringify(server.payload));
    empty.turns = [];
    server.payload = empty;
    await rig.resync();

    assert.equal(rig.screen(), screenBefore, "the operator was stranded");
    assert.ok(rig.log().some((l) => l.includes("keeping the current one")),
      "it held the schedule but did not say why");
  }));

test("it never takes another rig's schedule",
  withRig({ search: "?demo" }, async (rig) => {
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();
    const mine = rig.rail().rig;

    server.payload = payloadFor("RIG-07");
    await rig.resync();

    assert.equal(rig.rail().rig, mine,
      "the rig took on another rig's identity, so every episode from here "
      + "would be filed under the wrong machine");
  }));

test("a server that cannot be reached leaves the rig running",
  withRig({ search: "?demo" }, async (rig) => {
    serving(payloadFor("RIG-03"));
    await rig.settle();
    rig.press(2); rig.frames(1);
    const before = rig.screen();

    global.fetch = async () => { throw new Error("no network"); };
    await rig.resync();

    assert.equal(rig.screen(), before, "a failed fetch disturbed the rig");
    assert.deepEqual(rig.errors, []);
  }));

// ------------------------------------------------- it is not a restart

test("picking up a schedule does not reset the rig",
  withRig({ search: "?demo" }, async (rig) => {
    /* boot() resets the state machine, clears the drawer log and files a
       fresh shift check. It is what the demo's rig switcher calls, and
       wiring it here would wipe the screen under somebody mid-shift. */
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();

    rig.press(2); rig.frames(1);
    rig.press(2); rig.frames(4);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);          // one take filed
    const takesBefore = rig.eventsOf("episode_saved").length;
    const checksBefore = rig.eventsOf("shift_check").length;

    server.payload = renamed(server.payload, "Corrected Person");
    await rig.resync();

    assert.equal(rig.eventsOf("episode_saved").length, takesBefore,
      "the take was lost");
    assert.equal(rig.eventsOf("shift_check").length, checksBefore,
      "it filed a fresh shift check, so it restarted rather than updated");
    assert.notEqual(rig.screen(), "checklist", "the rig went back to boot");
  }));

test("work already filed keeps the operator it was recorded under",
  withRig({ search: "?demo" }, async (rig) => {
    /* Append-only. A correction changes who owns work from now on, never
       who owned it then. */
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();

    rig.press(2); rig.frames(1);
    rig.press(2); rig.frames(4);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);
    const before = rig.eventsOf("episode_saved")[0];

    server.payload = renamed(server.payload, "Corrected Person");
    await rig.resync();

    const after = rig.eventsOf("episode_saved")[0];
    assert.equal(after.operatorId, before.operatorId,
      "a schedule correction rewrote who recorded a take that was already filed");
  }));
