/* =====================================================================
 * passwd.test.js  -  an operator changing their own password
 *
 * The same panel as the desk's, on the screen the other half of the
 * floor uses. It is here rather than shared because the two apps have
 * always owned their own markup - what they share is the logic, in
 * packages/session, and that is the part that must not be written
 * twice.
 *
 * The service is the gate, and it is tested with the browser removed
 * entirely in backend/tests/test_password_change.py. What is tested
 * here is the half a browser owns, and one thing specific to this
 * screen: My Shift is left open in a break room for a whole shift, so a
 * panel that keeps a typed password in a box behind it matters more
 * here than anywhere else in this system.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { mountMyShift } = require("./test/dom.js");

const AN_OPERATOR = {
  personAuth: "on", csrfToken: "csrf-from-session",
  account: { name: "Mei Chen", role: "operator", operatorId: "op-a2" },
};

/* A stand-in for the service. Only the routes this file needs. */
function service(opts) {
  opts = opts || {};
  const calls = [];
  const impl = (url, init) => {
    const p = String(url);
    calls.push({ path: p, init: init || {} });

    if (p === "/api/auth/session") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(AN_OPERATOR) });
    }
    if (p === "/api/auth/password") {
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
          name: "Mei Chen", role: "operator", operatorId: "op-a2",
          csrfToken: "csrf-after-change",
        }),
      });
    }
    if (p.startsWith("/api/me/shift")) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
        operatorId: "op-a2", name: "Mei Chen", turns: [],
        shift: { label: "Morning", date: "2026-08-27",
                 start: "08:00", end: "16:00", tz: "UTC" },
      }) });
    }
    if (p === "/api/auth/logout") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    }
    return Promise.reject(new Error("unexpected " + p));
  };
  impl.calls = calls;
  impl.hit = q => calls.filter(c => c.path === q);
  return impl;
}

function withShift(opts, fn) {
  return async () => {
    const impl = service(opts);
    const app = await mountMyShift({ fetchImpl: impl });
    await app.settle();
    try { await fn(app, impl); } finally { app.stop(); }
  };
}

async function open(app) {
  app.click(app.$("btn-passwd"));
  await app.settle();
}

async function submit(app, current, next, again) {
  app.$("pw-current").value = current;
  app.$("pw-new").value = next;
  app.$("pw-again").value = again === undefined ? next : again;
  app.fire(app.$("passwd-form"), "submit");
  await app.settle();
}

/* ------------------------------------------------------------ opening */

test("the panel is not on screen until it is asked for",
  withShift({}, async app => {
    assert.equal(app.$("passwd-modal").hidden, true);
  }));

test("the button opens it",
  withShift({}, async app => {
    await open(app);
    assert.equal(app.$("passwd-modal").hidden, false);
  }));

test("every id the script wires actually exists in the markup",
  withShift({}, async app => {
    assert.deepEqual(app.missingIds, [],
      "the script asked for ids the page does not have: " + app.missingIds);
  }));

/* ------------------------------------------------------- what it sends */

test("an operator's change sends both passwords and the CSRF token",
  withShift({}, async (app, impl) => {
    await open(app);
    await submit(app, "old-password-12", "new-password-34");

    const sent = impl.hit("/api/auth/password");
    assert.equal(sent.length, 1);
    const body = JSON.parse(sent[0].init.body);
    assert.equal(body.currentPassword, "old-password-12");
    assert.equal(body.newPassword, "new-password-34");
    assert.equal(sent[0].init.headers["x-csrf-token"], "csrf-from-session");
  }));

test("it says so when it worked, and empties the boxes",
  withShift({}, async app => {
    await open(app);
    await submit(app, "old-password-12", "new-password-34");
    assert.equal(app.$("passwd-ok").hidden, false);
    assert.equal(app.$("passwd-error").hidden, true);
    assert.equal(app.$("pw-current").value, "");
    assert.equal(app.$("pw-new").value, "");
    assert.equal(app.$("pw-again").value, "");
  }));

/* ------------------------------------------------------ what it refuses */

test("two different new passwords are caught before the service is asked",
  withShift({}, async (app, impl) => {
    await open(app);
    await submit(app, "old-password-12", "new-password-34", "new-password-56");
    assert.equal(app.$("passwd-error").hidden, false);
    assert.match(app.$("passwd-error").textContent, /not the same/i);
    assert.equal(impl.hit("/api/auth/password").length, 0);
  }));

test("a refusal from the service is shown as it was worded",
  withShift({ passwdStatus: 400, passwdDetail: "that is not your current password" },
    async app => {
      await open(app);
      await submit(app, "wrong", "new-password-34");
      assert.equal(app.$("passwd-error").hidden, false);
      assert.match(app.$("passwd-error").textContent, /current password/i);
      assert.equal(app.$("passwd-ok").hidden, true);
    }));

test("a throttle says how long to wait, in the units the service gave",
  withShift({ passwdStatus: 429, retryAfter: "900" }, async app => {
    await open(app);
    await submit(app, "wrong", "new-password-34");
    assert.match(app.$("passwd-error").textContent, /15 minutes/);
  }));

/* ------------------------------------------------------------ closing */

test("cancel closes it and leaves no password in the boxes",
  withShift({}, async app => {
    /* This screen sits open in a break room for a whole shift. A typed
       password left in a box behind a hidden panel is a worse thing
       here than anywhere else in this system. */
    await open(app);
    app.$("pw-current").value = "old-password-12";
    app.$("pw-new").value = "new-password-34";
    app.click(app.$("btn-passwd-cancel"));
    await app.settle();

    assert.equal(app.$("passwd-modal").hidden, true);
    assert.equal(app.$("pw-current").value, "");
    assert.equal(app.$("pw-new").value, "");
  }));

test("escape closes it",
  withShift({}, async app => {
    await open(app);
    app.key("Escape");
    await app.settle();
    assert.equal(app.$("passwd-modal").hidden, true);
  }));

test("opening it again starts clean",
  withShift({}, async app => {
    await open(app);
    await submit(app, "old-password-12", "new-password-34");
    assert.equal(app.$("passwd-ok").hidden, false);

    app.click(app.$("btn-passwd-cancel"));
    await open(app);
    assert.equal(app.$("passwd-ok").hidden, true);
    assert.equal(app.$("passwd-error").hidden, true);
  }));

/* ------------------------------------------------------------- the CSS */

test("hidden actually hides the panel", () => {
  /* This app has had this exact bug: six elements set display:flex and
     so outranked the browser's own [hidden] rule, each drawing as an
     empty sliver while every headless test agreed it was hidden. */
  const css = fs.readFileSync(
    path.join(__dirname, "assets", "my-shift.css"), "utf8");
  assert.match(css, /\.modal\[hidden\]\s*\{\s*display:\s*none/,
    "`.modal` sets display:flex, which outranks [hidden] - so the panel "
    + "draws over the day while claiming to be hidden");
});
