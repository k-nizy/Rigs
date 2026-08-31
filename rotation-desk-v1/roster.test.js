/* =====================================================================
 * roster.test.js  -  the desk stops treating its own file as the truth
 *
 * Two failures with one shape, and the shape is: a push replaces the
 * whole day on all twelve rigs, and nothing anywhere notices that it
 * replaced somebody else's work.
 *
 *   The roster is a file compiled into the page. An edit to it never
 *   leaves the browser tab it was made in, so the night manager's
 *   correction reaches the floor and no other desk. The next person to
 *   push sends the file's version back over it and the correction is
 *   gone - silently, and with the recordings from then on filed under
 *   whoever the file said.
 *
 *   Two managers editing at once still ends with the later push
 *   winning, even when the second one opened their screen before the
 *   first one pushed.
 *
 * The first is fixed by reading the roster back off the floor, and only
 * ever after proving the recovered roster rebuilds the schedule the
 * floor is actually running. The second is fixed by asking, on the way
 * out, whether the floor has moved since this screen read it.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { mountDesk } = require("./test/dom.js");
const RE = require(path.resolve(__dirname, "../packages/engine/rotation-engine.js"));

function withDesk(opts, fn) {
  return async () => {
    const desk = await mountDesk(opts);
    try { await fn(desk); } finally { desk.stop(); }
  };
}

const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };

/* A floor running names that are in nobody's file. Four groups, because
   the desk's own roster has four and a recovery that quietly dropped
   three of them would otherwise look like a success. */
const FLOOR_GROUPS = [
  { key: "A", task: "Floor task A", rigs: ["RIG-01", "RIG-02", "RIG-03"],
    ops: ["Floor One", "Floor Two", "Floor Three", "Floor Four"] },
  { key: "B", task: "Floor task B", rigs: ["RIG-04", "RIG-05", "RIG-06"],
    ops: ["Floor Five", "Floor Six", "Floor Seven", "Floor Eight"] },
  { key: "C", task: "Floor task C", rigs: ["RIG-07", "RIG-08", "RIG-09"],
    ops: ["Floor Nine", "Floor Ten", "Floor Eleven", "Floor Twelve"] },
  { key: "D", task: "Floor task D", rigs: ["RIG-10", "RIG-11", "RIG-12"],
    ops: ["Floor Thirteen", "Floor Fourteen", "Floor Fifteen", "Floor Sixteen"] },
];

const FLOOR = (() => {
  const p = RE.buildPlan(
    { shift: "morning", date: "2026-08-23", blockMin: 15, stintBlocks: 3, mode: "hold" },
    FLOOR_GROUPS);
  return FLOOR_GROUPS.reduce((all, g) => all.concat(g.rigs.map(r => RE.rigPayload(p, r))), []);
})();

/* A server holding a floor, that records what gets pushed to it. Its
   `pushedAt` is a field rather than a constant so a test can move the
   floor on underneath the desk, which is the whole of the second
   failure. */
function serving(payloads, pushedAt) {
  const state = { pushedAt: pushedAt || "2026-08-23T09:00:00.000Z", sent: null, pushes: 0 };
  state.fetchImpl = (url, init) => {
    const u = String(url);
    if (u === "/api/state") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        pushedAt: state.pushedAt, rigs: payloads.map(x => x.rigId) }) });
    }
    if (u === "/api/push") {
      state.pushes++;
      state.sent = JSON.parse(init.body).payloads;
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        ok: true, count: state.sent.length, pushedAt: "2026-08-23T11:00:00.000Z" }) });
    }
    if (u.startsWith("/api/rigs/")) {
      const id = decodeURIComponent(u.split("/")[3]);
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        payloads.find(x => x.rigId === id)) });
    }
    return Promise.reject(new Error("no such route"));
  };
  return state;
}

const namesIn = sent => [...new Set(sent
  .filter(p => p.shift.label === "Morning")
  .flatMap(p => p.turns.map(t => t.operator.name)))].sort();

// ------------------------------------------- reading the roster back

test("the desk opens on the floor's roster, not on its own file",
  (async () => {
    const server = serving(FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();

      assert.ok(server.sent, "nothing was pushed");
      const names = namesIn(server.sent);
      assert.ok(names.includes("Floor One"),
        "the desk pushed its own file back over the floor - the names it "
        + "sent were " + names.slice(0, 4).join(", "));
      assert.ok(!names.some(n => n.includes("Petrov")),
        "the file's roster went to the floor alongside the floor's own");
    } finally { desk.stop(); }
  }));

test("the task travels with the names",
  (async () => {
    const server = serving(FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      const tasks = [...new Set(server.sent.map(p => p.task))].sort();
      assert.deepEqual(tasks,
        ["Floor task A", "Floor task B", "Floor task C", "Floor task D"]);
    } finally { desk.stop(); }
  }));

test("every shift of the day is redrawn from the recovered roster",
  (async () => {
    /* The floor answers with one shift - whichever is running - and a
       push sends three. The recovery has to reach the two nobody read
       back, or a push made from a corrected roster still sends the
       file's names to the night crew. */
    const server = serving(FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();

      const night = server.sent.filter(p => p.shift.label === "Night");
      assert.equal(night.length, 12, "sanity: the night shift went up");
      const names = [...new Set(night.flatMap(p => p.turns.map(t => t.operator.name)))];
      assert.ok(names.includes("Floor One"),
        "the night sheet was drawn from the file while the morning came from the floor");
    } finally { desk.stop(); }
  }));

test("a roster the desk cannot rebuild is refused, out loud",
  (async () => {
    /* One turn handed to somebody who is not in the rotation at all. The
       recovery still produces four names per group, and the schedule it
       rebuilds does not match - which is exactly the case where trusting
       it would push a roster nobody can account for. */
    const bent = JSON.parse(JSON.stringify(FLOOR));
    bent[0].turns[4].operator = { id: "op-zz", name: "Somebody Else" };

    const server = serving(bent);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      assert.match(desk.$("toast").textContent, /cannot rebuild/,
        "the desk adopted a roster it had not verified, or said nothing about refusing it");

      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      const names = namesIn(server.sent);
      assert.ok(names.some(n => n.includes("Petrov")),
        "the unverifiable roster was pushed anyway");
    } finally { desk.stop(); }
  }));

test("an edit on this screen is never overwritten by the floor",
  (async () => {
    /* The manager is already typing when the floor answers. Adopting
       over that is the same silent clobber, only faster. */
    const server = serving(FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");

      const first = desk.$("rosters")
        .querySelectorAll("[aria-label=Group A operator 1]")[0];
      assert.ok(first, "the roster card did not draw an input to type into");
      desk.fire(first, "input", { value: "Typed By Hand" });
      await settle();

      desk.click(desk.$("btn-refresh"));      // the floor answers again
      await settle();

      desk.click(desk.$("btn-push"));
      await settle();
      assert.ok(namesIn(server.sent).includes("Typed By Hand"),
        "the floor's roster landed on top of what the manager had typed");
    } finally { desk.stop(); }
  }));

test("with no server the desk is exactly the file, as it always was",
  withDesk({ at: "10:37:22" }, async desk => {
    await settle();
    assert.doesNotMatch(desk.$("toast").textContent, /roster/i,
      "a desk with no floor behind it said something about a roster");
  }));

// ---------------------------------------- pushing over a newer floor

test("a push over a floor that moved since this screen read it is refused",
  (async () => {
    const server = serving(FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      // Somebody else pushes while this screen sits open.
      server.pushedAt = "2026-08-23T10:30:00.000Z";

      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();

      assert.equal(server.pushes, 0,
        "the desk replaced a push it had never read, and told nobody");
      assert.match(desk.$("push-note").textContent, /after you opened this screen/);
      assert.equal(desk.$("btn-push").textContent, "Push anyway",
        "the refusal has to offer a way through - only the manager knows if they meant it");
    } finally { desk.stop(); }
  }));

test("and pressing again goes through, because they may well have meant it",
  (async () => {
    const server = serving(FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      server.pushedAt = "2026-08-23T10:30:00.000Z";

      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      desk.click(desk.$("btn-push"));
      await settle();

      assert.equal(server.pushes, 1, "the second press did not push");
      assert.equal(desk.$("btn-push").textContent, "Push to floor",
        "the button was left saying Push anyway after the push had gone");
    } finally { desk.stop(); }
  }));

test("reading the floor back answers the refusal instead of pushing over it",
  (async () => {
    const server = serving(FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      server.pushedAt = "2026-08-23T10:30:00.000Z";

      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      assert.equal(desk.$("btn-push").textContent, "Push anyway", "sanity: refused");

      desk.click(desk.$("btn-refresh"));
      await settle();
      assert.equal(desk.$("btn-push").textContent, "Push to floor",
        "this screen now holds what the floor holds; there is nothing left to warn about");
    } finally { desk.stop(); }
  }));

test("a floor that has not moved pushes first time",
  (async () => {
    /* The control. Without it every test above would pass on a desk that
       had simply stopped pushing. */
    const server = serving(FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      assert.equal(server.pushes, 1, "an unchanged floor should not need a second press");
    } finally { desk.stop(); }
  }));

test("a desk with no floor to compare against is not stopped from pushing",
  (async () => {
    /* No server, or one too old for /api/state. The push itself will
       fail and say so; being blocked by a warning about a floor nobody
       can read would be a second, wrong answer. */
    let pushes = 0;
    const desk = await mountDesk({
      at: "10:37:22",
      fetchImpl: (url) => {
        if (String(url) === "/api/push") {
          pushes++;
          return Promise.resolve({ ok: true, json: () => Promise.resolve({
            ok: true, count: 36, pushedAt: "2026-08-23T11:00:00.000Z" }) });
        }
        return Promise.reject(new Error("no server"));
      },
    });
    try {
      await settle();
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      assert.equal(pushes, 1, "a desk with no readable floor refused to push at all");
    } finally { desk.stop(); }
  }));
