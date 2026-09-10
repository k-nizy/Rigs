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
 * The first was fixed, for a while, by reading the roster back off the
 * floor and proving the recovered roster rebuilt the schedule the floor
 * was running. It is fixed now by the roster riding on the push, in the
 * same row and transaction as the payloads, so a desk reads it instead
 * of reconstructing it. The second is fixed by asking, on the way out,
 * whether the floor has moved since this screen read it.
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
function serving(payloads, pushedAt, roster) {
  const state = { pushedAt: pushedAt || "2026-08-23T09:00:00.000Z", sent: null, pushes: 0,
                  roster: roster === undefined ? FLOOR_GROUPS : roster };
  state.fetchImpl = (url, init) => {
    const u = String(url);
    if (u === "/api/roster") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        pushedAt: state.roster ? state.pushedAt : null, roster: state.roster }) });
    }
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

/* "a roster the desk cannot rebuild is refused, out loud" lived here.
   It pinned the read-back-and-prove path - rebuild the roster from
   twelve payloads, redraw, compare turn by turn, refuse on disagreement.
   That path is gone: the roster rides on the push, in the same row and
   transaction as the payloads it produced, so there is nothing left to
   prove. "the roster the service holds is taken without reconstructing
   it from payloads", below, is its replacement and asserts the
   opposite: disagreeing payloads no longer matter. */

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

// ----------------------------------------- the person survives a read-back

/* A floor whose payloads name people, not just seats. This is what the
   floor looks like once the desk assigns by picking. */
const PEOPLE_GROUPS = FLOOR_GROUPS.map(g => ({
  key: g.key, task: g.task, rigs: g.rigs.slice(),
  ops: g.ops.map((name, i) => ({ name: name, personId: "person-" + g.key + (i + 1) })),
}));

const PEOPLE_FLOOR = (() => {
  const p = RE.buildPlan(
    { shift: "morning", date: "2026-08-23", blockMin: 15, stintBlocks: 3, mode: "hold" },
    PEOPLE_GROUPS);
  return PEOPLE_GROUPS.reduce((all, g) => all.concat(g.rigs.map(r => RE.rigPayload(p, r))), []);
})();

const personIdsIn = sent => [...new Set(sent
  .filter(p => p.shift.label === "Morning")
  .flatMap(p => p.turns.map(t => t.operator.personId)))].filter(Boolean).sort();

test("a person id read back off the floor is pushed out again",
  (async () => {
    /* The failure this prevents: the desk adopts the floor's roster,
       rebuilds it from `operator.name` alone, and the next push drops
       every `personId` on the floor - silently, and from a screen the
       manager never touched. Losing the id is worse than losing a name,
       because the id is the thing a take is filed under and nothing
       downstream can tell it went missing. */
    const server = serving(PEOPLE_FLOOR, undefined, PEOPLE_GROUPS);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.click(desk.$("btn-push"));
      await settle();

      assert.ok(server.sent, "nothing was pushed");
      assert.deepEqual(personIdsIn(server.sent),
        ["person-A1","person-A2","person-A3","person-A4",
         "person-B1","person-B2","person-B3","person-B4",
         "person-C1","person-C2","person-C3","person-C4",
         "person-D1","person-D2","person-D3","person-D4"],
        "the desk read the floor back and pushed it out without the people");
    } finally { desk.stop(); }
  }));

test("and the names still come with them",
  (async () => {
    const server = serving(PEOPLE_FLOOR, undefined, PEOPLE_GROUPS);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.click(desk.$("btn-push"));
      await settle();
      assert.deepEqual(namesIn(server.sent).slice(0, 4),
        ["Floor Eight", "Floor Eleven", "Floor Fifteen", "Floor Five"],
        "the names did not survive alongside the ids");
    } finally { desk.stop(); }
  }));

test("a floor of plain names still pushes plain names",
  (async () => {
    /* The other half, and the one that keeps a laptop demo working: a
       roster with no people in it must push byte for byte what it always
       pushed - no `personId` key at all, not one set to null. */
    const server = serving(FLOOR, undefined, FLOOR_GROUPS);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.click(desk.$("btn-push"));
      await settle();
      const anyKey = server.sent.some(p => p.turns.some(t => "personId" in t.operator));
      assert.equal(anyKey, false,
        "a roster of plain names grew a personId key it was never given");
    } finally { desk.stop(); }
  }));

// ----------------------------------------------- assigned by picking

/* A service with people on it. The roster card is a picker here: a
   name resolves to a person on change, a name nobody has offers to add
   one, and two people with one name are flagged rather than guessed. */
function servingPeople(people, payloads) {
  const state = { people: people.slice(), posted: [], renamed: [], sent: null, pushedAt: "2026-08-23T09:00:00.000Z" };
  state.fetchImpl = (url, init) => {
    const u = String(url);
    const method = ((init && init.method) || "GET").toUpperCase();
    if (u === "/api/state") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        pushedAt: state.pushedAt, rigs: (payloads || []).map(x => x.rigId) }) });
    }
    if (u.startsWith("/api/rigs/")) {
      const id = decodeURIComponent(u.split("/")[3]);
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        (payloads || []).find(x => x.rigId === id)) });
    }
    if (u === "/api/push") {
      state.sent = JSON.parse(init.body).payloads;
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        ok: true, count: state.sent.length, pushedAt: "2026-08-23T11:00:00.000Z" }) });
    }
    if (u === "/api/people" && method === "GET") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ people: state.people }) });
    }
    if (u === "/api/people" && method === "POST") {
      const body = JSON.parse(init.body);
      const person = { id: "person-" + (state.people.length + 1), name: body.name, email: null, disabledAt: null };
      state.people.push(person); state.posted.push(body);
      return Promise.resolve({ ok: true, status: 201, json: () => Promise.resolve(person) });
    }
    const m = u.match(/^\/api\/people\/([^/]+)$/);
    if (m && method === "PATCH") {
      const id = decodeURIComponent(m[1]);
      const body = JSON.parse(init.body);
      const person = state.people.find(x => x.id === id);
      person.name = body.name; state.renamed.push({ id, name: body.name });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(person) });
    }
    return Promise.reject(new Error("no such route " + method + " " + u));
  };
  return state;
}

const PEOPLE = [
  { id: "person-mei",  name: "Mei Chen",   email: "m.chen@verlet.co", disabledAt: null },
  { id: "person-ben1", name: "Ben Carter", email: "b.carter@verlet.co", disabledAt: null },
  { id: "person-ben2", name: "Ben Carter", email: null, disabledAt: null },
];

/* The first operator input of group A, and the note beside it. */
const firstOp = desk => {
  const card = desk.find(desk.$("rosters"), "grp")[0];
  const line = desk.find(card, "op-line")[0];
  const inp = line.children[1];
  assert.equal(inp.getAttribute("aria-label"), "Group A operator 1",
    "the first op-line of group A did not hold operator 1");
  return { inp, note: line.children[2] };
};

test("with people on the floor, a typed name becomes the person and the push carries their id",
  (async () => {
    const server = servingPeople(PEOPLE);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      const { inp, note } = firstOp(desk);
      desk.fire(inp, "input",  { value: "Mei Chen" });
      desk.fire(inp, "change", { value: "Mei Chen" });
      await settle();

      assert.equal(note.hidden, false, "a resolved person with an email shows it");
      assert.match(note.textContent, /m\.chen@verlet\.co/);

      desk.click(desk.$("btn-push"));
      await settle();
      assert.ok(server.posted.length === 0, "resolving an existing person must not create one");
      assert.ok(personIdsIn(server.sent).includes("person-mei"),
        "the push did not carry the picked person's id");
    } finally { desk.stop(); }
  }));

test("a name nobody has is offered for adding, and typing alone never mints a person",
  (async () => {
    const server = servingPeople(PEOPLE);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      const { inp, note } = firstOp(desk);
      desk.fire(inp, "input",  { value: "Zoe Bright" });
      desk.fire(inp, "change", { value: "Zoe Bright" });
      await settle();

      assert.equal(server.posted.length, 0, "typing a name created a person by itself");
      assert.match(note.textContent, /Nobody on the floor is called Zoe Bright/);

      const add = desk.find(note, "op-act")[0];
      assert.ok(add, "no add button was offered");
      desk.click(add);
      await settle();

      assert.deepEqual(server.posted, [{ name: "Zoe Bright" }], "adding did not POST the typed name");
      desk.click(desk.$("btn-push"));
      await settle();
      assert.ok(personIdsIn(server.sent).includes("person-4"),
        "the newly added person's id was not pushed");
    } finally { desk.stop(); }
  }));

test("two people with one name are flagged, never guessed, and one can be renamed apart",
  (async () => {
    const server = servingPeople(PEOPLE);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      const { inp, note } = firstOp(desk);
      desk.fire(inp, "input",  { value: "Ben Carter" });
      desk.fire(inp, "change", { value: "Ben Carter" });
      await settle();

      assert.match(note.textContent, /2 people are called Ben Carter/);
      desk.click(desk.$("btn-push"));
      await settle();
      assert.ok(!personIdsIn(server.sent).some(id => id.startsWith("person-ben")),
        "the desk guessed which Ben Carter was meant");

      /* The remedy the doc licenses: rename one, the id does not move. */
      const renames = desk.find(note, "op-act").filter(b => b.textContent === "Rename");
      assert.equal(renames.length, 2, "each Ben Carter should offer a rename");
      desk.click(renames[1]);
      await settle();
      const box = desk.$("rosters").querySelectorAll("[aria-label=New name for Ben Carter]")[0];
      assert.ok(box, "rename offered no box to type the new name into");
      desk.fire(box, "input", { value: "Ben Carter (nights)" });
      const save = desk.find(note, "op-act").find(b => b.textContent === "Save");
      desk.click(save);
      await settle();

      assert.deepEqual(server.renamed, [{ id: "person-ben2", name: "Ben Carter (nights)" }]);
      desk.click(desk.$("btn-push"));
      await settle();
      assert.ok(personIdsIn(server.sent).includes("person-ben2"),
        "renaming apart did not assign the renamed person");
    } finally { desk.stop(); }
  }));

test("with no service at all the names are read-only, and the rest still edits",
  withDesk({ at: "10:37:22" }, async desk => {
    /* A desk that cannot reach the service cannot push, and pushing is
       its whole job - so an edited name here could never go anywhere. */
    await settle();
    desk.mode("plan");
    const card = desk.find(desk.$("rosters"), "grp")[0];
    assert.equal(desk.find(card, "op-line")[0].children[1].tagName, "span",
      "with no service the operator slot should be text, not an input");
    assert.equal(card.children[1].children[1].tagName, "input",
      "the task should still be editable with no service");
  }));

// ------------------------------------------------ the roster, server-side

/* The desk reads who is on the floor from the service instead of
   reconstructing it from twelve payloads. A push carries the roster on
   screen, and the next desk to open reads it back - so a correction made
   here reaches every other desk, which is what the read-back was for. */
function servingRoster(roster, payloads, people) {
  const state = { roster, pushedAt: "2026-08-23T09:00:00.000Z", sent: null, sentRoster: null, pushes: 0 };
  state.fetchImpl = (url, init) => {
    const u = String(url);
    const method = ((init && init.method) || "GET").toUpperCase();
    if (u === "/api/state") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        pushedAt: state.pushedAt, rigs: (payloads || []).map(x => x.rigId) }) });
    }
    if (u === "/api/roster") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        pushedAt: state.roster ? state.pushedAt : null, roster: state.roster }) });
    }
    if (u.startsWith("/api/rigs/")) {
      const id = decodeURIComponent(u.split("/")[3]);
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        (payloads || []).find(x => x.rigId === id)) });
    }
    if (u === "/api/push") {
      state.pushes++;
      const body = JSON.parse(init.body);
      state.sent = body.payloads; state.sentRoster = body.roster;
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        ok: true, count: state.sent.length, pushedAt: "2026-08-23T11:00:00.000Z" }) });
    }
    if (u === "/api/people" && method === "GET") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ people: people || [] }) });
    }
    return Promise.reject(new Error("no such route " + method + " " + u));
  };
  return state;
}

const SERVED_ROSTER = FLOOR_GROUPS.map(g => ({ key: g.key, task: g.task, rigs: g.rigs.slice(), ops: g.ops.slice() }));

test("the desk opens on the roster the service holds",
  (async () => {
    const server = servingRoster(SERVED_ROSTER, FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      assert.ok(server.sent, "nothing was pushed");
      assert.ok(namesIn(server.sent).includes("Floor One"),
        "the desk pushed its own file rather than the roster the service holds");
    } finally { desk.stop(); }
  }));

test("a push carries the roster on screen, so the next desk reads it",
  (async () => {
    const server = servingRoster(SERVED_ROSTER, FLOOR);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      assert.ok(Array.isArray(server.sentRoster), "the push carried no roster");
      assert.deepEqual(server.sentRoster.map(g => g.key), ["A", "B", "C", "D"]);
      assert.deepEqual(server.sentRoster[0].ops, SERVED_ROSTER[0].ops);
    } finally { desk.stop(); }
  }));

test("the roster the service holds is taken without reconstructing it from payloads",
  (async () => {
    /* The service is the authority now. Payloads that disagree with it
       are a floor mid-push or a bug elsewhere; the desk no longer
       rebuilds and proves, it reads. */
    const disagreeing = FLOOR.map(p => Object.assign({}, p, { task: "Not what the roster says" }));
    const server = servingRoster(SERVED_ROSTER, disagreeing);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      const tasks = [...new Set(server.sent.map(p => p.task))].sort();
      assert.deepEqual(tasks, ["Floor task A", "Floor task B", "Floor task C", "Floor task D"],
        "the desk rebuilt the roster from payloads instead of reading the service's");
      assert.doesNotMatch(desk.$("toast").textContent, /cannot rebuild/,
        "the read-back-and-prove path is still running");
    } finally { desk.stop(); }
  }));

test("a service that holds no roster leaves the file in place, without complaint",
  (async () => {
    const server = servingRoster(null, []);
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();
      assert.ok(namesIn(server.sent).some(n => n.includes("Petrov")),
        "a floor on its first morning should push the file's roster");
      assert.doesNotMatch(desk.$("toast").textContent, /roster/i);
    } finally { desk.stop(); }
  }));
