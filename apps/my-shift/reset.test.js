/* =====================================================================
 * reset.test.js  -  an operator who has forgotten their password
 *
 * The same two doors as the desk's, on the screen the other half of the
 * floor uses - and the half that matters most for this feature. Night
 * runs 00:00 to 08:00, and an operator locked out at two in the morning
 * with no manager in the building is the case self-service exists for.
 *
 * The service is the gate and is tested with the browser removed in
 * backend/tests/test_password_reset.py. What is covered here is the half
 * a browser owns.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");

const { mountMyShift } = require("./test/dom.js");

const NOBODY = { personAuth: "on", account: null, csrfToken: null };

const SENT = "If that address belongs to an account, a link to set a new "
  + "password is on its way to it.";

function service(opts) {
  opts = opts || {};
  const calls = [];
  const impl = (url, init) => {
    const p = String(url);
    calls.push({ path: p, init: init || {} });

    if (p === "/api/auth/session") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(NOBODY) });
    }
    if (p === "/api/auth/reset/request") {
      if (opts.askStatus && opts.askStatus !== 200) {
        return Promise.resolve({
          ok: false, status: opts.askStatus,
          headers: { get: k => (k === "retry-after" ? opts.retryAfter || null : null) },
          json: () => Promise.resolve({ detail: opts.askDetail || "no" }),
        });
      }
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ ok: true, detail: SENT }),
      });
    }
    if (p === "/api/auth/reset") {
      if (opts.useStatus && opts.useStatus !== 200) {
        return Promise.resolve({
          ok: false, status: opts.useStatus,
          headers: { get: () => null },
          json: () => Promise.resolve({ detail: opts.useDetail || "no" }),
        });
      }
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
          name: "Mei Chen", role: "operator", operatorId: "op-a2",
          csrfToken: "csrf-after-reset",
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
    return Promise.reject(new Error("unexpected " + p));
  };
  impl.calls = calls;
  impl.hit = q => calls.filter(c => c.path === q);
  return impl;
}

function withShift(opts, fn, where, search) {
  return async () => {
    const impl = service(opts);
    const app = await mountMyShift({
      fetchImpl: impl, hash: where || "", search: search || "" });
    await app.settle();
    try { await fn(app, impl); } finally { app.stop(); }
  };
}

function showing(app) {
  if (!app.$("view-reset").hidden) return "reset";
  if (!app.$("view-forgot").hidden) return "forgot";
  if (!app.$("view-signin").hidden) return "signin";
  if (!app.$("view-denied").hidden) return "denied";
  if (!app.$("view-day").hidden) return "day";
  return "nothing";
}

/* ------------------------------------------------------ asking for one */

test("a signed-out operator sees the sign-in card, not a reset door",
  withShift({}, async app => {
    assert.equal(showing(app), "signin");
  }));

test("the forgotten link opens the ask-for-a-link door",
  withShift({}, async app => {
    app.click(app.$("btn-forgot"));
    await app.settle();
    assert.equal(showing(app), "forgot");
  }));

test("asking sends the address and repeats the service's own wording",
  withShift({}, async (app, impl) => {
    app.click(app.$("btn-forgot"));
    await app.settle();
    app.$("forgot-email").value = "m.chen@verlet.co";
    app.fire(app.$("forgot-form"), "submit");
    await app.settle();

    const sent = impl.hit("/api/auth/reset/request");
    assert.equal(sent.length, 1);
    assert.equal(JSON.parse(sent[0].init.body).email, "m.chen@verlet.co");
    assert.equal(app.$("forgot-ok").textContent, SENT,
      "the page rewrote a message the service worded carefully");
  }));

test("a floor with no relay says to ask a manager instead",
  withShift({ askStatus: 404,
              askDetail: "this floor has no mail relay configured, so it "
                         + "cannot send a reset link. Ask a manager." },
    async app => {
      app.click(app.$("btn-forgot"));
      await app.settle();
      app.$("forgot-email").value = "m.chen@verlet.co";
      app.fire(app.$("forgot-form"), "submit");
      await app.settle();
      assert.equal(app.$("forgot-error").hidden, false);
      assert.match(app.$("forgot-error").textContent, /manager/i);
      assert.equal(app.$("forgot-ok").hidden, true);
    }));

test("back returns to the sign-in card",
  withShift({}, async app => {
    app.click(app.$("btn-forgot"));
    await app.settle();
    app.click(app.$("btn-forgot-back"));
    await app.settle();
    assert.equal(showing(app), "signin");
  }));

/* ------------------------------------------------------ opening a link */

test("a reset link opens the reset door, not the sign-in card",
  withShift({}, async app => {
    assert.equal(showing(app), "reset",
      "somebody who followed a reset link was shown a password box");
  }, "#reset=a-token-from-the-email"));

test("the token comes straight out of the address bar",
  withShift({}, async app => {
    assert.ok(!String(app.url()).includes("a-token-from-the-email"),
      "the reset token is still in the address bar: " + app.url());
  }, "#reset=a-token-from-the-email"));

test("setting a password sends the token, and lands on the day",
  withShift({}, async (app, impl) => {
    app.$("reset-new").value = "a-brand-new-password-34";
    app.$("reset-again").value = "a-brand-new-password-34";
    app.fire(app.$("reset-form"), "submit");
    await app.settle();

    const sent = impl.hit("/api/auth/reset");
    assert.equal(sent.length, 1);
    assert.equal(JSON.parse(sent[0].init.body).token, "a-token-from-the-email");
    assert.equal(showing(app), "day",
      "after setting a password they were not shown their day");
  }, "#reset=a-token-from-the-email"));

test("their own day is what loads, by the id the session gave",
  withShift({}, async (app, impl) => {
    app.$("reset-new").value = "a-brand-new-password-34";
    app.$("reset-again").value = "a-brand-new-password-34";
    app.fire(app.$("reset-form"), "submit");
    await app.settle();
    assert.ok(impl.hit("/api/me/shift").length >= 1
      || impl.calls.some(c => c.path.startsWith("/api/me/shift")),
      "it did not go on to read the operator's own shift");
  }, "#reset=a-token-from-the-email"));

test("two different passwords are caught before the one-use link is spent",
  withShift({}, async (app, impl) => {
    app.$("reset-new").value = "a-brand-new-password-34";
    app.$("reset-again").value = "a-different-password-56";
    app.fire(app.$("reset-form"), "submit");
    await app.settle();

    assert.equal(app.$("reset-error").hidden, false);
    assert.match(app.$("reset-error").textContent, /not the same/i);
    assert.equal(impl.hit("/api/auth/reset").length, 0,
      "it spent the one-use link on a typo it could see for itself");
  }, "#reset=a-token-from-the-email"));

test("a spent link says so and stays put",
  withShift({ useStatus: 400,
              useDetail: "that link has expired or has already been used." },
    async app => {
      app.$("reset-new").value = "a-brand-new-password-34";
      app.$("reset-again").value = "a-brand-new-password-34";
      app.fire(app.$("reset-form"), "submit");
      await app.settle();
      assert.match(app.$("reset-error").textContent, /expired|used/i);
      assert.equal(showing(app), "reset");
    }, "#reset=a-token-from-the-email"));

test("the token is read from the fragment, never the query string",
  withShift({}, async app => {
    assert.equal(showing(app), "reset", "a fragment token was not read");
  }, "#reset=a-token-from-the-email"));

test("a token in the query string is deliberately not accepted",
  withShift({}, async app => {
    /* The form that ends up in nginx's access log. Nothing issues it,
       and nothing should quietly start working again if something does. */
    assert.equal(showing(app), "signin",
      "a `?reset=` token opened the reset door");
  }, "", "?reset=a-token-from-the-email"));

test("every id the reset screens wire actually exists in the markup",
  withShift({}, async app => {
    assert.deepEqual(app.missingIds, [],
      "the script asked for ids the page does not have: " + app.missingIds);
  }, "#reset=a-token-from-the-email"));
