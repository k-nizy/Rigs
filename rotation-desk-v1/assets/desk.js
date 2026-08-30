/* =====================================================================
 * desk.js  -  Rotation Desk v1
 *
 * The manager's screen. Two modes, because a manager has two jobs and
 * they happen at different times of day:
 *
 *   LIVE   what the floor is doing right now - every rig, who is on it,
 *          how long they have left, who relieves them, where they go.
 *          Nothing to fill in. This is where the desk opens.
 *
 *   PLAN   who is on shift and what they are doing. Touched once at the
 *          top of a shift, then left alone.
 *
 * Live reads the schedule the floor was actually pushed, not the one
 * being typed on this screen - if a name has been edited but not pushed,
 * Live still shows what the rigs are running, and says so. With no
 * server it falls back to the plan on this screen and says that instead.
 *
 * None of the arithmetic is here. It is all in rotation-engine.js, which
 * the rig loads too, so the rig cannot compute a different answer from
 * the desk that scheduled it.
 *
 * v1 is locked to the reference sheet: 15-minute blocks, hold-rig, three
 * rigs and four operators to a group. There is deliberately no control
 * for any of that - see rotation-desk-v1/README.md.
 * ===================================================================== */

"use strict";

var RE = window.RotationEngine;

/* The locked format. Changing these three values is changing v1. */
const FORMAT = { blockMin: 15, stintBlocks: 3, mode: "hold" };

/* The roster the desk edits. It starts as the file compiled into this
   page and is replaced, once, by whatever the floor is actually running
   - see "The floor's roster" below. Not a const for that reason. */
let GROUPS = window.DEMO_ROSTER.groups;

/* Whether anybody has edited the plan on this screen yet. The floor's
   roster is only ever adopted over an untouched screen: arriving late
   and overwriting a name a manager is halfway through typing would be
   the same silent clobber this whole feature exists to stop, only
   faster. */
let planTouched = false;

const cfg = {
  shift: window.DEMO_ROSTER.defaults.shift,
  /* The floor's date, not UTC's. toISOString() is UTC, so on a floor at
     UTC+2 everything between midnight and 02:00 was dated to yesterday -
     and a schedule dated yesterday covers a window that has already
     closed, which puts every rig on Standby. */
  date: new Date().toLocaleDateString("en-CA"),
  blockMin: FORMAT.blockMin,
  stintBlocks: FORMAT.stintBlocks,
  mode: FORMAT.mode,
  view: "live",
  tab: "ops",
  pushRig: "RIG-01",
};

/* What the floor is running. Filled from the server when there is one. */
const floor = {
  payloads: [],
  pushedAt: null,
  source: "plan",     // "floor" once the server has answered
};

let ticker = null;

/* ===================================================================
 * Atoms
 * =================================================================== */

const $ = id => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const plan = () => RE.buildPlan(cfg, GROUPS);

const toMin = s => Number(s.slice(0, 2)) * 60 + Number(s.slice(3, 5));
const pad2 = n => (n < 10 ? "0" : "") + n;

/* mm:ss, which is all a turn ever needs - no turn is longer than 45. */
const mmss = secs => Math.floor(secs / 60) + ":" + pad2(secs % 60);

/* "3h 23m", for the shift and for anything measured in whole minutes. */
function hm(mins) {
  const h = Math.floor(mins / 60), m = mins % 60;
  return (h ? h + "h " : "") + m + "m";
}

/* theyGoTo is either a period or a rig id. Periods read as words in a
 * sentence; a rig id is a name and keeps its capitals. */
function goesTo(where) {
  return (where === "Break" || where === "Think" || where === "End of shift")
    ? where.toLowerCase()
    : where;
}

function shortName(full) {
  if (!full) return "-";
  const parts = full.trim().split(/\s+/);
  if (parts.length === 1) return parts[0];
  return parts[0][0] + ". " + parts[parts.length - 1];
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("up");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("up"), 2400);
}

/* ===================================================================
 * LIVE
 * =================================================================== */

/* Every payload the desk would push right now, for when there is no
 * server to ask. Same function the push itself uses. */
/* Every shift of the day, which is what a push sends.
 *
 * A payload covers one shift. Pushing only the one on screen is why a rig
 * ran until 16:00 and then went quiet: it was still holding a Morning
 * schedule with no turns left in it, and nothing newer existed to pick
 * up. Three shifts of twelve rigs is thirty-six payloads, well inside the
 * sixty-four a push already accepts.
 *
 * The sheet is untouched. This is the same rotation drawn three times,
 * once per shift, not a different format. */
function dayPayloads() {
  const out = [];
  RE.SHIFTS.forEach(shift => {
    const p = RE.buildPlan(Object.assign({}, cfg, { shift: shift.id }), GROUPS);
    GROUPS.forEach(g => g.rigs.forEach(r => {
      const one = RE.rigPayload(p, r);
      if (one) out.push(one);
    }));
  });
  return out;
}

function localPayloads() {
  const p = plan();
  const out = [];
  GROUPS.forEach(g => g.rigs.forEach(r => {
    const one = RE.rigPayload(p, r);
    if (one) out.push(one);
  }));
  return out;
}

/* ===================================================================
 * The floor's roster
 *
 * The roster lives in a file compiled into this page, and an edit to it
 * never leaves the browser tab it was made in. So the desk and the floor
 * drift apart: the floor has the night manager's correction, every other
 * desk still has the file, and the next person to push sends the file's
 * version back over it. The correction disappears and nothing anywhere
 * records that it did.
 *
 * The fix is to stop treating the file as the truth. What the floor is
 * running is already on the floor, and it comes back whole: each payload
 * carries its group, its task and every operator in it, and under hold
 * rig the first block of a group names three of its four operators in
 * rig order, the fourth being the one who is off. That is the entire
 * recovery.
 *
 * It is not believed on the strength of that argument. Whatever comes
 * back is rebuilt into a schedule and compared, rig by rig and turn by
 * turn, against the schedule the floor is actually running. Agreement
 * makes the recovery correct by demonstration rather than by reasoning.
 * Disagreement keeps the file and says so out loud - a roster the desk
 * cannot verify is worse than a stale one, because it would go to twelve
 * rigs under a manager's name.
 *
 * What this does not fix: two managers editing at the same moment still
 * ends with the later push winning. The window shrinks from all day to
 * the minutes between opening the desk and pressing the button, and the
 * staleness check on the push closes most of what is left of it.
 * =================================================================== */

/* The roster that produced these payloads, or null if they do not look
   like a floor this desk could have drawn. */
function rosterFromFloor(payloads) {
  const byGroup = new Map();
  payloads.forEach(p => {
    if (!p || !p.group || !Array.isArray(p.turns) || !p.turns.length) return;
    if (!byGroup.has(p.group)) byGroup.set(p.group, []);
    byGroup.get(p.group).push(p);
  });
  if (!byGroup.size) return null;

  const out = [];
  for (const [key, rigs] of byGroup) {
    /* Sorted, and the operators read out in that same order, so the two
       arrays stay consistent with each other. A group whose rigs were
       listed in some other order rebuilds to the same schedule with both
       rotated together - which is the thing the check below proves. */
    rigs.sort((a, b) => String(a.rigId).localeCompare(String(b.rigId)));

    const everyone = new Set();
    rigs.forEach(p => p.turns.forEach(t => {
      if (t.operator && t.operator.name) everyone.add(t.operator.name);
    }));

    // The first block of the shift, read across the rigs in order.
    const opensAt = rigs.reduce(
      (a, p) => (a === null || toMin(p.turns[0].from) < a ? toMin(p.turns[0].from) : a), null);
    const opening = rigs.map(p => p.turns.find(t => toMin(t.from) === opensAt));
    if (opening.some(t => !t || !t.operator || !t.operator.name)) return null;

    const onRig = opening.map(t => t.operator.name);
    if (new Set(onRig).size !== onRig.length) return null;   // one name, two rigs

    const off = [...everyone].filter(n => !onRig.includes(n));
    out.push({
      key: key,
      task: rigs[0].task,
      rigs: rigs.map(p => p.rigId),
      ops: onRig.concat(off),
    });
  }

  out.sort((a, b) => String(a.key).localeCompare(String(b.key)));
  return out;
}

/* Does this roster, run through the engine, produce the schedule the
   floor is actually running? The whole of the trust in a recovered
   roster is this function. */
function rosterAgrees(groups, payloads) {
  const shift = RE.SHIFTS.find(s => s.label === payloads[0].shift.label);
  if (!shift) return false;
  let built;
  try {
    built = RE.buildPlan(
      Object.assign({}, cfg, { shift: shift.id, date: payloads[0].shift.date }), groups);
  } catch (e) { return false; }

  return payloads.every(want => {
    const got = RE.rigPayload(built, want.rigId);
    if (!got) return false;
    if (got.group !== want.group || got.task !== want.task) return false;
    if (got.turns.length !== want.turns.length) return false;
    return got.turns.every((t, i) => {
      const w = want.turns[i];
      return t.from === w.from && t.to === w.to && t.minutes === w.minutes
          && t.operator.name === (w.operator && w.operator.name)
          && t.relievedBy === w.relievedBy && t.theyGoTo === w.theyGoTo;
    });
  });
}

const sameRoster = (a, b) =>
  a.length === b.length && a.every((g, i) =>
    g.key === b[i].key && g.task === b[i].task &&
    g.rigs.join() === b[i].rigs.join() && g.ops.join() === b[i].ops.join());

/* Take the floor's roster onto this screen, if it can be verified. */
function adoptFloorRoster() {
  if (planTouched) return;

  const found = rosterFromFloor(floor.payloads);
  if (!found) return;
  if (sameRoster(found, GROUPS)) return;      // they already agree

  if (!rosterAgrees(found, floor.payloads)) {
    /* Loud, because it means the floor is running something this desk
       cannot reproduce - so the plan on screen is not what the floor
       has, and pushing it would replace a schedule nobody here can
       account for. */
    toast("The floor is running a roster this desk cannot rebuild - showing the file");
    return;
  }

  GROUPS = found;
  remountRosters();
  renderPlan();
  toast("Roster read back from the floor");
}

/* Ask the server what the floor is actually running. Falls back to the
 * plan on this screen, which is what makes the desk work as a plain
 * static page with no server behind it at all. */
async function loadFloor(announce) {
  /* Whatever this call finds, a refusal to push over a floor this screen
     had not read is about to stop being true - either because it now has
     read it, or because there is no floor to read. */
  disarmPush();
  try {
    const state = await api("/api/state").then(r => r.ok ? r.json() : Promise.reject());
    if (!state.rigs || !state.rigs.length) throw new Error("nothing pushed");

    floor.payloads = await Promise.all(state.rigs.map(id =>
      api("/api/rigs/" + encodeURIComponent(id) + "/schedule.json")
        .then(r => r.ok ? r.json() : Promise.reject())));
    floor.pushedAt = state.pushedAt;
    floor.source = "floor";
    adoptFloorRoster();
    if (announce) toast("Read " + floor.payloads.length + " rigs from the floor");
  } catch (e) {
    floor.payloads = localPayloads();
    floor.pushedAt = null;
    floor.source = "plan";
    if (announce) toast("No pushed schedule - showing the plan on this screen");
  }
  renderLive();
}

/* The twelve payloads, gathered back into the four groups they came
 * from. The payload carries its own group and task, so this needs
 * nothing from the roster on screen. */
function boardGroups() {
  const map = new Map();
  floor.payloads.forEach(p => {
    if (!map.has(p.group)) map.set(p.group, { key: p.group, task: p.task, rigs: [] });
    map.get(p.group).rigs.push(p);
  });
  const out = [...map.values()];
  out.forEach(g => g.rigs.sort((a, b) => a.rigId.localeCompare(b.rigId)));
  out.sort((a, b) => String(a.key).localeCompare(String(b.key)));
  return out;
}

/* Who in this group is off right now, what they are off for, and - the
 * part the board never used to say - which rig they walk to when they
 * come back.
 *
 * The payloads describe rigs, not people, so the operator who is off is
 * the one who appears in the group's turns but is not on a rig at this
 * minute. Their destination is their own next turn: whichever of the
 * group's three rigs claims them soonest. */
function offNow(group, atRel, relOf) {
  const all = new Set();
  group.rigs.forEach(p => p.turns.forEach(t => {
    if (t.operator.name) all.add(t.operator.name);
  }));

  const on = new Set();
  group.rigs.forEach(p => {
    const w = RE.whoIsOn(p, relOf.absolute(atRel));
    if (w && w.turn.operator.name) on.add(w.turn.operator.name);
  });

  const names = [...all].filter(n => !on.has(n));
  if (!names.length) return null;
  const name = names[0];

  // what kind of off: their own last turn said where they were going
  let kind = null, endedRel = -1;
  // where they go next: their own next turn says which rig
  let backTo = null, backAt = null, backRel = Infinity;

  group.rigs.forEach(p => p.turns.forEach(t => {
    if (t.operator.name !== name) return;
    const startRel = relOf.of(toMin(t.from));
    const endRel   = relOf.of(toMin(t.to));

    if ((t.theyGoTo === "Break" || t.theyGoTo === "Think") && endRel <= atRel && endRel > endedRel) {
      endedRel = endRel;
      kind = t.theyGoTo;
    }
    if (startRel > atRel && startRel < backRel) {
      backRel = startRel;
      backTo = p.rigId;
      backAt = t.from;
    }
  }));

  return {
    name: name,
    kind: kind,                                   // null only at the top of a shift
    backTo: backTo,                               // null in the last block
    backAt: backAt,
    backRel: backRel === Infinity ? null : backRel,
  };
}

/* What Live is showing, worked out once a tick. Nothing in here touches
 * the DOM, which is what lets the paint step below decide whether it has
 * to rebuild anything at all. */
function liveState() {
  if (!floor.payloads.length) return null;

  const first    = floor.payloads[0];
  const startMin = toMin(first.shift.start);

  /* The floor's wall clock, not this browser's.
   *
   * Every "HH:MM" on this screen is time where the rigs are, and the
   * payload carries the zone the desk wrote when it pushed. Reading them
   * against whatever machine happens to be viewing meant a desk opened
   * from another zone said "the shift has not started" while the floor
   * was three hours into it. On a desk sitting in the same building the
   * two are identical and nothing changes. */
  const nowFloat = RE.minutesOnFloor(first, Date.now());
  const nowMin   = Math.floor(nowFloat);
  const secInMin = Math.floor((nowFloat - nowMin) * 60);

  /* Everything is measured from the top of the shift, so the night
   * shift's midnight crossing needs no special case anywhere below. */
  const relOf = {
    of: m => (m - startMin + 1440) % 1440,
    absolute: r => (startMin + r) % 1440,
  };
  const nowRel  = relOf.of(nowMin);

  /* Whether this sheet is running is a COMPARISON against the window the
     desk wrote, not a calculation from the clock - the same rule the rig
     and the server follow, and the same call the Live badge already
     makes.

     `nowRel` is minutes since the top of the shift *modulo a day*, so it
     carries no date. Asked whether a shift is running it answers about a
     time of day: a Morning sheet from yesterday, read at 10:37, came out
     RUNNING with live countdowns beside a badge saying nothing was
     scheduled. Asked how long until it starts, it counted forward to a
     16:00 belonging to a different day, so a Day sheet that ended at
     midnight was announced at 02:19 as starting in 13h 44m.

     A payload with no window - an older push, before the date travelled
     - falls back to the old arithmetic, which is all there is to go on. */
  const win     = RE.shiftWindow(first);
  const nowMs   = Date.now();
  const running = win ? (nowMs >= win.start && nowMs < win.end)
                      : nowRel < RE.SHIFT_MINUTES;
  const over    = win ? nowMs >= win.end : false;
  const atRel   = running ? nowRel : 0;   // not running: the opening line-up

  /* One clock for the whole page: whole minutes to the moment, less the
   * seconds already spent in this minute. Every countdown on screen is
   * this same subtraction, so no two of them can disagree. */
  const secsTo = endRel => Math.max(0, (endRel - nowRel) * 60 - secInMin);

  const groups = boardGroups().map(g => {
    const rigs = g.rigs.map(p => {
      const w = RE.whoIsOn(p, relOf.absolute(atRel));
      if (!w) return { rigId: p.rigId, turn: null, leftSec: 0, gone: 0 };

      const endRel  = relOf.of(toMin(w.turn.to));
      const spanSec = w.turn.minutes * 60;
      const leftSec = running ? secsTo(endRel) : spanSec;

      return {
        rigId: p.rigId,
        turn: w.turn,
        leftSec: leftSec,
        gone: Math.min(1, Math.max(0, 1 - leftSec / spanSec)),
      };
    });

    const off = offNow(g, atRel, relOf);
    if (off && off.backRel != null) off.leftSec = running ? secsTo(off.backRel) : null;

    return { key: g.key, task: g.task, rigs: rigs, off: off };
  });

  let soonest = null;
  const moves = [];
  if (running) {
    groups.forEach(g => g.rigs.forEach(r => {
      if (!r.turn) return;
      moves.push({ rigId: r.rigId, turn: r.turn, leftSec: r.leftSec });
      if (soonest === null || r.leftSec < soonest.leftSec) soonest = r;
    }));
    moves.sort((a, b) => a.leftSec - b.leftSec);
  }

  return {
    clock: pad2(Math.floor(nowMin / 60) % 24) + ":" + pad2(nowMin % 60),
    shift: first.shift,
    running: running,
    over: over,
    leftOfShift: RE.SHIFT_MINUTES - nowRel,
    /* Only meaningful before the shift. Once it is over there is no
       "starts in" to state, and stating one anyway is the bug. */
    startsIn: (1440 - nowRel) % 1440,
    groups: groups,
    soonest: soonest,
    moves: moves,
  };
}

/* Who is where - the one thing that decides whether the board has to be
 * rebuilt or merely re-timed. */
function boardSignature(s) {
  return s.running + "|" + s.over + "|" + s.groups.map(g =>
    g.key + ":" + g.task + ":" +
    g.rigs.map(r => r.rigId + "/" + (r.turn ? r.turn.operator.name + ">" + r.turn.theyGoTo : "-")).join(",") +
    "/" + (g.off ? g.off.name + ":" + g.off.kind + ">" + g.off.backTo : "-")
  ).join(";");
}

/* Running, this column counts down. Before the shift there is nothing to
 * count, so it says how long the opening turn is. */
function leftText(s, rig) {
  return s.running ? mmss(rig.leftSec) : rig.turn.minutes + " min";
}
function leftClass(s, rig) {
  if (!s.running) return "left mono";
  if (rig.leftSec <= 60)  return "left mono crit";
  if (rig.leftSec <= 300) return "left mono warn";
  return "left mono";
}

/* Break and Think are periods, anything else is a rig id. The chip is
 * painted the way the sheet paints them - grey for break, the amber
 * hatch for think, the group's own colour for a rig - so the board and
 * the sheet say the same thing in the same language. */
function destOf(where) {
  if (where === "Break") return { cls: "brk", label: "BREAK" };
  if (where === "Think") return { cls: "thk", label: "THINK" };
  if (where === "End of shift") return { cls: "end", label: "OFF SHIFT" };
  return { cls: "to-rig", label: where };
}

function destChip(where, at) {
  const d = destOf(where);
  const n = el("span", "dest " + d.cls);
  n.appendChild(el("i", null, "→"));
  n.appendChild(el("b", null, d.label));
  if (at) n.appendChild(el("small", null, at));
  return n;
}

/* Build the board once. The nodes that change every second are kept on
 * `refs` so the next 59 ticks only write text, not DOM - which is what
 * stops the board flickering and lets the progress bars glide. */
function buildBoard(s) {
  const board = $("board");
  board.textContent = "";
  const refs = [];

  s.groups.forEach((g, gi) => {
    const card = el("section", "gcard");
    card.style.setProperty("--gc", "var(--g" + (gi % 4) + ")");
    card.style.setProperty("--gc-soft", "var(--g" + (gi % 4) + "-soft)");

    const head = el("header", "gcard-head");
    head.appendChild(el("span", "grp-key", "GROUP " + g.key));
    head.appendChild(el("span", "gtask", g.task));
    card.appendChild(head);

    const rigRefs = [];
    g.rigs.forEach(rig => {
      const row = el("div", "rig");
      if (!rig.turn) {
        row.appendChild(el("span", "op", "-"));
        card.appendChild(row);
        rigRefs.push(null);
        return;
      }

      const top = el("div", "rig-top");
      top.appendChild(el("b", "rig-id", rig.rigId));
      top.appendChild(el("span", "op", rig.turn.operator.name || "-"));
      const leftEl = el("span", leftClass(s, rig), leftText(s, rig));
      top.appendChild(leftEl);
      row.appendChild(top);

      const bar = el("div", "rig-bar");
      const fill = el("i");
      fill.style.width = (rig.gone * 100).toFixed(1) + "%";
      bar.appendChild(fill);
      row.appendChild(bar);

      const foot = el("div", "rig-foot");
      foot.appendChild(destChip(rig.turn.theyGoTo, rig.turn.to));
      if (rig.turn.relievedBy) {
        foot.appendChild(el("span", "relief", shortName(rig.turn.relievedBy) + " takes over"));
      }
      row.appendChild(foot);

      card.appendChild(row);
      rigRefs.push({ leftEl: leftEl, fill: fill });
    });

    /* The fourth operator, now given the same three facts as the other
     * three: what they are doing, where they go, and how long. */
    const offRow = el("div", "offrow");
    let offLeftEl = null;
    if (g.off) {
      offRow.appendChild(el("span", "off-tag " + (g.off.kind === "Think" ? "thk" : "brk"),
        g.off.kind || "OFF"));
      offRow.appendChild(el("span", "off-name", g.off.name));
      if (g.off.backTo) {
        offRow.appendChild(destChip(g.off.backTo, g.off.backAt));
        if (s.running) {
          offLeftEl = el("span", "left mono", mmss(g.off.leftSec));
          offRow.appendChild(offLeftEl);
        }
      } else {
        offRow.appendChild(destChip("End of shift", null));
      }
    } else {
      offRow.appendChild(el("span", "off-tag", "-"));
      offRow.appendChild(el("span", "off-name", "everyone is on a rig"));
    }
    card.appendChild(offRow);

    board.appendChild(card);
    refs.push({ rigs: rigRefs, offLeftEl: offLeftEl });
  });

  return refs;
}

let boardRefs = null;
let boardSig  = null;

function renderLive() {
  const s = liveState();
  const board = $("board");

  if (!s) {
    board.textContent = "";
    board.appendChild(el("p", "empty", "Nothing to show yet."));
    $("now-src").textContent = "";
    boardSig = null;
    return;
  }

  // ---- the strip along the top
  $("now-time").textContent = s.clock;
  $("now-shift").textContent = s.shift.label + " · " + s.shift.start + "-" + s.shift.end
    + (s.running ? " · " + hm(s.leftOfShift) + " left" : "");

  /* Whether anything the floor is holding actually covers this minute.
     The badge used to read "On the floor - pushed 4:12 PM" all night,
     which is true and useless: at 00:05 the sheet it names has expired,
     every rig is in Standby, and the one screen a manager would check to
     find that out was quietly reassuring them instead. A rota that has
     run out has to look different from one that is running. */
  const covering = floor.payloads.some(p => RE.coversAt(p, Date.now()));
  const pushedAtText = floor.pushedAt
    ? new Date(floor.pushedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : "";
  const state = floor.source !== "floor" ? "plan" : (covering ? "floor" : "dry");

  $("now-src").textContent =
    state === "plan" ? "Not pushed · showing this screen's plan"
    : state === "floor" ? "On the floor · pushed " + pushedAtText
    : "Nothing scheduled for now · last push " + pushedAtText;
  $("now-src").className = "src " + state;

  const banner = $("banner");
  banner.hidden = s.running;
  if (s.over) {
    /* Not a countdown. What is on the board is the line-up of a shift
       that has finished, and saying which one it was is the only thing
       here that helps somebody work out what to do about it. */
    banner.textContent = s.shift.label + " shift ended at " + s.shift.end
      + " on " + s.shift.date + ". This is the last sheet the floor ran.";
  } else if (!s.running) {
    banner.textContent = s.shift.label + " shift starts at " + s.shift.start
      + " - in " + hm(s.startsIn) + ". Showing the opening line-up.";
  }

  // ---- the board itself, rebuilt only when somebody actually moves
  const sig = boardSignature(s);
  if (sig !== boardSig || !boardRefs) {
    boardRefs = buildBoard(s);
    boardSig = sig;
  } else {
    s.groups.forEach((g, gi) => {
      const ref = boardRefs[gi];
      if (!ref) return;
      g.rigs.forEach((rig, ri) => {
        const r = ref.rigs[ri];
        if (!r || !rig.turn) return;
        r.leftEl.textContent = leftText(s, rig);
        r.leftEl.className = leftClass(s, rig);
        r.fill.style.width = (rig.gone * 100).toFixed(1) + "%";
      });
      if (ref.offLeftEl && g.off && g.off.leftSec != null) {
        ref.offLeftEl.textContent = s.running ? mmss(g.off.leftSec) : "at " + g.off.backAt;
      }
    });
  }

  renderNext(s);
}

/* One line: the very next thing that happens anywhere on the floor. */
function renderNext(s) {
  const up = $("upnext");
  if (!s.running || !s.soonest) {
    up.textContent = s.running ? ""
      : s.over ? "The shift has ended. Nothing on the floor covers now."
      : "The shift has not started.";
    return;
  }
  const t = s.soonest.turn;
  up.textContent = t.relievedBy
    ? "Next handover in " + mmss(s.soonest.leftSec) + " · " + s.soonest.rigId + " · "
      + (t.operator.name || "-") + " goes to " + goesTo(t.theyGoTo)
      + ", " + t.relievedBy + " takes over"
    : "Shift ends in " + mmss(s.soonest.leftSec) + " · everyone comes off together.";
}

/* ===================================================================
 * PLAN
 * =================================================================== */

function renderSetup() {
  const seg = $("seg-shift");
  if (!seg.children.length) {
    RE.SHIFTS.forEach(s => {
      const b = el("button", null, s.label);
      b.type = "button";
      b.dataset.shift = s.id;
      b.addEventListener("click", () => { cfg.shift = s.id; onPlanChanged(); });
      seg.appendChild(b);
    });
  }
  [...seg.children].forEach(b => b.setAttribute("aria-pressed", String(b.dataset.shift === cfg.shift)));

  /* One shift is chosen and everything below is that shift. Say so in
   * words, in one place, so there is never a doubt about what is on
   * screen. */
  const s = RE.shiftById(cfg.shift);
  const span = RE.hhmm(s.start) + " \u2013 " + RE.hhmm((s.start + RE.SHIFT_MINUTES) % 1440);

  $("chosen").textContent = s.label + " shift \u00b7 " + span + " \u00b7 " + longDate(cfg.date);
  $("fmt-line").textContent = cfg.blockMin + " min blocks  \u00b7  hold rig  \u00b7  3 rigs / 4 operators";

  $("sheet-title").textContent = s.label + " shift \u00b7 " + span;
  $("sheet-sub").textContent = "Names down the side, time across the top. "
    + s.label + " only \u2013 pick another shift above to see that one.";
}

/* "Sun 23 Aug 2026", from the yyyy-mm-dd the date input gives back. */
function longDate(iso) {
  const parts = String(iso).split("-");
  if (parts.length !== 3) return iso;
  const d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  if (isNaN(d.getTime())) return iso;
  return d.toDateString().replace(/^(\w{3}) (\w{3}) (\d+) /, "$1 $3 $2 ");
}

/* One line when it is fine, the detail underneath when it is not. */
function renderFit(a) {
  $("v-dot").className = "dot " + a.level;
  $("v-text").textContent = a.level === "ok" ? "Everything checks out" : a.text;
  $("v-sub").textContent = a.work.join("/") + " min work \u00b7 " + a.brk.join("/") + " break \u00b7 "
    + a.think.join("/") + " think, each";
  $("v-note").textContent = a.note;

  const checks = $("checks");
  checks.textContent = "";
  a.checks.forEach(c => {
    const row = el("div", "check " + (c.ok ? "pass" : (c.soft ? "soft" : "fail")));
    row.appendChild(el("i", null, c.ok ? "OK" : (c.soft ? "!" : "X")));
    row.appendChild(el("b", null, c.label));
    row.appendChild(el("span", null, c.note));
    checks.appendChild(row);
  });

  // A problem should not need opening to be noticed.
  if (a.level !== "ok") $("fit").open = true;
  $("fit").className = "fit " + a.level;
}

/* The roster cards are mounted once and then left alone, because the
   inputs in them are live. Reading the floor's roster is the one thing
   that has to redraw them, and that only ever happens over an untouched
   screen - so it clears them and lets renderRosters build them again
   against the roster that replaced the file. */
function remountRosters() {
  $("rosters").textContent = "";
  renderRosters();
}

function renderRosters() {
  const host = $("rosters");
  if (host.children.length) return;   // inputs are live; never re-mount them

  GROUPS.forEach((g, gi) => {
    const card = el("section", "grp");
    card.style.setProperty("--gc", "var(--g" + gi + ")");
    card.style.setProperty("--gc-soft", "var(--g" + gi + "-soft)");

    const head = el("div", "grp-head");
    head.appendChild(el("span", "grp-key", "GROUP " + g.key));
    head.appendChild(el("span", "eyebrow", g.rigs.join(" \u00b7 ")));
    card.appendChild(head);

    const taskWrap = el("div");
    taskWrap.appendChild(el("label", null, "Task for the shift"));
    const task = el("input");
    task.type = "text"; task.value = g.task;
    task.addEventListener("input", () => { g.task = task.value; onPlanChanged(); });
    taskWrap.appendChild(task);
    card.appendChild(taskWrap);

    const opWrap = el("div");
    opWrap.appendChild(el("label", null, "Operators"));
    const list = el("div", "op-list");
    g.ops.forEach((o, oi) => {
      const line = el("div", "op-line");
      line.appendChild(el("span", "idx", String(oi + 1)));
      const inp = el("input");
      inp.type = "text"; inp.value = o;
      inp.setAttribute("aria-label", "Group " + g.key + " operator " + (oi + 1));
      inp.addEventListener("input", () => { g.ops[oi] = inp.value; onPlanChanged(); });
      line.appendChild(inp);
      list.appendChild(line);
    });
    opWrap.appendChild(list);
    card.appendChild(opWrap);

    const rigWrap = el("details", "rig-edit");
    rigWrap.appendChild(el("summary", null, "Rig ids"));
    const rigRow = el("div", "rig-row");
    g.rigs.forEach((r, ri) => {
      const inp = el("input");
      inp.type = "text"; inp.value = r;
      inp.setAttribute("aria-label", "Group " + g.key + " rig " + (ri + 1));
      inp.addEventListener("input", () => { g.rigs[ri] = inp.value; onPlanChanged(); });
      rigRow.appendChild(inp);
    });
    rigWrap.appendChild(rigRow);
    card.appendChild(rigWrap);

    host.appendChild(card);
  });
}

/* ------------------------------------------------------- the sheet
 *
 * Names down the side, time across the top. One table for the whole
 * floor, banded by group, so the four groups are read down a single
 * column of time rather than four tables that have to be lined up by
 * eye. The rig sheet is the same table with rigs on the side.
 * ------------------------------------------------------------------ */

/* The row of times along the top. Every hour gets a heavier rule and a
 * darker label, which is all the structure 32 columns need. */
function timeHead(p, corner) {
  const thead = el("thead");
  const tr = el("tr");

  const stub = el("th", "stub");
  stub.scope = "col";
  stub.textContent = corner;
  tr.appendChild(stub);

  for (let b = 0; b < p.nBlocks; b++) {
    const onHour = RE.blockStart(p, b) % 60 === 0;
    const th = el("th", "tick" + (onHour ? " hour" : ""), RE.hhmm(RE.blockStart(p, b)));
    th.scope = "col";
    tr.appendChild(th);
  }

  thead.appendChild(tr);
  return thead;
}

/* A group's title row, spanning the whole width. */
function bandRow(p, g, gi) {
  const tr = el("tr", "band");
  tr.style.setProperty("--gc", "var(--g" + gi + ")");

  const th = el("th", "stub");
  th.scope = "row";
  th.appendChild(el("b", null, "GROUP " + g.key));
  tr.appendChild(th);

  const td = el("td");
  td.colSpan = p.nBlocks;
  td.appendChild(el("b", null, g.task || "-"));
  td.appendChild(el("span", null, g.rigs.join("  ")));
  tr.appendChild(td);

  return tr;
}

function cellNode(cls, text) {
  return el("td", "cell " + cls, text);
}

function renderGrid(p, kind) {
  const tbl = $(kind === "ops" ? "tbl-ops" : "tbl-rigs");
  tbl.textContent = "";
  tbl.appendChild(timeHead(p, kind === "ops" ? "Operator" : "Rig"));

  const tbody = el("tbody");

  p.groups.forEach((g, gi) => {
    tbody.appendChild(bandRow(p, g, gi));

    const rows = kind === "ops" ? g.ops : g.rigs;
    rows.forEach((name, i) => {
      const tr = el("tr");
      tr.style.setProperty("--gc", "var(--g" + gi + ")");
      tr.style.setProperty("--gc-soft", "var(--g" + gi + "-soft)");

      const th = el("th", "stub");
      th.scope = "row";
      th.appendChild(el("i", null, (kind === "ops" ? "Op " : "Rig ") + (i + 1)));
      th.appendChild(el("b", null, name || "-"));
      if (kind === "ops") {
        const t = g.totals[i];
        th.appendChild(el("small", null, (t.work / 60).toFixed(t.work % 60 ? 1 : 0) + "h"));
      }
      tr.appendChild(th);

      let prev = null;
      for (let b = 0; b < p.nBlocks; b++) {
        const onHour = RE.blockStart(p, b) % 60 === 0 ? " hour" : "";
        let td, key;

        if (kind === "ops") {
          const cell = g.rows[i][b];
          key = String(cell);
          if (cell === RE.BREAK)      td = cellNode("brk" + onHour, "Break");
          else if (cell === RE.THINK) td = cellNode("thk" + onHour, "Think");
          else                        td = cellNode("work" + onHour, g.rigs[cell] || "-");
        } else {
          const oi = RE.holderAt(g, i, b);
          key = String(oi);
          td = oi === -1 ? cellNode("idle" + onHour, "-")
                         : cellNode("work name" + onHour, shortName(g.ops[oi]));
        }

        // A change of value is a handover, and gets the bar down its left.
        if (key !== prev) td.classList.add("start");
        prev = key;
        tr.appendChild(td);
      }

      tbody.appendChild(tr);
    });
  });

  tbl.appendChild(tbody);
}

/* ------------------------------------------------- what a rig gets */

function highlightJSON(text) {
  return text
    .replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))
    .replace(/"([^"\\]*)":/g, '<span class="k">"$1"</span>:')
    .replace(/: "([^"\\]*)"/g, ': <span class="s">"$1"</span>')
    .replace(/: (-?\d+(?:\.\d+)?|true|false|null)/g, ': <span class="n">$1</span>');
}

function renderTools(p) {
  const tools = $("tab-tools");
  tools.textContent = "";
  if (cfg.tab !== "push") return;

  const sel = el("select");
  sel.setAttribute("aria-label", "Rig to inspect");
  GROUPS.forEach(g => g.rigs.forEach(r => {
    const o = el("option", null, r);
    o.value = r;
    if (r === cfg.pushRig) o.selected = true;
    sel.appendChild(o);
  }));
  sel.addEventListener("change", () => { cfg.pushRig = sel.value; renderPlan(); });
  tools.appendChild(sel);

  const copy = el("button", "btn ghost", "Copy this rig");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(RE.rigPayload(p, cfg.pushRig), null, 2));
      toast("Copied " + cfg.pushRig);
    } catch (e) { toast("Clipboard blocked - select the text instead"); }
  });
  tools.appendChild(copy);
}

function renderPushPanel(p) {
  const one = RE.rigPayload(p, cfg.pushRig);
  const head = $("push-head");
  head.textContent = "";
  if (!one) { $("push-body").textContent = "No such rig."; return; }
  head.appendChild(el("b", null, one.rigId));
  head.appendChild(el("span", null, one.task));
  head.appendChild(el("span", null, one.turns.length + " turns / " + one.shift.start + "-" + one.shift.end));
  $("push-body").innerHTML = highlightJSON(JSON.stringify(one, null, 2));
}

/* ------------------------------------------------- pushing over a newer floor
 *
 * A push is not a merge. It replaces the whole day on all twelve rigs,
 * so a manager who opened the desk at 08:58 and pushes at 09:01 wipes
 * out the push somebody else made at 09:00 - and neither of them is told.
 *
 * The desk already knows when the floor was last pushed: `floor.pushedAt`
 * is what the Live badge reads. So it can ask again on the way out and
 * compare. If the floor has moved since this screen last read it, the
 * first press refuses and says what happened; a second press goes
 * through, because the manager may well have meant it and only they can
 * know. Refresh is the better answer and is named first.
 *
 * Deliberately not a lock. Locking twelve rigs behind whoever opened a
 * tab first is a much bigger promise, and this closes almost all of the
 * window for a few lines. */
let pushArmed = false;   // a refusal is standing; the next press goes through

function disarmPush() {
  pushArmed = false;
  $("btn-push").textContent = "Push to floor";
}

/* When the floor was last pushed, if that is newer than what this screen
   is holding. Null when it is not, and null when the question cannot be
   answered - no server, or one too old for /api/state - because a push
   that would have failed anyway must not be blocked by a warning about
   a floor nobody can read. */
async function floorMovedOn() {
  if (!floor.pushedAt) return null;         // this screen never read a floor
  try {
    const state = await api("/api/state").then(r => r.ok ? r.json() : Promise.reject());
    if (!state || !state.pushedAt) return null;
    const theirs = Date.parse(state.pushedAt);
    const ours = Date.parse(floor.pushedAt);
    if (!(theirs > ours)) return null;
    return state.pushedAt;
  } catch (e) {
    return null;
  }
}

/* Push every rig in one shot. All-or-nothing: the server rejects a mix
 * of valid and invalid payloads, so the floor never runs half-updated. */
async function pushFloor() {
  const btn = $("btn-push");
  btn.disabled = true;
  try {
    const moved = await floorMovedOn();
    if (moved && !pushArmed) {
      const at = new Date(moved).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      pushArmed = true;
      btn.textContent = "Push anyway";
      $("push-note").textContent =
        "The floor was pushed at " + at + ", after you opened this screen. "
        + "Read it back before pushing, or press again to replace it.";
      toast("The floor changed since you opened this");
      return;
    }
  } finally {
    btn.disabled = false;
  }

  const payloads = dayPayloads();
  btn.disabled = true;
  $("push-note").textContent = "Pushing " + RE.SHIFTS.length + " shifts...";
  try {
    const res = await api("/api/push", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ payloads }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok) {
      const at = new Date(body.pushedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const rigs = Math.round(body.count / RE.SHIFTS.length);
      $("push-note").textContent = "Pushed " + rigs + " rigs, " +
        RE.SHIFTS.length + " shifts, at " + at;
      toast("Pushed " + rigs + " rigs for the day");
      /* This screen is now the floor, so the refusal has nothing left to
         protect. Cleared here rather than in `finally` - a push that was
         rejected leaves the older floor in place and the warning still
         true. */
      disarmPush();
      await loadFloor(false);          // Live should now show the floor, not the plan
    } else if (res.status === 401 || res.status === 403) {
      /* Not a bad payload. Either the session went while the tab sat
         open, or this account may not push - and "Rejected" would send
         a manager hunting through a schedule that is perfectly fine. */
      $("push-note").textContent = body.detail || "Not allowed to push.";
      toast("Not allowed to push");
      await probeSession();
      applyGate();
    } else {
      $("push-note").textContent = "Rejected: " + (body.error || res.status);
      toast("Push rejected");
    }
  } catch (e) {
    $("push-note").textContent = "Could not reach the server.";
    toast("Push failed - is the server running?");
  } finally {
    btn.disabled = false;
  }
}

/* The payload tab is a developer surface, not part of the sheet. It shows
   the exact JSON the floor is sent, which is the contract three things
   depend on - but a manager who opens it learns nothing from
   `"blockMinutes": 15`, and the sheet has an operator grid and a rig grid
   and no third thing. So it is asked for, the same way the rig's demo
   clock is: open the desk with `?dev`.

   Hidden, not deleted. When a rig shows the wrong person, the fastest
   diagnosis on the floor is comparing what it displays against what it
   was actually sent. */
const DEV = /[?&]dev\b/.test(window.location.search || "");

function renderTabs() {
  const pushTab = [...$("tabs").querySelectorAll("[role=tab]")].find(b => b.dataset.tab === "push");
  if (pushTab) pushTab.hidden = !DEV;
  if (!DEV && cfg.tab === "push") cfg.tab = "ops";   // never strand a manager on it

  [...$("tabs").querySelectorAll("[role=tab]")].forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.tab === cfg.tab)));
  $("panel-ops").hidden  = cfg.tab !== "ops";
  $("panel-rigs").hidden = cfg.tab !== "rigs";
  $("panel-push").hidden = cfg.tab !== "push";
}

function renderPlan() {
  const p = plan();
  renderSetup();
  renderFit(RE.auditPlan(p));
  renderRosters();
  renderTabs();
  renderGrid(p, "ops");
  renderGrid(p, "rigs");
  renderTools(p);
  if (cfg.tab === "push") renderPushPanel(p);
}

/* A change on the plan side only reaches Live if Live is showing the
 * plan. Once something has been pushed, Live shows the floor until the
 * next push - which is the honest answer to "what are they running?" */
function onPlanChanged() {
  planTouched = true;
  renderPlan();
  if (floor.source === "plan") {
    floor.payloads = localPayloads();
    renderLive();
  }
}

/* ===================================================================
 * The gate
 *
 * The desk asks who is at it before it draws anything. Four answers,
 * and the fourth is the one that needed thinking about:
 *
 *   no accounts here    open the desk, show no chip, behave exactly as
 *                       this screen did before there was such a thing
 *   nobody signed in    the sign-in card
 *   an operator         the "not your screen" card, naming them
 *   a manager           the desk
 *
 * **A server that cannot be reached is treated as the first.** That is
 * deliberate and it is not a hole: with no service there is nothing to
 * read and no push that can succeed, so the desk falls back to the plan
 * generated on this screen - which is what makes it work as a plain
 * static page, and what `./serve.sh` and the single-file build depend
 * on. A 401 is different in kind: that is a service that exists and has
 * said no, and it shows the card.
 *
 * None of this is the security gate. The gate is on the service, on
 * every route. This stops somebody wandering into a screen they cannot
 * use; `require_manager` is what stops them using it.
 * =================================================================== */

const S = window.Session;

/* Signed in as somebody who may use this screen - or a deployment that
 * never asked. The desk is a manager's screen. */
function mayUseTheDesk() { return S.mayUse("manager"); }

/* Kept as local names so the rest of this file reads as it did. */
function api(path, init) { return S.api(path, init); }
function probeSession() { return S.probe(); }

/* Show the door, or the desk. Called after every change of who is at it. */
function applyGate() {
  const open = mayUseTheDesk();
  const denied = !open && S.wrongRole("manager");   // signed in, wrong role

  $("view-signin").hidden = open || !!denied;
  $("view-denied").hidden = !denied;
  $("modes").hidden = !open;

  /* The chip is hidden entirely where nobody signs in, so a floor that
     has not configured accounts sees the screen it always saw. */
  const chip = $("who");
  chip.hidden = !S.state.account;
  if (S.state.account) {
    $("who-name").textContent = S.state.account.name;
    $("who-role").textContent = S.state.account.role;
    $("who-role").setAttribute("data-role", S.state.account.role);
  }
  if (denied) $("denied-name").textContent = S.state.account.name;

  /* A password will not fix a service that cannot answer, so say what is
     actually wrong instead of letting somebody retype it three times. */
  if (S.state.broken) {
    $("signin-error").textContent =
      "The service is not answering. Signing in will not work until it does.";
    $("signin-error").hidden = false;
  }

  if (!open) {
    /* Nothing of the desk while the door is shut - not hidden panels
       with the floor still ticking behind them.
       
       Emptied, not just hidden. `hidden` is a styling instruction: the
       names of everyone on the floor stay in the document, readable by
       whoever walks up to an unattended screen and opens the inspector.
       Signing out has to actually take the floor off the page. */
    clearInterval(ticker);
    $("view-live").hidden = true;
    $("view-plan").hidden = true;
    ["board", "upnext", "rosters", "tbl-ops", "tbl-rigs"].forEach(id => {
      $(id).textContent = "";
    });
  }
}

async function signIn(e) {
  if (e && e.preventDefault) e.preventDefault();
  const btn = $("btn-signin"), err = $("signin-error");
  err.hidden = true;
  btn.disabled = true;
  try {
    const out = await S.signIn($("in-email").value, $("in-password").value);
    if (!out.ok) {
      err.textContent = out.message;
      err.hidden = false;
      return;
    }
    $("in-password").value = "";
    openTheDesk();
  } finally {
    btn.disabled = false;
  }
}

async function signOut() {
  await S.signOut();
  floor.payloads = [];
  floor.pushedAt = null;
  floor.source = "plan";
  applyGate();
}

/* Draw the desk for somebody who is allowed it. Separate from boot so
   signing in does not have to reload the page. */
function openTheDesk() {
  applyGate();
  if (!mayUseTheDesk()) return;
  renderPlan();
  floor.payloads = localPayloads();
  setView(cfg.view === "plan" ? "plan" : "live");
  loadFloor(false);
}

/* ===================================================================
 * Wiring
 * =================================================================== */

function setView(v) {
  cfg.view = v;
  [...$("modes").children].forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.mode === v)));
  $("view-live").hidden = v !== "live";
  $("view-plan").hidden = v !== "plan";
  document.body.dataset.view = v;

  clearInterval(ticker);
  if (v === "live") {
    renderLive();
    ticker = setInterval(renderLive, 1000);
  } else {
    renderPlan();
  }
}

[...$("modes").children].forEach(b =>
  b.addEventListener("click", () => setView(b.dataset.mode)));
[...$("tabs").querySelectorAll("[role=tab]")].forEach(b =>
  b.addEventListener("click", () => { cfg.tab = b.dataset.tab; renderPlan(); }));

$("in-date").value = cfg.date;
$("in-date").addEventListener("change", e => { cfg.date = e.target.value; onPlanChanged(); });
$("btn-push").addEventListener("click", pushFloor);
$("btn-print").addEventListener("click", () => window.print());
$("btn-refresh").addEventListener("click", () => loadFloor(true));
$("signin-form").addEventListener("submit", signIn);
$("btn-signout").addEventListener("click", signOut);
$("btn-denied-out").addEventListener("click", signOut);

/* Ask who is at the desk before drawing any of it. Everything below the
   masthead waits on that answer - a screen that renders the floor and
   then hides it has already put it on the wire. */
(async function boot() {
  await probeSession();
  openTheDesk();
})();

/* Re-read the floor now and then, in case somebody pushed from another
 * desk. Cheap: twelve small GETs a minute. */
setInterval(() => {
  if (cfg.view === "live" && mayUseTheDesk()) loadFloor(false);
}, 60000);
