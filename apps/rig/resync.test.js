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

/* ------------------------------------------- the point of the whole thing

   A manager changes who is on a rig and presses push. The rig picks it up
   on its own within five minutes. From that moment the person actually
   sitting there is the person the schedule now names - so every take they
   record has to be filed under THEM, and land in the backend under them.

   Everything above tests what the rig refuses to do. This tests what it is
   FOR. Nothing asserted it until now: the tests checked that the new name
   reached the screen, and that already-filed work was left alone, but not
   that the next take actually belongs to the new operator. */

test("after a correction, the next take is filed under the new operator",
  withRig({ search: "?demo" }, async (rig) => {
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();

    // One take under the roster as originally pushed.
    rig.press(2); rig.frames(1);
    rig.press(2); rig.frames(4);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);
    const before = rig.eventsOf("episode_saved")[0];
    assert.ok(before, "no take was filed before the correction");
    assert.notEqual(before.operatorId, "op-zz", "sanity: the roster starts uncorrected");

    // The desk corrects the roster; the rig picks it up without a reload.
    server.payload = renamed(server.payload, "Corrected Person");
    await rig.resync();

    // Back into a take.
    for (let i = 0; i < 5 && rig.screen() !== "recording"; i++) { rig.press(2); rig.frames(3); }
    assert.equal(rig.screen(), "recording", "could not start a second take");
    rig.frames(10);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    const takes = rig.eventsOf("episode_saved");
    assert.equal(takes.length, 2, "expected a second take after the correction");
    assert.equal(takes[1].operatorId, "op-zz",
      "the new take was filed under " + takes[1].operatorId + ", but the corrected "
      + "schedule says op-zz is the one on this rig");
    assert.equal(takes[0].operatorId, before.operatorId,
      "the correction reached back and rewrote a take that was already filed");
  }));

test("the new operator owns the stint, and the numbers that go with it",
  withRig({ search: "?demo" }, async (rig) => {
    /* Efficiency is computed per operator from four seconds columns. If the
       stint is filed against the wrong person both operators are wrong: one
       loses credit, the other gains work they never did. */
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();

    server.payload = renamed(server.payload, "Corrected Person");
    await rig.resync();

    for (let i = 0; i < 5 && rig.screen() !== "recording"; i++) { rig.press(2); rig.frames(3); }
    assert.equal(rig.screen(), "recording", "could not start a take");
    rig.frames(10);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    for (const e of rig.eventsOf("episode_saved")) {
      assert.equal(e.operatorId, "op-zz", "a take escaped the correction");
      assert.ok(e.data.episodeId, "a take with no id cannot be joined to its video");
    }
  }));

test("what the backend receives names the new operator too",
  withRig({ search: "?demo" }, async (rig) => {
    /* The rig can be right on screen and wrong in the envelope - that was
       exactly the stint bug. So this asserts the thing that is actually
       uploaded, not the thing that is displayed. */
    const server = serving(payloadFor("RIG-03"));
    await rig.settle();

    server.payload = renamed(server.payload, "Corrected Person");
    await rig.resync();

    for (let i = 0; i < 5 && rig.screen() !== "recording"; i++) { rig.press(2); rig.frames(3); }
    assert.equal(rig.screen(), "recording", "could not start a take");
    rig.frames(10);
    rig.press(3); rig.frames(1);
    rig.press(2); rig.frames(1);

    const filed = rig.events().filter(e => e.bucket === "episodes");
    assert.ok(filed.length, "nothing was filed for upload");
    for (const e of filed) {
      assert.equal(e.operatorId, "op-zz",
        "the envelope bound for the backend still names " + e.operatorId);
      assert.ok(e.rigId && e.shiftDate && e.shiftLabel && e.turnFrom,
        "an episode reached the upload queue without enough to file it against");
    }
  }));

/* ------------------------------------- a schedule that has run out

   Found live, at half past midnight, on a floor where only the previous
   day had been pushed. The rig was handed the Night sheet dated
   YESTERDAY and ran it: `whoIsOn` matches on the time of day alone, so
   00:30 fell inside a turn that had actually been worked twenty-four
   hours earlier. The screen named a real operator, the pedals worked,
   and every take would have been filed against the wrong shift on the
   wrong day.

   A rig with no schedule must look like a rig with no schedule. */

const LIVE = { search: "" };          // no ?demo: the deployed clock

function payloadDated(date, shift) {
  const plan = RE.buildPlan(
    Object.assign({}, ROSTER.defaults, { date, shift: shift || "morning" }),
    ROSTER.groups);
  return RE.rigPayload(plan, "RIG-03");
}

/* The rig reads its schedule while it BOOTS, so the payload has to be in
   place before it mounts. The tests above set up a server afterwards on
   purpose, because they are about picking a new schedule up later; these
   are about what the rig does with the one it starts holding. */
function withHeld(payload, opts, fn) {
  return async () => {
    const rig = await mountRig(Object.assign({ pushed: payload }, opts));
    try { await fn(rig); } finally { rig.stop(); }
  };
}

test("a sheet for today runs normally",
  withHeld(payloadDated("2026-08-23", "morning"), LIVE, async (rig) => {
    /* The control. The harness clock sits at 10:37 on 2026-08-23, inside
       that day's Morning shift. Without this, the two tests below would
       pass on a rig that was broken in some other way. */
    await rig.settle();
    assert.notEqual(rig.screen(), "standby",
      "a schedule that covers right now was treated as expired");
  }));

test("a sheet from yesterday does not put somebody on the rig",
  withHeld(payloadDated("2026-08-22", "morning"), LIVE, async (rig) => {
    await rig.settle();
    assert.equal(rig.screen(), "standby",
      "the rig ran a schedule dated yesterday, so every take would be "
      + "filed against a shift that already happened");
  }));

test("last night's sheet does not claim somebody is on tonight",
  withHeld(payloadDated("2026-08-22", "night"),
             Object.assign({ at: "00:30" }, LIVE), async (rig) => {
    /* The exact shape of the live failure: a Night sheet whose turns span
       00:00-08:00, read at 00:30 the FOLLOWING night. */
    await rig.settle();
    assert.equal(rig.screen(), "standby",
      "at 00:30 the rig picked up a turn from the previous night's sheet");
  }));

test("an expired schedule cannot be driven into a take",
  withHeld(payloadDated("2026-08-22", "morning"), LIVE, async (rig) => {
    /* Standby still offers the pre-shift check - a technician sweeping the
       floor at 07:40 should not have to wait for 08:00. So the rig does
       move off standby when the pedal is pressed. What it must not do is
       carry on into a take, because there is no valid schedule to file one
       against. */
    await rig.settle();
    assert.equal(rig.screen(), "standby", "an expired sheet put the rig to work");

    rig.press(2); rig.frames(2);
    assert.equal(rig.screen(), "checklist", "standby should still allow a check");

    rig.press(2); rig.frames(2);
    assert.equal(rig.screen(), "standby",
      "the check led into a shift, on a schedule that expired yesterday");

    // Lean on it. No sequence of presses may reach a recording.
    for (let i = 0; i < 12; i++) { rig.press((i % 3) + 1); rig.frames(2); }
    assert.notEqual(rig.screen(), "recording",
      "the rig was driven into a take with no schedule covering now");

    const takes = rig.events().filter(e => e.bucket === "episodes");
    assert.deepEqual(takes, [],
      takes.length + " takes were filed against " + (takes[0] || {}).shiftDate +
      ", a shift that was over before this rig booted");
  }));

test("a rig with nothing to run asks for a schedule more often than one that is working", () => {
  /* Arithmetic rather than behaviour, but it is what makes "stay put"
     bearable: a stranded rig recovers seconds after somebody pushes,
     instead of sitting idle for a whole polling interval. */
  const src = require("node:fs").readFileSync(
    require("node:path").resolve(__dirname, "assets/rig.js"), "utf8");
  const idle = Number(/RESYNC_IDLE_MS\s*=\s*(\d+)/.exec(src)[1]);
  const hungry = Number(/RESYNC_HUNGRY_MS\s*=\s*(\d+)/.exec(src)[1]);
  assert.ok(hungry < idle, "a stranded rig should ask more often, not less");
  assert.ok(idle <= 60000,
    "a corrected roster should reach the floor inside a minute, not " + (idle / 1000) + "s");
});
