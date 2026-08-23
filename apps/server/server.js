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

const ROOT      = path.resolve(__dirname, "..", "..");
const STATE     = process.env.STATE_FILE || path.join(__dirname, "state.json");
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
 * { pushedAt: "2026-08-22T09:00:00Z", rigs: { "RIG-01": <payload>, ... } }
 *
 * Kept in memory and written to disk on every accepted push, so a rig
 * restart or a server restart still shows the current shift. */
let store = { pushedAt: null, rigs: {} };
try {
  if (fs.existsSync(STATE)) store = JSON.parse(fs.readFileSync(STATE, "utf8"));
} catch (e) {
  console.error("could not read state.json, starting empty:", e.message);
}

function persist() {
  fs.writeFileSync(STATE, JSON.stringify(store, null, 2));
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

  const next = { pushedAt: new Date().toISOString(), rigs: {} };
  payloads.forEach(p => { next.rigs[p.rigId] = p; });
  store = next;
  persist();

  sendJSON(res, 200, { ok: true, pushedAt: store.pushedAt, count: payloads.length });
}

function sendRigSchedule(res, rigId) {
  const p = store.rigs[rigId];
  if (!p) return sendJSON(res, 404, { error: "nothing pushed for " + rigId + " yet" });
  sendJSON(res, 200, p);
}

/* --------------------------------------------------------------- static */

function sendStatic(req, res, url) {
  // Map "/" to the landing page, everything else to a file under the repo.
  let rel = decodeURIComponent(url.pathname);
  if (rel.endsWith("/")) rel += "index.html";
  const filePath = path.join(ROOT, rel);

  // Refuse anything that escapes the repo, even after ..-resolution.
  if (!filePath.startsWith(ROOT)) return sendJSON(res, 403, { error: "forbidden" });

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
