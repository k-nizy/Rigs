/* =====================================================================
 * stint-resume.test.js  -  a reload does not cost the operator their turn
 *
 * `boot()` builds the whole session from zero, because every boot is
 * treated as the start of a shift. That is right exactly once - at the
 * crew change - and wrong every other time the rig starts.
 *
 * The events survive: the rig journals each one before it touches the
 * network, so an episode recorded before a reload is in the ledger and
 * the desk can still see it. What does not survive is `S` - the episode
 * count, the recorded seconds, the fault and downtime totals. So an
 * operator who reloads watches their own turn go back to nothing while
 * the board across the room still shows it.
 *
 * The journal cannot answer this on its own. It is an outbox, not an
 * archive: `forgetEvents` runs the moment the server accepts a batch, so
 * on an ordinary reload it holds nothing at all. The running total has
 * to be kept as a running total.
 *
 * What is pinned here is the rule for picking it up again - the same
 * turn, the same shift, the same rig, or start clean. Deliberately not a
 * grace period: a turn is what work is attributed to, so the turn
 * boundary is the honest edge, and the crew change crosses one by
 * definition.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");

const { mountRig } = require("./test/dom.js");

/* Survives a remount, which is what a reload is. Same shape as the one
   in journal.test.js, plus the stint row. */
function fakeJournal() {
  const events = new Map();
  const videos = new Map();
  let stint = null;
  return {
    durable: true,
    async load(rigId) {
      return {
        events: [...events.values()].filter((e) => e.rigId === rigId)
                                    .sort((a, b) => a.seq - b.seq),
        videos: [...videos.values()].filter((v) => v.rigId === rigId),
        stint: stint && stint.rigId === rigId ? stint : null,
      };
    },
    async appendEvent(e) { events.set(e.eventId, e); },
    async forgetEvents(ids) { ids.forEach((id) => events.delete(id)); },
    async putVideo(v) { videos.set(v.key, v); },
    async forgetVideo(k) { videos.delete(k); },
    async putStint(row) { stint = row; },
    heldEvents: () => [...events.values()],
    heldStint: () => stint,
  };
}

/* A journal from before there was a stint row - an older browser
   database, or the fakes the other suites inject. */
function journalWithoutStints() {
  const events = new Map();
  return {
    durable: true,
    async load() { return { events: [...events.values()], videos: [] }; },
    async appendEvent(e) { events.set(e.eventId, e); },
    async forgetEvents(ids) { ids.forEach((id) => events.delete(id)); },
    async putVideo() {},
    async forgetVideo() {},
  };
}

const accepting = () => {
  global.fetch = async (url) => {
    if (String(url).includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    return { ok: true, status: 200, json: async () => ({ accepted: 1 }) };
  };
};

function saveATake(rig) {
  rig.press(2); rig.frames(1);      // check passed
  rig.press(2); rig.frames(30);     // start, record
  rig.press(3); rig.frames(1);      // save
  rig.press(2); rig.frames(1);      // scored
}

/* Work a turn, then come back as a reload would: same journal, same
   schedule, new mount. */
async function workThenReload(j, opts) {
  const o = opts || {};
  const first = await mountRig({ search: "?demo", journal: j });
  let before;
  try {
    accepting();
    saveATake(first);
    saveATake(first);
    before = first.stint();
    if (o.upload) await first.upload();
    /* Journal writes are a promise chain, as they are in the browser.
       A reload does not wait for it either - this is the same window
       `rig.js` already documents, where a hard cut can lose the last
       write - but a test that raced it would be testing the clock. */
    await first.settle();
  } finally { first.stop(); }

  const second = await mountRig({ search: o.search || "?demo", journal: j });
  return { before, second };
}

// ------------------------------------------------------- what is kept

test("a reload keeps the episodes the operator already recorded", async () => {
  const j = fakeJournal();
  const { before, second } = await workThenReload(j);
  try {
    assert.equal(before.episode, 2, "two takes were saved before the reload");
    assert.equal(second.stint().episode, 2,
      "the operator's own screen went back to zero while the ledger kept them");
    assert.equal(second.stint().recordedSecs, before.recordedSecs,
      "recorded seconds are the numerator of their efficiency");
  } finally { second.stop(); }
});

test("it is kept even once the server has the events", async () => {
  /* The case that rules the journal out as the source. `forgetEvents`
     runs on acknowledgement, so after a successful upload the journal
     holds no episodes to count - only the running total remains. */
  const j = fakeJournal();
  const { second } = await workThenReload(j, { upload: true });
  try {
    assert.deepEqual(j.heldEvents().filter((e) => e.event === "episode_saved"), [],
      "the journal is an outbox; acknowledged events are gone from it");
    assert.equal(second.stint().episode, 2,
      "so the count cannot come from the events - it has to be kept as a total");
  } finally { second.stop(); }
});

test("time already worked in the turn is not given back", async () => {
  /* assignedSecs is the denominator. Restore the numerator alone and a
     restarted stint scores a full turn's work against a few minutes,
     which reads as a perfect one. */
  const j = fakeJournal();
  const { before, second } = await workThenReload(j);
  try {
    assert.ok(before.assignedSecs > 0, "the first mount worked some of the turn");
    assert.ok(second.stint().assignedSecs >= before.assignedSecs,
      "the turn does not start again just because the page did: " +
      before.assignedSecs + "s worked, " + second.stint().assignedSecs + "s after reload");
  } finally { second.stop(); }
});

test("the operator is told, rather than the numbers just reappearing", async () => {
  const j = fakeJournal();
  const { second } = await workThenReload(j);
  try {
    const log = second.$("log").textContent;
    assert.match(log, /stint_resumed/,
      "a total that changes with no explanation is the thing being fixed");
  } finally { second.stop(); }
});

// --------------------------------------------------- what is not kept

test("a different turn does not inherit the last one's numbers", async () => {
  /* The whole rule. Work belongs to a turn, so a boot into a different
     turn starts clean - which is what the crew change is. */
  const j = fakeJournal();
  const first = await mountRig({ search: "?demo", journal: j });
  try {
    accepting();
    saveATake(first);
    assert.equal(first.stint().episode, 1);
    await first.settle();
  } finally { first.stop(); }

  const held = j.heldStint();
  assert.ok(held && held.turnKey, "a stint was held with the turn it belongs to");
  j.heldStint().turnKey = "23:59";      // as a later turn would read it

  const second = await mountRig({ search: "?demo", journal: j });
  try {
    assert.equal(second.stint().episode, 0,
      "a turn that is not this one must not hand its work to whoever is here now");
  } finally { second.stop(); }
});

test("the same turn on a different day does not either", async () => {
  /* Turn labels are "HH:MM" and repeat on every shift of every day. The
     turn alone would let a rig booting a day later inherit a stranger's
     numbers - the same trap floor.py names about matching a finished
     block against a schedule. */
  const j = fakeJournal();
  const first = await mountRig({ search: "?demo", journal: j });
  try {
    accepting();
    saveATake(first);
    await first.settle();
  } finally { first.stop(); }

  j.heldStint().shiftDate = "1999-01-01";

  const second = await mountRig({ search: "?demo", journal: j });
  try {
    assert.equal(second.stint().episode, 0, "same turn label, different day");
  } finally { second.stop(); }
});

test("the checklist is not resumed, because the rig has not been looked at", async () => {
  const j = fakeJournal();
  const { second } = await workThenReload(j);
  try {
    assert.equal(second.screen(), "checklist",
      "the numbers are a record of what happened; the check is a claim " +
      "about the rig now, and a machine that just restarted has not been seen");
  } finally { second.stop(); }
});

test("a journal that has never heard of stints still works", async () => {
  /* The seam has to tolerate what is already out there: an older browser
     database, or a harness that injects only the two stores. */
  const j = journalWithoutStints();
  const rig = await mountRig({ search: "?demo", journal: j });
  try {
    accepting();
    saveATake(rig);
    assert.equal(rig.stint().episode, 1, "the rig keeps working");
    assert.equal(rig.journal().broken, false, "and does not report a broken journal");
  } finally { rig.stop(); }
});
