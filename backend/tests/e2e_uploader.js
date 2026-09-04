/* =====================================================================
 * e2e_uploader.js  -  the rig's uploader against a real service
 *
 * `e2e_rig_to_floor.js` drives the page through a shift and proves the
 * seam a browser sits on. This proves the other one: the process that
 * carries the journal when the page is not there.
 *
 * The two halves of the uploader are tested in different places for a
 * reason. Its decisions - when the mark may move, when a take's only
 * copy may be unlinked, which URLs may carry the rig's token - live in
 * `apps/rig/desktop/src/upload.rs` behind a trait and are tested there,
 * headless, in the crate. What no trait can test is whether the real
 * service accepts what it actually sends, and that is this file.
 *
 * So it does the one thing the unit tests cannot:
 *
 *   1. asks the service which rig this machine is, at the same path the
 *      page asks - `rig-config.js`, answered per caller
 *   2. writes real events to a real journal on disk, in the format
 *      `journal.rs` writes, having validated them against the schema the
 *      desk and the service already share
 *   3. runs the actual `rig-uploader` binary against the actual service
 *   4. waits for the ledger's own cursor to move, and for the journal to
 *      empty itself
 *
 * Nothing here mocks anything. If the service starts refusing what the
 * uploader sends, this is the file that says so.
 * ===================================================================== */

"use strict";

const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const EventSchema = require("../../packages/schema/event.js");

const BASE = process.env.E2E_BASE || "http://127.0.0.1:8001";
const BIN = process.env.RIG_UPLOADER || "rig-uploader";
/* Generous, because the take cannot be presigned until the episode has
   been projected into a row and projection runs on its own timer. */
const PATIENCE_MS = 90_000;

let failures = 0;

function ok(what) {
  console.log(`  ok    ${what}`);
}

function bad(what, detail) {
  failures += 1;
  console.log(`  FAIL  ${what}`);
  if (detail) console.log(`        ${detail}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------ identity */

/* The same file the page reads, at the same path, parsed the same way
   the uploader parses it. If this cannot name a rig then neither can the
   uploader, and the rest of the test would be meaningless. */
async function identity() {
  const r = await fetch(`${BASE}/apps/rig/rig-config.js`);
  if (!r.ok) throw new Error(`rig-config.js answered ${r.status}`);
  const body = await r.text();
  const read = (name) => {
    const m = body.match(new RegExp(`window\\.${name}\\s*=\\s*(.+?);`));
    if (!m) return null;
    try { return JSON.parse(m[1]); } catch { return null; }
  };
  return { rigId: read("RIG_ID"), token: read("RIG_TOKEN") };
}

/* ------------------------------------------------------- the work to do */

const iso = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");
const today = () => new Date().toISOString().slice(0, 10);

function event(rigId, seq, name, bucket, data) {
  return {
    eventId: crypto.randomUUID(),
    seq,
    at: iso(),
    rigId,
    shiftDate: today(),
    shiftLabel: "Morning",
    turnFrom: "08:00",
    operatorId: "op-a1",
    bucket,
    event: name,
    data,
  };
}

/* Validated here rather than discovered at the service. A malformed event
   would come back as a 422, which the uploader deliberately treats as
   "refused for ever" and retires - so the run would go green having sent
   nothing. Checking against the schema the desk and the service share
   means a failure below is the service refusing something valid. */
function checked(events) {
  events.forEach((ev, i) => {
    const v = EventSchema.validate(ev);
    if (!v.ok) {
      throw new Error(`event ${i} (${ev.event}) is not valid: ${v.errors.join("; ")}`);
    }
  });
  return events;
}

/* The on-disk shapes, exactly as apps/rig/desktop/src/journal.rs writes
   them. Written by hand on purpose: if the journal format changes under
   the uploader, this is one of the two places that has to notice. */
function seed(dir, rigId, episodeId) {
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(path.join(dir, "video"), { recursive: true });

  const events = checked([
    event(rigId, 0, "shift_check", "rig_shift_checks", { outcome: "pass" }),
    event(rigId, 1, "episode_saved", "episodes",
          { episodeId, durationSecs: 42, score: 4 }),
    event(rigId, 2, "stint_ended", "rig_productivity_blocks",
          { episodes: 1, recordedSecs: 42, assignedSecs: 900,
            faultSecs: 0, downSecs: 0 }),
  ]);

  fs.writeFileSync(
    path.join(dir, "journal.ndjson"),
    events.map((e) => JSON.stringify({ op: "put", event: e })).join("\n") + "\n"
  );

  const camera = "wrist-l";
  const key = `${episodeId}/${camera}`;
  const stem = Buffer.from(key, "utf8").toString("hex");
  const take = Buffer.alloc(64 * 1024);
  for (let i = 0; i < take.length; i++) take[i] = i % 251;
  fs.writeFileSync(path.join(dir, "video", `${stem}.bin`), take);
  fs.writeFileSync(
    path.join(dir, "video", `${stem}.json`),
    JSON.stringify({ key, rigId, episodeId, camera })
  );

  return { events, key, take };
}

/* --------------------------------------------------------------- checks */

async function cursor(rigId, token) {
  const r = await fetch(
    `${BASE}/api/rigs/${encodeURIComponent(rigId)}/cursor`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} }
  );
  if (!r.ok) throw new Error(`cursor answered ${r.status}`);
  return r.json();
}

async function until(what, check) {
  const deadline = Date.now() + PATIENCE_MS;
  let last = "";
  while (Date.now() < deadline) {
    try {
      const got = await check();
      if (got === true) return true;
      last = String(got);
    } catch (e) {
      last = String((e && e.message) || e);
    }
    await sleep(1000);
  }
  bad(what, `still not true after ${PATIENCE_MS / 1000}s: ${last}`);
  return false;
}

const emptyDir = (d) => !fs.existsSync(d) || fs.readdirSync(d).length === 0;
const lines = (f) =>
  fs.existsSync(f) ? fs.readFileSync(f, "utf8").split("\n").filter(Boolean).length : 0;

/* ----------------------------------------------------------------- main */

async function main() {
  console.log(`the rig's uploader, against ${BASE}`);

  const who = await identity();
  if (!who.rigId) {
    throw new Error(
      "the service did not place this caller as a rig - set RIG_ADDRESSES " +
      "so the address this test calls from names one"
    );
  }
  ok(`the service places this machine as ${who.rigId}`);

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "rig-e2e-"));
  const episodeId = crypto.randomUUID();
  const { events, key, take } = seed(dir, who.rigId, episodeId);
  ok(`${events.length} valid events and a ${take.length} byte take on disk`);

  const before = await cursor(who.rigId, who.token);
  const startedAt = typeof before.seq === "number" ? before.seq : -1;

  const child = spawn(BIN, [], {
    env: { ...process.env, RIG_URL: `${BASE}/apps/rig/`, RIG_JOURNAL_DIR: dir },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const said = [];
  child.stdout.on("data", (b) => said.push(String(b)));
  child.stderr.on("data", (b) => said.push(String(b)));
  child.on("error", (e) => bad("the uploader would not start", String(e.message || e)));

  try {
    await until("the ledger's cursor moved past what was seeded", async () => {
      const now = await cursor(who.rigId, who.token);
      return now.seq > startedAt ? true : `cursor is ${now.seq}, was ${startedAt}`;
    }) && ok("the service accepted the events the uploader sent");

    await until("the journal emptied itself", async () => {
      const left = lines(path.join(dir, "journal.ndjson"));
      return left === 0 ? true : `${left} events still owed`;
    }) && ok("nothing is still owed");

    await until("the take was confirmed and unlinked", async () => {
      const held = emptyDir(path.join(dir, "video"));
      return held ? true : "the take is still held";
    }) && ok(`the take was accepted, checksummed and released (${key})`);

    const mark = path.join(dir, "uploaded.json");
    if (fs.existsSync(mark)) {
      const m = JSON.parse(fs.readFileSync(mark, "utf8"));
      if (m.rigId === who.rigId && m.seq >= 2) {
        ok(`the uploader recorded its place: ${m.rigId} up to seq ${m.seq}`);
      } else {
        bad("the mark does not name this rig and this shift", JSON.stringify(m));
      }
    } else {
      bad("the uploader never recorded where it got to");
    }
  } finally {
    child.kill("SIGTERM");
    if (failures) {
      console.log("--- what the uploader said ---");
      console.log(said.join("").split("\n").map((l) => `  ${l}`).join("\n"));
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

main().then(
  () => {
    if (failures) {
      console.log(`\n${failures} failed`);
      process.exit(1);
    }
    console.log("\nthe uploader and the service agree");
  },
  (e) => {
    console.error(`\n${(e && e.stack) || e}`);
    process.exit(1);
  }
);
