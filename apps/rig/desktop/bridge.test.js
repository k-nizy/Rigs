/* =====================================================================
 * bridge.test.js  -  the journal the Tauri shell injects into the page
 *
 * `apps/rig/desktop/src/bridge.rs` carries a string of JavaScript that it
 * injects before any page script runs. That string is what makes
 * `window.RIG_JOURNAL` a file on disk rather than an IndexedDB store, and
 * nothing compiles it: it is a Rust string literal, so a typo in it is
 * invisible to `cargo build`, to `cargo test`, and to every test in this
 * repository that mounts the rig with a journal of its own.
 *
 * The way that fails is the quiet way. rig.js reads `window.RIG_JOURNAL`
 * and falls back to the browser journal when it is missing - so a script
 * that will not parse does not break the rig, it *demotes* it. The screen
 * still says the work is safe, because the browser journal also reports
 * `durable: true`, and the only thing actually lost is the synchronous
 * write the shell exists to provide. A rig could run a whole shift like
 * that and nothing on it would say so.
 *
 * That is not hypothetical. Writing the video half of this bridge put an
 * invalid escape into the script, and cargo build, cargo test, npm test
 * and CI all went green. Only a webview on a screen showed it.
 *
 * So this file reads the string out of the Rust source, runs it against a
 * stand-in for the shell that refuses what the real commands refuse, and
 * then hands the result to the real rig. If it parses, if it answers the
 * calls rig.js makes, and if a take survives a restart through it, the
 * bridge is doing its job.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { mountRig } = require("../test/dom.js");

/* -------------------------------------------------------------------
 * The script, straight out of the Rust source.
 *
 * Read rather than copied. A copy here would be the thing that passes
 * while the shipped one is broken, which is the whole failure this file
 * is about.
 * ------------------------------------------------------------------- */

function injectedScript() {
  const src = fs.readFileSync(path.join(__dirname, "src", "bridge.rs"), "utf8");
  const fn = src.indexOf("pub fn init_script()");
  assert.notEqual(fn, -1, "bridge.rs no longer has an init_script()");
  const open = src.indexOf('r#"', fn) + 3;
  const close = src.indexOf('"#', open);
  assert.ok(close > open, "init_script()'s raw string is not where it was");
  return src.slice(open, close);
}

/* -------------------------------------------------------------------
 * A stand-in for the shell.
 *
 * It answers the commands the way `bridge.rs` and `journal.rs` do, and it
 * is strict in the two places the real one is strict: a take arrives as
 * bytes and never as JSON, and its metadata arrives in a header, which
 * may carry only visible ASCII. Being lax about either would let this
 * file go green over a bridge a real webview refuses.
 * ------------------------------------------------------------------- */

const isBytes = (v) =>
  v instanceof ArrayBuffer || ArrayBuffer.isView(v) || Array.isArray(v);

function visibleAscii(s) {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c < 0x20 || c > 0x7e) return false;
  }
  return true;
}

function shell() {
  const events = new Map();     // eventId -> event
  const meta = new Map();       // key -> the row `load` lists
  const bytes = new Map();      // key -> Uint8Array, never listed
  const stints = new Map();     // rigId -> row
  const calls = [];

  function handle(cmd, payload, options) {
    switch (cmd) {
      case "rig_journal_load": {
        const rigId = payload.rigId;
        return {
          events: [...events.values()]
            .filter((e) => e.rigId === rigId)
            .sort((a, b) => (a.seq || 0) - (b.seq || 0)),
          /* Metadata only, exactly as `Journal::load` does: a boot with a
             full queue must not cost several hundred megabytes. */
          videos: [...meta.values()].filter((v) => v.rigId === rigId),
          stint: stints.get(rigId) || null,
        };
      }
      case "rig_journal_append_event":
        events.set(payload.event.eventId, payload.event);
        return null;
      case "rig_journal_forget_events":
        payload.ids.forEach((id) => events.delete(id));
        return null;
      case "rig_journal_put_stint":
        stints.set(payload.row.rigId, payload.row);
        return null;
      case "rig_journal_put_video": {
        /* The refusals the Rust makes, made here too. */
        if (!isBytes(payload)) {
          throw new Error("a video must be sent as bytes, not JSON");
        }
        const header = ((options && options.headers) || {})["x-rig-video"];
        if (typeof header !== "string") throw new Error("a video arrived with no metadata");
        if (!visibleAscii(header)) {
          throw new Error("a header may not carry " + JSON.stringify(header));
        }
        const m = JSON.parse(header);
        if (typeof m.key !== "string") throw new Error("a video's metadata has no key");
        if (typeof m.rigId !== "string") throw new Error("a video's metadata has no rigId");
        meta.set(m.key, m);
        bytes.set(m.key, new Uint8Array(payload));
        return null;
      }
      case "rig_journal_read_video": {
        const held = bytes.get(payload.key);
        if (!held) throw new Error("no such take: " + payload.key);
        return held.buffer.slice(held.byteOffset, held.byteOffset + held.byteLength);
      }
      case "rig_journal_forget_video":
        meta.delete(payload.key);
        bytes.delete(payload.key);
        return null;
      default:
        throw new Error("no such command: " + cmd);
    }
  }

  const win = {
    __TAURI_INTERNALS__: {
      invoke(cmd, payload, options) {
        calls.push({ cmd, payload, options });
        try {
          return Promise.resolve(handle(cmd, payload === undefined ? {} : payload, options));
        } catch (e) {
          return Promise.reject(e);
        }
      },
    },
  };

  new Function("window", injectedScript())(win);

  const j = win.RIG_JOURNAL;
  assert.ok(j, "the injected script did not define window.RIG_JOURNAL");
  /* So a test that hands this to mountRig can read the durable half. */
  j.heldEvents = () => [...events.values()];
  j.heldVideos = () => [...meta.values()];
  j.heldBytes = () => [...bytes.values()];
  j.calls = calls;
  j.called = (cmd) => calls.filter((c) => c.cmd === cmd);
  return j;
}

// ------------------------------------------------------------ it parses

test("the script the shell injects is a script", () => {
  /* new Function throws a SyntaxError on a bad escape, which is what took
     a whole webview to notice the first time. */
  assert.doesNotThrow(() => new Function("window", injectedScript()));
});

test("it answers every call rig.js makes on a journal", () => {
  const j = shell();
  assert.equal(j.durable, true, "the page decides what to promise from this");
  for (const call of ["load", "appendEvent", "forgetEvents",
                      "putVideo", "forgetVideo", "putStint"]) {
    assert.equal(typeof j[call], "function", call + " is missing from the bridge");
  }
});

// ------------------------------------------------------------- the bytes

test("a take crosses as bytes, not as a JSON array of numbers", async () => {
  const j = shell();
  const take = new Uint8Array(4096).map((_, i) => i % 251);

  await j.putVideo({ key: "ep-1/wrist-l", rigId: "RIG-03", episodeId: "ep-1",
                     camera: "wrist-l", blob: new Blob([take]) });

  const [sent] = j.called("rig_journal_put_video");
  assert.ok(isBytes(sent.payload),
    "the take went over the JSON channel - tens of megabytes as decimal digits");
  assert.equal(sent.payload.byteLength, take.length);
  assert.deepEqual(j.heldBytes()[0], take, "the bytes on the other side are not the take");
});

test("a take's metadata rides in a header, and the blob does not", async () => {
  const j = shell();
  await j.putVideo({ key: "ep-1/wrist-l", rigId: "RIG-03", episodeId: "ep-1",
                     camera: "wrist-l", blob: new Blob([new Uint8Array(8)]) });

  const [sent] = j.called("rig_journal_put_video");
  const meta = JSON.parse(sent.options.headers["x-rig-video"]);
  assert.deepEqual(meta, { key: "ep-1/wrist-l", rigId: "RIG-03",
                           episodeId: "ep-1", camera: "wrist-l" },
    "what lands on disk should be named, not whatever the caller passed");
  assert.equal(meta.blob, undefined, "the blob is the body, not the metadata");
});

test("metadata a header could not carry is escaped rather than sent", async () => {
  const j = shell();
  /* The stand-in refuses a header value outside visible ASCII, because a
     real header cannot carry one. JSON's own escape is itself ASCII and
     serde_json reads it back, which is why the page escapes rather than
     encodes. */
  const odd = "RIG-Ø1";
  await j.putVideo({ key: "ep-é/wrist-l", rigId: odd,
                     episodeId: "ep-é", camera: "wrist-l",
                     blob: new Blob([new Uint8Array(8)]) });

  const [sent] = j.called("rig_journal_put_video");
  const header = sent.options.headers["x-rig-video"];
  assert.ok(visibleAscii(header), "a header value must be visible ASCII");
  assert.equal(JSON.parse(header).rigId, odd, "escaping it must not change what arrives");
});

test("held takes are listed without their bytes, and read when asked for", async () => {
  const j = shell();
  const take = new Uint8Array(2048).map((_, i) => i % 251);
  await j.putVideo({ key: "ep-1/wrist-l", rigId: "RIG-03", episodeId: "ep-1",
                     camera: "wrist-l", blob: new Blob([take]) });

  const held = await j.load("RIG-03");
  assert.equal(held.videos.length, 1);
  assert.equal(j.called("rig_journal_read_video").length, 0,
    "load pulled the bytes across before anything asked for them");

  const back = new Uint8Array(await held.videos[0].blob.arrayBuffer());
  assert.equal(j.called("rig_journal_read_video").length, 1);
  assert.deepEqual(back, take, "the take came back changed");
});

test("a rig's takes are its own", async () => {
  const j = shell();
  const one = new Blob([new Uint8Array(16)]);
  await j.putVideo({ key: "ep-1/wrist-l", rigId: "RIG-03", episodeId: "ep-1",
                     camera: "wrist-l", blob: one });
  await j.putVideo({ key: "ep-2/wrist-l", rigId: "RIG-07", episodeId: "ep-2",
                     camera: "wrist-l", blob: one });

  const held = await j.load("RIG-03");
  assert.deepEqual(held.videos.map((v) => v.key), ["ep-1/wrist-l"]);
});

// ------------------------------------------------- and the real rig on it

const TAKE = new Blob([new Uint8Array(1024).fill(3)]);

const offline = () => { global.fetch = async () => { throw new Error("no network"); }; };

const confirming = () => {
  global.fetch = async (url) => {
    const u = String(url);
    if (u.includes("/cursor")) return { ok: true, json: async () => ({ seq: 0 }) };
    if (u.includes("video:presign")) {
      return { ok: true, status: 200, json: async () => ({ url: "/upload", method: "PUT" }) };
    }
    if (u.includes("video:complete")) {
      return { ok: true, status: 200,
               json: async () => ({ safeToDelete: true, key: "k" }) };
    }
    return { ok: true, status: 200,
             json: async () => ({ accepted: 3, duplicates: 0, cursor: 3 }) };
  };
};

/* Checklist, record, stop, review - the same four presses the other rig
   tests use to land one episode. */
function saveATake(rig) {
  rig.press(2); rig.frames(1);
  rig.press(2); rig.frames(30);
  rig.press(3); rig.frames(1);
  rig.press(2); rig.frames(1);
}

test("a take survives a restart across the real bridge", async () => {
  /* The same journal handed to two mounts, which is what a reboot is -
     only this time the journal is the shell's own, evaluated out of the
     Rust source rather than written for the test. */
  const j = shell();

  const first = await mountRig({ search: "?demo", journal: j });
  try {
    offline();
    first.setVideoSource(() => TAKE);
    saveATake(first);
    await first.settle();
    assert.equal(first.video().queued, 3);
    assert.equal(j.heldVideos().length, 3, "the bytes never reached the shell");
  } finally { first.stop(); }

  const second = await mountRig({ search: "?demo", journal: j });
  try {
    assert.equal(second.video().queued, 3,
      "a restart lost takes the shell was holding on disk");
  } finally { second.stop(); }
});

test("a take the server has verified is released from the shell too", async () => {
  const j = shell();
  const rig = await mountRig({ search: "?demo", journal: j });
  try {
    confirming();
    rig.setVideoSource(() => TAKE);
    saveATake(rig);
    await rig.settle();
    assert.equal(j.heldVideos().length, 3);

    await rig.uploadVideo(4);
    assert.equal(rig.video().queued, 0);
    assert.deepEqual(j.heldVideos(), [],
      "the shell kept bytes the server had already verified");
    assert.deepEqual(j.heldBytes(), [], "the metadata went but the bytes stayed");
  } finally { rig.stop(); }
});
