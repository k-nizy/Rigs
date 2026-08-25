/* =====================================================================
 * journal.test.js  -  what the rig still owes after the tab goes away
 *
 * The rig tells the operator, on the session-ended screen:
 *
 *     Downtime and episodes are queued for upload.
 *
 * Until the journal existed that sentence was aspirational. The outbox
 * was an array, so a reload, a crash, a closed lid or a browser
 * discarding a background tab took every event since boot with it - and
 * the screen said they were safe the whole time.
 *
 * These tests are about that promise. A mount is a boot; handing the
 * same journal to two mounts is a restart. What is worth asserting is
 * not that writing works, but the two halves of the bookkeeping:
 *
 *   what is still owed  - survives a restart, keeps its place in the
 *                         sequence, and includes the bytes
 *   what is not owed    - anything the server has acknowledged, and
 *                         anything it has refused outright
 *
 * A journal that has stopped working must never stop the rig, so that
 * is here too. The floor keeps running; what changes is that the screen
 * stops promising.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");

const { mountRig } = require("./test/dom.js");

const TAKE = new Blob([new Uint8Array(1024).fill(3)]);

function withRig(opts, fn) {
  return async () => {
    const rig = await mountRig(opts);
    try { await fn(rig); } finally { rig.stop(); }
  };
}

/* Persists across mounts, which is what a reload is. */
function fakeJournal() {
  const events = new Map();
  const videos = new Map();
  return {
    durable: true,
    async load(rigId) {
      return {
        events: [...events.values()].filter((e) => e.rigId === rigId)
                                    .sort((a, b) => a.seq - b.seq),
        videos: [...videos.values()].filter((v) => v.rigId === rigId),
      };
    },
    async appendEvent(e) { events.set(e.eventId, e); },
    async forgetEvents(ids) { ids.forEach((id) => events.delete(id)); },
    async putVideo(v) { videos.set(v.key, v); },
    async forgetVideo(k) { videos.delete(k); },
    heldEvents: () => [...events.values()],
    heldVideos: () => [...videos.values()],
  };
}

const offline = () => { global.fetch = async () => { throw new Error("no network"); }; };

const accepting = () => {
  global.fetch = async (url) => {
    if (String(url).includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    return { ok: true, status: 200,
             json: async () => ({ accepted: 3, duplicates: 0, cursor: 3 }) };
  };
};

const refusing = () => {
  global.fetch = async (url) => {
    if (String(url).includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    return { ok: false, status: 422, json: async () => ({}) };
  };
};

/* A store that answers the three video steps and always confirms. */
const confirming = () => {
  global.fetch = async (url, init) => {
    const u = String(url);
    if (u.includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    if (u.includes("video:presign")) {
      const camera = JSON.parse(init.body).camera;
      return { ok: true, status: 200, json: async () => ({
        key: "RIG-03/ep/" + camera + ".mp4",
        url: "/api/storage/RIG-03/ep/" + camera + ".mp4", method: "PUT",
      }) };
    }
    if (u.includes("/api/storage/")) return { ok: true, status: 200, json: async () => ({}) };
    if (u.includes("video:complete")) {
      return { ok: true, status: 200,
               json: async () => ({ key: "k", safeToDelete: true }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
};

/* checklist -> handover -> recording -> review -> scored: the only route
   by which a take is ever saved. */
function saveATake(rig) {
  rig.press(2); rig.frames(1);
  rig.press(2); rig.frames(30);
  rig.press(3); rig.frames(1);
  rig.press(2); rig.frames(1);
}

// ------------------------------------------------------------- no journal

test("without a journal the rig still runs, and says it is not durable",
  withRig({ search: "?demo" }, async (rig) => {
    offline();
    saveATake(rig);
    assert.equal(rig.journal().durable, false);
    assert.ok(rig.outbox().queued > 0, "events are still held in memory");
  }));

// ------------------------------------------------------- what is still owed

test("what the rig files is written down before the network is touched",
  withRig({ search: "?demo", journal: fakeJournal() }, async (rig) => {
    offline();
    saveATake(rig);
    await rig.settle();
    const filed = rig.events().map((e) => e.eventId).sort();
    const written = rig.journalHeld().map((e) => e.eventId).sort();
    assert.deepEqual(written, filed,
      "an event reached the outbox without reaching the journal");
  }));

test("a shift that was never uploaded is still owed after a restart", async () => {
  const j = fakeJournal();

  const first = await mountRig({ search: "?demo", journal: j });
  try {
    offline();
    saveATake(first);
    await first.settle();
    await first.upload();                       // fails: no network
    assert.ok(first.outbox().queued >= 3);
  } finally { first.stop(); }

  /* The tab goes away. Nothing was ever uploaded. */
  const second = await mountRig({ search: "?demo", journal: j });
  try {
    assert.ok(second.outbox().queued >= 3,
      "a restart lost the events the screen said were queued for upload");
    assert.ok(second.log().some((l) => l.includes("still waiting")),
      "the operator was not told there was a backlog");
  } finally { second.stop(); }
});

test("recovered events keep their place in the sequence", async () => {
  const j = fakeJournal();
  let highest = -1;

  const first = await mountRig({ search: "?demo", journal: j });
  try {
    offline();
    saveATake(first);
    await first.settle();
    highest = Math.max(...j.heldEvents().map((e) => e.seq));
  } finally { first.stop(); }

  const second = await mountRig({ search: "?demo", journal: j });
  try {
    assert.ok(second.outbox().nextSeq > highest,
      "seq went backwards after a restart: the cursor would be ambiguous");
  } finally { second.stop(); }
});

// --------------------------------------------------- what is not owed

test("what the server has acknowledged is not owed twice", async () => {
  const j = fakeJournal();

  let sent = [];

  const first = await mountRig({ search: "?demo", journal: j });
  try {
    accepting();
    saveATake(first);
    sent = first.events().map((e) => e.eventId);
    await first.upload();
    assert.equal(first.outbox().queued, 0, "the outbox did not drain");
    await first.settle();
    assert.deepEqual(j.heldEvents(), [],
      "the journal still holds events the server already has");
  } finally { first.stop(); }

  const second = await mountRig({ search: "?demo", journal: j });
  try {
    /* By id, not by count. Every boot raises the shift check and files
       one of its own, so an empty outbox is the wrong thing to ask for -
       what matters is that nothing already acknowledged came back. */
    const owed = second.outbox().queued;
    const recovered = second.events().filter((e) => sent.includes(e.eventId));
    assert.deepEqual(recovered, [],
      "a restart re-sent events the server had already accepted");
    assert.equal(owed, 1, "the only thing owed should be this boot's own check");
  } finally { second.stop(); }
});

test("a batch the server refuses is not carried for ever",
  withRig({ search: "?demo", journal: fakeJournal() }, async (rig) => {
    refusing();
    saveATake(rig);
    await rig.upload();
    await rig.settle();

    assert.ok(rig.outbox().rejected > 0, "the events were set aside");
    assert.deepEqual(rig.journalHeld(), [],
      "a batch that can never land would be reloaded and re-refused every boot");
  }));

// --------------------------------------------------------------- the bytes

test("a take that was never uploaded is still on the rig after a restart",
  async () => {
    const j = fakeJournal();

    const first = await mountRig({ search: "?demo", journal: j });
    try {
      offline();
      first.setVideoSource(() => TAKE);
      saveATake(first);
      await first.settle();
      assert.equal(first.video().queued, 3);
      assert.equal(j.heldVideos().length, 3, "the bytes were never written down");
    } finally { first.stop(); }

    /* No camera attached this time. A recovered take is owed regardless of
       whether this boot can record a new one. */
    const second = await mountRig({ search: "?demo", journal: j });
    try {
      assert.equal(second.video().queued, 3,
        "a restart lost video that had no other copy anywhere");
    } finally { second.stop(); }
  });

test("a confirmed take is released from the journal too",
  withRig({ search: "?demo", journal: fakeJournal() }, async (rig) => {
    confirming();
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    await rig.settle();
    assert.equal(rig.journalVideos().length, 3);

    await rig.uploadVideo(4);
    assert.equal(rig.video().queued, 0);
    assert.deepEqual(rig.journalVideos(), [],
      "the rig kept bytes the server had already verified");
  }));

// ------------------------------------------------------ when it goes wrong

test("a journal that stops working does not stop the rig",
  withRig({
    search: "?demo",
    journal: {
      durable: true,
      async load() { return { events: [], videos: [] }; },
      async appendEvent() { throw new Error("QuotaExceededError"); },
      async forgetEvents() {},
      async putVideo() { throw new Error("QuotaExceededError"); },
      async forgetVideo() {},
    },
  }, async (rig) => {
    offline();
    saveATake(rig);
    await rig.settle();

    assert.equal(rig.screen(), "resetting", "a failed journal write blocked the loop");
    assert.ok(rig.outbox().queued > 0, "the events are still held in memory");
    assert.equal(rig.journal().broken, true);
    assert.ok(rig.log().some((l) => l.includes("could not be written")),
      "the rig went quietly non-durable");
    assert.deepEqual(rig.errors, []);
  }));

test("a journal that cannot even be read leaves the rig usable",
  withRig({
    search: "?demo",
    journal: {
      durable: true,
      async load() { throw new Error("the database is corrupt"); },
      async appendEvent() {}, async forgetEvents() {},
      async putVideo() {}, async forgetVideo() {},
    },
  }, async (rig) => {
    offline();
    saveATake(rig);
    assert.equal(rig.screen(), "resetting");
    assert.deepEqual(rig.errors, []);
  }));
