/* =====================================================================
 * in-force.test.js  -  which of these shifts is running right now
 *
 * A payload covers one shift. Anything holding a whole day - the push
 * server, and the desk's Live view reading back from it - has to pick
 * one, and the way it picks is the thing under test.
 *
 * The bug this was written for: the push server kept ONE payload per
 * rig, so a whole-day push of 36 wrote Morning, then Day, then Night
 * into the same slot and the last one silently won. At 16:30 on a
 * Tuesday the entire floor was being handed the Night shift, which had
 * not started. Live showed it, every rig fetched it, and nothing
 * anywhere reported an error - the payload was valid, it was simply the
 * wrong one.
 *
 * The rule being protected: this READS the window the desk wrote into
 * the payload and compares. It never works out when a shift runs. A
 * second opinion about the schedule is the one thing that can disagree
 * with the desk that made it.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const RE = require(path.resolve(__dirname, "rotation-engine.js"));

const WINDOWS = { Morning: ["08:00", "16:00"], Day: ["16:00", "00:00"], Night: ["00:00", "08:00"] };

function payload(label, opts) {
  const o = opts || {};
  const w = WINDOWS[label];
  return {
    rigId: o.rig || "RIG-03", group: "A", task: "Box transfer",
    shift: { label: label, date: o.date || "2026-08-25",
             start: w[0], end: w[1], tz: o.tz || "UTC" },
    blockMinutes: 15, rotation: "hold", autoSignIn: true, turns: [],
  };
}

const day = (opts) => ["Morning", "Day", "Night"].map((l) => payload(l, opts));
const at = (iso) => Date.parse(iso);
const labelAt = (held, iso) => {
  const got = RE.inForce(held, at(iso));
  return got ? got.shift.label : null;
};

// ------------------------------------------------------------ the bug

test("at half four in the afternoon the floor is on Day, not Night", () => {
  /* The exact failure: 36 payloads collapsed to one slot per rig and the
     last written - Night - was served to everybody. */
  assert.equal(labelAt(day(), "2026-08-25T16:30:00Z"), "Day");
});

test("each of the three shifts is picked inside its own hours", () => {
  const held = day();
  assert.equal(labelAt(held, "2026-08-25T09:00:00Z"), "Morning");
  assert.equal(labelAt(held, "2026-08-25T18:00:00Z"), "Day");
  assert.equal(labelAt(held, "2026-08-25T03:00:00Z"), "Night");
});

test("the order the payloads arrive in does not decide the answer", () => {
  /* The old behaviour in one line: whichever came last won. */
  const forward = day();
  const backward = day().slice().reverse();
  const noon = "2026-08-25T12:00:00Z";
  assert.equal(labelAt(forward, noon), "Morning");
  assert.equal(labelAt(backward, noon), "Morning",
    "the answer changed when the list was reordered, so it is still last-write-wins");
});

// ------------------------------------------------------- the boundaries

test("a shift owns its first minute and not its last", () => {
  const held = day();
  assert.equal(labelAt(held, "2026-08-25T15:59:59Z"), "Morning");
  assert.equal(labelAt(held, "2026-08-25T16:00:00Z"), "Day",
    "16:00 belongs to the shift starting, or two shifts claim one minute");
  assert.equal(labelAt(held, "2026-08-25T23:59:59Z"), "Day");
});

test("the Day shift crosses midnight and still ends on time", () => {
  /* 16:00 to 00:00. Its end is on the following date, and a naive
     start < now < end comparison decides it covers nothing at all. */
  const held = day();
  assert.equal(labelAt(held, "2026-08-25T22:00:00Z"), "Day");

  // 00:30 on the 26th is NOT the 25th's Night - that one ran this morning.
  assert.equal(labelAt(held, "2026-08-26T00:30:00Z"), null,
    "a finished shift was reported as running, which is how a rig ends up "
    + "holding yesterday's schedule");
});

test("the next day's Night is a different shift from this day's", () => {
  const held = day().concat(day({ date: "2026-08-26" }));
  const got = RE.inForce(held, at("2026-08-26T00:30:00Z"));
  assert.ok(got, "nothing covered 00:30");
  assert.equal(got.shift.label, "Night");
  assert.equal(got.shift.date, "2026-08-26",
    "it matched the previous day's Night, which had already finished");
});

// ------------------------------------------------------- the floor's zone

test("the floor's clock decides, not the server's", () => {
  /* A UTC server and a UTC+2 floor. 14:30Z is 16:30 on the floor, so the
     floor's Day shift is running even though the server's own clock says
     it is still the afternoon. This is the case that was live when the
     bug was found. */
  const held = day({ tz: "Europe/Budapest" });
  assert.equal(labelAt(held, "2026-08-25T14:30:00Z"), "Day");
  assert.equal(labelAt(held, "2026-08-25T13:30:00Z"), "Morning",
    "15:30 on the floor is still the Morning shift");
});

test("a floor west of UTC is handled the same way", () => {
  const held = day({ tz: "America/Denver" });                 // UTC-6 in August
  assert.equal(labelAt(held, "2026-08-25T22:30:00Z"), "Day"); // 16:30 local
  assert.equal(labelAt(held, "2026-08-25T15:00:00Z"), "Morning");
});

test("a shift is eight hours long wherever it is", () => {
  for (const tz of ["UTC", "Europe/Budapest", "America/Denver", "Asia/Kolkata", "Pacific/Chatham"]) {
    const w = RE.shiftWindow(payload("Day", { tz }));
    assert.equal((w.end - w.start) / 60000, 480, tz + " produced a shift that is not 8h");
  }
});

// ------------------------------------------------- malformed, not malicious

test("a payload with no window is never claimed to cover", () => {
  const broken = payload("Morning");
  delete broken.shift.start;
  assert.equal(RE.shiftWindow(broken), null);
  assert.equal(RE.coversAt(broken, at("2026-08-25T09:00:00Z")), false,
    "a payload with no window matched, so it would match every instant");
});

test("nothing at all is null rather than a guess", () => {
  assert.equal(RE.inForce([], at("2026-08-25T09:00:00Z")), null);
  assert.equal(RE.inForce(null, at("2026-08-25T09:00:00Z")), null);
});

test("the desk's stated end wins over the eight-hour assumption", () => {
  /* If the desk ever draws a different length, the window has to follow
     what it said - not the constant in here. */
  const short = payload("Morning");
  short.shift.end = "10:00";
  assert.equal((RE.shiftWindow(short).end - RE.shiftWindow(short).start) / 60000, 120);
  assert.equal(RE.coversAt(short, at("2026-08-25T09:00:00Z")), true);
  assert.equal(RE.coversAt(short, at("2026-08-25T11:00:00Z")), false);
});

test("one rig's schedule is never mistaken for another's", () => {
  /* inForce answers about time only. Whose payloads these are is the
     caller's business, and this makes that explicit. */
  const held = [payload("Day", { rig: "RIG-07" })];
  const got = RE.inForce(held, at("2026-08-25T18:00:00Z"));
  assert.equal(got.rigId, "RIG-07");
});
