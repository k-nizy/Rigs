/* =====================================================================
 * credentials.test.js  -  the machine authenticates, the operator never does
 *
 * The rig has no login by design, and that is not an omission - it is the
 * central idea of the screen. So the credential belongs to the machine:
 * the service hands the page a token in rig-config.js, answered per
 * caller from RIG_ADDRESSES, and every call the rig makes carries it.
 * Nobody standing at the rig types anything, ever.
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


/* =====================================================================
 * which rig this machine is
 *
 * `start()` used to be called with no argument, so every rig fell back to
 * the default and asked the server for RIG-03's schedule. Twelve machines
 * all believing they were the same one, filing every episode under one
 * rig id and uploading video into one prefix.
 *
 * Nothing downstream could have caught it. From the server's side twelve
 * rigs reporting as RIG-03 is indistinguishable from one very busy rig -
 * the events are well-formed, the schedule is real, the cursor advances.
 * It would have been found by a manager asking why eleven rigs looked
 * idle, which is a bad way to find it.
 * ===================================================================== */

test("a machine with no config asks for nothing in particular",
  withRig({ search: "?demo" }, async (rig) => {
    const seen = recording();
    await rig.settle();
    assert.equal(rig.journal().durable, false);
  }));

test("a configured machine asks the server for its own schedule",
  withRig({ search: "?demo", rigId: "RIG-07" }, async (rig) => {
    const asked = [];
    global.fetch = async (url) => {
      const u = String(url);
      asked.push(u);
      if (u.includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
      throw new Error("no server");
    };
    await global.switchRig("RIG-07");
    assert.ok(asked.some((u) => u.includes("/rigs/RIG-07/schedule")),
      "asked for: " + asked.join(", "));
  }));

test("two machines do not both believe they are the same rig", async () => {
  /* The actual defect, written as the thing a floor would see. */
  const seenBy = {};

  for (const id of ["RIG-05", "RIG-09"]) {
    const asked = [];
    const rig = await mountRig({
      search: "?demo", rigId: id, settle: false,
      fetchImpl: async (url) => {
        asked.push(String(url));
        if (String(url).includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
        throw new Error("no server");
      },
    });
    try {
      await rig.settle();
      seenBy[id] = asked.filter((u) => u.includes("/schedule"));
    } finally { rig.stop(); }
  }

  assert.ok(seenBy["RIG-05"].some((u) => u.includes("RIG-05")),
    "RIG-05 asked for: " + seenBy["RIG-05"].join(", "));
  assert.ok(seenBy["RIG-09"].some((u) => u.includes("RIG-09")),
    "RIG-09 asked for: " + seenBy["RIG-09"].join(", "));
  assert.ok(!seenBy["RIG-09"].some((u) => u.includes("RIG-05")),
    "one machine asked for another machine's schedule");
});

/* ====================================================================
 * A rig that was never told which rig it is
 *
 * `apps/rig/index.html` loads rig-config.js by a relative path, and the
 * kiosk loads the page from the server - so a config written onto the rig
 * machine is never read. Twelve machines loaded one blank file, took the
 * default at the top of rig.js, and every one of them believed it was the
 * same rig. From the service's side that is indistinguishable from one
 * very busy rig, so nothing downstream could ever notice.
 *
 * The service now decides, per caller. These pin the other half: what a
 * rig does when the answer is "you are nobody". It stops. Idle time is
 * loud, cheap and recoverable; work filed under the wrong rig is silent
 * and permanent.
 * ==================================================================== */

/* A floor answers /api/health with rigIdentity, which says whether it
   tells its rigs apart at all. `identity` here is that answer. */
function floorSaying(identity, extra) {
  return async (url) => {
    const u = String(url);
    if (u.includes("/api/health")) {
      return { ok: true, status: 200,
               json: async () => Object.assign({ ok: true, rigIdentity: identity,
                                                 rigAuth: "off" }, extra) };
    }
    if (u.includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    if (u.includes("/schedule")) return { ok: true, status: 200, json: async () => PAYLOAD };
    return { ok: true, status: 200, json: async () => ({}) };
  };
}

test("a machine the floor cannot place refuses to be a rig",
  withRig({ search: "?demo", seenAs: "10.0.0.99", fetchImpl: floorSaying("on") },
    async (rig) => {
      rig.frames(1);
      assert.equal(rig.screen(), "no_identity",
        "an unidentified machine carried on as the default rig");
    }));

test("it offers nothing to press, because there is nothing safe to start",
  withRig({ search: "?demo", seenAs: "10.0.0.99", fetchImpl: floorSaying("on") },
    async (rig) => {
      rig.frames(1);
      assert.deepEqual(rig.pedals(), ["—", "—", "—"],
        "a rig with no identity was still offered a way to start work");
    }));

test("it names the machine by the address the floor saw, for whoever is sent to it",
  withRig({ search: "?demo", seenAs: "10.0.0.99", fetchImpl: floorSaying("on") },
    async (rig) => {
      rig.frames(1);
      assert.match(rig.stage(), /10\.0\.0\.99/,
        "the screen does not say which machine this is: " + rig.stage());
    }));

test("no take can be started from it, however long the shift runs",
  withRig({ search: "?demo", seenAs: "10.0.0.99", fetchImpl: floorSaying("on") },
    async (rig) => {
      rig.frames(1);
      rig.press(0); rig.press(1); rig.press(2);
      rig.frames(600);                       // a demo hour, straight through a turn boundary
      assert.equal(rig.screen(), "no_identity",
        "the refusal was lifted by pressing pedals or by time passing");
      assert.deepEqual(rig.logOf("episode_saved"), [], "a take was recorded anyway");
    }));

test("a turn boundary does not quietly hand it to the next operator",
  withRig({ search: "?demo", seenAs: "10.0.0.99", fetchImpl: floorSaying("on") },
    async (rig) => {
      /* Long enough to matter. The rig mounts at 10:37 inside a turn that
         ends at 10:45, and rotate() would put a working handover screen in
         front of whoever walked up next. Measured against the unguarded
         version, that happens somewhere past 3000 frames of demo clock -
         so 12000, which is several turns, not one boundary scraped. A
         refusal a passing turn boundary lifts is not a refusal. */
      rig.frames(1);
      const block = rig.rail().block;
      rig.frames(12000);
      assert.equal(rig.screen(), "no_identity",
        "a turn boundary handed a rig with no identity to the next operator");
      assert.equal(rig.rail().block, block,
        "the clock ran on a rig that never started");
    }));

test("a rig the floor does place is untouched",
  withRig({ search: "?demo", rigId: "RIG-02", seenAs: "10.0.0.12",
            fetchImpl: floorSaying("on") },
    async (rig) => {
      rig.frames(1);
      assert.notEqual(rig.screen(), "no_identity",
        "a properly configured rig was stopped");
      assert.equal(rig.rail().rig, "RIG-02");
    }));

test("a floor that does not identify its rigs is a demo, and still runs",
  withRig({ search: "?demo", fetchImpl: floorSaying("off") },
    async (rig) => {
      /* The laptop case, and the OPEN A RIG button on the landing page.
         Deliberately not keyed on rigAuth: the worst version of the
         failure above is a floor whose auth is off, where twelve rigs
         filing as one are accepted rather than refused. */
      rig.frames(1);
      assert.notEqual(rig.screen(), "no_identity",
        "the demo stopped working");
    }));

test("no service to ask leaves the rig as it was",
  withRig({ search: "?demo",
            fetchImpl: async (url) => {
              if (String(url).includes("/api/health")) throw new Error("no server");
              if (String(url).includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
              throw new Error("no server");
            } },
    async (rig) => {
      /* A static deploy with no service behind it. Nothing can be filed
         either, so there is nothing to be wrong about. */
      rig.frames(1);
      assert.notEqual(rig.screen(), "no_identity");
    }));

test("nothing filed before the answer came back is ever sent",
  withRig({ search: "?demo", seenAs: "10.0.0.99", fetchImpl: floorSaying("on") },
    async (rig) => {
      /* Found in a browser, not in a test. boot() raises a shift check
         immediately, so by the time the health answer lands there is
         already one event in the outbox filed under the default rig -
         and the rail was still naming it. On a floor with auth off that
         event is accepted, which is the whole failure arriving through
         the one window where the rig did not yet know. */
      rig.frames(1);
      const sent = [];
      global.fetch = async (url, init) => {
        sent.push(String(url));
        return { ok: true, status: 200, json: async () => ({ accepted: 0 }) };
      };
      await rig.upload();
      assert.deepEqual(sent.filter((u) => u.includes("/events")), [],
        "a rig with no identity uploaded events filed under the default rig");
    }));

test("the rail stops naming a rig, so the screen does not contradict itself",
  withRig({ search: "?demo", seenAs: "10.0.0.99", fetchImpl: floorSaying("on") },
    async (rig) => {
      /* Of a screen saying "this machine has no identity" and a rail
         saying RIG-03, the operator believes the one that names a rig. */
      rig.frames(1);
      assert.equal(rig.rail().rig, "",
        "the rail still named a rig on a machine that has none");
      assert.equal(rig.rail().task, "",
        "the rail still named a task on a machine that has no rig");
    }));
