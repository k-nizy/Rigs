/* =====================================================================
 * event.js
 *
 * The shape of what a rig sends back - the contract for the return
 * arrow, as payload.js is the contract for the schedule going out.
 *
 * validate(event) -> { ok, errors }
 *
 * Same discipline as payload.js on purpose: no dependencies, runs in the
 * browser and under node, so the rig validates before it journals and
 * the server validates before it stores. Where the two sides are written
 * in different languages, `event.schema.json` is the canonical spec and
 * `fixtures/` is what proves they still agree.
 *
 * Two rules this file exists to enforce:
 *
 *   1. Every event carries its own identity. `eventId` is minted on the
 *      rig at pedal-press, before anything downstream is told, so a
 *      batch can be resent blindly and land exactly once. `seq` is
 *      per-rig and monotonic, which is what the uploader's cursor reads.
 *
 *   2. Measurements, never conclusions. The rig used to emit the string
 *      "74% efficiency". Stored, that becomes the only copy of the truth
 *      and cannot be corrected without re-running the floor. What ships
 *      instead is the four seconds columns the ratio was made of, so the
 *      formula can change in six months and every shift already recorded
 *      recomputes correctly.
 * ===================================================================== */

(function (root) {
  "use strict";

  var HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;
  var DATE = /^\d{4}-\d{2}-\d{2}$/;
  var ISO  = /^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
  var UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  /* The five buckets the rig app already names in emit(), and the events
     that land in each. A bucket is a table on the far side, so this map
     is also the list of every event the floor can produce. */
  var BUCKETS = {
    episodes: ["episode_saved", "episode_discarded"],
    rig_shift_checks: ["shift_check", "fault_opened", "fault_reclassified",
                       "fault_closed", "fault_cancelled"],
    rig_downtime_events: ["rig_down", "rig_up"],
    rig_productivity_blocks: ["stint_ended"],
    sessions: ["session_ended"],
  };

  /* What each event's `data` has to carry. Anything not listed here is
     allowed through - the far side ignores what it does not know, which
     is what lets the rig ship a new field before the server reads it. */
  var REQUIRED_DATA = {
    episode_saved:     { episodeId: "uuid", durationSecs: "secs", score: "score" },
    episode_discarded: { episodeId: "uuid", durationSecs: "secs" },
    shift_check:       { outcome: "string" },
    fault_opened:      { subsystem: "string" },
    fault_reclassified:{ subsystem: "string" },
    fault_closed:      { subsystem: "string", secondsCharged: "secs" },
    fault_cancelled:   { subsystem: "string", secondsCharged: "secs" },
    rig_down:          { issue: "string", needsManager: "bool", chargedTo: "string" },
    rig_up:            { downSecs: "secs" },
    stint_ended:       { episodes: "count", recordedSecs: "secs",
                         assignedSecs: "secs", faultSecs: "secs", downSecs: "secs" },
    session_ended:     { endedBy: "string" },
  };

  function validate(ev) {
    var e = [];
    if (!ev || typeof ev !== "object") { return { ok: false, errors: ["event is not an object"] }; }

    // ---- identity
    if (typeof ev.eventId !== "string" || !UUID.test(ev.eventId)) {
      e.push("eventId must be a uuid");
    }
    if (typeof ev.seq !== "number" || !isFinite(ev.seq) || ev.seq < 0 || ev.seq % 1 !== 0) {
      e.push("seq must be a non-negative whole number");
    }
    if (typeof ev.at !== "string" || !ISO.test(ev.at)) {
      e.push("at must be an ISO 8601 timestamp with a zone");
    }

    // ---- where and when it happened, all of it from the pushed payload
    reqString(ev, "rigId", e);
    if (typeof ev.shiftDate !== "string" || !DATE.test(ev.shiftDate)) {
      e.push("shiftDate must be YYYY-MM-DD");
    }
    reqString(ev, "shiftLabel", e);
    // turnFrom and operatorId are null when nothing is scheduled - a rig
    // on standby still reports a shift check.
    if (ev.turnFrom != null && (typeof ev.turnFrom !== "string" || !HHMM.test(ev.turnFrom))) {
      e.push("turnFrom must be HH:MM or null");
    }
    if (ev.operatorId != null && typeof ev.operatorId !== "string") {
      e.push("operatorId must be a string or null");
    }
    /* The id is a seat - rotation-engine builds it as "op-" + group +
       slot - so the same string is a different person on a cover day,
       and nothing downstream keeps a name to tell them apart.

       Optional on purpose, and it must stay optional: every event
       already queued on a rig was written before this field existed,
       and a rig drops a refused batch from its outbox and forgets it
       from the journal rather than retrying. Demanding it would destroy
       that backlog rather than delay it. */
    if (ev.operatorName != null && typeof ev.operatorName !== "string") {
      e.push("operatorName must be a string or null");
    }

    // ---- what happened
    reqString(ev, "bucket", e);
    reqString(ev, "event", e);
    if (ev.bucket && !BUCKETS[ev.bucket]) {
      e.push("bucket " + JSON.stringify(ev.bucket) + " is not one of: " + Object.keys(BUCKETS).join(", "));
    } else if (ev.bucket && ev.event && BUCKETS[ev.bucket].indexOf(ev.event) === -1) {
      e.push("event " + JSON.stringify(ev.event) + " does not belong in bucket " + JSON.stringify(ev.bucket));
    }

    if (!ev.data || typeof ev.data !== "object" || Array.isArray(ev.data)) {
      e.push("data must be an object");
    } else if (ev.event && REQUIRED_DATA[ev.event]) {
      validateData(ev.event, ev.data, e);
    }

    return { ok: e.length === 0, errors: e };
  }

  function validateData(event, data, e) {
    var want = REQUIRED_DATA[event];
    Object.keys(want).forEach(function (k) {
      var at = "data." + k;
      var kind = want[k];
      var v = data[k];
      if (kind === "uuid") {
        if (typeof v !== "string" || !UUID.test(v)) e.push(at + " must be a uuid");
      } else if (kind === "secs" || kind === "count") {
        // Seconds are measurements: never negative, never a percentage,
        // never a formatted string.
        if (typeof v !== "number" || !isFinite(v) || v < 0) {
          e.push(at + " must be a number of " + (kind === "secs" ? "seconds" : "items") + ", not below zero");
        }
      } else if (kind === "score") {
        if (v !== 3 && v !== 4 && v !== 5) e.push(at + " must be 3, 4 or 5");
      } else if (kind === "bool") {
        if (typeof v !== "boolean") e.push(at + " must be a boolean");
      } else {
        if (typeof v !== "string" || !v) e.push(at + " must be a non-empty string");
      }
    });
  }

  function reqString(o, k, e, prefix) {
    if (typeof o[k] !== "string" || !o[k]) e.push((prefix || "") + k + " must be a non-empty string");
  }

  var api = { validate: validate, BUCKETS: BUCKETS, REQUIRED_DATA: REQUIRED_DATA };

  root.EventSchema = api;
  if (typeof module === "object" && module.exports) module.exports = api;

})(typeof window !== "undefined" ? window : globalThis);
