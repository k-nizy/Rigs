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

const GROUPS = window.DEMO_ROSTER.groups;

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

/* Ask the server what the floor is actually running. Falls back to the
 * plan on this screen, which is what makes the desk work as a plain
 * static page with no server behind it at all. */
async function loadFloor(announce) {
  try {
    const state = await fetch("/api/state").then(r => r.ok ? r.json() : Promise.reject());
    if (!state.rigs || !state.rigs.length) throw new Error("nothing pushed");

    floor.payloads = await Promise.all(state.rigs.map(id =>
      fetch("/api/rigs/" + encodeURIComponent(id) + "/schedule.json")
        .then(r => r.ok ? r.json() : Promise.reject())));
    floor.pushedAt = state.pushedAt;
    floor.source = "floor";
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
  const running = nowRel < RE.SHIFT_MINUTES;
  const atRel   = running ? nowRel : 0;   // before the shift: the opening line-up

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
    leftOfShift: RE.SHIFT_MINUTES - nowRel,
    startsIn: (1440 - nowRel) % 1440,
    groups: groups,
    soonest: soonest,
    moves: moves,
  };
}

/* Who is where - the one thing that decides whether the board has to be
 * rebuilt or merely re-timed. */
function boardSignature(s) {
  return s.running + "|" + s.groups.map(g =>
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
  if (!s.running) {
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
    up.textContent = s.running ? "" : "The shift has not started.";
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

/* Push every rig in one shot. All-or-nothing: the server rejects a mix
 * of valid and invalid payloads, so the floor never runs half-updated. */
async function pushFloor() {
  const btn = $("btn-push");
  const payloads = dayPayloads();
  btn.disabled = true;
  $("push-note").textContent = "Pushing " + RE.SHIFTS.length + " shifts...";
  try {
    const res = await fetch("/api/push", {
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
      await loadFloor(false);          // Live should now show the floor, not the plan
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
  renderPlan();
  if (floor.source === "plan") {
    floor.payloads = localPayloads();
    renderLive();
  }
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

renderPlan();
floor.payloads = localPayloads();
setView("live");
loadFloor(false);

/* Re-read the floor now and then, in case somebody pushed from another
 * desk. Cheap: twelve small GETs a minute. */
setInterval(() => { if (cfg.view === "live") loadFloor(false); }, 60000);
