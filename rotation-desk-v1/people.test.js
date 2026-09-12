/* =====================================================================
 * people.test.js  -  the floor's people, and who signs in
 *
 * The desk had two screens: the floor running now, and the day being
 * planned. Managing the people themselves had no screen at all - a
 * person was created as a side effect of typing a name nobody had into
 * a roster seat, and giving somebody a way to sign in meant a terminal
 * and a password read out loud.
 *
 * So there is a third screen. It lists everybody on the floor, says
 * where each one sits today and whether they sign in yet, and offers
 * the one action that makes sense for each: invite somebody with an
 * address and no account, send the link again if it went astray, or
 * retire somebody who has left. The seat is read from the roster on
 * screen; who signs in comes from the service.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");

const { mountDesk } = require("./test/dom.js");

const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };

const PEOPLE = [
  { id: "p-mei",   name: "Mei Chen",     email: "m.chen@verlet.co",  disabledAt: null, account: "active" },
  { id: "p-fat",   name: "Fatima Zahra", email: "f.zahra@verlet.co", disabledAt: null, account: "invited" },
  { id: "p-ines",  name: "Ines Costa",   email: "i.costa@verlet.co", disabledAt: null, account: "none" },
  { id: "p-omar",  name: "Omar Haddad",  email: null,                disabledAt: null, account: "none" },
];

/* A service with people on it, and an invite route that answers. */
function servingPeople(people) {
  const state = { people: (people || PEOPLE).map(p => ({ ...p })), invited: [], added: [], disabled: [] };
  state.fetchImpl = (url, init) => {
    const u = String(url);
    const method = ((init && init.method) || "GET").toUpperCase();
    const ok = body => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });

    if (u === "/api/state") return ok({ pushedAt: null, rigs: [] });
    if (u.startsWith("/api/rigs/")) return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
    if (u === "/api/people" && method === "GET") return ok({ people: state.people });
    if (u === "/api/people" && method === "POST") {
      const body = JSON.parse(init.body);
      const person = { id: "p-new", name: body.name, email: body.email || null,
                       disabledAt: null, account: "none" };
      state.people.push(person); state.added.push(body);
      return Promise.resolve({ ok: true, status: 201, json: () => Promise.resolve(person) });
    }
    const invite = u.match(/^\/api\/people\/([^/]+)\/invite$/);
    if (invite && method === "POST") {
      const id = decodeURIComponent(invite[1]);
      if (state.inviteStatus) {
        return Promise.resolve({ ok: false, status: state.inviteStatus,
          json: () => Promise.resolve({ detail: state.inviteDetail || "no" }) });
      }
      state.invited.push(id);
      const person = state.people.find(x => x.id === id);
      person.account = "invited";
      return ok({ ok: true, email: person.email, account: "created", sent: true });
    }
    const disable = u.match(/^\/api\/people\/([^/]+)\/disable$/);
    if (disable && method === "POST") {
      const id = decodeURIComponent(disable[1]);
      state.disabled.push(id);
      state.people.find(x => x.id === id).disabledAt = "2026-09-12T00:00:00Z";
      return ok({ ok: true, changed: 1 });
    }
    return Promise.reject(new Error("no such route " + method + " " + u));
  };
  return state;
}

/* The People screen, read back as rows. */
function peopleRows(desk) {
  return desk.find(desk.$("people-list"), "prow").map(row => ({
    id: row.getAttribute("data-person"),
    name: desk.textIn(row, "prow-name"),
    mail: desk.textIn(row, "prow-mail"),
    seat: desk.textIn(row, "prow-seat"),
    signs: desk.textIn(row, "prow-signs"),
    actions: desk.find(row, "prow-act").map(b => b.textContent),
    button: label => desk.find(row, "prow-act").find(b => b.textContent === label),
  }));
}

async function onPeople(desk) {
  await settle();
  desk.mode("people");
  await settle();
  return peopleRows(desk);
}

test("the desk has a third screen, and it lists everyone on the floor",
  (async () => {
    const server = servingPeople();
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      const rows = await onPeople(desk);
      assert.equal(desk.$("view-people").hidden, false, "the People screen should be showing");
      assert.equal(desk.$("view-live").hidden, true);
      assert.equal(desk.$("view-plan").hidden, true);
      assert.deepEqual(rows.map(r => r.name),
        ["Fatima Zahra", "Ines Costa", "Mei Chen", "Omar Haddad"], "everybody, in name order");
      assert.equal(rows.find(r => r.id === "p-mei").mail, "m.chen@verlet.co");
      assert.equal(rows.find(r => r.id === "p-omar").mail, "no email");
    } finally { desk.stop(); }
  }));

test("it says where each person sits today, from the roster on screen",
  (async () => {
    const server = servingPeople();
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await settle();
      desk.mode("plan");
      await settle();
      /* Seat Mei in group A's first chair, the way a manager would. */
      const card = desk.find(desk.$("rosters"), "grp")[0];
      const field = desk.find(desk.find(card, "op-line")[0], "op-pick")[0];
      desk.click(field);
      desk.click(desk.find(desk.find(card, "op-line")[0], "pick-row")
        .find(r => r.getAttribute("data-person") === "p-mei").children[0]);
      await settle();

      const rows = await onPeople(desk);
      assert.equal(rows.find(r => r.id === "p-mei").seat, "Group A · 1");
      assert.equal(rows.find(r => r.id === "p-ines").seat, "not today");
    } finally { desk.stop(); }
  }));

test("somebody with an address and no account is offered an invite, and it sends",
  (async () => {
    const server = servingPeople();
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      let rows = await onPeople(desk);
      const ines = rows.find(r => r.id === "p-ines");
      assert.equal(ines.signs, "Not yet");
      assert.ok(ines.button("Invite"), "no invite was offered to somebody who could have one");

      desk.click(ines.button("Invite"));
      await settle();

      assert.deepEqual(server.invited, ["p-ines"], "the invite route was not called");
      rows = peopleRows(desk);
      assert.equal(rows.find(r => r.id === "p-ines").signs, "Invited",
        "the row should say so once the link has gone");
    } finally { desk.stop(); }
  }));

test("an invite already sent offers to send again, and one already taken offers neither",
  (async () => {
    const server = servingPeople();
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      const rows = await onPeople(desk);
      const fatima = rows.find(r => r.id === "p-fat");
      assert.equal(fatima.signs, "Invited");
      assert.ok(fatima.button("Send again"), "a link that may have gone astray should be re-sendable");

      const mei = rows.find(r => r.id === "p-mei");
      assert.equal(mei.signs, "Yes");
      assert.equal(mei.button("Invite"), undefined, "somebody who signs in must not be re-invited");
      assert.equal(mei.button("Send again"), undefined);
    } finally { desk.stop(); }
  }));

test("somebody with no address is told what is missing, and offered no invite",
  (async () => {
    const server = servingPeople();
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      const rows = await onPeople(desk);
      const omar = rows.find(r => r.id === "p-omar");
      assert.match(omar.signs, /email/i, "it should say why they cannot be invited");
      assert.equal(omar.button("Invite"), undefined);
    } finally { desk.stop(); }
  }));

test("a refused invite says why, and the row does not pretend it went",
  (async () => {
    const server = servingPeople();
    server.inviteStatus = 503;
    server.inviteDetail = "this floor has no mail relay configured";
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      const rows = await onPeople(desk);
      desk.click(rows.find(r => r.id === "p-ines").button("Invite"));
      await settle();

      assert.match(desk.$("toast").textContent, /no mail relay/i,
        "the floor's own words should reach the manager");
      assert.equal(peopleRows(desk).find(r => r.id === "p-ines").signs, "Not yet",
        "a refused invite must not read as sent");
    } finally { desk.stop(); }
  }));

test("a person can be added here, with an address, and is then invitable",
  (async () => {
    const server = servingPeople();
    const desk = await mountDesk({ at: "10:37:22", fetchImpl: server.fetchImpl });
    try {
      await onPeople(desk);
      desk.fire(desk.$("add-name"), "input", { value: "Zoe Bright" });
      desk.fire(desk.$("add-email"), "input", { value: "z.bright@verlet.co" });
      desk.click(desk.$("btn-add-person"));
      await settle();

      assert.deepEqual(server.added, [{ name: "Zoe Bright", email: "z.bright@verlet.co" }]);
      const zoe = peopleRows(desk).find(r => r.name === "Zoe Bright");
      assert.ok(zoe, "the new person should appear in the list");
      assert.ok(zoe.button("Invite"), "and be invitable, having an address");
      assert.equal(desk.$("add-name").value, "", "the form should clear itself");
    } finally { desk.stop(); }
  }));

test("with no service behind it the desk offers no People screen at all",
  (async () => {
    const desk = await mountDesk({ at: "10:37:22" });   // nothing answers
    try {
      await settle();
      const modes = desk.$("modes").children.map(b => b.dataset.mode);
      assert.deepEqual(modes.filter(m => m), ["live", "plan"],
        "there is nobody to list and no service to add one to");
    } finally { desk.stop(); }
  }));

test("the desk's header carries no company mark or name",
  async () => {
    const fs = require("node:fs");
    const html = fs.readFileSync(require("node:path").join(__dirname, "index.html"), "utf8")
      .replace(/<title>[^<]*<\/title>/, "");
    const head = html.slice(html.indexOf("<header"), html.indexOf("</header>"));
    assert.ok(!/v-mark|Verlet Robotics|class="eyebrow"/.test(head),
      "the mark and the company name were asked off this screen");
    assert.match(head, /Rotation Desk/, "the desk still says what it is");
  });
