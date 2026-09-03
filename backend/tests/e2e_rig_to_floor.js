/* =====================================================================
 * e2e_rig_to_floor.js
 *
 * The seam, end to end. Every other test proves one half: the rig files
 * correct envelopes, or the backend accepts and projects them. Nothing
 * proved that the halves meet.
 *
 * This drives the *real* apps/rig/assets/rig.js through a real shift
 * against a *real* FastAPI server on a *real* Postgres, then reads the
 * floor board back and asserts what the operator did is on it.
 *
 * Not part of `npm test`: it needs a database and a running service.
 *
 *   cd backend && .venv/Scripts/python.exe -m uvicorn local_gateway:app --port 8000
 *   node backend/tests/e2e_rig_to_floor.js
 *
 * On a database that has accounts in it, the desk routes are gated on a
 * signed-in manager and this has to sign in as one:
 *
 *   RIGS_EMAIL=you@example.com RIGS_PASSWORD=... node backend/tests/e2e_rig_to_floor.js
 *
 * Both come from the environment because they are a secret and this file
 * is in the repository. Left unset, nothing signs in, which is right
 * wherever the gate is open - a fresh database, and so CI.
 * ===================================================================== */

"use strict";

const path = require("node:path");
const assert = require("node:assert");

const REPO = path.resolve(__dirname, "../..");
const { mountRig } = require(path.join(REPO, "apps/rig/test/dom.js"));
const RE = require(path.join(REPO, "packages/engine/rotation-engine.js"));
const ROSTER = require(path.join(REPO, "packages/demo-roster/demo-roster.js"));

const API = process.env.RIGS_API || "http://127.0.0.1:8000";
const RIG = "RIG-03";

/* Stand-in footage, from the same recorder the rig's own suite uses.
   `apps/rig/tools/mock-roda.js` sizes a take from the seconds the rig
   recorded, which is what lets the last assertion in step 9 exist: the
   service divides bytes by the episode's `durationSecs` and must arrive
   back at the rate that produced them.

   The rate is small on purpose. This runs on every push, and a take at
   the plan's real 875,000 B/s per camera is a couple of hundred
   megabytes through a live service and a real spool. 8 KB/s keeps a
   29-second take at roughly the 256 KB this used to send, and every
   property being checked is about the ratio rather than the magnitude.

   For an actual measurement - what the plan wants and has never had -
   run it at the real rate against a service somebody is willing to have
   carry synthetic takes:

       RIGS_VIDEO_RATE=875000 node backend/tests/e2e_rig_to_floor.js  */
const { mockRoda, BYTES_PER_SECOND_PER_CAMERA } =
  require(path.join(REPO, "apps/rig/tools/mock-roda.js"));
const RATE = Number(process.env.RIGS_VIDEO_RATE || 8192);
const roda = mockRoda({ bytesPerSecond: RATE });

/* `measured` on /floor/video is an average over every take the database
   holds, so a developer's database that already has some cannot be
   compared against directly. Totals can: takes x avg is a sum, and two
   sums subtract. This is what keeps the assertion exact on a database
   that is not empty, which is the one this gets run against by hand. */
const totalsOf = (m) => ({
  takes: m.takes || 0,
  bytes: (m.takes || 0) * (m.avgBytesPerTake || 0),
  secs: (m.takes || 0) * (m.avgDurationSecs || 0),
});

const ok = (m) => console.log("  ✓ " + m);
const step = (m) => console.log("\n" + m);

/* Captured before anything replaces global.fetch. The rig harness sets
   global.fetch to whatever it is handed, so a wrapper that calls the
   global by name ends up calling itself. */
const nodeFetch = globalThis.fetch;

/* The rig fetches relative URLs; point them at the running service. */
const realFetch = (url, opts) =>
  nodeFetch(url.startsWith("http") ? url : API + url, opts);

/* The desk's sign-in, when the service has people in it.
 *
 * `require_manager` is off-until-configured: on a deployment with no
 * accounts it falls through, which is how this runs in CI against a fresh
 * database and how `npm run serve` works with no setup. The moment one
 * account exists it is a real gate, and this script is the desk - so on a
 * developer's database, which does have accounts, it has to sign in like
 * one.
 *
 *     RIGS_EMAIL=r.osei@verlet.co RIGS_PASSWORD=... node backend/tests/e2e_rig_to_floor.js
 *
 * Credentials come from the environment because they are a secret and
 * this file is in the repository. Absent, nothing signs in and the run
 * only works where the gate is open - which is exactly the CI case.
 *
 * The session belongs to `api()` and to nothing else. The rig's own calls
 * go through `realFetch` and must stay cookie-free: `require_csrf` applies
 * only to callers presenting a session cookie, precisely because a rig
 * with a bearer token is not a browser and has no CSRF to protect. Attach
 * this to the rig and it would start being asked for a token it has no
 * reason to hold. */
const CREDENTIALS = process.env.RIGS_EMAIL && process.env.RIGS_PASSWORD
  ? { email: process.env.RIGS_EMAIL, password: process.env.RIGS_PASSWORD }
  : null;

let deskSession = null;      // { cookie, csrf } once signed in

async function signInAsDesk() {
  if (!CREDENTIALS) return null;

  const r = await nodeFetch(API + "/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(CREDENTIALS),
  });
  const text = await r.text();
  const body = text ? JSON.parse(text) : null;

  if (r.status !== 200) {
    throw new Error("sign-in failed (" + r.status + "): " + JSON.stringify(body)
      + "\n  RIGS_EMAIL and RIGS_PASSWORD have to name a manager - the desk is "
      + "for managers, and an operator account is refused with 403.");
  }

  /* Both cookies, not just the session one. The CSRF check is a double
     submit: the header has to equal the cookie, so sending one without
     the other is refused with 403 rather than let through. */
  const raw = typeof r.headers.getSetCookie === "function"
    ? r.headers.getSetCookie()
    : [r.headers.get("set-cookie")].filter(Boolean);
  const cookie = raw.map((c) => String(c).split(";")[0]).join("; ");

  if (!cookie || !body.csrfToken) {
    throw new Error("signed in but got no session cookie or CSRF token back");
  }
  return { cookie, csrf: body.csrfToken, who: body.name, role: body.role };
}

async function api(method, route, body) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (deskSession) {
    headers["Cookie"] = deskSession.cookie;
    headers["x-csrf-token"] = deskSession.csrf;
  }
  const r = await nodeFetch(API + route, {
    method,
    headers: Object.keys(headers).length ? headers : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  return { status: r.status, body: text ? JSON.parse(text) : null };
}

function todaysPayload() {
  const today = new Date().toISOString().slice(0, 10);
  const plan = RE.buildPlan(Object.assign({}, ROSTER.defaults, { date: today }), ROSTER.groups);
  return RE.rigPayload(plan, RIG);
}

(async () => {
  step("0. the service is up");
  const health = await api("GET", "/api/health");
  assert.equal(health.status, 200, "no service at " + API);
  ok("health " + health.status);

  deskSession = await signInAsDesk();
  if (deskSession) {
    ok("signed in as " + deskSession.who + " (" + deskSession.role + ")");
  } else if (health.body && health.body.personAuth === "on") {
    /* Said here rather than left to a 401 four lines down, because "not
       signed in" from a push is a confusing way to learn that this
       service has accounts and this script was given none. */
    throw new Error(
      "this service has accounts, so the desk routes need a manager signed in.\n"
      + "  Set RIGS_EMAIL and RIGS_PASSWORD, or run against a database with no "
      + "accounts, where the gate is open.");
  } else {
    ok("no accounts on this service - the desk gate is open");
  }

  /* A schedule has to exist first: every event is stamped with the shift
     it happened under, and the ledger links each row to the push that was
     in force. That link is what keeps attribution auditable. */
  step("1. the desk pushes a schedule");
  const payload = todaysPayload();
  const pushed = await api("POST", "/api/schedules/push", { payloads: [payload] });
  assert.equal(pushed.status, 200, "schedule push failed: " + JSON.stringify(pushed.body));
  ok(payload.shift.label + " " + payload.shift.date + " pushed to " + RIG);

  const before = await api("GET", `/api/rigs/${RIG}/cursor`);
  ok("cursor before the shift: " + before.body.seq);

  /* Taken before the take, so step 9 can subtract. */
  const videoBefore = await api("GET", "/api/floor/video");
  assert.equal(videoBefore.status, 200);
  const wasMeasured = totalsOf(videoBefore.body.measured);

  step("2. an operator works a take on the real rig app");
  global.fetch = realFetch;
  const rig = await mountRig({ search: "?demo=30", fetchImpl: realFetch });
  try {
    /* A browser tab has no camera. This is the seam a Tauri shell will
       fill with RODA-RS; here it is the stand-in recorder, which proves
       the rule that matters - the rig lets go of a take only after the
       server has verified what landed - and now also that the size of
       what landed agrees with how long the rig says it recorded. */
    rig.setVideoSource(roda);
    /* mountRig settles microtasks; a real HTTP round-trip needs wall
       time. Without this the rig is still waiting for its payload. */
    await new Promise((r) => setTimeout(r, 600));
    rig.frames(1);
    assert.equal(rig.$("rail-mode").textContent, "demo clock 30×",
      "the rig did not get the pushed schedule: " + rig.$("rail-mode").textContent);
    assert.equal(rig.screen(), "checklist", "expected the shift check");

    rig.press(2); rig.frames(1);                    // check passed
    assert.equal(rig.screen(), "handover");

    rig.press(2); rig.frames(60);                   // start, and record a while
    assert.equal(rig.screen(), "recording");

    rig.press(3); rig.frames(1);                    // save
    assert.equal(rig.screen(), "review");

    rig.press(2); rig.frames(1);                    // scored 4/5
    assert.equal(rig.screen(), "resetting");
    ok("checklist -> handover -> recording -> review -> resetting");

    const filed = rig.events();
    ok(filed.length + " events filed: " + filed.map((e) => e.event).join(", "));
    assert.ok(filed.some((e) => e.event === "episode_saved"), "no episode was saved");

    step("3. the rig uploads them");
    await rig.upload();
    const box = rig.outbox();
    assert.equal(box.queued, 0, "the outbox did not drain: " + JSON.stringify(box));
    assert.equal(box.rejected, 0, "the server refused events the rig filed");
    ok("outbox empty, nothing refused");

    step("4. the ledger holds them");
    const after = await api("GET", `/api/rigs/${RIG}/cursor`);
    assert.ok(after.body.seq > before.body.seq,
      `cursor did not move: ${before.body.seq} -> ${after.body.seq}`);
    ok("cursor " + before.body.seq + " -> " + after.body.seq);

    step("5. re-sending the same shift changes nothing");
    const again = await api("POST", `/api/rigs/${RIG}/events`, { events: filed });
    assert.equal(again.status, 200);
    assert.equal(again.body.accepted, 0, "a resend inserted rows it should have skipped");
    assert.equal(again.body.duplicates, filed.length);
    ok("resent " + filed.length + ", accepted 0, duplicates " + again.body.duplicates);

    step("6. projection turns them into facts");
    const projected = await api("POST", "/api/dev/project");
    assert.equal(projected.status, 200);
    ok("projected " + projected.body.projected + " ledger rows");

    step("7. the take shows on the floor");
    /* The episode this run just recorded, by id - not "some saved episode",
       which an earlier run leaves lying in the table and which would let
       this step pass while nothing at all had projected. */
    const mine = filed.find((e) => e.event === "episode_saved");
    const wanted = mine.data.episodeId;

    const eps = await api("GET", "/api/dev/episodes?rig_id=" + RIG);
    assert.equal(eps.status, 200);
    const one = eps.body.episodes.find((e) => e.episodeId === wanted);
    assert.ok(one, `episode ${wanted} never reached the episodes table`);
    assert.equal(one.outcome, "saved");
    assert.equal(one.score, mine.data.score, "the score changed on the way in");
    assert.ok(one.durationSecs > 0, "duration did not survive: " + one.durationSecs);
    assert.equal(one.operatorId, mine.operatorId,
      "the episode was attributed to somebody else");
    /* The id is the seat; this is the person who sat in it. Without it the
       only answer to "who recorded this" is a join back to the pushed
       schedule, which returns whoever holds the seat now. */
    assert.equal(one.operatorName, mine.operatorName,
      "the operator's name did not survive the trip to the floor");
    assert.ok(one.operatorName, "the episode reached the floor with no name on it");
    ok(`episode ${one.episodeId.slice(0, 8)} scored ${one.score}, ` +
       `${one.durationSecs}s, ${one.operatorName} (${one.operatorId}) - ` +
       `the one this run recorded`);

    step("8. the desk's board can read it");
    const beat = await api("POST", `/api/rigs/${RIG}/heartbeat`,
                           { at: new Date().toISOString() });
    assert.equal(beat.status, 200);

    const state = await api("GET", "/api/floor/state");
    assert.equal(state.status, 200);
    const board = state.body.rigs.find((r) => r.rigId === RIG);
    assert.ok(board, RIG + " is missing from the floor board");
    assert.ok(board.lastSeenAt, "the heartbeat was not recorded");
    ok(`${board.rigId} · ${board.task} · last seen ${board.lastSeenAt.slice(11, 19)}`);

    step("9. the video follows, and only then does the rig let go of it");
    const queued = rig.video().queued;
    assert.equal(queued, 3, "a saved take should have queued one video per camera");
    ok(queued + " cameras queued");

    /* One flush per camera. Each is presign -> PUT -> complete against
       the real service, and each releases only on a verified confirm. */
    await rig.uploadVideo(queued + 1);
    const v = rig.video();
    assert.equal(v.queued, 0, "video did not drain: " + JSON.stringify(v));
    assert.equal(v.sent.length, queued, "not every camera was confirmed");
    ok("confirmed and released: " + v.sent.map((k) => k.split("/").pop()).join(", "));

    const vb = await api("GET", "/api/floor/video");
    assert.equal(vb.status, 200);

    /* All three, not one. A take is three cameras and the bookkeeping was
       a single key on the episode, so `confirm()` overwrote it with
       whichever landed last - one camera archived, two orphaned on the
       spool for ever, and a backlog that reported a third of the truth. */
    const landed = ["on_prem", "archived"].reduce(
      (n, st) => n + ((vb.body.byState[st] || {}).cameras || 0), 0);
    assert.ok(landed >= queued,
      `only ${landed} of ${queued} cameras are accounted for: ` +
      JSON.stringify(vb.body.byState));
    ok(`${landed} cameras accounted for, spool holding ` +
       `${vb.body.spool.cameras} (${vb.body.spool.bytes} bytes)`);

    assert.ok(vb.body.measured.takes >= 1, "nothing was measured");

    /* The round trip, as one number.
     *
     * The rig recorded N seconds and filed that in the ledger. The
     * recorder produced RATE bytes for each of those seconds, on each of
     * three cameras. The bytes went out through presign, PUT and a
     * confirm the service only grants once it has checksummed what
     * landed, and projection turned them into rows. Divide what the
     * service now holds by the duration it holds, and the rate that
     * produced it has to come back.
     *
     * Until the recorder was sized from the duration, this number could
     * not be asserted on at all - a fixed block of bytes over a variable
     * take is a rate nobody chose, and it was printed rather than
     * checked. */
    const saved = filed.find((e) => e.event === "episode_saved");
    const secs = saved.data.durationSecs;
    const now = totalsOf(vb.body.measured);
    const added = {
      takes: now.takes - wasMeasured.takes,
      bytes: now.bytes - wasMeasured.bytes,
      secs: now.secs - wasMeasured.secs,
    };

    assert.equal(added.takes, 1, "this run should have added exactly one measured take");
    assert.equal(Math.round(added.secs), secs,
      `the rig filed ${secs}s; the service measured ${Math.round(added.secs)}s`);
    assert.equal(Math.round(added.bytes), 3 * RATE * secs,
      `three cameras at ${RATE} B/s for ${secs}s should be ${3 * RATE * secs} bytes, ` +
      `not ${Math.round(added.bytes)}`);
    assert.equal(Math.round(added.bytes / added.secs), 3 * RATE,
      "bytes divided by durationSecs must give back the rate that produced them");
    ok(`${secs}s x 3 cameras at ${RATE} B/s -> ${Math.round(added.bytes)} bytes, ` +
       `read back as ${Math.round(added.bytes / added.secs)} B/s`);

    /* The plan's 7 Mbps per camera now lives in two languages: this
       recorder and core/workflows/video.py. Neither may move alone. */
    assert.equal(vb.body.measured.planAssumedBytesPerSecond,
                 3 * BYTES_PER_SECOND_PER_CAMERA,
      "the plan's assumed rate has drifted between mock-roda.js and video.py");
    ok(`the plan's assumed ${vb.body.measured.planAssumedBytesPerSecond} B/s is ` +
       `the same number in both halves`);

    console.log("\nend to end: pedal press -> envelope -> ledger -> facts -> board");
    console.log("             take -> presign -> bytes -> verified -> released");
  } finally {
    rig.stop();
  }
})().catch((e) => {
  console.error("\nFAILED: " + e.message);
  process.exit(1);
});
