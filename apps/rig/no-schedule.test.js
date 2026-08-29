/* =====================================================================
 * no-schedule.test.js  -  a schedule nobody pushed is not a schedule
 *
 * The rig runs what the desk pushed it. That is the founding invariant
 * pointed at one machine, and it is why the rig has no login, no task
 * picker, and does not choose which rig it is: if the desk decided it,
 * the rig obeys it, and if the desk did not decide it, the rig does not
 * invent it.
 *
 * `loadPayload()` broke that at the last step. Server, then a co-located
 * `schedule.json`, and then - if neither answers - a schedule *generated
 * on the spot from `packages/demo-roster`*. The names in that file are
 * demonstration data. Nobody on a real floor is called Aleksandr Petrov.
 *
 * The window check does not catch it, and that is the trap. An expired
 * sheet is refused because `coversAt()` says its window has closed; a
 * generated sheet is stamped with today and therefore always covers now.
 * The guard that stops a rig running yesterday's real schedule waves
 * through a fabricated one.
 *
 * What follows is a rig recording against it: three envelopes queued for
 * upload, journalled to disk, carrying `operatorId: op-a3`. The uploader
 * posts them the moment the service returns, the ledger is append-only,
 * and there is no correction mechanism. The one thing on screen saying
 * anything is a line of small text reading "schedule not pushed", and a
 * line of text is not a refusal.
 *
 * It needs a total outage less than you would think. `rig-config.js` is
 * a static file and the schedule is an API call, so a service restarting
 * behind a web server that is still up - which is what every deployment
 * looks like from the rig - hits exactly this window, with the identity
 * check already satisfied and skipped.
 *
 * Same trade as Standby, and the same answer: idle is loud, cheap and
 * recoverable; work filed under somebody who was never there is silent,
 * permanent and poisons the training data.
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

/* A machine that knows which rig it is - the service served it
   `rig-config.js` - and cannot get an answer about what to run. Nothing
   is pushed, there is no `schedule.json`, and every fetch fails. */
const OUTAGE = { search: "", at: "10:37", rigId: "RIG-03" };

/* Drive the whole loop as an operator would: pass the check, start,
   save, score. On a rig with nothing scheduled none of it should reach
   a take. */
const workATake = (rig) => {
  rig.press(2); rig.frames(2);
  rig.press(2); rig.frames(5);
  rig.press(3); rig.frames(1);
  rig.press(2); rig.frames(2);
};

// ------------------------------------------------ it does not invent one

test("a rig that cannot reach the service does not invent a schedule",
  withRig(OUTAGE, async (rig) => {
    rig.frames(3);
    assert.equal(rig.screen(), "standby",
      "the rig generated a roster out of the demo file and opened the shift check on it");
  }));

test("and nothing it shows names an operator, or a task, that nobody set",
  withRig(OUTAGE, async (rig) => {
    rig.frames(3);
    const rail = rig.rail();
    const demoNames = ROSTER.groups.flatMap((g) => g.ops);
    const demoTasks = ROSTER.groups.map((g) => g.task);
    const shown = [rail.next, rail.then, rail.task, rig.stage()].join(" | ");
    assert.ok(!demoNames.some((n) => shown.includes(n)),
      "a demonstration name was in front of an operator on a real floor: " + rail.next);
    assert.ok(!demoTasks.some((t) => shown.includes(t)),
      "and a demonstration task with it: " + rail.task);
  }));

test("no take can be filed against a name nobody scheduled",
  withRig(OUTAGE, async (rig) => {
    rig.frames(3);
    workATake(rig);

    const episodes = rig.events().filter((e) => e.bucket === "episodes");
    assert.deepEqual(episodes.map((e) => e.event), [],
      "the rig recorded and filed a take with no schedule behind it");

    const named = rig.events().filter((e) => e.operatorId || e.turnFrom);
    assert.deepEqual(named.map((e) => e.event + ":" + e.operatorId), [],
      "an envelope was stamped with an operator the desk never sent");
  }));

test("and nothing is queued for the moment the service comes back",
  withRig(OUTAGE, async (rig) => {
    rig.frames(3);
    workATake(rig);
    const held = rig.outbox();
    /* The uploader drains on reconnect, and the ledger is append-only.
       Whatever is in here when the service returns is in the ledger for
       good, so this is the assertion that actually matters. */
    assert.ok(!held.queued || held.queued === rig.events().length,
      "events were queued that are not in the event list - something filed invisibly");
    const fabricated = rig.events().filter((e) => e.operatorId);
    assert.deepEqual(fabricated, [], "fabricated attribution was queued for upload");
  }));

test("the screen says the schedule was never pushed, not that nobody is due",
  withRig(OUTAGE, async (rig) => {
    rig.frames(3);
    const mode = rig.$("rail-mode").textContent || "";
    assert.match(mode, /not pushed/,
      "a rig with no schedule looked exactly like a rig between shifts");
  }));

// ------------------------------------- but it keeps asking, and starts

test("it keeps asking, and goes to work the moment one is pushed",
  withRig(OUTAGE, async (rig) => {
    rig.frames(3);
    assert.equal(rig.screen(), "standby", "sanity");

    /* The service comes back. The rig polls on its own timer; the test
       asks now rather than waiting on it. */
    const plan = RE.buildPlan(
      Object.assign({}, ROSTER.defaults, { date: "2026-08-23", shift: "morning" }),
      ROSTER.groups);
    const pushed = RE.rigPayload(plan, "RIG-03");
    pushed.shift.tz = Intl.DateTimeFormat().resolvedOptions().timeZone;

    global.fetch = async (url) => {
      const u = String(url);
      if (u.includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
      if (u.includes("/schedule")) return { ok: true, status: 200, json: async () => pushed };
      return { ok: true, status: 200, json: async () => ({}) };
    };
    await rig.resync();
    rig.frames(3);

    assert.notEqual(rig.screen(), "standby",
      "a rig with no payload never asked again - it would sit idle until somebody reloaded it");
    assert.equal(rig.rail().rig, "RIG-03");
  }));

// ------------------------------------------------ the demo still runs

/* The line is the one `rig-config.js` already draws, and it is drawn on
   whether the SERVICE identified this machine - not on a URL parameter.
   A laptop, or a static deploy with nothing behind it, sets nothing and
   is a demo. A machine that has been told it is RIG-07 is a rig on a
   floor, and it is the one whose filings will be believed. */

test("a laptop nothing identified still generates its own floor",
  withRig({ search: "", at: "10:37" }, async (rig) => {
    rig.frames(3);
    assert.notEqual(rig.screen(), "standby",
      "the demo has no service by design and has to run without one");
    assert.equal(rig.rail().rig, "RIG-03");
  }));

test("and so does the demo proper",
  withRig({ search: "?demo", at: "10:37" }, async (rig) => {
    rig.frames(3);
    assert.notEqual(rig.screen(), "standby");
  }));
