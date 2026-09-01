/* =====================================================================
 * passwd.test.js  -  changing your own password, from the desk
 *
 * The gate that counts is on the service, and it is tested with the
 * browser removed entirely in backend/tests/test_password_change.py:
 * the current password is required there, the length rule is applied
 * there, and every other session is revoked there. None of that is
 * re-litigated here.
 *
 * What is tested here is the half a browser owns. That the panel does
 * not draw until it is asked for; that it does not leave three filled
 * password fields behind a hidden panel on an unattended desk; that the
 * one check the service cannot make - did they type the same thing
 * twice - is made before the request rather than after; and that the
 * fresh CSRF token the reply carries actually replaces the dead one, so
 * the next thing the manager does is not refused for a reason nothing
 * on screen could explain.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { mountDesk } = require("./test/dom.js");

async function settle() {
  for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
}

const A_MANAGER = {
  personAuth: "on", csrfToken: "csrf-from-session",
  account: { name: "Ruth Osei", role: "manager", operatorId: null },
};

/* A stand-in for the service. `opts.passwdStatus` drives the one route
 * this file cares about. */
function service(opts) {
  opts = opts || {};
  const calls = [];
  const impl = (url, init) => {
    const path_ = String(url);
    calls.push({ path: path_, init: init || {} });

    if (path_ === "/api/auth/session") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(A_MANAGER) });
    }
    if (path_ === "/api/auth/password") {
      if (opts.passwdStatus && opts.passwdStatus !== 200) {
        return Promise.resolve({
          ok: false, status: opts.passwdStatus,
          headers: { get: k => (k === "retry-after" ? opts.retryAfter || null : null) },
          json: () => Promise.resolve({ detail: opts.passwdDetail || "no" }),
        });
      }
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
          name: "Ruth Osei", role: "manager", operatorId: null,
          csrfToken: "csrf-after-change",
        }),
      });
    }
    if (path_ === "/api/push") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        ok: true, count: 36, pushedAt: "2026-08-27T10:00:00.000Z" }) });
    }
    if (path_ === "/api/state") return Promise.reject(new Error("nothing pushed"));
    if (path_ === "/api/auth/logout") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    }
    return Promise.reject(new Error("unexpected " + path_));
  };
  impl.calls = calls;
  impl.hit = p => calls.filter(c => c.path === p);
  return impl;
}

function withDesk(opts, fn) {
  return async () => {
    const impl = service(opts);
    const desk = await mountDesk({ fetchImpl: impl });
    await settle();
    try { await fn(desk, impl); } finally { desk.stop(); }
  };
}

/* Fill the three boxes and submit. */
async function submit(desk, current, next, again) {
  desk.$("pw-current").value = current;
  desk.$("pw-new").value = next;
  desk.$("pw-again").value = again === undefined ? next : again;
  desk.fire(desk.$("passwd-form"), "submit");
  await settle();
}

/* Open the panel the way a person does. */
async function open(desk) {
  desk.click(desk.$("btn-passwd"));
  await settle();
}

/* ------------------------------------------------------------ opening */

test("the panel is not on screen until it is asked for",
  withDesk({}, async desk => {
    assert.equal(desk.$("passwd-modal").hidden, true);
  }));

test("the button opens it",
  withDesk({}, async desk => {
    await open(desk);
    assert.equal(desk.$("passwd-modal").hidden, false);
  }));

test("every id the script wires actually exists in the markup",
  withDesk({}, async desk => {
    /* The harness records a lookup that found nothing. A typo in an id
       would otherwise be a button that silently does nothing. */
    assert.deepEqual(desk.missingIds, [],
      "the script asked for ids the page does not have: " + desk.missingIds);
  }));

/* ------------------------------------------------------- what it sends */

test("a change sends the two passwords and the CSRF token",
  withDesk({}, async (desk, impl) => {
    await open(desk);
    await submit(desk, "old-password-12", "new-password-34");

    const sent = impl.hit("/api/auth/password");
    assert.equal(sent.length, 1, "expected exactly one call");
    const body = JSON.parse(sent[0].init.body);
    assert.equal(body.currentPassword, "old-password-12");
    assert.equal(body.newPassword, "new-password-34");
    assert.equal(sent[0].init.headers["x-csrf-token"], "csrf-from-session",
      "the write went without the token the service asks for");
  }));

test("it says so when it worked",
  withDesk({}, async desk => {
    await open(desk);
    await submit(desk, "old-password-12", "new-password-34");
    assert.equal(desk.$("passwd-ok").hidden, false, "nothing said it worked");
    assert.equal(desk.$("passwd-error").hidden, true);
  }));

test("the boxes are emptied once it has worked",
  withDesk({}, async desk => {
    await open(desk);
    await submit(desk, "old-password-12", "new-password-34");
    assert.equal(desk.$("pw-current").value, "");
    assert.equal(desk.$("pw-new").value, "");
    assert.equal(desk.$("pw-again").value, "");
  }));

/* ------------------------------------------------ the fresh CSRF token */

test("the new CSRF token replaces the dead one",
  withDesk({}, async (desk, impl) => {
    /* The service revoked every session including this one and issued a
       new pair. A page still holding the old token has its next write
       refused, and nothing on screen could explain why. */
    await open(desk);
    await submit(desk, "old-password-12", "new-password-34");

    desk.click(desk.$("btn-push"));
    await settle();

    const pushes = impl.hit("/api/push");
    assert.ok(pushes.length >= 1, "the push did not go");
    assert.equal(pushes[pushes.length - 1].init.headers["x-csrf-token"],
      "csrf-after-change",
      "the desk kept using the token the change had already revoked");
  }));

/* ------------------------------------------------------ what it refuses */

test("two different new passwords are caught before the service is asked",
  withDesk({}, async (desk, impl) => {
    await open(desk);
    await submit(desk, "old-password-12", "new-password-34", "new-password-56");

    assert.equal(desk.$("passwd-error").hidden, false);
    assert.match(desk.$("passwd-error").textContent, /not the same/i);
    assert.equal(impl.hit("/api/auth/password").length, 0,
      "it asked the service about a typo it could see for itself");
  }));

test("a refusal from the service is shown as it was worded",
  withDesk({ passwdStatus: 400, passwdDetail: "that is not your current password" },
    async desk => {
      await open(desk);
      await submit(desk, "wrong", "new-password-34");
      assert.equal(desk.$("passwd-error").hidden, false);
      assert.match(desk.$("passwd-error").textContent, /current password/i);
      assert.equal(desk.$("passwd-ok").hidden, true, "it claimed success on a refusal");
    }));

test("a throttle says how long to wait, in the units the service gave",
  withDesk({ passwdStatus: 429, retryAfter: "900" }, async desk => {
    await open(desk);
    await submit(desk, "wrong", "new-password-34");
    assert.match(desk.$("passwd-error").textContent, /15 minutes/,
      "somebody locked for fifteen minutes was told to wait one");
  }));

test("a short password is the service's to refuse, and is not pre-empted here",
  withDesk({ passwdStatus: 400, passwdDetail: "too short - 12 characters" },
    async (desk, impl) => {
      /* One rule, in one place. A second length check written into the
         page is a second answer that can drift from the first. */
      await open(desk);
      await submit(desk, "old-password-12", "short");
      assert.equal(impl.hit("/api/auth/password").length, 1,
        "the page invented its own length rule instead of asking");
      assert.match(desk.$("passwd-error").textContent, /short/i);
    }));

/* ------------------------------------------------------------ closing */

test("cancel closes it and leaves nothing in the boxes",
  withDesk({}, async desk => {
    await open(desk);
    desk.$("pw-current").value = "old-password-12";
    desk.$("pw-new").value = "new-password-34";
    desk.click(desk.$("btn-passwd-cancel"));
    await settle();

    assert.equal(desk.$("passwd-modal").hidden, true);
    assert.equal(desk.$("pw-current").value, "",
      "a password was left in the box behind a hidden panel");
    assert.equal(desk.$("pw-new").value, "");
  }));

test("opening it again starts clean rather than showing the last result",
  withDesk({}, async desk => {
    await open(desk);
    await submit(desk, "old-password-12", "new-password-34");
    assert.equal(desk.$("passwd-ok").hidden, false);

    desk.click(desk.$("btn-passwd-cancel"));
    await settle();
    await open(desk);
    assert.equal(desk.$("passwd-ok").hidden, true,
      "it opened still claiming the last change had just worked");
    assert.equal(desk.$("passwd-error").hidden, true);
  }));

test("escape closes it, and does nothing when it is already closed",
  withDesk({}, async desk => {
    /* A panel that can only be dismissed by finding the right button is
       one people close by reloading the page - which on the desk costs
       whatever plan was laid out. */
    await open(desk);
    desk.key("Escape");
    await settle();
    assert.equal(desk.$("passwd-modal").hidden, true);

    desk.key("Escape");
    await settle();
    assert.equal(desk.$("passwd-modal").hidden, true, "it reopened itself");
  }));

test("a click on the backdrop closes it, a click inside does not",
  withDesk({}, async desk => {
    await open(desk);
    const modal = desk.$("passwd-modal");
    /* Inside the card first - the click bubbles up to the backdrop, and
       a modal that shuts when you click its own form is unusable. */
    (modal._on["click"] || []).forEach(fn => fn({ target: desk.$("passwd-form") }));
    await settle();
    assert.equal(modal.hidden, false, "clicking the form closed the panel");

    (modal._on["click"] || []).forEach(fn => fn({ target: modal }));
    await settle();
    assert.equal(modal.hidden, true, "clicking the backdrop did not close it");
  }));

/* ------------------------------------------------------------- the CSS */

test("hidden actually hides the panel", () => {
  /* Not visible to the DOM stub, and that is the point: this exact bug
     shipped on My Shift, where six elements set display:flex and so
     outranked the browser's own [hidden] rule. Each drew as an empty
     sliver while every headless test agreed it was hidden. */
  const css = fs.readFileSync(
    path.join(__dirname, "assets", "desk.css"), "utf8");
  assert.match(css, /\.modal\[hidden\]\s*\{\s*display:\s*none/,
    "`.modal` sets display:flex, which outranks the browser's [hidden] "
    + "rule - so the panel draws over the desk while claiming to be hidden");
});
