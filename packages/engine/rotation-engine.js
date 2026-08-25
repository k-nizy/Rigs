/* =====================================================================
 * rotation-engine.js
 *
 * The schedule itself, with no screen attached. Nothing in this file
 * touches the DOM or reads a global, so the manager desk and the rig
 * platform can run the identical code and be guaranteed to agree about
 * who is where. The rig cannot be allowed to compute a different answer
 * from the desk that scheduled it.
 *
 * Everything takes an explicit config:
 *
 *   { shift, date, blockMin, stintBlocks, mode }
 *
 * mode is "hold" or "rotate". Times are minutes-since-midnight.
 * ===================================================================== */

(function (root) {
  "use strict";

  // ---------------------------------------------------------- the floor

  var SHIFTS = [
    { id: "morning", label: "Morning", start: 8 * 60 },
    { id: "day",     label: "Day",     start: 16 * 60 },
    { id: "night",   label: "Night",   start: 0 },
  ];

  var SHIFT_MINUTES = 480;   // all three shifts are eight hours
  var HOLD_STINT    = 3;     // "hold" is forced to a 3-block on-run by the 3:1 duty

  /* The one rule that decides whether a schedule works.
   *
   * An operator's cycle is four stints - three on a rig, one off. Break
   * and think alternate across the off-stints, so an operator needs an
   * even number of them: eight stints per cycle. Eight stints must fit
   * the 480-minute shift a whole number of times, so
   *
   *     stint x 8  divides  480     <=>     stint divides 60
   *
   * That is all of it. Any time-on-rig that divides an hour works; any
   * that does not cannot, at any block size. Block length only sets the
   * resolution of the grid - 15min x 4 and 20min x 3 are the same
   * schedule, because both are 60. */
  var HOUR_DIVISORS = [5, 10, 12, 15, 20, 30, 60];

  var OFF   = -1;          // this operator is not on a rig this block
  var BREAK = "BREAK";
  var THINK = "THINK";

  // ------------------------------------------------------------- config

  function shiftById(id) {
    for (var i = 0; i < SHIFTS.length; i++) if (SHIFTS[i].id === id) return SHIFTS[i];
    return SHIFTS[0];
  }

  function stintBlocksFor(cfg) {
    return cfg.mode === "hold" ? HOLD_STINT : cfg.stintBlocks;
  }

  function stintMinutes(cfg) {
    return stintBlocksFor(cfg) * cfg.blockMin;
  }

  /* Times-on-rig that divide the hour and are a whole number of blocks. */
  function cleanStints(blockMin) {
    return HOUR_DIVISORS.filter(function (m) {
      return m % blockMin === 0 && m >= blockMin;
    });
  }

  // --------------------------------------------------------- generation

  /* "hold" - an operator keeps one rig for the whole on-run and steps
   * off for a single block. The rig changes hands every third block.
   *
   * This mode can never produce equal-length turns when the whole crew
   * starts together: at block 0 three operators take three rigs, but only
   * one operator can be off per block, so those three must step off on
   * three different blocks. Their opening turns are therefore 1, 2 and 3
   * blocks long, whatever the block size. The stubs are a property of the
   * simultaneous crew change, not of the arithmetic. */
  function planHold(nBlocks, nOps, nRigs) {
    var rows = [], held = [], i, b;
    for (i = 0; i < nOps; i++) { rows.push(new Array(nBlocks).fill(OFF)); held.push(OFF); }

    for (b = 0; b < nBlocks; b++) {
      var offOp = b % nOps;
      var free = [];

      if (held[offOp] !== OFF) { free.push(held[offOp]); held[offOp] = OFF; }

      var taken = {};
      for (i = 0; i < nOps; i++) if (held[i] !== OFF) taken[held[i]] = true;
      for (var r = 0; r < nRigs; r++) if (!taken[r] && free.indexOf(r) === -1) free.push(r);

      for (i = 0; i < nOps; i++) {
        if (i === offOp) continue;
        if (held[i] === OFF) held[i] = free.length ? free.shift() : OFF;
      }
      for (i = 0; i < nOps; i++) rows[i][b] = (i === offOp) ? OFF : held[i];
    }

    /* The reference sheet counts the other way round: at block 0 it is the
     * LAST operator who is off, and rigs fill from the far end. Mirroring
     * both indexes reproduces that sheet cell for cell instead of merely
     * isomorphically, so a generated grid can be diffed against it. */
    var mirrored = [];
    for (i = 0; i < nOps; i++) mirrored.push(new Array(nBlocks).fill(OFF));
    for (i = 0; i < nOps; i++) {
      for (b = 0; b < nBlocks; b++) {
        var v = rows[i][b];
        mirrored[nOps - 1 - i][b] = (v === OFF) ? OFF : (nRigs - 1 - v);
      }
    }
    return mirrored;
  }

  /* Turn lengths ignoring the first and last turn on each rig - the two
   * that a simultaneous crew change necessarily makes short. */
  function interiorStintLengths(plan) {
    var lens = {};
    plan.groups.forEach(function (g) {
      for (var ri = 0; ri < g.rigs.length; ri++) {
        var runs = [], prev = null, run = 0;
        for (var b = 0; b < plan.nBlocks; b++) {
          var who = holderAt(g, ri, b);
          if (who === prev) { run++; }
          else { if (prev !== null) runs.push(run); prev = who; run = 1; }
        }
        runs.push(run);
        var inner = runs.length > 2 ? runs.slice(1, -1) : runs;
        inner.forEach(function (r) { lens[r * plan.blockMin] = true; });
      }
    });
    return Object.keys(lens).map(Number).sort(function (a, b) { return a - b; });
  }

  /* Block lengths that give an operator an even number of off-turns, which
   * is what "hold" needs for break and think to come out equal. */
  function cleanBlocks(options) {
    return (options || [5, 10, 12, 15, 20, 30, 40]).filter(function (bm) {
      var n = SHIFT_MINUTES / bm;
      return n === Math.round(n) && n % 8 === 0;
    });
  }

  /* "rotate" - the shift is cut into equal stints first, and an operator
   * moves to the next rig each stint, taking a whole stint off. Every rig
   * hands over on the stint boundary, so nothing dangles at shift end
   * provided the stint divides the hour. */
  function planRotate(nBlocks, nOps, nRigs, stintBlocks) {
    var rows = [], i, b;
    for (i = 0; i < nOps; i++) rows.push(new Array(nBlocks).fill(OFF));

    for (b = 0; b < nBlocks; b++) {
      var stint = Math.floor(b / stintBlocks);
      var offOp = stint % nOps;
      for (i = 0; i < nOps; i++) {
        rows[i][b] = (i === offOp) ? OFF : ((i - offOp - 1 + nOps) % nOps) % nRigs;
      }
    }
    return rows;
  }

  /* Off-runs alternate: rest, think, rest, think. Alternating is what
   * keeps the two budgets equal without needing a second rule. */
  function labelOffRuns(row) {
    var out = row.slice(), runIndex = 0, b = 0;
    while (b < out.length) {
      if (out[b] !== OFF) { b++; continue; }
      var label = runIndex % 2 === 0 ? BREAK : THINK;
      while (b < out.length && out[b] === OFF) { out[b] = label; b++; }
      runIndex++;
    }
    return out;
  }

  /* groups: [{ key, task, rigs:[id], ops:[name] }] */
  function buildPlan(cfg, groups) {
    var blockMin    = cfg.blockMin;
    var nBlocks     = Math.round(SHIFT_MINUTES / blockMin);
    var stintBlocks = stintBlocksFor(cfg);

    var planned = groups.map(function (g) {
      var raw = cfg.mode === "rotate"
        ? planRotate(nBlocks, g.ops.length, g.rigs.length, stintBlocks)
        : planHold(nBlocks, g.ops.length, g.rigs.length);

      var rows = raw.map(labelOffRuns);

      var totals = rows.map(function (r) {
        var t = { work: 0, brk: 0, think: 0 };
        r.forEach(function (c) {
          if (c === BREAK) t.brk += blockMin;
          else if (c === THINK) t.think += blockMin;
          else t.work += blockMin;
        });
        return t;
      });

      return { key: g.key, task: g.task, rigs: g.rigs.slice(), ops: g.ops.slice(),
               rows: rows, totals: totals };
    });

    return {
      blockMin: blockMin,
      stintBlocks: stintBlocks,
      stintMin: stintBlocks * blockMin,
      mode: cfg.mode,
      shift: shiftById(cfg.shift),
      date: cfg.date,
      tz: cfg.tz || localZone(),
      nBlocks: nBlocks,
      groups: planned,
    };
  }

  // --------------------------------------------------------------- time

  /* The floor's own zone, as an IANA name.
   *
   * Every "HH:MM" in a payload is wall-clock time on the floor, and a
   * reader in another zone - a server keeping UTC, say - cannot recover
   * that from the string. So it travels with the schedule instead of
   * being assumed at both ends, which is the only way the desk, the rig
   * and the backend can agree on when a turn starts. */
  function localZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (e) {
      return "UTC";
    }
  }

  function hhmm(mins) {
    var h = Math.floor(mins / 60) % 24, m = mins % 60;
    return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
  }
  function blockStart(plan, b)  { return (plan.shift.start + b * plan.blockMin) % 1440; }
  function blockEnd(plan, b)    { return (plan.shift.start + (b + 1) * plan.blockMin) % 1440; }
  function isHourMark(plan, b)  { return blockStart(plan, b) % 60 === 0; }
  function shiftEnd(plan)       { return (plan.shift.start + SHIFT_MINUTES) % 1440; }

  // ---------------------------------------------------------- fit check

  /* Measure the turns the plan actually produces. Divisibility is not a
   * proxy for this - a plan can divide cleanly and still open and close
   * the shift on a stub. */
  function rigStintLengths(plan) {
    var lens = {};
    plan.groups.forEach(function (g) {
      for (var ri = 0; ri < g.rigs.length; ri++) {
        var prev = null, run = 0;
        for (var b = 0; b < plan.nBlocks; b++) {
          var who = holderAt(g, ri, b);
          if (who === prev) { run++; }
          else { if (prev !== null) lens[run * plan.blockMin] = true; prev = who; run = 1; }
        }
        lens[run * plan.blockMin] = true;
      }
    });
    return Object.keys(lens).map(Number).sort(function (a, b) { return a - b; });
  }

  function holderAt(group, rigIndex, block) {
    for (var i = 0; i < group.rows.length; i++) if (group.rows[i][block] === rigIndex) return i;
    return -1;
  }

  function auditPlan(plan) {
    var sMin        = plan.stintMin;
    var dividesHour = 60 % sMin === 0;
    var stintLens   = rigStintLengths(plan);
    var stintFits   = stintLens.length === 1;

    var every = [];
    plan.groups.forEach(function (g) { every = every.concat(g.totals); });
    var uniq = function (key) {
      var seen = {}, out = [];
      every.forEach(function (t) { if (!seen[t[key]]) { seen[t[key]] = true; out.push(t[key]); } });
      return out.sort(function (a, b) { return a - b; });
    };
    var work = uniq("work"), brk = uniq("brk"), think = uniq("think");

    var budgetOk = work.length === 1 && work[0] === 360
                && brk.length === 1 && brk[0] === 60
                && think.length === 1 && think[0] === 60;

    var budgetCheck = { ok: budgetOk, soft: false,
      label: "Every operator gets 6h work, 1h break, 1h think",
      note: work.join("/") + " / " + brk.join("/") + " / " + think.join("/") };

    var checks, inner = [], ends = [];

    if (plan.mode === "hold") {
      /* Holding one rig is not governed by the hour at all. Its structure
       * comes from the operator cycle: three blocks on, one off. Break and
       * think alternate across those off-blocks, so an operator needs an
       * even number of them - eight per shift - which is nBlocks % 8 === 0.
       * The short turns at each end are inherent to a simultaneous crew
       * change and are not a defect, so they are reported, not failed. */
      var cycleFits = plan.nBlocks % 8 === 0;
      inner = interiorStintLengths(plan);
      ends  = stintLens.filter(function (l) { return inner.indexOf(l) === -1; });

      checks = [
        { ok: cycleFits, soft: false,
          label: "Operator cycle fits the shift",
          note: plan.nBlocks + " blocks / 8 = "
              + (cycleFits ? (plan.nBlocks / 8) : (plan.nBlocks / 8).toFixed(2)) },
        { ok: inner.length === 1, soft: true,
          label: "Turns are equal through the shift",
          note: inner.join(" / ") + " min"
              + (ends.length ? ", " + ends.join(" and ") + " at the ends" : "") },
        budgetCheck,
      ];
    } else {
      checks = [
        { ok: dividesHour, soft: false,
          label: "Time on rig divides the hour",
          note: sMin + " min into 60 = " + (dividesHour ? (60 / sMin) : (60 / sMin).toFixed(2)) },
        { ok: stintFits, soft: true,
          label: "Every turn on a rig is the same length",
          note: stintLens.join(" / ") + " min" },
        budgetCheck,
      ];
    }

    if (plan.mode === "rotate") {
      var offCounts = plan.groups[0].rows.map(function (r) {
        var runs = 0;
        for (var b = 0; b < r.length; b++) {
          var isOff   = r[b] === BREAK || r[b] === THINK;
          var prevOff = b > 0 && (r[b - 1] === BREAK || r[b - 1] === THINK);
          if (isOff && !prevOff) runs++;
        }
        return runs;
      });
      var balanced = offCounts.every(function (c) { return c === offCounts[0]; });
      checks.push({ ok: balanced, soft: false,
        label: "Time off splits evenly between the four",
        note: offCounts.join(" / ") + " per operator" });
    }

    var failed = checks.filter(function (c) { return !c.ok; });
    var hard   = failed.filter(function (c) { return !c.soft; });

    var level, text, note;
    if (failed.length === 0) {
      level = "ok";
      text  = "Clean fit";
      note  = plan.mode === "hold"
        ? "Every operator works 6 hours, breaks for 60 minutes and thinks for 60. Rigs change hands "
          + "every " + inner[0] + " minutes"
          + (ends.length ? ", with a " + ends.join(" and ") + " minute turn at each end of the shift "
                         + "- unavoidable when the whole crew changes at once." : ".")
        : "Every operator works 6 hours, breaks for 60 minutes and thinks for 60. Every rig hands "
          + "over on a " + stintLens[0] + "-minute turn, and the last handover lands exactly on the "
          + "shift boundary.";
    } else if (hard.length === 0) {
      level = "warn";
      text  = "Fits, with uneven turns";
      note  = "The operator budget is exact, but turns come out at " + stintLens.join(", ")
            + " minutes and the middle of the shift is not uniform either. Worth a look before "
            + "you push this to the floor.";
    } else {
      level = "crit";
      text  = "Does not fit";
      note  = "This breaks the shift budget - " + hard[0].label.toLowerCase() + " fails. "
            + (plan.mode === "hold"
                ? "Holding one rig needs an even number of off-turns per operator. Pick one of the grid sizes below."
                : "A time on rig only works if it divides an hour. Pick one of the options below.");
    }

    return { level: level, text: text, note: note, checks: checks,
             work: work, brk: brk, think: think, stintLens: stintLens };
  }

  // ------------------------------------------------- what a rig receives

  /* The rig does not need the grid. It needs to know who holds it, from
   * when, who arrives next, and where the outgoing operator goes - which
   * is exactly the top rail on the rig screen, and why the login and the
   * task picker can be deleted. */
  function rigPayload(plan, rigId) {
    var gi = -1, ri = -1;
    plan.groups.forEach(function (g, i) {
      var j = g.rigs.indexOf(rigId);
      if (j !== -1) { gi = i; ri = j; }
    });
    if (gi === -1) return null;

    var g = plan.groups[gi], b, e;

    var holders = [];
    for (b = 0; b < plan.nBlocks; b++) holders.push(holderAt(g, ri, b));

    var turns = [];
    b = 0;
    while (b < plan.nBlocks) {
      var who = holders[b];
      e = b;
      while (e + 1 < plan.nBlocks && holders[e + 1] === who) e++;

      var after = e + 1 < plan.nBlocks ? g.rows[who][e + 1] : null;
      var goesTo = "End of shift";
      if (after === BREAK) goesTo = "Break";
      else if (after === THINK) goesTo = "Think";
      else if (typeof after === "number") goesTo = g.rigs[after];

      turns.push({
        from: hhmm(blockStart(plan, b)),
        to: hhmm(blockEnd(plan, e)),
        minutes: (e - b + 1) * plan.blockMin,
        operator: { id: "op-" + g.key.toLowerCase() + (who + 1), name: g.ops[who] || null },
        relievedBy: e + 1 < plan.nBlocks ? (g.ops[holders[e + 1]] || null) : null,
        theyGoTo: goesTo,
      });
      b = e + 1;
    }

    return {
      rigId: rigId,
      group: g.key,
      task: g.task,
      shift: {
        label: plan.shift.label,
        date: plan.date,
        start: hhmm(plan.shift.start),
        end: hhmm(shiftEnd(plan)),
        tz: plan.tz || localZone(),
      },
      blockMinutes: plan.blockMin,
      rotation: plan.mode,
      autoSignIn: true,
      turns: turns,
    };
  }

  /* Who is on this rig at a given wall-clock minute. This is the call the
   * rig platform makes on a timer - it is why the rig never needs a login. */
  function whoIsOn(payload, minutesSinceMidnight) {
    var t = ((minutesSinceMidnight % 1440) + 1440) % 1440;
    var toMin = function (s) { return Number(s.slice(0, 2)) * 60 + Number(s.slice(3, 5)); };
    for (var i = 0; i < payload.turns.length; i++) {
      var turn = payload.turns[i];
      var from = toMin(turn.from), to = toMin(turn.to);
      var inside = from < to ? (t >= from && t < to) : (t >= from || t < to);  // wraps midnight
      if (inside) {
        var endsIn = (to - t + 1440) % 1440;
        return { turn: turn, minutesLeft: endsIn === 0 ? payload.blockMinutes : endsIn };
      }
    }
    return null;
  }

  /* ------------------------------------------------ which shift is running

     A payload covers ONE shift. A rig that is handed a whole day, or a
     server holding three payloads per rig, has to answer "which of these
     is running right now" - and that answer must be a COMPARISON, never a
     calculation. The desk already wrote the window into the payload:
     `shift.date`, `start`, `end` and `tz`. This reads it back.

     The distinction is the founding rule of the system. Working out when
     a shift runs, rather than reading what the desk said, would be a
     second opinion about the schedule - and the first one able to
     disagree with the desk that made it. */

  /* Minutes that `tz` is ahead of UTC at a given instant. Uses the
     formatter to render that instant in the zone, then reads the wall
     clock back - the standard way to get at the tz database without
     shipping one. */
  function zoneOffset(utcMs, tz) {
    var dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hour12: false,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit"
    });
    var p = {};
    dtf.formatToParts(new Date(utcMs)).forEach(function (x) { p[x.type] = x.value; });
    var asUTC = Date.UTC(+p.year, +p.month - 1, +p.day,
                         (+p.hour) % 24, +p.minute, +p.second);
    return asUTC - utcMs;
  }

  /* A wall-clock time on the floor, as an instant. Guess, then correct -
     the second pass matters on the two days a year a shift starts inside
     a daylight-saving jump. */
  function floorInstant(y, mo, d, hh, mi, tz) {
    var guess = Date.UTC(y, mo - 1, d, hh, mi);
    var off = zoneOffset(guess, tz);
    var ms = guess - off;
    var again = zoneOffset(ms, tz);
    return again === off ? ms : guess - again;
  }

  /* The half-open window [start, end) this payload covers, in real time.
     Null if the payload does not carry one - malformed rather than
     malicious, but it must not be treated as covering everything. */
  function shiftWindow(payload) {
    var s = payload && payload.shift;
    if (!s) return null;
    var d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s.date || ""));
    var t = /^(\d{2}):(\d{2})$/.exec(String(s.start || ""));
    if (!d || !t) return null;

    var startMin = (+t[1]) * 60 + (+t[2]);
    var mins = SHIFT_MINUTES;
    var e = /^(\d{2}):(\d{2})$/.exec(String(s.end || ""));
    if (e) {
      // The desk is the authority on length, not the constant here.
      mins = ((((+e[1]) * 60 + (+e[2])) - startMin) + 1440) % 1440;
      if (mins === 0) mins = 1440;          // a full round trip, not zero
    }

    var start = floorInstant(+d[1], +d[2], +d[3], +t[1], +t[2], s.tz || "UTC");
    return { start: start, end: start + mins * 60000 };
  }

  var asMs = function (at) {
    if (at == null) return Date.now();
    if (at instanceof Date) return at.getTime();
    return typeof at === "number" ? at : Date.parse(at);
  };

  function coversAt(payload, at) {
    var w = shiftWindow(payload);
    if (!w) return false;
    var ms = asMs(at);
    return ms >= w.start && ms < w.end;
  }

  /* The one of these payloads whose own window contains `at`, or null.
     Deliberately does not fall back: what to show when no shift is
     running is the caller's decision, and hiding it here would let a
     finished schedule look like a live one. */
  function inForce(payloads, at) {
    if (!Array.isArray(payloads)) return null;
    var ms = asMs(at);
    for (var i = 0; i < payloads.length; i++) {
      if (coversAt(payloads[i], ms)) return payloads[i];
    }
    return null;
  }

  // ------------------------------------------------------------- export

  root.RotationEngine = {
    SHIFTS: SHIFTS,
    SHIFT_MINUTES: SHIFT_MINUTES,
    HOLD_STINT: HOLD_STINT,
    HOUR_DIVISORS: HOUR_DIVISORS,
    OFF: OFF, BREAK: BREAK, THINK: THINK,

    shiftById: shiftById,
    stintBlocksFor: stintBlocksFor,
    stintMinutes: stintMinutes,
    cleanStints: cleanStints,

    buildPlan: buildPlan,
    auditPlan: auditPlan,
    rigStintLengths: rigStintLengths,
    interiorStintLengths: interiorStintLengths,
    cleanBlocks: cleanBlocks,
    holderAt: holderAt,

    rigPayload: rigPayload,
    whoIsOn: whoIsOn,
    shiftWindow: shiftWindow,
    coversAt: coversAt,
    inForce: inForce,

    hhmm: hhmm,
    blockStart: blockStart,
    blockEnd: blockEnd,
    isHourMark: isHourMark,
    shiftEnd: shiftEnd,
  };

  if (typeof module === "object" && module.exports) module.exports = root.RotationEngine;

})(typeof window !== "undefined" ? window : globalThis);
