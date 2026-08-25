/* =====================================================================
 * credentials.test.js  -  the machine authenticates, the operator never does
 *
 * The rig has no login by design, and that is not an omission - it is the
 * central idea of the screen. So the credential belongs to the machine:
 * Ansible places a token beside /etc/rig/id, the page is served with it,
 * and every call the rig makes carries it. Nobody standing at the rig
 * types anything, ever.
 *
 * Two things are worth pinning:
 *
 *   every call carries it  - a route added later must not be the one that
 *                            quietly forgot, which is why they all go
 *                            through one wrapper
 *   only ours does         - a presigned upload URL points at the object
 *                            store and is already signed. Attaching our
 *                            bearer token to it sends a floor credential
 *                            to somebody else's server, and into somebody
 *                            else's logs.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");

const path = require("node:path");

const { mountRig } = require("./test/dom.js");
const RE = require(path.resolve(__dirname, "../../packages/engine/rotation-engine.js"));
const ROSTER = require(path.resolve(__dirname, "../../packages/demo-roster/demo-roster.js"));

const PLAN = RE.buildPlan(
  Object.assign({}, ROSTER.defaults, { date: "2026-08-23" }), ROSTER.groups);
const PAYLOAD = RE.rigPayload(PLAN, "RIG-02");

const TOKEN = "a-token-placed-by-ansible";
const TAKE = new Blob([new Uint8Array(64).fill(1)]);

function withRig(opts, fn) {
  return async () => {
    const rig = await mountRig(opts);
    try { await fn(rig); } finally { rig.stop(); }
  };
}

/* Records what was sent where, and answers the whole video sequence with
   an ABSOLUTE upload url - which is what a real presigned PUT looks like
   and the case that matters here. */
function recording(opts) {
  const seen = [];
  /* Not `|| default`: "" is a legitimate value here and is falsy, which
     silently turned the same-origin case back into the absolute one. */
  const presignedHost = opts && opts.presignedHost !== undefined
    ? opts.presignedHost : "https://store.example.com";
  global.fetch = async (url, init) => {
    const u = String(url);
    seen.push({ url: u, auth: (init && init.headers && init.headers.Authorization) || null });
    if (u.includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    if (u.includes("video:presign")) {
      const camera = JSON.parse(init.body).camera;
      return { ok: true, status: 200, json: async () => ({
        key: "RIG-03/ep/" + camera + ".mp4",
        url: presignedHost + "/rigs-video/RIG-03/ep/" + camera + ".mp4?X-Amz-Signature=abc",
        method: "PUT",
      }) };
    }
    if (u.includes("video:complete")) {
      return { ok: true, status: 200, json: async () => ({ key: "k", safeToDelete: true }) };
    }
    if (u.includes("/schedule.json")) {
      /* A real payload. Answering {} here made the rig crash on boot,
         which is a fault in this fake and not in the rig. */
      return { ok: true, status: 200, json: async () => PAYLOAD };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  return seen;
}

function saveATake(rig) {
  rig.press(2); rig.frames(1);
  rig.press(2); rig.frames(30);
  rig.press(3); rig.frames(1);
  rig.press(2); rig.frames(1);
}

const ourCalls = (seen) => seen.filter((c) => c.url.startsWith("/api/"));

// ------------------------------------------------------------- no token

test("with no token placed, the rig sends no credentials",
  withRig({ search: "?demo" }, async (rig) => {
    const seen = recording();
    saveATake(rig);
    await rig.upload();
    assert.ok(seen.length > 0, "nothing was sent at all");
    assert.deepEqual(seen.filter((c) => c.auth).map((c) => c.url), [],
      "a rig with no token invented one");
  }));

// ---------------------------------------------------------- with a token

test("every call the rig makes to its own service carries the token",
  withRig({ search: "?demo", token: TOKEN }, async (rig) => {
    const seen = recording();
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    await rig.upload();
    await rig.uploadVideo(2);

    const ours = ourCalls(seen);
    assert.ok(ours.length >= 3,
      "expected the cursor, the events and the video steps: " +
      ours.map((c) => c.url).join(", "));
    const bare = ours.filter((c) => c.auth !== "Bearer " + TOKEN);
    assert.deepEqual(bare.map((c) => c.url), [],
      "these calls went out unauthenticated");
  }));

test("the schedule read carries it too",
  withRig({ search: "?demo", token: TOKEN }, async (rig) => {
    const seen = recording();
    await rig.settle();
    /* switchRig re-reads the payload, which is the same route the rig
       uses on boot and at every turn boundary. */
    await global.switchRig("RIG-02");
    const schedule = seen.filter((c) => c.url.includes("/schedule.json"));
    assert.ok(schedule.length > 0, "the schedule was never fetched");
    assert.ok(schedule.every((c) => c.auth === "Bearer " + TOKEN),
      "the schedule read went out unauthenticated");
  }));

// --------------------------------------------------- somebody else's server

test("a presigned upload does not carry the floor credential",
  withRig({ search: "?demo", token: TOKEN }, async (rig) => {
    const seen = recording();
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    await rig.uploadVideo(2);

    const uploads = seen.filter((c) => c.url.startsWith("https://store.example.com"));
    assert.ok(uploads.length > 0, "no presigned upload was attempted");
    assert.deepEqual(uploads.filter((c) => c.auth).map((c) => c.url), [],
      "a rig token was sent to the object store, and into its logs");
  }));

test("the gateway upload model does carry it, because that one is ours",
  withRig({ search: "?demo", token: TOKEN }, async (rig) => {
    /* LocalStorage has no presigning and points uploads back at the
       service, so the same PUT is same-origin and must be authenticated. */
    const seen = recording({ presignedHost: "" });
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    await rig.uploadVideo(2);

    const puts = seen.filter((c) => c.url.startsWith("/rigs-video/"));
    assert.ok(puts.length > 0, "no upload was attempted");
    assert.ok(puts.every((c) => c.auth === "Bearer " + TOKEN),
      "an upload to our own service went out unauthenticated");
  }));
