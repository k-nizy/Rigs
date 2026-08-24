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

const ok = (m) => console.log("  ✓ " + m);
const step = (m) => console.log("\n" + m);

/* Captured before anything replaces global.fetch. The rig harness sets
   global.fetch to whatever it is handed, so a wrapper that calls the
   global by name ends up calling itself. */
const nodeFetch = globalThis.fetch;

/* The rig fetches relative URLs; point them at the running service. */
const realFetch = (url, opts) =>
  nodeFetch(url.startsWith("http") ? url : API + url, opts);

async function api(method, route, body) {
  const r = await nodeFetch(API + route, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
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

  step("2. an operator works a take on the real rig app");
  global.fetch = realFetch;
  const rig = await mountRig({ search: "?demo=30", fetchImpl: realFetch });
  try {
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
    ok(`episode ${one.episodeId.slice(0, 8)} scored ${one.score}, ` +
       `${one.durationSecs}s, ${one.operatorId} - the one this run recorded`);

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

    console.log("\nend to end: pedal press -> envelope -> ledger -> facts -> board");
  } finally {
    rig.stop();
  }
})().catch((e) => {
  console.error("\nFAILED: " + e.message);
  process.exit(1);
});
