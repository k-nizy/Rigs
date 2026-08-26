/* =====================================================================
 * mock-roda.test.js  -  bytes at the size a real take would be
 *
 * The video path has been built, tested and deployed without ever
 * carrying a plausible byte. Every take it has moved was a couple of
 * kilobytes of filler, which proves the plumbing and proves nothing
 * about the volume: the ~9 GB per rig-hour in `BACKEND-PLAN.md` is still
 * an estimate, and `DEPLOY.md` says so in its opening paragraph.
 *
 * `tools/mock-roda.js` is the standing-in camera. What is pinned here is
 * the one property that makes it worth having - that a take's size is
 * the rate times the seconds the rig actually recorded, and that those
 * seconds are the same ones it put in the ledger. Anything downstream
 * dividing bytes by `durationSecs` then gets the rate back, rather than
 * a number nobody chose.
 *
 * The last test is the wiring, and it is the one that failed first: the
 * seam handed the recorder an episode and a camera and never told it how
 * long the take was, so a size-following recorder could only ever return
 * nothing.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { mountRig } = require("./test/dom.js");
const { mockRoda, BYTES_PER_SECOND_PER_CAMERA } =
  require(path.resolve(__dirname, "tools/mock-roda.js"));

function withRig(opts, fn) {
  return async () => {
    const rig = await mountRig(opts);
    try { await fn(rig); } finally { rig.stop(); }
  };
}

/* Enough of a store to let the uploader run to completion, and it keeps
   every PUT rather than the last one - the whole point here is comparing
   three cameras against each other. */
function fakeStore() {
  const seen = { puts: [] };
  global.fetch = async (url, init) => {
    const u = String(url);
    if (u.includes("video:presign")) {
      const camera = JSON.parse(init.body).camera;
      return { ok: true, status: 200, json: async () => ({
        key: "RIG-03/ep/" + camera, url: "/api/storage/RIG-03/ep/" + camera, method: "PUT",
      }) };
    }
    if (u.includes("/api/storage/")) {
      seen.puts.push(init.body.byteLength);
      return { ok: true, status: 200, json: async () => ({ bytes: init.body.byteLength }) };
    }
    if (u.includes("video:complete")) {
      return { ok: true, status: 200, json: async () => ({ key: "k", safeToDelete: true }) };
    }
    if (u.includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  return seen;
}

function saveATake(rig) {
  rig.press(2); rig.frames(1);      // check passed
  rig.press(2); rig.frames(30);     // start, record
  rig.press(3); rig.frames(1);      // save
  rig.press(2); rig.frames(1);      // scored
}

const bytesOf = async (blob) => new Uint8Array(await blob.arrayBuffer());

// ------------------------------------------------------------ the size

test("a take is the plan's rate times the seconds recorded", async () => {
  const roda = mockRoda();
  const blob = roda("ep-1", "front", 10);
  assert.equal(blob.size, BYTES_PER_SECOND_PER_CAMERA * 10,
    "ten seconds of one 1080p30 camera at ~7 Mbps");
  assert.equal(blob.size, 8750000, "and that is 8.75 MB, stated rather than derived");
});

test("bytes and durationSecs agree, so the rate can be read back out", async () => {
  const roda = mockRoda();
  for (const secs of [1, 7, 45, 300]) {
    const blob = roda("ep-1", "front", secs);
    assert.equal(blob.size / secs, BYTES_PER_SECOND_PER_CAMERA,
      "anything dividing bytes by duration must get the rate back, at " + secs + "s");
  }
});

test("three cameras for eight hours is the shift the sizing table claims", async () => {
  const roda = mockRoda();
  const shift = BYTES_PER_SECOND_PER_CAMERA * 3 * 8 * 3600;
  assert.ok(shift / 1e9 > 71 && shift / 1e9 < 76,
    "~72 GB per rig per 8h shift, per BACKEND-PLAN.md - got " +
    Math.round(shift / 1e9) + " GB");
});

test("a measured rate replaces the estimate without touching anything else", async () => {
  const roda = mockRoda({ bytesPerSecond: 1000 });
  assert.equal(roda("ep-1", "front", 10).size, 10000);
  assert.equal(roda.bytesPerSecond, 1000);
});

test("a rate that is not a positive number is refused at construction", async () => {
  assert.throws(() => mockRoda({ bytesPerSecond: 0 }), /positive/);
  assert.throws(() => mockRoda({ bytesPerSecond: -1 }), /positive/);
  assert.throws(() => mockRoda({ bytesPerSecond: "camera" }), /positive/);
});

// ---------------------------------------------------------- the content

test("every camera and every episode gets different bytes", async () => {
  const roda = mockRoda({ bytesPerSecond: 4096 });
  const front = await bytesOf(roda("ep-1", "front", 2));
  const wrist = await bytesOf(roda("ep-1", "wrist-l", 2));
  const later = await bytesOf(roda("ep-2", "front", 2));

  assert.equal(front.length, wrist.length, "same take, same length");
  assert.notDeepEqual(front, wrist,
    "identical bytes per camera means a mixed-up key checksums as correct");
  assert.notDeepEqual(front, later, "and the same for a different episode");
});

test("the same take twice is the same bytes", async () => {
  const roda = mockRoda({ bytesPerSecond: 4096 });
  const once = await bytesOf(roda("ep-1", "front", 3));
  const again = await bytesOf(roda("ep-1", "front", 3));
  assert.deepEqual(once, again,
    "a reload re-reads the blob from the journal; a recorder that answered " +
    "differently each call would read as corruption");
});

test("a file pulled out of the spool says which take it is", async () => {
  const roda = mockRoda({ bytesPerSecond: 4096 });
  const bytes = await bytesOf(roda("ep-7", "overhead", 2));
  const head = Buffer.from(bytes.subarray(0, 64)).toString("ascii");
  assert.match(head, /^MOCK-RODA ep-7\/overhead 2s 8192B\n/);
});

test("a take of no seconds queues nothing, and says so", async () => {
  const roda = mockRoda();
  assert.equal(roda("ep-1", "front", 0), null);
  assert.equal(roda("ep-1", "front", undefined), null);
  assert.equal(roda("ep-1", "front", -5), null);
  assert.equal(roda("", "front", 10), null);
  assert.equal(roda.stats().skipped, 4,
    "silence is what the seam asks for, but it must be countable");
  assert.equal(roda.stats().takes, 0);
});

test("stats report what was actually produced", async () => {
  const roda = mockRoda({ bytesPerSecond: 1000 });
  roda("ep-1", "front", 2);
  roda("ep-1", "wrist-l", 3);
  assert.deepEqual(roda.stats(), { takes: 2, bytes: 5000, skipped: 0 });
});

// ---------------------------------------------------------- the wiring

test("a saved take is sized from the duration the rig filed in the ledger",
  withRig({ search: "?demo" }, async (rig) => {
    const store = fakeStore();
    const RATE = 1000;          // not the plan's rate: this is about wiring,
                                // and a real 30-frame take at 875 kB/s is 200 MB
    const roda = mockRoda({ bytesPerSecond: RATE });
    rig.setVideoSource(roda);

    saveATake(rig);
    assert.equal(rig.video().queued, 3, "three panes, three videos");

    const saved = rig.eventsOf("episode_saved");
    assert.equal(saved.length, 1, "one take was saved");
    const secs = saved[0].data.durationSecs;
    assert.ok(secs > 0, "the ledger recorded a duration: " + secs);

    /* One take per flush - the uploader sends a camera and backs off. */
    for (let i = 0; i < 6 && rig.video().queued > 0; i++) await rig.uploadVideo();
    assert.equal(rig.video().queued, 0, "the queue drained");

    assert.deepEqual(store.puts, [RATE * secs, RATE * secs, RATE * secs],
      "every camera's bytes must match the duration on the episode row, " +
      "or /api/floor/video measures a rate nobody chose");
    assert.equal(roda.stats().skipped, 0,
      "the seam must hand the recorder the duration, not leave it to guess");
  }));
