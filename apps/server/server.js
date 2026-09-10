#!/usr/bin/env node
/* =====================================================================
 * server.js
 *
 * Two jobs, no dependencies:
 *
 *   1. serve the static repo tree so the desk and every rig load in a
 *      browser (this is what serve.sh used to do)
 *
 *   2. carry the push - the one thing the reference sheet explicitly
 *      asks for that the two apps could not do alone:
 *
 *        POST /api/push                       body: { payloads: [...] }
 *        GET  /api/rigs/:rigId/schedule.json  -> the payload for that rig
 *        GET  /api/state                      -> everything currently pushed
 *        GET  /api/roster                     -> who is on the floor, as the last push said
 *
 * Each payload is validated against packages/schema/payload.js before it
 * is stored, so a malformed push is rejected at the door and never
 * reaches a rig. The store is a plain JSON file so a restart does not
 * lose the current shift.
 * ===================================================================== */

"use strict";

const http = require("node:http");
const fs   = require("node:fs");
const path = require("node:path");

const { validate } = require("../../packages/schema/payload.js");
const RE = require("../../packages/engine/rotation-engine.js");

const ROOT      = path.resolve(__dirname, "..", "..");
const STATE     = process.env.STATE_FILE || path.join(__dirname, "state.json");
const PUSHLOG   = process.env.PUSH_LOG_FILE ||
                  path.join(path.dirname(STATE), "pushes.jsonl");
const PORT      = Number(process.env.PORT || 8765);
const HOST      = process.env.HOST || "127.0.0.1";
const MIME      = {
  ".html": "text/html; charset=utf-8",
  ".css":  "text/css; charset=utf-8",
  ".js":   "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg":  "image/svg+xml",
  ".png":  "image/png",
  ".ico":  "image/x-icon",
  ".webmanifest": "application/manifest+json",
};

/* ------------------------------------------------------------- the store
 *
 * { pushedAt: "2026-08-22T09:00:00Z", rigs: { "RIG-01": [<payload>, ...] } }
 *
 * Kept in memory so a request costs no disk. What it is *derived from*
 * is `pushes.jsonl`, one line per accepted push, appended and never
 * rewritten - the same shape the backend's ledger has, for the same
 * reason: a push replaces the floor whole, and without a record of what
 * it replaced, "the desk pushed the wrong roster at 06:00, put it back"
 * has no answer.
 *
 * `state.json` survives as a cache of the last line, so a restart does
 * not have to read the whole log. Losing it costs nothing.
 *
 * The log is history for people and for recovery. It is deliberately
 * not an input to `pick()` - which schedule a rig runs is decided by
 * the window the desk wrote, compared against now, and by nothing else.
 * A push id that reached a rig would be a second answer to "which
 * schedule is real", and the invariant is that there is exactly one. */
let store = { pushedAt: null, rigs: {} };

/* Every push that was accepted, oldest first. A trailing unparseable
   line is a torn append - the process died mid-write - and costs only
   that one entry, because everything before it is already complete. */
function readPushLog() {
  if (!fs.existsSync(PUSHLOG)) return null;
  const lines = fs.readFileSync(PUSHLOG, "utf8").split("\n").filter(l => l.trim());
  const out = [];
  lines.forEach((line, i) => {
    try { out.push(JSON.parse(line)); }
    catch {
      if (i === lines.length - 1) console.error("pushes.jsonl: ignoring a torn final line");
      else throw new Error("pushes.jsonl is damaged at line " + (i + 1));
    }
  });
  return out;
}

function storeFrom(entry) {
  // The roster the push carried, or null for a push that carried none -
  // a desk that predates the field, or the demo. Kept in the log line
  // beside the payloads it was built from, so a restart brings it back.
  const next = { pushedAt: entry.pushedAt, roster: entry.roster || null, rigs: {} };
  entry.payloads.forEach(p => { (next.rigs[p.rigId] = next.rigs[p.rigId] || []).push(p); });
  return next;
}

/* The log is the record; state.json is a cache; a corrupt cache with no
   record behind it is the one case we refuse to start on. Coming up
   empty would answer 404 for all twelve rigs, and twelve rigs sitting
   in Standby looks exactly like a manager who forgot to push - the
   failure would be silent on the only screen anyone checks. Same trade
   as a rig that cannot place itself: refuse rather than guess. */
try {
  const log = readPushLog();
  if (log && log.length) {
    store = storeFrom(log[log.length - 1]);
    persist();
  } else if (fs.existsSync(STATE)) {
    store = JSON.parse(fs.readFileSync(STATE, "utf8"));
  }
} catch (e) {
  console.error("cannot read the floor's state (" + STATE + "): " + e.message);
  console.error("refusing to start empty - twelve rigs would go to Standby with no signal.");
  process.exit(1);
}

/* Truncate-in-place is what left a half-written file to be found at the
   next boot. Write beside it, then rename over it: a reader sees either
   the whole old file or the whole new one. */
function persist() {
  const tmp = STATE + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(store, null, 2));
  fs.renameSync(tmp, STATE);
}

function appendPush(entry) {
  fs.appendFileSync(PUSHLOG, JSON.stringify(entry) + "\n");
}

/* ------------------------------------------------------------ the server */

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host}`);

    if (req.method === "POST" && url.pathname === "/api/push") {
      return await handlePush(req, res);
    }

    let m;
    if (req.method === "GET" && (m = url.pathname.match(/^\/api\/rigs\/([^/]+)\/schedule\.json$/))) {
      return sendRigSchedule(res, decodeURIComponent(m[1]));
    }

    if (req.method === "GET" && url.pathname === "/api/state") {
      return sendJSON(res, 200, { pushedAt: store.pushedAt, rigs: Object.keys(store.rigs) });
    }

    if (req.method === "GET" && url.pathname === "/api/roster") {
      return sendJSON(res, 200, { pushedAt: store.pushedAt, roster: store.roster || null });
    }

    if (req.method === "GET" || req.method === "HEAD") {
      return sendStatic(req, res, url);
    }

    sendJSON(res, 405, { error: "method not allowed" });
  } catch (err) {
    console.error(err);
    sendJSON(res, 500, { error: err.message });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`floor  -> http://${HOST}:${PORT}/`);
  console.log(`desk   -> http://${HOST}:${PORT}/rotation-desk-v1/`);
  console.log(`rig    -> http://${HOST}:${PORT}/apps/rig/`);
  console.log(`push   -> POST http://${HOST}:${PORT}/api/push`);
});

/* ----------------------------------------------------------------- push */

async function handlePush(req, res) {
  const body = await readBody(req);
  let parsed;
  try { parsed = JSON.parse(body); }
  catch { return sendJSON(res, 400, { error: "body is not JSON" }); }

  const payloads = parsed && parsed.payloads;
  if (!Array.isArray(payloads) || payloads.length === 0) {
    return sendJSON(res, 400, { error: "body must be { payloads: [...] } with at least one" });
  }

  // The roster is optional, and shape-checked exactly as far as the real
  // service checks it: a list of groups. Anything else would have the
  // next desk open on garbage, and is refused with the whole push.
  const roster = parsed.roster == null ? null : parsed.roster;
  if (roster !== null && !(Array.isArray(roster) && roster.every(g => g && typeof g === "object" && !Array.isArray(g)))) {
    return sendJSON(res, 422, { error: "roster must be a list of groups, or absent" });
  }

  // Validate every payload first. All-or-nothing: a bad push must not
  // leave the floor with a mix of new and stale schedules.
  const problems = [];
  payloads.forEach((p, i) => {
    const v = validate(p);
    if (!v.ok) problems.push({ index: i, rigId: p && p.rigId, errors: v.errors });
  });
  if (problems.length) {
    return sendJSON(res, 422, { error: "one or more payloads failed validation", problems });
  }

  // Every shift of the day, not just the one on screen. A payload covers
  // one shift, so keying a rig to a single payload meant the last one
  // pushed silently won and the floor ran whichever shift happened to be
  // written last.
  // The log first, then the floor. If the append fails there is no record
  // of this push, so it must not become the schedule either - a floor the
  // log cannot account for is the thing the log exists to prevent.
  const entry = { pushedAt: new Date().toISOString(), payloads, roster };
  try { appendPush(entry); }
  catch (e) { return sendJSON(res, 500, { error: "could not record the push: " + e.message }); }

  store = storeFrom(entry);
  persist();

  sendJSON(res, 200, { ok: true, pushedAt: store.pushedAt, count: payloads.length });
}

/* What this rig was pushed, oldest first. Tolerates a state.json written
   before a push carried the whole day, so a restart across the change
   does not 404 the floor. */
function heldFor(rigId) {
  const held = store.rigs[rigId];
  if (!held) return [];
  return Array.isArray(held) ? held : [held];
}

/* The schedule this rig should be running, by comparison against the
   window the desk wrote - never by working out when a shift runs. */
function pick(held, now) {
  const live = RE.inForce(held, now);
  if (live) return live;

  // Nothing covers now. Show the shift that starts next, so a rig sitting
  // before its first turn is waiting on the right one rather than holding
  // a finished schedule. The rig reads that as Standby, which is true.
  const dated = held.map(p => [RE.shiftWindow(p), p]).filter(x => x[0]);
  if (!dated.length) return held[0];

  // Chosen from the payloads, never from the order they arrived in. Every
  // rig on the floor holds the same shifts, so every rig has to land on
  // the same answer - otherwise one rig waits in Standby against the Day
  // sheet while its neighbour waits against the Night one, out of a
  // single push.
  const upcoming = dated.filter(x => x[0].start > now);
  if (upcoming.length) {
    return upcoming.reduce((a, b) => (b[0].start < a[0].start ? b : a))[1];
  }
  // They have all finished: the one that finished most recently.
  return dated.reduce((a, b) => (b[0].end > a[0].end ? b : a))[1];
}

function sendRigSchedule(res, rigId) {
  const held = heldFor(rigId);
  if (!held.length) return sendJSON(res, 404, { error: "nothing pushed for " + rigId + " yet" });
  sendJSON(res, 200, pick(held, Date.now()));
}

/* --------------------------------------------------------------- static */

/* The three folders nginx serves, and the landing page. Everything else
 * under this checkout is not web content and must not be reachable.
 *
 * This used to serve the whole repository, because it only refused paths
 * that escaped it. `deploy/nginx.conf` says plainly that nothing under
 * `backend/` should be served, and this contradicted that: `/backend/.env`
 * came back with the database password in it, and `/.git/config` with the
 * remote. Bound to 127.0.0.1 by default, so it took `HOST=0.0.0.0` to
 * matter - which is the obvious thing to type to show somebody the demo
 * on the office network.
 *
 * Same list, same order, same reason as the location blocks in nginx.conf.
 * If one changes the other has to.
 */
const SERVED = [path.join("apps", "rig"), path.join("apps", "my-shift"),
                "packages", "rotation-desk-v1"]
  .map((d) => path.join(ROOT, d) + path.sep);
const LANDING = path.join(ROOT, "index.html");

function sendStatic(req, res, url) {
  // Map "/" to the landing page, everything else to a file under the repo.
  let rel = decodeURIComponent(url.pathname);
  const isRoot = rel === "/";
  if (isRoot) rel = "/index.html";
  if (rel.endsWith("/")) rel += "index.html";

  /* Resolved first, then checked. A prefix test on the raw path is not a
     check at all: `/apps/../backend/.env` starts with an allowed folder
     and lands two directories away from it. */
  const filePath = path.resolve(ROOT, "." + rel);

  /* The landing page answers to "/" and to nothing else, which is what
     nginx does - `location = /` serves it and `location /` returns 404.
     A dev server that answers more URLs than the deployment is one that
     hides the difference until somebody is standing on a floor. */
  const allowed = (isRoot && filePath === LANDING)
    || SERVED.some((dir) => filePath.startsWith(dir));
  if (!allowed) return sendJSON(res, 404, { error: "not found" });

  fs.stat(filePath, (err, stat) => {
    if (err || !stat.isFile()) return sendJSON(res, 404, { error: "not found" });
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, {
      "content-type":  MIME[ext] || "application/octet-stream",
      "cache-control": "no-cache",
    });
    if (req.method === "HEAD") return res.end();
    fs.createReadStream(filePath).pipe(res);
  });
}

/* --------------------------------------------------------------- helpers */

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", chunk => { data += chunk; if (data.length > 5e6) reject(new Error("body too large")); });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}
function sendJSON(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    "content-type":  "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  res.end(body);
}
