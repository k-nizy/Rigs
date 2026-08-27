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
 * ===================================================================== */

"use strict";

const S = window.Session;

const $ = id => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

/* What the service last told us. */
const day = { shift: null, turns: [], rows: [] };
let ticker = null;

/* ===================================================================
 * Clock arithmetic
 *
 * Times in a payload are floor wall-clock, "HH:MM". Minutes-of-day is
 * all this screen needs - with one wrinkle: a shift may cross midnight
 * (Day runs 16:00-00:00), and "00:00" as an *end* means the end of the
 * day, not the start of it.
 * =================================================================== */

const toMin = hhmm => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));
const endMin = hhmm => (toMin(hhmm) === 0 ? 1440 : toMin(hhmm));
const pad2 = n => (n < 10 ? "0" : "") + n;

function nowMin() {
  const d = new Date();
  return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60;
}

/* Whether `at` falls inside [from, to), wrapping past midnight. */
function covers(from, to, at) {
  const f = toMin(from), t = endMin(to);
  return t > f ? (at >= f && at < t) : (at >= f || at < t);
}

/* "1h 05m" / "45m", for a span of whole minutes. */
function hm(mins) {
  const h = Math.floor(mins / 60), m = Math.round(mins % 60);
  return (h ? h + "h " + pad2(m) + "m" : m + "m");
}

/* "18:32" - minutes and seconds to a moment, the way the desk counts. */
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

function buildRows(turns) {
  const rows = [];
  turns.forEach((t, i) => {
    rows.push({ kind: "work", from: t.from, to: t.to, rigId: t.rigId,
                minutes: t.minutes, relievedBy: t.relievedBy,
                goesTo: t.theyGoTo });

    const next = turns[i + 1];
    if (!next) return;
    /* Only when there is a gap. Back-to-back turns on two rigs are a
       walk across the floor, not a break, and inventing a zero-minute
       Break row would put a rest in the day that nobody was given. */
    const gapMins = toMin(next.from) - endMin(t.to);
    if (gapMins <= 0) return;

    const kind = (t.theyGoTo === "Break" || t.theyGoTo === "Think")
      ? t.theyGoTo : "Break";
    rows.push({ kind: kind, from: t.to, to: next.from, minutes: gapMins,
                backTo: next.rigId });
  });
  return rows;
}

/* ===================================================================
 * Drawing
 * =================================================================== */

function renderShiftLine() {
  const line = $("shiftline");
  line.textContent = "";
  if (!day.shift) return;

  const when = day.shift.date
    ? new Date(day.shift.date + "T00:00:00").toLocaleDateString(
        [], { weekday: "short", day: "numeric", month: "short" })
    : "";
  const bits = [
    el("b", null, day.shift.label || "Shift"),
    el("span", null, when),
  ];
  if (day.shift.group) bits.push(el("span", null, "Group " + day.shift.group));
  if (day.shift.task) bits.push(el("b", null, day.shift.task));

  bits.forEach((b, i) => {
    if (i) line.appendChild(document.createTextNode(" · "));
    line.appendChild(b);
  });
}

function renderNow() {
  const box = $("now");
  box.textContent = "";
  const at = nowMin();
  const row = day.rows.find(r => covers(r.from, r.to, at));

  if (!row) {
    /* Before the first turn or after the last. Both are true answers and
       neither is an error, so neither gets an error's colour. */
    box.removeAttribute("data-kind");
    const first = day.rows[0], last = day.rows[day.rows.length - 1];
    const left = el("div");
    left.appendChild(el("div", "now-k", "Not on shift"));
    if (first && at < toMin(first.from)) {
      left.appendChild(el("div", "now-v", "Your shift starts at " + first.from));
    } else if (last) {
      left.appendChild(el("div", "now-v", "Your shift has finished"));
    } else {
      left.appendChild(el("div", "now-v", "Nothing scheduled"));
    }
    box.appendChild(left);
    return;
  }

  box.setAttribute("data-kind", row.kind);

  const left = el("div");
  left.appendChild(el("div", "now-k", row.kind === "work" ? "On now" : row.kind));
  const v = el("div", "now-v");
  if (row.kind === "work") {
    v.appendChild(el("span", "mono", row.rigId));
    v.appendChild(document.createTextNode(" until "));
    v.appendChild(el("span", "mono", row.to));
  } else {
    v.appendChild(document.createTextNode("Until "));
    v.appendChild(el("span", "mono", row.to));
  }
  left.appendChild(v);
  box.appendChild(left);

  const right = el("div", "now-r");
  const t = endMin(row.to);
  const left_mins = (t > at ? t : t + 1440) - at;
  right.appendChild(el("div", "now-left", mmss(left_mins)));
  if (row.kind === "work" && row.relievedBy) {
    right.appendChild(el("div", "now-sub",
      row.relievedBy + " takes over · you go to " + String(row.goesTo).toLowerCase()));
  } else if (row.backTo) {
    right.appendChild(el("div", "now-sub", "then " + row.backTo));
  }
  box.appendChild(right);
}

function renderTimeline() {
  const list = $("timeline");
  list.textContent = "";
  const at = nowMin();

  day.rows.forEach(row => {
    const li = el("li", "tl");
    li.setAttribute("data-kind", row.kind);
    if (covers(row.from, row.to, at)) li.classList.add("is-now");
    else if (endMin(row.to) <= at) li.classList.add("is-past");

    li.appendChild(el("div", "tl-t", row.from));

    const body = el("div", "tl-b");
    const what = el("div", "tl-what");
    if (row.kind === "work") {
      what.appendChild(document.createTextNode("Work "));
      what.appendChild(el("span", "mono", row.rigId));
    } else {
      what.appendChild(document.createTextNode(row.kind));
    }
    body.appendChild(what);

    const sub = el("div", "tl-sub");
    sub.appendChild(document.createTextNode(hm(row.minutes)));
    if (row.kind === "work" && row.goesTo) {
      sub.appendChild(document.createTextNode(" · then "));
      sub.appendChild(el("b", null, row.goesTo));
    }
    if (row.kind === "work" && row.relievedBy) {
      sub.appendChild(document.createTextNode(" · "));
      sub.appendChild(el("b", null, row.relievedBy));
      sub.appendChild(document.createTextNode(" relieves you"));
    }
    if (row.backTo) {
      sub.appendChild(document.createTextNode(" · back on "));
      sub.appendChild(el("b", null, row.backTo));
    }
    body.appendChild(sub);

    li.appendChild(body);
    list.appendChild(li);
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
  const right = work === 360 && brk === 60 && think === 60;
  $("budget-note").textContent = right
    ? "Six hours of work, an hour of break and an hour to think - the full shift."
    : "The full shift is 6h of work, 1h break and 1h think.";
}

function renderDay() {
  renderShiftLine();
  renderNow();
  renderTimeline();
  renderBudget();
}

/* ===================================================================
 * Loading
 * =================================================================== */

async function loadShift() {
  try {
    const r = await S.api("/api/me/shift");
    if (!r.ok) throw new Error("no shift");
    const body = await r.json();
    day.shift = body.shift || null;
    day.turns = body.turns || [];
    day.rows = buildRows(day.turns);
    $("empty").hidden = day.rows.length > 0;
    if (!day.rows.length) {
      /* Nothing pushed that covers now. The rig says Standby for the
         same reason and it is the same honest answer: this is a floor
         waiting on a schedule, not an operator with no work. */
      $("empty").textContent =
        "Nothing is scheduled for you right now. If a shift should be "
        + "running, the floor has not been pushed a schedule for it yet.";
    }
  } catch (ignored) {
    day.shift = null;
    day.turns = [];
    day.rows = [];
    $("empty").hidden = false;
    $("empty").textContent = "Could not reach the server.";
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

  $("view-signin").hidden = open || denied;
  $("view-denied").hidden = !denied;
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
    $("timeline").textContent = "";
    $("now").textContent = "";
    $("shiftline").textContent = "";
    $("budget").hidden = true;
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

/* ------------------------------------------------------------ wiring */

$("signin-form").addEventListener("submit", signIn);
$("btn-signout").addEventListener("click", signOut);
$("btn-denied-out").addEventListener("click", signOut);

(async function boot() {
  await S.probe();
  await openTheDay();
})();

/* Re-read now and then. A schedule pushed mid-shift changes this
 * screen, and an operator should not have to reload to find out. */
setInterval(() => { if (S.mayUse("operator") && S.state.account) loadShift(); }, 60000);
