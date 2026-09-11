/* =====================================================================
 * my-shift.js  -  one operator's day, and nobody else's
 *
 * The rig shows the turn it is running. The desk shows the whole floor.
 * Neither shows an operator *their own day*, and neither can: a rig
 * knows only its own turns, and Mei Chen's morning walks across three of
 * them. That is the same fact that put `theyGoTo` in the payload - where
 * an outgoing operator goes is a fact about their day, not about the rig
 * they are leaving.
 *
 * Read only. There is deliberately no control on this screen that
 * changes anything on the floor, which is what lets it be handed to
 * sixteen people without a second thought.
 *
 * **The gaps are drawn here, not sent.** `/api/me/shift` returns the
 * turns and nothing else, because the server is not allowed to derive a
 * rotation - that is the founding invariant of this system. Each turn
 * carries `theyGoTo`, so the break between two turns is two pushed facts
 * laid side by side rather than a third answer computed by anybody.
 *
 * Scoping is not done here either. The route sends one operator's turns
 * because a page can hide a row and only a route can decline to send it.
 * This screen never sees anybody else's day to hide.
 *
 * It is built for a three-second glance on a phone, so the order it
 * answers questions in is fixed: how long have I got, where do I go
 * next, then the shape of the day. See the stylesheet for why.
 * ===================================================================== */

"use strict";

const S = window.Session;
const RE = window.RotationEngine;

const $ = id => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const txt = t => document.createTextNode(t);

/* What the service last told us. */
const day = {
  shift: null, turns: [], rows: [],
  checkedAt: null,     // when the last successful read landed
  stale: false,        // a read failed and we are showing the old one
  signature: "",       // to notice the desk re-pushing under us
};
let ticker = null;

/* How close to a handover counts as "soon". Long enough to finish a
 * take and walk, short enough that it is not shouting for half the
 * turn. */
const SOON_MINS = 5;

/* ===================================================================
 * Clock arithmetic
 *
 * Times in a payload are floor wall-clock, "HH:MM". Everything on this
 * page is measured on one axis: minutes since the shift started -
 * negative before it, past its length after it. `at`, wherever it
 * appears below, is that number.
 *
 * Minutes-of-day was the first axis, and it cannot answer the question
 * this page exists to answer. The route serves a person's shift before
 * it starts and after it ends, not only while it runs; a shift may
 * cross midnight (Day runs 16:00-00:00); and 23:00 the night before a
 * Night shift is the same minute-of-day as 23:00 the night after it.
 * Only the date tells them apart, so `at` is measured from the window
 * the desk wrote into the payload, through the engine, the same way
 * the floor's clock is read.
 * =================================================================== */

const toMin = hhmm => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));
const pad2 = n => (n < 10 ? "0" : "") + n;

/* Where a floor "HH:MM" sits on the axis. `offset` for the start of a
 * turn, `offsetEnd` for the end of one, because "00:00" as an *end*
 * means the end of the day, not the start of it - the Day shift's last
 * turn ends at 00:00, and that is 480 minutes in, not zero. */
function shiftStart() {
  return day.shift && day.shift.start ? toMin(day.shift.start) : 0;
}
function offset(hhmm) {
  return (toMin(hhmm) - shiftStart() + 1440) % 1440;
}
function offsetEnd(hhmm) {
  const o = toMin(hhmm) - shiftStart();
  return o <= 0 ? o + 1440 : o;
}

/* The minute it is *on the floor*, not on this device.
 *
 * Every time on this page is floor wall-clock, written by the desk. An
 * operator reading their shift from a phone in another zone - on a
 * train, at home the night before - would otherwise be told they are
 * mid-turn when the floor has not started. The desk had exactly this
 * bug and it is fixed in one place: minutesOnFloor() in the engine,
 * which reads the zone the desk wrote into the payload. Computing it a
 * second time here would be a second answer, and the first one able to
 * disagree. */
function nowMin() {
  if (RE && RE.minutesOnFloor && day.shift) {
    return RE.minutesOnFloor({ shift: day.shift }, Date.now());
  }
  const d = new Date();
  return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60;
}

/* Minutes since the shift started, from the window the desk wrote.
 *
 * The engine's shiftWindow() turns the payload's date, start and zone
 * into an instant - the same reading the rig makes before it trusts a
 * sheet. Without a dated window (no shift loaded, or a payload too old
 * to carry a date) it falls back to the floor's minute-of-day past the
 * shift's start, wrapping: the old axis with the old ambiguity, reached
 * only by data no desk has pushed. */
function nowOnShift() {
  const w = RE && RE.shiftWindow && day.shift ? RE.shiftWindow({ shift: day.shift }) : null;
  if (w) return (Date.now() - w.start) / 60000;
  return (nowMin() - shiftStart() + 1440) % 1440;
}

/* Whether `at` falls inside a row. */
function covers(row, at) {
  return at >= row.start && at < row.end;
}

/* "1h 05m" / "45m", for a span of whole minutes. */
function hm(mins) {
  const h = Math.floor(mins / 60), m = Math.round(mins % 60);
  return h ? h + "h " + pad2(m) + "m" : m + "m";
}

/* "18:32" - minutes and seconds, the way the desk counts down. */
function mmss(minsLeft) {
  const total = Math.max(0, Math.round(minsLeft * 60));
  return Math.floor(total / 60) + ":" + pad2(total % 60);
}

/* ===================================================================
 * The day, as rows
 *
 * A turn, then the gap after it, then the next turn. The gap's kind is
 * the outgoing turn's own `theyGoTo`, which is why nothing here has to
 * work out whose break is whose.
 * =================================================================== */

function buildRows(turns, shift) {
  const rows = [];
  turns.forEach((t, i) => {
    /* The task of the rig, from the turn; a service too old to put it
       there gets the shift's, which is the same thing on a floor where
       a group keeps one task all shift. */
    rows.push({ kind: "work", from: t.from, to: t.to, rigId: t.rigId,
                start: offset(t.from), end: offsetEnd(t.to),
                minutes: t.minutes, relievedBy: t.relievedBy,
                goesTo: t.theyGoTo,
                task: t.task || (shift && shift.task) || null });

    const next = turns[i + 1];
    if (!next) return;
    /* Only when there is a gap. Back-to-back turns on two rigs are a
       walk across the floor, not a break, and inventing a zero-minute
       Break row would put a rest in the day that nobody was given. */
    const gapMins = offset(next.from) - offsetEnd(t.to);
    if (gapMins <= 0) return;

    const kind = (t.theyGoTo === "Break" || t.theyGoTo === "Think")
      ? t.theyGoTo : "Break";
    rows.push({ kind: kind, from: t.to, to: next.from, minutes: gapMins,
                start: offsetEnd(t.to), end: offset(next.from),
                backTo: next.rigId });
  });
  return rows;
}

/* How long the shift is, on the axis. Read from the window the desk
 * wrote; a payload with no end falls back to where the last row ends. */
function shiftLength() {
  if (day.shift && day.shift.end) return offsetEnd(day.shift.end);
  return day.rows.length ? day.rows[day.rows.length - 1].end : 480;
}

/* A floor "HH:MM" for a minute on the axis, for the strip's hour marks. */
function hhmmAt(mins) {
  const m = ((Math.round(mins) % 1440) + 1440) % 1440;
  return pad2(Math.floor(m / 60)) + ":" + pad2(m % 60);
}

/* Where we are in the day: the row happening now, the one after it,
 * and how much work is behind us. */
function whereWeAre(at) {
  const i = day.rows.findIndex(r => covers(r, at));
  const worked = day.rows
    .filter(r => r.kind === "work")
    .reduce((sum, r) => {
      if (r.end <= at) return sum + r.minutes;
      if (covers(r, at)) return sum + (at - r.start);
      return sum;
    }, 0);
  const turns = day.rows.filter(r => r.kind === "work");
  const turnNo = i < 0 ? 0
    : day.rows.slice(0, i + 1).filter(r => r.kind === "work").length;
  return {
    i: i,
    row: i < 0 ? null : day.rows[i],
    next: i < 0 ? null : day.rows[i + 1] || null,
    workedMins: Math.max(0, Math.round(worked)),
    turnNo: turnNo,
    turnsTotal: turns.length,
  };
}

/* ===================================================================
 * Drawing
 * =================================================================== */

function renderClock() {
  const c = $("clock");
  if (!day.shift) { c.hidden = true; return; }
  c.hidden = false;
  const at = nowMin();
  $("clock-time").textContent =
    pad2(Math.floor(at / 60) % 24) + ":" + pad2(Math.floor(at % 60));
  /* The floor's zone, named once. Times on this page are the floor's,
     and an operator reading it from somewhere else has no way to know
     that unless it is said. */
  $("clock-zone").textContent = day.shift.tz || "floor time";
}

function renderShiftLine() {
  const line = $("shiftline");
  line.textContent = "";
  if (!day.shift) return;

  const when = day.shift.date
    ? new Date(day.shift.date + "T00:00:00").toLocaleDateString(
        [], { weekday: "short", day: "numeric", month: "short" })
    : "";
  const parts = [el("b", null, day.shift.label || "Shift")];
  if (when) parts.push(el("span", null, when));
  if (day.shift.group) parts.push(el("span", null, "Group " + day.shift.group));
  if (day.shift.task) parts.push(el("b", null, day.shift.task));

  parts.forEach((p, i) => {
    if (i) line.appendChild(el("span", "sep", "·"));
    line.appendChild(p);
  });
}

/* The hero. Four states, and the wording changes with each - a colour
 * alone would not survive a phone in sunlight. */
function renderNow(at) {
  const box = $("now");
  box.textContent = "";
  const here = whereWeAre(at);

  if (!here.row) return renderOffShift(box, at);

  const row = here.row;
  const left = row.end - at;
  const soon = row.kind === "work" && left <= SOON_MINS;

  box.setAttribute("data-kind", soon ? "soon" : row.kind);
  box.setAttribute("data-state", "");

  box.appendChild(el("p", "now-k",
    soon ? "Hand over soon" : row.kind === "work" ? "On now" : row.kind));

  const where = el("p", "now-where");
  if (row.kind === "work") {
    where.appendChild(el("span", "mono", row.rigId));
  } else {
    where.appendChild(txt(row.kind === "Break" ? "On your break" : "Thinking time"));
  }
  box.appendChild(where);
  if (row.kind === "work" && row.task) box.appendChild(el("p", "now-task", row.task));
  box.appendChild(el("p", "now-when",
    row.from + " – " + row.to + " · " + hm(row.minutes)));

  const count = el("div", "now-count");
  count.appendChild(el("b", "now-left", mmss(left)));
  const unit = el("div", "now-unit");
  unit.appendChild(txt(row.kind === "work" ? "until you hand over" : "until you are back"));
  count.appendChild(unit);
  box.appendChild(count);

  const bar = el("div", "bar");
  const fill = el("i");
  const done = Math.max(0, Math.min(1, (row.minutes - left) / row.minutes));
  fill.style.width = (done * 100).toFixed(1) + "%";
  bar.appendChild(fill);
  box.appendChild(bar);

  const hand = el("p", "now-hand");
  if (row.kind === "work" && row.relievedBy) {
    hand.appendChild(el("b", null, row.relievedBy));
    hand.appendChild(txt(" takes over · you go to "));
    hand.appendChild(el("b", null, String(row.goesTo).toLowerCase()));
  } else if (row.backTo) {
    hand.appendChild(txt("Back on "));
    hand.appendChild(el("b", null, row.backTo));
    hand.appendChild(txt(" at " + row.to));
  } else if (row.kind === "work") {
    hand.appendChild(txt("Last turn of the shift"));
  }
  if (hand.childNodes.length) box.appendChild(hand);
}

/* Before the first turn, or after the last. Both are true answers and
 * neither is an error, so neither gets an error's colour. */
function renderOffShift(box, at) {
  box.setAttribute("data-kind", "off");
  box.setAttribute("data-state", "");
  const first = day.rows[0], last = day.rows[day.rows.length - 1];

  if (!first) {
    box.appendChild(el("p", "now-k", "Nothing scheduled"));
    box.appendChild(el("p", "now-where", "No shift for you right now"));
    return;
  }

  if (at < first.start) {
    box.appendChild(el("p", "now-k", "Not started"));
    const w = el("p", "now-where");
    w.appendChild(txt("Your shift starts at "));
    w.appendChild(el("span", "mono", first.from));
    box.appendChild(w);

    const count = el("div", "now-count");
    count.appendChild(el("b", "now-left", hm(first.start - at)));
    count.appendChild(el("div", "now-unit", "from now"));
    box.appendChild(count);

    const hand = el("p", "now-hand");
    hand.appendChild(txt("You start on "));
    hand.appendChild(el("b", null, first.rigId || "your first rig"));
    box.appendChild(hand);
    return;
  }

  box.appendChild(el("p", "now-k", "Finished"));
  box.appendChild(el("p", "now-where", "Your shift is done"));
  box.appendChild(el("p", "now-when",
    "It ran " + first.from + " – " + last.to));
}

function renderNext(at) {
  const box = $("next");
  box.textContent = "";
  const here = whereWeAre(at);
  const next = here.next;

  if (!here.row || !next) {
    /* No next row is either "not started" or "finished", and the hero
       has already said which. A second card repeating it is noise. */
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.setAttribute("data-kind", next.kind);

  box.appendChild(el("span", "next-k", "Next"));

  const body = el("div", "next-body");
  const what = el("p", "next-what");
  if (next.kind === "work") {
    what.appendChild(txt("Work "));
    what.appendChild(el("span", "mono", next.rigId));
  } else {
    what.appendChild(txt(next.kind));
  }
  body.appendChild(what);

  if (next.kind === "work" && next.task) body.appendChild(el("p", "next-task", next.task));

  const sub = el("p", "next-sub");
  sub.appendChild(txt(next.from + " – " + next.to + " · " + hm(next.minutes)));
  if (next.backTo) sub.appendChild(txt(" · then back on " + next.backTo));
  body.appendChild(sub);
  box.appendChild(body);

  box.appendChild(el("span", "next-in", "in " + hm(here.row.end - at)));
}

function renderProgress(at) {
  const box = $("progress");
  box.textContent = "";
  const here = whereWeAre(at);
  if (!day.rows.length || !here.row) { box.hidden = true; return; }
  box.hidden = false;

  const totalWork = day.rows.filter(r => r.kind === "work")
    .reduce((s, r) => s + r.minutes, 0);

  const turn = el("span");
  turn.appendChild(txt("Turn "));
  turn.appendChild(el("b", null, String(here.turnNo || 1)));
  turn.appendChild(txt(" of " + here.turnsTotal));

  const worked = el("span");
  worked.appendChild(el("b", null, hm(here.workedMins)));
  worked.appendChild(txt(" worked"));

  const toGo = el("span", null, hm(Math.max(0, totalWork - here.workedMins)) + " to go");

  [turn, worked, toGo].forEach((part, i) => {
    if (i) box.appendChild(el("span", "sep", "·"));
    box.appendChild(part);
  });
}

/* The whole shift as one strip. Every row is a segment as wide as it is
 * long, in the row's colour; time before the first turn and after the
 * last is drawn as nothing, so the strip is always the shift and a late
 * start reads as a late start. Rebuilt with the table, not every second
 * - only the marker moves, in renderNowMark. */
function renderStrip(at) {
  const box = $("strip"), track = $("track"), ticks = $("ticks");
  track.textContent = "";
  ticks.textContent = "";
  if (!day.rows.length || !day.shift) { box.hidden = true; return; }
  box.hidden = false;
  const len = shiftLength();

  const filler = (from, to) => {
    if (to - from <= 0) return;
    const seg = el("div", "seg");
    seg.setAttribute("data-kind", "off");
    seg.style.setProperty("--w", String(to - from));
    track.appendChild(seg);
  };
  let cursor = 0;
  day.rows.forEach(row => {
    filler(cursor, row.start);
    const seg = el("div", "seg");
    seg.setAttribute("data-kind", row.kind);
    seg.style.setProperty("--w", String(row.end - row.start));
    /* The rig's name, where there is room for it - a fifteen-minute
       stub at the end of the shift shows three letters of it, which
       reads as a fault. The table under the strip names every one. */
    if (row.kind === "work" && row.end - row.start >= 30) {
      seg.appendChild(el("span", "mono", row.rigId));
    }
    if (covers(row, at)) seg.classList.add("on");
    else if (row.end <= at) seg.classList.add("gone");
    track.appendChild(seg);
    cursor = row.end;
  });
  filler(cursor, len);

  /* Hour marks, the first and last written in full. */
  const start = shiftStart();
  for (let m = 0; m <= len; m += 60) {
    const full = m === 0 || m === len;
    const tick = el("span", null,
      full ? hhmmAt(start + m) : String(Math.floor(((start + m) / 60) % 24)));
    tick.style.left = ((m / len) * 100).toFixed(1) + "%";
    ticks.appendChild(tick);
  }
}

/* Where now is on the strip. Off it before the shift starts and after
 * it ends - the now card already says which, and a marker pinned to an
 * edge would read as a shift that is running. */
function renderNowMark(at) {
  const mark = $("nowmark");
  const len = shiftLength();
  if (!day.rows.length || at < 0 || at >= len) { mark.hidden = true; return; }
  mark.hidden = false;
  mark.style.left = ((at / len) * 100).toFixed(1) + "%";
  $("nowmark-t").textContent = "now " + clockString();
}

function renderTimeline(at) {
  const body = $("timeline");
  body.textContent = "";

  day.rows.forEach(row => {
    const isNow = covers(row, at);
    const isPast = !isNow && row.end <= at;

    const tr = el("tr", "tl");
    tr.setAttribute("data-kind", row.kind);
    if (isNow) tr.classList.add("is-now");
    if (isPast) tr.classList.add("is-past");

    tr.appendChild(el("td", "tl-t", row.from));
    tr.appendChild(el("td", "tl-to", "– " + row.to));

    const what = el("td", "tl-what");
    if (row.kind === "work") {
      what.appendChild(el("span", "mono", row.rigId));
    } else {
      what.appendChild(txt(row.kind));
    }
    what.appendChild(el("span", "tl-mins", hm(row.minutes)));
    /* The task, on every turn: reading down the table is how an
       operator sees what the next turns are for, not only this one. */
    if (row.kind === "work" && row.task) {
      what.appendChild(el("span", "tl-task", row.task));
    }
    tr.appendChild(what);

    const sub = el("td", "tl-sub");
    if (row.kind === "work" && row.goesTo) {
      sub.appendChild(txt("then "));
      sub.appendChild(el("b", null, row.goesTo));
    }
    if (row.kind === "work" && row.relievedBy) {
      sub.appendChild(txt(sub.childNodes.length ? " · " : ""));
      sub.appendChild(el("b", null, row.relievedBy));
      sub.appendChild(txt(" relieves you"));
    }
    if (row.backTo) {
      sub.appendChild(txt(sub.childNodes.length ? " · " : ""));
      sub.appendChild(txt("back on "));
      sub.appendChild(el("b", null, row.backTo));
    }
    tr.appendChild(sub);

    body.appendChild(tr);
  });
}

function renderBudget() {
  const box = $("budget");
  if (!day.rows.length) { box.hidden = true; return; }
  box.hidden = false;

  const total = kind => day.rows
    .filter(r => r.kind === kind)
    .reduce((sum, r) => sum + r.minutes, 0);

  const work = total("work"), brk = total("Break"), think = total("Think");
  $("b-work").textContent = hm(work);
  $("b-break").textContent = hm(brk);
  $("b-think").textContent = hm(think);

  /* The budget CLAUDE.md holds the floor to. Said plainly rather than
     scored, because a shift that is short is usually the schedule's
     doing and not the operator's. */
  $("budget-note").textContent = (work === 360 && brk === 60 && think === 60)
    ? "Six hours of work, an hour of break and an hour to think - the full shift."
    : "The full shift is 6h of work, 1h break and 1h think.";
}

/* The countdown, kept on screen once the hero has scrolled away. */
function renderPerch(at) {
  const perch = $("perch");
  const here = whereWeAre(at);
  const hero = $("now");
  const heroGone = hero.getBoundingClientRect
    ? hero.getBoundingClientRect().bottom < 8
    : false;

  if (!here.row || !heroGone || $("view-day").hidden) {
    perch.hidden = true;
    return;
  }
  perch.hidden = false;
  const row = here.row;
  const left = row.end - at;
  perch.setAttribute("data-kind",
    row.kind === "work" && left <= SOON_MINS ? "soon" : row.kind);
  perch.style.setProperty("--state",
    row.kind === "work" ? "var(--work)"
      : row.kind === "Break" ? "var(--break)" : "var(--think)");
  $("perch-what").textContent = row.kind === "work"
    ? row.rigId + " until " + row.to
    : row.kind + " until " + row.to;
  $("perch-left").textContent = mmss(left);
}

function renderFreshness() {
  $("stale").hidden = !day.stale;
  if (day.stale) {
    $("stale").textContent =
      "Could not reach the server. This is the schedule as it was at "
      + (day.checkedAt || "the last check") + " - it may have changed since.";
  }
  $("checked").textContent = day.checkedAt
    ? "Last checked " + day.checkedAt
    : "";
}

/* What the timeline is currently drawing. The list is only rebuilt when
 * this changes - see below. */
let drawn = "";

function renderDay() {
  const at = nowOnShift();

  /* Every second: the clock and the things that count down. All of them
     are above the timeline and none changes the page's height. */
  renderClock();
  renderNow(at);
  renderNext(at);
  renderProgress(at);
  renderPerch(at);
  renderNowMark(at);

  /* The timeline, only when it would actually differ.
   *
   * Rebuilding seventeen rows every second looked harmless and was not:
   * emptying the list collapses the document, the browser clamps the
   * scroll offset to the shorter page, and an operator who had scrolled
   * to look at the end of their day was thrown back to the top a second
   * later. Every second. The list changes when the schedule changes or
   * when the current row moves on - at most once a minute and usually
   * far less. The strip is rebuilt with it, for the same reason. */
  const sig = [day.signature, whereWeAre(at).i, day.stale,
               day.checkedAt].join("|");
  if (sig === drawn) return;
  drawn = sig;

  renderShiftLine();
  renderStrip(at);
  renderTimeline(at);
  renderBudget();
  renderFreshness();
  /* No scrolling to the current row. The page opens on now - the strip
     and the two cards are the top of it - and jumping to the table row
     scrolled exactly those out of view. */
}

/* ===================================================================
 * Loading
 * =================================================================== */

function clockString() {
  const at = nowMin();
  return pad2(Math.floor(at / 60) % 24) + ":" + pad2(Math.floor(at % 60));
}

async function loadShift() {
  try {
    const r = await S.api("/api/me/shift");
    if (!r.ok) throw new Error("no shift");
    const body = await r.json();

    const before = day.signature;
    day.shift = body.shift || null;
    day.turns = body.turns || [];
    day.rows = buildRows(day.turns, day.shift);
    day.signature = JSON.stringify(day.turns.map(t => [t.from, t.to, t.rigId]));
    day.checkedAt = clockString();
    day.stale = false;

    if (before && before !== day.signature) {
      /* The desk re-pushed under us. Swapping the day silently is how
         somebody walks to the wrong rig. */
      $("empty").hidden = false;
      $("empty").textContent =
        "Your schedule changed just now - the desk pushed a new one. "
        + "This is the current version.";
    } else {
      $("empty").hidden = day.rows.length > 0;
      if (!day.rows.length) {
        /* Nothing that names this person, and the line says why: the
           route reports whether anything was pushed and whether any of
           it names people, and the case that applies is read back. No
           push is the rig's Standby, the same honest answer - a floor
           waiting on a schedule, not an operator with no work. A push
           that names nobody is a roster of plain names (CLAUDE.md), and
           one that names people and not this person is a manager's
           decision or an oversight; either way a manager is who fixes
           it. A service too old to say has no `floor` and gets the
           first line. */
        const floor = body.floor || {};
        $("empty").textContent = "Nothing is scheduled for you right now. "
          + (!floor.pushed
              ? "If a shift should be running, the floor has not been pushed "
                + "a schedule for it yet."
              : !floor.namesPeople
                ? "The floor's schedule names no people yet, so it cannot say "
                  + "which day is yours - ask a manager."
                : "Today's schedule does not name you - ask a manager if it should.");
      }
    }
  } catch (ignored) {
    if (day.rows.length) {
      /* Keep what we have and mark it. A schedule you can still read
         beats an error message that replaced it. */
      day.stale = true;
    } else {
      day.shift = null; day.turns = []; day.rows = [];
      $("empty").hidden = false;
      $("empty").textContent = "Could not reach the server.";
    }
  }
  renderDay();
}

/* ===================================================================
 * The gate
 *
 * The same four answers the desk asks for, from the same shared module.
 * The only difference is the role this screen requires.
 * =================================================================== */

function applyGate() {
  const open = S.mayUse("operator");
  const denied = S.wrongRole("operator");
  /* A door of its own, and it outranks the sign-in card: somebody who
     opened a reset link has no password to sign in with, so offering
     them the box is offering the one thing they know does not work. */
  const resetting = !open && !!doorState.resetToken;
  const forgetting = !open && !denied && !resetting && doorState.forgot;

  $("view-signin").hidden = open || denied || resetting || forgetting;
  $("view-denied").hidden = !denied;
  if ($("view-forgot")) $("view-forgot").hidden = !forgetting;
  if ($("view-reset")) $("view-reset").hidden = !resetting;
  $("view-day").hidden = !open;

  const chip = $("who");
  chip.hidden = !S.state.account;
  if (S.state.account) {
    $("who-name").textContent = S.state.account.name;
    $("who-role").textContent = S.state.account.role;
    $("who-role").setAttribute("data-role", S.state.account.role);
    if (denied) $("denied-name").textContent = S.state.account.name;
  }

  if (S.state.broken) {
    $("signin-error").textContent =
      "The service is not answering. Signing in will not work until it does.";
    $("signin-error").hidden = false;
  }

  if (!open) {
    /* Emptied, not hidden. `hidden` is a styling instruction and this
       screen holds one person's whole day. */
    clearInterval(ticker);
    day.shift = null; day.turns = []; day.rows = [];
    day.checkedAt = null; day.stale = false; day.signature = "";
    drawn = "";
    $("timeline").textContent = "";
    $("now").textContent = "";
    $("shiftline").textContent = "";
    $("next").hidden = true;
    $("progress").hidden = true;
    $("budget").hidden = true;
    $("strip").hidden = true;
    $("nowmark").hidden = true;
    $("clock").hidden = true;
    $("perch").hidden = true;
    $("checked").textContent = "";
  }
}

async function openTheDay() {
  applyGate();
  if (!S.mayUse("operator")) return;

  /* A deployment with no accounts has nobody to show a day for. The
     screen is honest about that rather than sitting blank. */
  if (!S.state.account) {
    $("empty").hidden = false;
    $("empty").textContent =
      "Nobody is signed in, and this floor has no accounts configured. "
      + "There is no operator whose shift this would be.";
    return;
  }

  await loadShift();
  clearInterval(ticker);
  ticker = setInterval(renderDay, 1000);
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
    await openTheDay();
  } finally {
    btn.disabled = false;
  }
}

async function signOut() {
  await S.signOut();
  applyGate();
}

/* --------------------------------------------- a password nobody remembers

   Two doors that are not the sign-in card: asking for a link, and
   opening one. Which is showing is `doorState`, read by applyGate. */

const doorState = { forgot: false, resetToken: null };

/* Take the token out of the address bar, keeping the token itself. It is
   a credential while it lives, and a URL is the least private place on a
   screen: history, referrers, and whoever is standing behind you. */
function claimResetToken() {
  const token = S.resetTokenInUrl();
  if (!token) return;
  doorState.resetToken = token;
  if (window.history && window.history.replaceState) {
    /* The path alone - the token is in the hash, so keeping the hash
       would be keeping the token. */
    window.history.replaceState(null, "", window.location.pathname);
  }
}

function showForgot(on) {
  doorState.forgot = on;
  if ($("forgot-error")) $("forgot-error").hidden = true;
  if ($("forgot-ok")) $("forgot-ok").hidden = true;
  if ($("forgot-email")) $("forgot-email").value = ($("in-email") || {}).value || "";
  applyGate();
}

async function sendResetLink(e) {
  if (e && e.preventDefault) e.preventDefault();
  const btn = $("btn-forgot-send");
  const err = $("forgot-error"), ok = $("forgot-ok");
  if (err) err.hidden = true;
  if (ok) ok.hidden = true;
  if (btn) btn.disabled = true;
  try {
    const out = await S.requestReset($("forgot-email").value);
    if (!out.ok) {
      if (err) { err.textContent = out.message; err.hidden = false; }
      return;
    }
    /* The service's own wording, which is careful not to say whether
       that address exists. Rewriting it here would undo that. */
    if (ok) { ok.textContent = out.message; ok.hidden = false; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function saveResetPassword(e) {
  if (e && e.preventDefault) e.preventDefault();
  const btn = $("btn-reset-save"), err = $("reset-error");
  if (err) err.hidden = true;

  if ($("reset-new").value !== $("reset-again").value) {
    if (err) {
      err.textContent = "The two new passwords are not the same.";
      err.hidden = false;
    }
    return;
  }

  if (btn) btn.disabled = true;
  try {
    const out = await S.finishReset(doorState.resetToken, $("reset-new").value);
    if (!out.ok) {
      if (err) { err.textContent = out.message; err.hidden = false; }
      return;
    }
    /* Spent, and the service has already signed them in. Clearing it
       stops a reload trying to use it again and being told, correctly
       but confusingly, that it has been used. */
    doorState.resetToken = null;
    ["reset-new", "reset-again"].forEach(id => {
      if ($(id)) $(id).value = "";
    });
    await openTheDay();
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* --------------------------------------------- changing your password

   An operator has an account like anybody else. Before this, a
   forgotten or shared password could only be fixed by asking a manager
   to re-mint the account and read the new one out. */

function openPasswd() {
  ["pw-current", "pw-new", "pw-again"].forEach(id => {
    if ($(id)) $(id).value = "";
  });
  if ($("passwd-error")) $("passwd-error").hidden = true;
  if ($("passwd-ok")) $("passwd-ok").hidden = true;
  if ($("passwd-modal")) $("passwd-modal").hidden = false;
  if ($("pw-current")) $("pw-current").focus();
}

function closePasswd() {
  /* Cleared on the way out. Three filled password fields behind a
     hidden panel on a screen in a break room is the same problem the
     desk empties the floor for. */
  ["pw-current", "pw-new", "pw-again"].forEach(id => {
    if ($(id)) $(id).value = "";
  });
  if ($("passwd-modal")) $("passwd-modal").hidden = true;
}

async function savePasswd(e) {
  if (e && e.preventDefault) e.preventDefault();
  const btn = $("btn-passwd-save");
  const err = $("passwd-error"), ok = $("passwd-ok");
  if (err) err.hidden = true;
  if (ok) ok.hidden = true;

  /* The only check this page can make that the service cannot: it
     cannot know what somebody meant to type twice. The current
     password and the length rule are the service's to answer, so they
     are asked rather than duplicated. */
  if ($("pw-new").value !== $("pw-again").value) {
    if (err) {
      err.textContent = "The two new passwords are not the same.";
      err.hidden = false;
    }
    return;
  }

  if (btn) btn.disabled = true;
  try {
    const out = await S.changePassword($("pw-current").value, $("pw-new").value);
    if (!out.ok) {
      if (err) {
        err.textContent = out.message;
        err.hidden = false;
      }
      return;
    }
    ["pw-current", "pw-new", "pw-again"].forEach(id => {
      if ($(id)) $(id).value = "";
    });
    if (ok) ok.hidden = false;
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ------------------------------------------------------------ wiring */

/* Wire a handler, and survive the element not being there.
 *
 * This is top-level code: one `null.addEventListener` throws before
 * boot() ever runs, and the whole screen is blank with nothing on it to
 * say why. That is exactly what happened - a browser holding a cached
 * copy of an older index.html loaded new script against old markup, and
 * an operator would have seen a masthead and an empty page.
 *
 * A missing id is still a real fault and still loud: the headless test
 * asserts `missingIds` is empty, so it cannot reach a floor unnoticed.
 * What it must not do is take the rest of the screen down with it. */
function on(id, type, fn) {
  const node = $(id);
  if (!node) {
    console.warn("my-shift: no #" + id + " on this page - " + type
                 + " not wired. The page is older than this script.");
    return;
  }
  node.addEventListener(type, fn);
}

on("signin-form", "submit", signIn);
on("btn-signout", "click", signOut);
on("btn-denied-out", "click", signOut);
on("btn-forgot", "click", () => showForgot(true));
on("btn-forgot-back", "click", () => showForgot(false));
on("forgot-form", "submit", sendResetLink);
on("reset-form", "submit", saveResetPassword);
on("btn-passwd", "click", openPasswd);
on("btn-passwd-cancel", "click", closePasswd);
on("passwd-form", "submit", savePasswd);
on("passwd-modal", "click", e => {
  if (e.target === $("passwd-modal")) closePasswd();
});
document.addEventListener("keydown", e => {
  const modal = $("passwd-modal");
  if (e.key === "Escape" && modal && !modal.hidden) closePasswd();
});
/* The perch follows the scroll, not the one-second tick - a sticky bar
   that appears a second after you scroll past the thing it replaces
   reads as a glitch. */
window.addEventListener("scroll", () => renderPerch(nowOnShift()), { passive: true });

/* Everything the render layer assumes is on the page.
 *
 * A browser holding a cached index.html from before this script was
 * written runs new code against old markup, and the failure is silent
 * and ugly: half a screen, or none, with an exception nobody sees. It
 * happened twice while this was being built.
 *
 * Guarding every single lookup would spread the problem thinly across
 * the file and still leave a screen that is quietly missing things. One
 * check, once, and an answer somebody can act on. */
const NEEDED = ["view-signin", "view-denied", "view-day", "clock", "clock-time",
                "clock-zone", "who", "who-name", "who-role", "shiftline",
                "stale", "now", "next", "progress", "empty", "strip", "track",
                "nowmark", "nowmark-t", "ticks",
                "timeline", "budget", "b-work", "b-break", "b-think",
                "budget-note", "checked", "perch", "perch-what", "perch-left"];

function pageIsCurrent() {
  const missing = NEEDED.filter(id => !document.getElementById(id));
  if (!missing.length) return true;

  console.error("my-shift: this page is older than this script; missing "
                + missing.join(", "));
  const box = $("view-day") || document.body;
  box.hidden = false;
  box.textContent = "";
  const p = document.createElement("p");
  p.className = "empty";
  p.textContent = "This page is out of date - your browser is showing an "
    + "older copy than the one on the server. Reload to get your shift.";
  box.appendChild(p);
  return false;
}

(async function boot() {
  if (!pageIsCurrent()) return;
  /* Before the probe. Claiming the token takes it out of the address
     bar, and the probe is the first thing that could be slow - a reset
     link left in the URL while a fetch is in flight is a credential on
     screen for as long as that takes. */
  claimResetToken();
  await S.probe();
  await openTheDay();
})();

/* Re-read now and then. A schedule pushed mid-shift changes this
 * screen, and an operator should not have to reload to find out. */
setInterval(() => { if (S.mayUse("operator") && S.state.account) loadShift(); }, 60000);
