/* =====================================================================
 * session.js  -  who is at this screen, for every screen that asks
 *
 * Two apps now need the same four answers, and they must not answer
 * differently. That is the same argument that put the rotation in one
 * shared file: a second copy is a second answer, and the first one that
 * can silently disagree.
 *
 *   no accounts here    the service has nobody to sign in as. Open the
 *                       app and behave as it did before there was such
 *                       a thing.
 *   nobody signed in    show the sign-in card.
 *   somebody            hand back their name and role; the app decides
 *                       whether that role may use it.
 *   the service is ill  keep the door shut and say so.
 *
 * **Only silence opens the door.** A fetch that never resolves means
 * there is no service behind this page - the static deploy, `./serve.sh`,
 * the single-file build - and there is nothing to gate. Everything else
 * is a service that exists: a 401 has said no, a 500 could not answer.
 * Treating either as "no accounts configured" unlocks the screen exactly
 * when nothing can be verified, and that mistake has now been made here
 * twice, in both directions, which is why it is written down rather
 * than left to be inferred.
 *
 * A 404 is the one deliberate exception: a service too old to know this
 * route cannot be gating anything either.
 *
 * None of this is the security gate. The gate is on the service, on
 * every route - `require_manager` and `require_operator`. This only
 * stops somebody wandering into a screen they cannot use.
 * ===================================================================== */

(function (root) {
  "use strict";

  const state = {
    personAuth: "off",   // "on" once the service says accounts exist
    account: null,       // { name, role, operatorId }
    csrf: null,
    reachable: true,
    broken: false,       // the service answered, and badly
  };

  /* No service behind the page. The one case that opens an app. */
  function notServed() {
    state.reachable = false;
    state.broken = false;
    state.personAuth = "off";
    state.account = null;
    state.csrf = null;
  }

  /* Every call a screen makes to the service.
   *
   * `same-origin` credentials so the session cookie travels, and the
   * CSRF token echoed on writes - the header has to equal the cookie,
   * which is a thing only a page on this origin can arrange. */
  function api(path, init) {
    const opts = Object.assign({ credentials: "same-origin" }, init || {});
    const method = (opts.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD" && state.csrf) {
      opts.headers = Object.assign({}, opts.headers,
                                   { "x-csrf-token": state.csrf });
    }
    return fetch(path, opts);
  }

  /* Ask the service who is at this screen. Never throws. */
  async function probe() {
    state.broken = false;
    try {
      const r = await api("/api/auth/session");

      if (r.status === 404) return notServed();

      if (!r.ok) {
        state.broken = true;
        state.reachable = true;
        state.personAuth = "on";
        state.account = null;
        state.csrf = null;
        return;
      }

      const body = await r.json();
      state.reachable = true;
      state.personAuth = body.personAuth === "on" ? "on" : "off";
      state.account = body.account || null;
      state.csrf = body.csrfToken || null;
    } catch (ignored) {
      notServed();
    }
  }

  /* Exchange an email and password for a session.
   *
   * Returns { ok: true } or { ok: false, message } - one message for
   * every way of failing, because the service gives one. Saying which
   * half was wrong answers "does this person work here" for anybody who
   * cares to ask. A throttle is the exception: that is not a wrong
   * password, and telling somebody to wait is useful. */
  async function signIn(email, password) {
    try {
      const r = await api("/api/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email: email, password: password }),
      });
      const body = await r.json().catch(() => ({}));

      if (!r.ok) {
        return {
          ok: false,
          message: r.status === 429
            ? "Too many attempts. Wait a minute and try again."
            : (body.detail || "Email or password is not right."),
        };
      }

      state.personAuth = "on";
      state.broken = false;
      state.reachable = true;
      state.account = {
        name: body.name, role: body.role, operatorId: body.operatorId,
      };
      state.csrf = body.csrfToken || null;
      return { ok: true };
    } catch (ignored) {
      return { ok: false, message: "Could not reach the server." };
    }
  }

  /* End the session, here and on the service. Never throws: a sign-out
   * that can fail is one somebody gives up on, and the local half has
   * to happen either way. */
  async function signOut() {
    try { await api("/api/auth/logout", { method: "POST" }); }
    catch (ignored) { /* the local half below still matters */ }
    state.account = null;
    state.csrf = null;
  }

  /* Whether somebody with this role may use the screen asking.
   *
   * `role` is what the app requires - "manager" for the desk, "operator"
   * for My Shift. A deployment with no accounts lets everybody in, which
   * is what keeps the laptop demo working; a service that could not
   * answer lets nobody in. */
  function mayUse(role) {
    if (state.broken) return false;
    if (state.personAuth === "off") return true;
    return !!(state.account && state.account.role === role);
  }

  /* Signed in, but as the wrong sort of person. The app shows a card
   * naming them rather than a password box, because their password was
   * never the problem. */
  function wrongRole(role) {
    return !state.broken && !!state.account && state.account.role !== role;
  }

  root.Session = {
    state: state,
    api: api,
    probe: probe,
    signIn: signIn,
    signOut: signOut,
    mayUse: mayUse,
    wrongRole: wrongRole,
  };

  if (typeof module === "object" && module.exports) module.exports = root.Session;

})(typeof window !== "undefined" ? window : globalThis);
