/* =====================================================================
 * payload.js
 *
 * The shape of what the desk pushes to a rig - the one contract three
 * things have to agree on now (desk emits, server relays, rig consumes).
 *
 * validate(payload) -> { ok, errors }
 *
 * Kept plain: no dependencies, runs in the browser and under node, so
 * the desk validates before pushing, the server validates before storing
 * and the rig can validate before signing anyone in.
 * ===================================================================== */

(function (root) {
  "use strict";

  var HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

  function validate(p) {
    var e = [];
    if (!p || typeof p !== "object") { return { ok: false, errors: ["payload is not an object"] }; }

    reqString(p, "rigId", e);
    reqString(p, "group", e);
    reqString(p, "task", e);
    reqNumber(p, "blockMinutes", e);
    reqString(p, "rotation", e);
    if (p.rotation && p.rotation !== "hold" && p.rotation !== "rotate") {
      e.push("rotation must be 'hold' or 'rotate', got " + JSON.stringify(p.rotation));
    }

    if (!p.shift || typeof p.shift !== "object") { e.push("shift is missing"); }
    else {
      reqString(p.shift, "label", e, "shift.");
      reqString(p.shift, "date",  e, "shift.");
      reqHHMM(p.shift,   "start", e, "shift.");
      reqHHMM(p.shift,   "end",   e, "shift.");
      // The floor's IANA zone. Required: every time in this payload is
      // floor wall-clock, and a reader in another zone cannot recover
      // which one without being told.
      reqString(p.shift, "tz", e, "shift.");
    }

    if (!Array.isArray(p.turns)) { e.push("turns is not an array"); }
    else {
      p.turns.forEach(function (t, i) { validateTurn(t, i, e); });
    }

    return { ok: e.length === 0, errors: e };
  }

  function validateTurn(t, i, e) {
    var at = "turns[" + i + "].";
    if (!t || typeof t !== "object") { e.push(at + "not an object"); return; }
    reqHHMM(t,   "from",    e, at);
    reqHHMM(t,   "to",      e, at);
    reqNumber(t, "minutes", e, at);
    if (!t.operator || typeof t.operator !== "object") { e.push(at + "operator is missing"); }
    else {
      reqString(t.operator, "id",   e, at + "operator.");
      reqString(t.operator, "name", e, at + "operator.");
      // The person in that seat, as the id that is theirs. Optional, and
      // it must stay optional: a laptop demo and a floor with no people
      // table push none, and neither is to be refused.
      if (t.operator.personId != null && typeof t.operator.personId !== "string") {
        e.push(at + "operator.personId must be a string or null");
      }
    }
    // relievedBy may be null on the last turn of the shift.
    if (t.relievedBy != null && typeof t.relievedBy !== "string") {
      e.push(at + "relievedBy must be a string or null");
    }
    // theyGoTo carries the destination the outgoing operator moves to.
    // It has to travel in the payload because it is a fact about their
    // day, not derivable from this rig alone.
    reqString(t, "theyGoTo", e, at);
  }

  function reqString(o, k, e, prefix) {
    if (typeof o[k] !== "string" || !o[k]) e.push((prefix || "") + k + " must be a non-empty string");
  }
  function reqNumber(o, k, e, prefix) {
    if (typeof o[k] !== "number" || !isFinite(o[k])) e.push((prefix || "") + k + " must be a number");
  }
  function reqHHMM(o, k, e, prefix) {
    if (typeof o[k] !== "string" || !HHMM.test(o[k])) {
      e.push((prefix || "") + k + " must be HH:MM");
    }
  }

  var api = { validate: validate };

  root.PayloadSchema = api;
  if (typeof module === "object" && module.exports) module.exports = api;

})(typeof window !== "undefined" ? window : globalThis);
