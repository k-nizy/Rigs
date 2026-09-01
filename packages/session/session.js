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
            ? "Too many attempts. Try again in " + waitFor(r) + "."
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

  /* How long the service said to wait, in words.
   *
   * "a minute" was the message whatever the header said, which is wrong
   * by a factor of fifteen once a run of failures has been backing off
   * for a while - and somebody told to wait a minute who is actually
   * locked for fifteen tries again fourteen times and stays locked. */
  function waitFor(response) {
    const secs = Number(response.headers && response.headers.get
      ? response.headers.get("retry-after") : 0);
    if (!secs || secs < 60) return "a minute";
    const mins = Math.ceil(secs / 60);
    return mins + " minute" + (mins === 1 ? "" : "s");
  }

  /* Ask for a link to set a new password.
   *
   * The reply is the same whether or not that address has an account -
   * deliberately, on the service - so this returns the same thing too.
   * A page that said "no such account" would put the enumeration leak
   * back in the one place the service went to the trouble of closing it.
   *
   * 404 means this floor has no mail relay, which is a real answer and
   * has to be shown rather than swallowed: the person needs to know to
   * ask a manager instead of waiting for mail that is not coming. */
  async function requestReset(email) {
    try {
      const r = await api("/api/auth/reset/request", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email: email }),
      });
      const body = await r.json().catch(() => ({}));

      if (!r.ok) {
        return {
          ok: false,
          message: r.status === 429
            ? "Too many requests. Try again in " + waitFor(r) + "."
            : (body.detail || "Could not send a link."),
        };
      }
      return { ok: true, message: body.detail || "" };
    } catch (ignored) {
      return { ok: false, message: "Could not reach the server." };
    }
  }

  /* Spend a reset token and set the password.
   *
   * The service signs them in on the way out, so the reply carries a
   * session and a CSRF token like a login does - and this has to record
   * both, or the page is signed in without knowing it. */
  async function finishReset(token, newPassword) {
    try {
      const r = await api("/api/auth/reset", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token: token, newPassword: newPassword }),
      });
      const body = await r.json().catch(() => ({}));

      if (!r.ok) {
        return { ok: false, message: body.detail || "That did not work." };
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

  /* The reset token in this page's own URL, or null.
   *
   * Read once at boot and then taken out of the address bar by the
   * caller - a token left in the URL is a token in the browser history,
   * in the referrer of anything the page loads, and on the screen of
   * whoever is standing behind the person using it. */
  function resetTokenInUrl(search) {
    const q = search === undefined
      ? (root.location ? root.location.search : "") : search;
    const m = /[?&]reset=([^&]+)/.exec(q || "");
    return m ? decodeURIComponent(m[1]) : null;
  }

  /* Change the password of whoever is signed in here.
   *
   * Unlike signIn, this says which half was wrong. There is no account
   * to enumerate: the caller is already signed in and is asking about
   * their own password, so telling them plainly saves them guessing at
   * both halves at once.
   *
   * The service revokes every session including this one and issues a
   * fresh pair, so the new CSRF token has to be taken from the reply.
   * Without that the page keeps a token the service has already thrown
   * away, and the next thing the person does fails for no reason they
   * could work out. */
  async function changePassword(currentPassword, newPassword) {
    try {
      const r = await api("/api/auth/password", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          currentPassword: currentPassword, newPassword: newPassword,
        }),
      });
      const body = await r.json().catch(() => ({}));

      if (!r.ok) {
        return {
          ok: false,
          message: r.status === 429
            ? "Too many attempts. Try again in " + waitFor(r) + "."
            : (body.detail || "That did not work."),
        };
      }

      if (body.csrfToken) state.csrf = body.csrfToken;
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
    changePassword: changePassword,
    requestReset: requestReset,
    finishReset: finishReset,
    resetTokenInUrl: resetTokenInUrl,
    mayUse: mayUse,
    wrongRole: wrongRole,
  };

  if (typeof module === "object" && module.exports) module.exports = root.Session;

})(typeof window !== "undefined" ? window : globalThis);
