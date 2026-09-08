/* =====================================================================
 * reset.test.js  -  a password nobody remembers, from the desk
 *
 * The service is the gate, and it is tested with the browser removed in
 * backend/tests/test_password_reset.py: the token is single-use there,
 * the reply says nothing about who exists there, and the routes refuse
 * with no relay there. None of that is re-litigated here.
 *
 * What this covers is the half a browser owns, and three things specific
 * to it:
 *
 *  - a reset link opens the *reset* door, not the sign-in card. Somebody
 *    who followed the link has no password; offering them the box is
 *    offering the one thing they know does not work.
 *  - the token comes out of the address bar immediately. It is a
 *    credential while it lives, and a URL is the least private place on
 *    a screen: history, referrers, and whoever is standing behind you.
 *  - the page repeats the service's wording rather than composing its
 *    own. The service is careful not to say whether an address exists,
 *    and a page that said "no such account" would undo that.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");

const { mountDesk } = require("./test/dom.js");

async function settle() {
  for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
}

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
          name: "Ruth Osei", role: "manager", operatorId: null,
          csrfToken: "csrf-after-reset",
        }),
      });
    }
    if (p === "/api/state") return Promise.reject(new Error("nothing pushed"));
    if (p === "/api/push") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        ok: true, count: 36, pushedAt: "2026-08-27T10:00:00.000Z" }) });
    }
    return Promise.reject(new Error("unexpected " + p));
  };
  impl.calls = calls;
  impl.hit = q => calls.filter(c => c.path === q);
  return impl;
}

/* `search` is this page's query string, which is how a reset link
 * arrives. */
function withDesk(opts, fn, where, search) {
  return async () => {
    const impl = service(opts);
    const desk = await mountDesk({
      fetchImpl: impl, hash: where || "", search: search || "" });
    await settle();
    try { await fn(desk, impl); } finally { desk.stop(); }
  };
}

function showing(desk) {
  if (!desk.$("view-reset").hidden) return "reset";
  if (!desk.$("view-forgot").hidden) return "forgot";
  if (!desk.$("view-signin").hidden) return "signin";
  if (!desk.$("view-denied").hidden) return "denied";
  if (!desk.$("view-live").hidden) return "live";
  if (!desk.$("view-plan").hidden) return "plan";
  return "nothing";
}

/* ------------------------------------------------------ asking for one */

test("the sign-in card is what a signed-out desk shows",
  withDesk({}, async desk => {
    assert.equal(showing(desk), "signin");
    assert.equal(desk.$("view-forgot").hidden, true);
    assert.equal(desk.$("view-reset").hidden, true);
  }));

test("the forgotten link opens the ask-for-a-link door",
  withDesk({}, async desk => {
    desk.click(desk.$("btn-forgot"));
    await settle();
    assert.equal(showing(desk), "forgot");
  }));

test("it carries over the address already typed, so it is not typed twice",
  withDesk({}, async desk => {
    desk.$("in-email").value = "r.osei@verlet.co";
    desk.click(desk.$("btn-forgot"));
    await settle();
    assert.equal(desk.$("forgot-email").value, "r.osei@verlet.co");
  }));

test("back returns to the sign-in card",
  withDesk({}, async desk => {
    desk.click(desk.$("btn-forgot"));
    await settle();
    desk.click(desk.$("btn-forgot-back"));
    await settle();
    assert.equal(showing(desk), "signin");
  }));

test("asking sends the address and shows the service's own wording",
  withDesk({}, async (desk, impl) => {
    desk.click(desk.$("btn-forgot"));
    await settle();
    desk.$("forgot-email").value = "r.osei@verlet.co";
    desk.fire(desk.$("forgot-form"), "submit");
    await settle();

    const sent = impl.hit("/api/auth/reset/request");
    assert.equal(sent.length, 1);
    assert.equal(JSON.parse(sent[0].init.body).email, "r.osei@verlet.co");

    assert.equal(desk.$("forgot-ok").hidden, false);
    assert.equal(desk.$("forgot-ok").textContent, SENT,
      "the page rewrote a message the service worded carefully");
  }));

test("the same wording comes back for an address with no account",
  withDesk({}, async desk => {
    /* The stub answers identically because the service does. What is
       asserted is that the page has not added a branch of its own that
       would put the enumeration leak back. */
    desk.click(desk.$("btn-forgot"));
    await settle();
    desk.$("forgot-email").value = "nobody-at-all@verlet.co";
    desk.fire(desk.$("forgot-form"), "submit");
    await settle();
    assert.equal(desk.$("forgot-ok").textContent, SENT);
    assert.equal(desk.$("forgot-error").hidden, true);
  }));

test("a floor with no mail relay says so instead of pretending",
  withDesk({ askStatus: 404,
             askDetail: "this floor has no mail relay configured, so it "
                        + "cannot send a reset link. Ask a manager." },
    async desk => {
      desk.click(desk.$("btn-forgot"));
      await settle();
      desk.$("forgot-email").value = "r.osei@verlet.co";
      desk.fire(desk.$("forgot-form"), "submit");
      await settle();
      assert.equal(desk.$("forgot-error").hidden, false);
      assert.match(desk.$("forgot-error").textContent, /manager/i,
        "somebody would be left waiting for mail that is not coming");
      assert.equal(desk.$("forgot-ok").hidden, true);
    }));

test("a throttle says how long to wait, in the units the service gave",
  withDesk({ askStatus: 429, retryAfter: "900" }, async desk => {
    desk.click(desk.$("btn-forgot"));
    await settle();
    desk.$("forgot-email").value = "r.osei@verlet.co";
    desk.fire(desk.$("forgot-form"), "submit");
    await settle();
    assert.match(desk.$("forgot-error").textContent, /15 minutes/);
  }));

/* ------------------------------------------------------ opening a link */

test("a reset link opens the reset door, not the sign-in card",
  withDesk({}, async desk => {
    assert.equal(showing(desk), "reset",
      "somebody who followed a reset link was shown a password box");
  }, "#reset=a-token-from-the-email"));

test("the token comes straight out of the address bar",
  withDesk({}, async desk => {
    /* A credential in a URL is in the history, in the referrer of
       anything this page loads next, and on screen behind whoever is
       using it. */
    assert.equal(showing(desk), "reset");
    assert.ok(!String(desk.url()).includes("a-token-from-the-email"),
      "the reset token is still in the address bar: " + desk.url());
  }, "#reset=a-token-from-the-email"));

test("setting a password sends the token and the new password",
  withDesk({}, async (desk, impl) => {
    desk.$("reset-new").value = "a-brand-new-password-34";
    desk.$("reset-again").value = "a-brand-new-password-34";
    desk.fire(desk.$("reset-form"), "submit");
    await settle();

    const sent = impl.hit("/api/auth/reset");
    assert.equal(sent.length, 1);
    const body = JSON.parse(sent[0].init.body);
    assert.equal(body.token, "a-token-from-the-email");
    assert.equal(body.newPassword, "a-brand-new-password-34");
  }, "#reset=a-token-from-the-email"));

test("and it lands on the desk, signed in, without a second login",
  withDesk({}, async desk => {
    desk.$("reset-new").value = "a-brand-new-password-34";
    desk.$("reset-again").value = "a-brand-new-password-34";
    desk.fire(desk.$("reset-form"), "submit");
    await settle();
    assert.ok(["live", "plan"].includes(showing(desk)),
      "after setting a password they were not let in, showing: " + showing(desk));
  }, "#reset=a-token-from-the-email"));

test("the new CSRF token is the one used afterwards",
  withDesk({}, async (desk, impl) => {
    desk.$("reset-new").value = "a-brand-new-password-34";
    desk.$("reset-again").value = "a-brand-new-password-34";
    desk.fire(desk.$("reset-form"), "submit");
    await settle();

    desk.click(desk.$("btn-push"));
    await settle();
    const pushes = impl.hit("/api/push");
    assert.ok(pushes.length >= 1, "the push did not go");
    assert.equal(pushes[pushes.length - 1].init.headers["x-csrf-token"],
      "csrf-after-reset");
  }, "#reset=a-token-from-the-email"));

test("two different passwords are caught before the token is spent",
  withDesk({}, async (desk, impl) => {
    /* A link works once. Burning it on a typo the page could see for
       itself would send somebody back to their mailbox. */
    desk.$("reset-new").value = "a-brand-new-password-34";
    desk.$("reset-again").value = "a-different-password-56";
    desk.fire(desk.$("reset-form"), "submit");
    await settle();

    assert.equal(desk.$("reset-error").hidden, false);
    assert.match(desk.$("reset-error").textContent, /not the same/i);
    assert.equal(impl.hit("/api/auth/reset").length, 0,
      "it spent the one-use link on a typo it could see for itself");
  }, "#reset=a-token-from-the-email"));

test("a spent or expired link says so, and stays on the reset door",
  withDesk({ useStatus: 400,
             useDetail: "that link has expired or has already been used. "
                        + "Ask for a new one." },
    async desk => {
      desk.$("reset-new").value = "a-brand-new-password-34";
      desk.$("reset-again").value = "a-brand-new-password-34";
      desk.fire(desk.$("reset-form"), "submit");
      await settle();
      assert.equal(desk.$("reset-error").hidden, false);
      assert.match(desk.$("reset-error").textContent, /expired|used/i);
      assert.equal(showing(desk), "reset");
    }, "#reset=a-token-from-the-email"));

test("a short password is the service's to refuse, and is not pre-empted",
  withDesk({ useStatus: 400, useDetail: "too short - 12 characters at the very least" },
    async (desk, impl) => {
      /* One rule, in one place. A second length check in the page is a
         second answer that can drift from the first. */
      desk.$("reset-new").value = "short";
      desk.$("reset-again").value = "short";
      desk.fire(desk.$("reset-form"), "submit");
      await settle();
      assert.equal(impl.hit("/api/auth/reset").length, 1,
        "the page invented its own length rule instead of asking");
      assert.match(desk.$("reset-error").textContent, /short/i);
    }, "#reset=a-token-from-the-email"));

test("the token is read from the fragment, never the query string",
  withDesk({}, async desk => {
    /* The half of FIX 2 that a token-shaped assertion cannot see.
       If the service emits `#reset=` while this reader still looks at
       `location.search`, every real link breaks - and a test that only
       splits on "reset=" passes either way, because it matches both
       forms. So: a fragment must work, and a query string must not. */
    assert.equal(showing(desk), "reset", "a fragment token was not read");
  }, "#reset=a-token-from-the-email"));

test("a token in the query string is deliberately not accepted",
  withDesk({}, async desk => {
    /* Nothing has ever issued one - this has not shipped - and a reader
       that took both would let the service quietly go back to the form
       that gets written into nginx's access log. */
    assert.equal(showing(desk), "signin",
      "a `?reset=` token opened the reset door, so the logged form still works");
  }, "", "?reset=a-token-from-the-email"));

test("no reset in the URL means no reset door",
  withDesk({}, async desk => {
    assert.equal(showing(desk), "signin");
  }, "?view=live"));

test("every id the reset screens wire actually exists in the markup",
  withDesk({}, async desk => {
    assert.deepEqual(desk.missingIds, [],
      "the script asked for ids the page does not have: " + desk.missingIds);
  }, "#reset=a-token-from-the-email"));
