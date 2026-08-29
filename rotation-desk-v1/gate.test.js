/* =====================================================================
 * gate.test.js  -  who the desk lets in
 *
 * The desk asks the service who is at it before it draws anything, and
 * there are four answers. Three of them are doors and one is the desk.
 *
 * None of this is the security gate - that is `require_manager` on the
 * service, tested in backend/tests/test_roles.py with the browser
 * removed entirely. What is tested here is the other half: that a
 * screen nobody may use is not drawn, that the floor is not fetched
 * before anybody has said who they are, and that the push carries the
 * credentials the service now asks for.
 *
 * The case worth reading first is "no server at all". The desk has to
 * keep working as a plain static page - that is what ./serve.sh and the
 * single-file build are - so an unreachable service opens the desk,
 * while a service that answers 401 shows the card. A reply is a
 * decision; silence is not.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");

const { mountDesk } = require("./test/dom.js");

/* Boot is async now - probe, then draw. A few turns of the loop so
 * everything the boot chain queued has settled before we look. */
async function settle() {
  for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
}

/* A stand-in for the service. `who` is what /api/auth/session answers. */
function service(who, opts) {
  opts = opts || {};
  const calls = [];
  const impl = (url, init) => {
    const path = String(url);
    calls.push({ path, init: init || {} });

    if (path === "/api/auth/session") {
      if (who === "unreachable") return Promise.reject(new Error("no server"));
      return Promise.resolve({ ok: true, json: () => Promise.resolve(who) });
    }
    if (path === "/api/auth/login") {
      const body = JSON.parse(init.body);
      if (opts.loginStatus && opts.loginStatus !== 200) {
        return Promise.resolve({
          ok: false, status: opts.loginStatus,
          json: () => Promise.resolve({ detail: opts.loginDetail || "no" }),
        });
      }
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve(Object.assign(
          { name: "Ruth Osei", role: "manager", operatorId: null,
            csrfToken: "csrf-from-login" },
          opts.loginAs || {}, { email: body.email })),
      });
    }
    if (path === "/api/auth/logout") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    }
    if (path === "/api/push") {
      if (opts.pushStatus && opts.pushStatus !== 200) {
        return Promise.resolve({
          ok: false, status: opts.pushStatus,
          json: () => Promise.resolve({ detail: opts.pushDetail || "no" }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        ok: true, count: 36, pushedAt: "2026-08-27T10:00:00.000Z" }) });
    }
    if (path === "/api/state") {
      return Promise.reject(new Error("nothing pushed"));
    }
    return Promise.reject(new Error("unexpected " + path));
  };
  impl.calls = calls;
  impl.pathsHit = () => calls.map(c => c.path);
  return impl;
}

const NOBODY = { personAuth: "on", account: null, csrfToken: null };
const NO_ACCOUNTS = { personAuth: "off", account: null, csrfToken: null };
const A_MANAGER = {
  personAuth: "on", csrfToken: "csrf-from-session",
  account: { name: "Ruth Osei", role: "manager", operatorId: null },
};
const AN_OPERATOR = {
  personAuth: "on", csrfToken: "csrf-from-session",
  account: { name: "Mei Chen", role: "operator", operatorId: "op-a2" },
};

function withGate(who, opts, fn) {
  return async () => {
    const impl = service(who, opts);
    const desk = await mountDesk({ fetchImpl: impl });
    await settle();
    try { await fn(desk, impl); } finally { desk.stop(); }
  };
}

/* What is on screen, as one word. */
function showing(desk) {
  if (!desk.$("view-signin").hidden) return "signin";
  if (!desk.$("view-denied").hidden) return "denied";
  if (!desk.$("view-live").hidden) return "live";
  if (!desk.$("view-plan").hidden) return "plan";
  return "nothing";
}

/* ------------------------------------------- a floor with no accounts */

test("with no accounts configured the desk opens, exactly as it always did",
  withGate(NO_ACCOUNTS, {}, async desk => {
    assert.equal(showing(desk), "live");
    assert.equal(desk.$("who").hidden, true, "no chip where nobody signs in");
    assert.equal(desk.$("modes").hidden, false);
  }));

test("with no server at all the desk still opens - it is a static page too",
  withGate("unreachable", {}, async desk => {
    assert.equal(showing(desk), "live");
    assert.equal(desk.$("who").hidden, true);
  }));

/* ------------------------------------------------- nobody signed in */

test("a service that says somebody must sign in shows the card, not the desk",
  withGate(NOBODY, {}, async desk => {
    assert.equal(showing(desk), "signin");
    assert.equal(desk.$("view-live").hidden, true);
    assert.equal(desk.$("view-plan").hidden, true);
  }));

test("the mode tabs are gone while the door is shut",
  withGate(NOBODY, {}, async desk => {
    assert.equal(desk.$("modes").hidden, true,
      "Live/Plan must not be offered to somebody who cannot open either");
  }));

test("the floor is not fetched before anybody has said who they are",
  withGate(NOBODY, {}, async (desk, impl) => {
    assert.ok(!impl.pathsHit().includes("/api/state"),
      "the desk asked for the floor while locked: " + impl.pathsHit().join(", "));
    assert.ok(!impl.pathsHit().some(p => p.includes("/schedule.json")),
      "the desk asked for a rig's schedule while locked");
  }));

/* --------------------------------------------------- the wrong role */

test("an operator is told plainly that this is not their screen",
  withGate(AN_OPERATOR, {}, async desk => {
    assert.equal(showing(desk), "denied");
    assert.equal(desk.$("denied-name").textContent, "Mei Chen");
    assert.equal(desk.$("view-live").hidden, true);
  }));

test("an operator is not shown a sign-in box - their password is not the problem",
  withGate(AN_OPERATOR, {}, async desk => {
    assert.equal(desk.$("view-signin").hidden, true);
  }));

test("an operator still gets the chip, so they can see who they are signed in as",
  withGate(AN_OPERATOR, {}, async desk => {
    assert.equal(desk.$("who").hidden, false);
    assert.equal(desk.$("who-name").textContent, "Mei Chen");
    assert.equal(desk.$("who-role").textContent, "operator");
  }));

test("an operator never reaches the floor either",
  withGate(AN_OPERATOR, {}, async (desk, impl) => {
    assert.ok(!impl.pathsHit().includes("/api/state"));
  }));

/* ------------------------------------------------------- a manager */

test("a manager gets the desk, and the chip names them",
  withGate(A_MANAGER, {}, async desk => {
    assert.equal(showing(desk), "live");
    assert.equal(desk.$("who").hidden, false);
    assert.equal(desk.$("who-name").textContent, "Ruth Osei");
    assert.equal(desk.$("who-role").textContent, "manager");
    assert.equal(desk.$("who-role").getAttribute("data-role"), "manager");
  }));

test("a manager's desk reads the floor, which the locked one did not",
  withGate(A_MANAGER, {}, async (desk, impl) => {
    assert.ok(impl.pathsHit().includes("/api/state"));
  }));

/* ------------------------------------------------------- signing in */

test("signing in opens the desk without a reload",
  withGate(NOBODY, {}, async desk => {
    assert.equal(showing(desk), "signin");

    desk.$("in-email").value = "r.osei@verlet.co";
    desk.$("in-password").value = "not-a-real-password-12";
    desk.fire(desk.$("signin-form"), "submit");
    await settle();

    assert.equal(showing(desk), "live");
    assert.equal(desk.$("who-name").textContent, "Ruth Osei");
  }));

test("the password is cleared out of the form once it has been used",
  withGate(NOBODY, {}, async desk => {
    desk.$("in-email").value = "r.osei@verlet.co";
    desk.$("in-password").value = "not-a-real-password-12";
    desk.fire(desk.$("signin-form"), "submit");
    await settle();
    assert.equal(desk.$("in-password").value, "");
  }));

test("signing in as an operator lands on the refusal, not the desk",
  withGate(NOBODY, { loginAs: { name: "Mei Chen", role: "operator",
                                operatorId: "op-a2" } },
    async desk => {
      desk.$("in-email").value = "m.chen@verlet.co";
      desk.$("in-password").value = "not-a-real-password-12";
      desk.fire(desk.$("signin-form"), "submit");
      await settle();
      assert.equal(showing(desk), "denied");
      assert.equal(desk.$("denied-name").textContent, "Mei Chen");
    }));

test("a refused sign-in says one thing and stays on the card",
  withGate(NOBODY, { loginStatus: 401,
                     loginDetail: "email or password is not right" },
    async desk => {
      desk.$("in-email").value = "r.osei@verlet.co";
      desk.$("in-password").value = "wrong";
      desk.fire(desk.$("signin-form"), "submit");
      await settle();

      assert.equal(showing(desk), "signin");
      assert.equal(desk.$("signin-error").hidden, false);
      assert.match(desk.$("signin-error").textContent, /email or password/i);
      /* Not "no such account" and not "wrong password" - the service
         answers one way for both and the screen must not undo that. */
      assert.doesNotMatch(desk.$("signin-error").textContent, /no such|unknown|exist/i);
    }));

test("being throttled says to wait, rather than blaming the password",
  withGate(NOBODY, { loginStatus: 429 }, async desk => {
    desk.$("in-email").value = "r.osei@verlet.co";
    desk.$("in-password").value = "wrong";
    desk.fire(desk.$("signin-form"), "submit");
    await settle();
    assert.match(desk.$("signin-error").textContent, /too many|wait/i);
  }));

test("a sign-in with no server behind it says so",
  withGate(NOBODY, {}, async desk => {
    // swap the stub for one that fails only the login
    global.fetch = (url) => String(url) === "/api/auth/login"
      ? Promise.reject(new Error("down"))
      : Promise.reject(new Error("down"));
    desk.$("in-email").value = "r.osei@verlet.co";
    desk.$("in-password").value = "x";
    desk.fire(desk.$("signin-form"), "submit");
    await settle();
    assert.match(desk.$("signin-error").textContent, /could not reach/i);
  }));

/* ------------------------------------------------------ signing out */

test("signing out shuts the door again",
  withGate(A_MANAGER, {}, async desk => {
    assert.equal(showing(desk), "live");
    desk.click(desk.$("btn-signout"));
    await settle();

    assert.equal(showing(desk), "signin");
    assert.equal(desk.$("who").hidden, true);
    assert.equal(desk.$("modes").hidden, true);
  }));

test("signing out drops the floor it was holding",
  withGate(A_MANAGER, {}, async desk => {
    desk.click(desk.$("btn-signout"));
    await settle();
    /* A board left on screen behind a login box is the floor's roster
       still readable by whoever walks up next. */
    assert.equal(desk.$("view-live").hidden, true);
    assert.equal(desk.board().length, 0);
  }));

test("an operator can sign out of the refusal screen",
  withGate(AN_OPERATOR, {}, async desk => {
    assert.equal(showing(desk), "denied");
    desk.click(desk.$("btn-denied-out"));
    await settle();
    assert.equal(showing(desk), "signin");
  }));

/* ------------------------------------------------- what the push sends */

test("the push carries the CSRF token the service handed out",
  withGate(A_MANAGER, {}, async (desk, impl) => {
    desk.mode("plan");
    desk.click(desk.$("btn-push"));
    await settle();

    const push = impl.calls.find(c => c.path === "/api/push");
    assert.ok(push, "the push never went");
    assert.equal(push.init.headers["x-csrf-token"], "csrf-from-session");
  }));

test("the push sends the session cookie with it",
  withGate(A_MANAGER, {}, async (desk, impl) => {
    desk.mode("plan");
    desk.click(desk.$("btn-push"));
    await settle();
    const push = impl.calls.find(c => c.path === "/api/push");
    assert.equal(push.init.credentials, "same-origin");
  }));

test("reading the floor sends the cookie too, or a manager reads nothing",
  withGate(A_MANAGER, {}, async (desk, impl) => {
    const state = impl.calls.find(c => c.path === "/api/state");
    assert.ok(state, "the desk never asked for the floor");
    assert.equal(state.init.credentials, "same-origin");
  }));

test("a read does not carry the CSRF token - it is for writes",
  withGate(A_MANAGER, {}, async (desk, impl) => {
    const state = impl.calls.find(c => c.path === "/api/state");
    assert.ok(!state.init.headers || !state.init.headers["x-csrf-token"]);
  }));

test("after signing in, the push uses the token that sign-in returned",
  withGate(NOBODY, {}, async (desk, impl) => {
    desk.$("in-email").value = "r.osei@verlet.co";
    desk.$("in-password").value = "not-a-real-password-12";
    desk.fire(desk.$("signin-form"), "submit");
    await settle();

    desk.mode("plan");
    desk.click(desk.$("btn-push"));
    await settle();

    const push = impl.calls.find(c => c.path === "/api/push");
    assert.equal(push.init.headers["x-csrf-token"], "csrf-from-login");
  }));

test("a push refused for who you are is not reported as a bad schedule",
  withGate(A_MANAGER, { pushStatus: 403,
                        pushDetail: "the desk is for managers" },
    async desk => {
      desk.mode("plan");
      desk.click(desk.$("btn-push"));
      await settle();

      /* "Rejected" sends a manager hunting through a schedule that is
         perfectly fine. The schedule was never the problem. */
      assert.doesNotMatch(desk.$("push-note").textContent, /rejected/i);
      assert.match(desk.$("push-note").textContent, /manager/i);
    }));

test("a session that expired while the tab sat open sends you back to the card",
  withGate(A_MANAGER, { pushStatus: 401, pushDetail: "not signed in" },
    async desk => {
      desk.mode("plan");
      // the service has since forgotten this session
      global.fetch = (url, init) => {
        const path = String(url);
        if (path === "/api/auth/session") {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(NOBODY) });
        }
        if (path === "/api/push") {
          return Promise.resolve({ ok: false, status: 401,
            json: () => Promise.resolve({ detail: "not signed in" }) });
        }
        return Promise.reject(new Error("no"));
      };
      desk.click(desk.$("btn-push"));
      await settle();

      assert.equal(showing(desk), "signin",
        "an expired session should put the sign-in card back up");
    }));

/* ------------------------------- a service that answers, and answers badly
 *
 * The distinction this file exists to protect. Three different things
 * can go wrong with the probe and they do not mean the same:
 *
 *   nothing answers    there is no service - the static deploy. Open.
 *   404                a service too old to have the route. Open.
 *   500 / 502 / 503    a service that is there and cannot answer. Shut.
 *
 * The third was originally read as the first, so a database outage
 * opened the desk - exactly when nothing could be verified. It is the
 * same category error as treating a 401 as "no accounts configured",
 * which this code was already careful about in the other direction.
 */

function answering(status) {
  const impl = (url) => {
    if (String(url) === "/api/auth/session") {
      return Promise.resolve({
        ok: false, status,
        json: () => Promise.resolve({ detail: "internal error" }),
      });
    }
    return Promise.reject(new Error("no"));
  };
  impl.calls = [];
  impl.pathsHit = () => [];
  return impl;
}

function withProbe(status, fn) {
  return async () => {
    const desk = await mountDesk({ fetchImpl: answering(status) });
    await settle();
    try { await fn(desk); } finally { desk.stop(); }
  };
}

[500, 502, 503].forEach(status => {
  test("a " + status + " from the service does not open the desk",
    withProbe(status, async desk => {
      assert.notEqual(showing(desk), "live",
        "a broken service opened the desk - a 500 is not 'no accounts here'");
      assert.equal(desk.$("view-live").hidden, true);
      assert.equal(desk.$("modes").hidden, true);
    }));
});

test("a broken service says what is wrong, not that the password is",
  withProbe(500, async desk => {
    assert.equal(showing(desk), "signin");
    assert.equal(desk.$("signin-error").hidden, false);
    assert.match(desk.$("signin-error").textContent, /not answering/i);
    assert.doesNotMatch(desk.$("signin-error").textContent, /password is not right/i);
  }));

test("a 404 is a server too old for the route, and still opens the desk",
  withProbe(404, async desk => {
    /* Nothing there to gate, so the desk behaves as it did before the
       route existed. This is what keeps an older deployment working. */
    assert.equal(showing(desk), "live");
    assert.equal(desk.$("who").hidden, true);
  }));

test("a broken service is not mistaken for a floor with no accounts",
  withProbe(503, async desk => {
    /* The specific confusion: "off" means nobody signs in here, and a
       service that could not answer has said no such thing. */
    assert.equal(desk.$("view-live").hidden, true);
  }));

/* ------------------------------------------- being told how long to wait
 *
 * The lockout backs off - one minute, then two, four, eight, up to
 * fifteen. A screen that says "wait a minute" whatever the service sent
 * is wrong by a factor of fifteen at the far end, and somebody who
 * believes it tries again fourteen times and stays locked the whole
 * while, each attempt pushing the wait out further.
 */

function refusing(status, retryAfter) {
  return (url) => {
    if (String(url) === "/api/auth/session") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(NOBODY) });
    }
    if (String(url) === "/api/auth/login") {
      return Promise.resolve({
        ok: false, status,
        headers: { get: (h) => (h === "retry-after" ? retryAfter : null) },
        json: () => Promise.resolve({ detail: "too many failed sign-in attempts" }),
      });
    }
    return Promise.reject(new Error("no"));
  };
}

async function messageAfterRefusal(retryAfter) {
  const desk = await mountDesk({ fetchImpl: refusing(429, retryAfter) });
  await settle();
  try {
    desk.$("in-email").value = "r.osei@verlet.co";
    desk.$("in-password").value = "wrong";
    desk.fire(desk.$("signin-form"), "submit");
    await settle();
    return desk.$("signin-error").textContent;
  } finally { desk.stop(); }
}

test("a long wait is reported as the length it actually is", async () => {
  assert.match(await messageAfterRefusal("480"), /8 minutes/);
  assert.match(await messageAfterRefusal("900"), /15 minutes/);
});

test("a short wait is still a minute, and reads as one", async () => {
  assert.match(await messageAfterRefusal("30"), /a minute/);
  assert.match(await messageAfterRefusal("60"), /1 minute\b/);
});

test("a service that sends no Retry-After still says something usable", async () => {
  const msg = await messageAfterRefusal(null);
  assert.match(msg, /too many/i);
  assert.match(msg, /minute/i);
});

test("being throttled never reads as a wrong password", async () => {
  /* The one thing this message must not do. Somebody who is locked out
     and told their password is wrong changes a password that was right. */
  const msg = await messageAfterRefusal("300");
  assert.doesNotMatch(msg, /password is not right/i);
});
