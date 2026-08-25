"use strict";

/* =====================================================================
 * The whole platform app is this file: one screen, three pedals, no
 * login and no task picker. The schedule decides who is at the rig, so
 * there is nothing left for the operator to choose — only to do.
 *
 * Every duration below is in *shift seconds*. The demo runs that clock
 * faster than wall time so a 45-minute stint is watchable; on a rig
 * SPEED is 1.
 * ===================================================================== */

// ------------------------------------------------------------ schedule

/* The rig decides nothing about the schedule any more. The desk pushes it
   one - rig id, task, and every turn in the shift - and the rig reads it.
   That is what removed the login screen and the task picker: given the
   payload and the clock there is nothing left to ask the operator.

   RotationEngine is the same file the desk runs, so the rig cannot
   compute a different answer from the desk that scheduled it. */

const RE = window.RotationEngine;

/* A rig follows the wall clock. The accelerated clock is a review tool
   and has to be asked for — `?demo`, `?demo=60`, or the flag the
   single-file build bakes in.

   This was the other way round for a long time, because the very first
   prototype ran at 30x "so a 45-minute stint is watchable" and nothing
   ever revisited it. A deployed rig therefore ran an accelerated demo
   clock, which quietly makes every timestamp it reports meaningless.
   The demo is the special case; the floor is the default. */
const DEMO = (function () {
  if (window.RIG_DEMO) return 30;
  const m = /[?&]demo(?:=(\d+))?/.exec(window.location.search || "");
  return m ? (Number(m[1]) || 30) : 0;
})();

let PAYLOAD = null;            // what was pushed to this rig
let RIG_ID  = "RIG-03";        // until the payload says otherwise
let SOURCE  = "generated";     // where this rig's schedule actually came from
let live    = !DEMO;           // follow the wall clock unless this is a demo
let turnKey = null;            // "HH:MM" of the turn currently in progress

const CAMERAS = ["Front", "Wrist L", "Overhead"];

// ------------------------------------------------------------ constants

const CHECKLIST_SECS = 60;
const CHECKLIST_ITEMS = [
  "Grippers tight on both arms",
  "Camera mounts firm, nothing loose",
  "Camera assignment and orientation correct",
  "Arm CAN cables secure to the table",
];
const HOLD_MS = 900;            // press-and-hold to abandon a fault report
const RESET_URGENT_SECS = 45;   // past this the reset timer turns red
const EFFICIENCY_FLOOR = 0.60;
const EFFICIENCY_WARMUP = 300;  // don't shout before the sample means anything

const FAULTS = { left: "Gripper", middle: "Camera", right: "CAN bus" };

/* Exactly three options per level, so a level always maps onto the three
   pedals — and "other" is always the right pedal, so the operator learns
   one rule instead of three menus. */
const ISSUE_TREE = {
  id: "root", label: "Hardware issue",
  children: [
    { id: "gripper_broken",      label: "Gripper broken" },
    { id: "camera_mount_broken", label: "Camera mount" },
    { id: "other", label: "Other", children: [
      { id: "software_problem", label: "Software" },
      { id: "robot_problem",    label: "Robot" },
      { id: "other_hardware", label: "Other hardware", children: [
        { id: "gello_problem", label: "Gello" },
        { id: "cable_problem", label: "Cable" },
        { id: "unknown",       label: "Other", needsManager: true },
      ]},
    ]},
  ],
};

// ----------------------------------------------------------------- state

let S;
function boot(screen) {
  S = {
    phase: "checklist",
    t: 0,               // shift seconds since the rig came up
    phaseAt: 0,         // shift seconds when the current phase began
    stint: 0,           // which 45-minute stint we are in
    stintAt: 0,         // shift seconds when this operator took the rig
    episode: 0,
    episodeId: null,   // minted at start_episode, names the video too
    recordedSecs: 0,
    faultSecs: 0,
    downSecs: 0,
    fault: null,
    issueNode: null,
    reportedIssue: null,
    reportedAtHandover: false,
    handoverDue: false, // block ended mid-episode; hand over when it lands
    pendingSecs: 0,
    demoWarm: false,    // demo only — skips the efficiency warm-up period
    checkedAt: null,    // shift seconds when the check last passed, or null
    fromStandby: false, // the check was started early, so return to Standby
  };
  logLines.length = 0;
  lastViewKey = null;
  lastPedalKey = null;
  holding = null;
  emit("shift_check", "rig_shift_checks", "shift started, checklist raised",
       { outcome: "raised" });
  /* Only a screen the app actually has. An unrecognised hash used to
     leave the rig on an undefined phase — blank stage, three dead
     pedals, nothing to press and no way back but a reload. */
  if (screen && screen !== "checklist" && SCREENS.some(([id]) => id === screen)) seed(screen);
  /* seed() moves the clock, so the turn in progress has to be read back
     *after* it. Miss this and the next frame sees a turn that does not
     match turnKey and fires a handover that never happened. */
  syncTurnKey();
  /* Nothing is scheduled at this rig right now — before the shift, after
     it, or on a day it does not run. A start-of-shift checklist counting
     down beside a rail reading "End of shift" is a contradiction the
     operator has to unpick before they can trust anything else on the
     screen. Standby says what is true instead.

     This is the sixth boot case in docs/SESSION-RULES.md and the only one
     reachable without a disk journal; resuming a stint after a crash
     needs Phase 1. */
  if (S.phase === "checklist" && !current()) S.phase = "standby";
  render();
}

/* Every screen is addressable — `#recording`, `#rig-down` and so on, or
   the buttons in the demo drawer. Reviewing the app with someone means
   jumping straight to the screen you want to argue about, not waiting
   forty-five minutes for a handover to come round. */
const SCREENS = [
  ["standby",    "Standby"],
  ["checklist",  "Shift check"],
  ["fault-class","Report a fault"],
  ["fault-fixing","Fixing a fault"],
  ["handover",   "Handover"],
  ["recording",  "Recording"],
  ["review",     "Self review"],
  ["resetting",  "Resetting"],
  ["issue-menu", "Hardware issue"],
  ["rig-down",   "Rig down"],
];

function seed(screen) {
  // Enough plausible history that each screen shows real numbers.
  S.t = 22 * 60;
  S.stintAt = 0;
  S.episode = 14;
  S.recordedSecs = 15 * 60;
  const phase = screen.replace(/-/g, "_");
  switch (phase) {
    case "fault_class":  break;
    case "fault_fixing": S.fault = "Camera"; break;
    case "recording":    break;
    case "review":       S.pendingSecs = 47; break;
    case "issue_menu":   S.issueNode = ISSUE_TREE; break;
    case "rig_down":
      S.reportedIssue = ISSUE_TREE.children[0];
      S.reportedAtHandover = false;
      break;
  }
  S.phase = phase;
  S.phaseAt = S.t - (phase === "resetting" ? 38 : phase === "recording" ? 26 : 0);
}

/* Minutes since midnight as this rig sees the clock. Live mode follows
   the wall clock, which is what a real rig does; demo mode runs the
   accelerated shift clock so a 45-minute turn is watchable. */
function shiftStartMin() {
  if (!PAYLOAD) return 0;
  const p = PAYLOAD.shift.start.split(":");
  return Number(p[0]) * 60 + Number(p[1]);
}
function nowMin() {
  if (live) {
    const d = new Date();
    return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60;
  }
  return shiftStartMin() + S.t / 60;
}

/* The whole of "who is at this rig right now" is this one call. */
const current = () => (PAYLOAD ? RE.whoIsOn(PAYLOAD, nowMin()) : null);

const operator     = () => { const c = current(); return c ? c.turn.operator.name : "—"; };
const nextOperator = () => { const c = current(); return c && c.turn.relievedBy ? c.turn.relievedBy : "End of shift"; };
/* Where the outgoing operator goes is a fact about their day, not about
   this rig - which is exactly why the rig cannot work it out alone and
   the payload has to carry it. */
const nextPeriod   = () => { const c = current(); return c ? c.turn.theyGoTo : "End of shift"; };

/* Whatever moved the clock — a fresh boot, a jump to a screen, the wall
   clock being switched on — the rig has to agree with it about which
   turn is in progress before the next frame looks. */
function syncTurnKey() {
  const c = current();
  turnKey = c ? c.turn.from : null;
}

/* The whole path taken through the issue tree, not just where it landed.
   "Other > Other hardware > Cable" says more about a recurring fault than
   "Cable" does on its own. */
function issuePath(leaf) {
  const walk = (node, trail) => {
    if (node === leaf) return trail.concat(node.label);
    for (const c of node.children || []) {
      const hit = walk(c, trail.concat(node === ISSUE_TREE ? [] : [node.label]));
      if (hit) return hit;
    }
    return null;
  };
  return (walk(ISSUE_TREE, []) || [leaf.label]).join(" > ");
}

/* The operator whose stint just *ended*, which is not the one arriving.
   rotate() only runs once the boundary has passed, so current() already
   names the incoming operator; the outgoing one is still held by
   turnKey. Getting this wrong credits every stint to the wrong person. */
function outgoingOperator() {
  const t = PAYLOAD && turnKey ? PAYLOAD.turns.find((x) => x.from === turnKey) : null;
  return t ? t.operator.name : operator();
}

const phaseSecs  = () => S.t - S.phaseAt;
const stintLeft  = () => { const c = current(); return c ? Math.max(0, c.minutesLeft * 60) : 0; };
const assignedSecs = () => S.t - S.stintAt;

/* Data time over the time the operator has held the rig. Reset, review
   and idle all land in the denominator — that is the point of the
   number. Fault and downtime do not: a loose mount is not the
   operator's productivity problem. */
function efficiency() {
  const chargeable = assignedSecs() - S.faultSecs - S.downSecs;
  if (chargeable <= 0) return 0;
  return Math.max(0, Math.min(1, S.recordedSecs / chargeable));
}
function isBehind() {
  // Don't shout before the sample means anything — the first episode of
  // a stint always looks terrible.
  if (!S.demoWarm && assignedSecs() < EFFICIENCY_WARMUP) return false;
  return efficiency() < EFFICIENCY_FLOOR;
}
/* Never while recording. Red means "you are burning time"; during an
   episode the operator is doing exactly what we want, and turning the
   clock red mid-take pressures the demonstration itself. */
const urgencyApplies = (p) => p === "resetting" || p === "handover";

// ------------------------------------------------------------ pedal map

function pedals() {
  switch (S.phase) {
    /* Nothing is due at this rig, so there is nothing to start. The one
       pedal that does anything lets a technician sweep the floor before
       the shift: check the rig at 07:40 and nobody burns 60 seconds at
       08:00. It also keeps the rule that every screen offers a way
       forward. */
    case "standby":
      return [null, act(S.checkedAt == null ? "Check the rig" : "Check again", "start_check"), null];
    case "checklist":
      return [act("Problem", "report_fault"), act("All good", "checklist_pass"), null];
    case "fault_class":
      return [act("Gripper", "fault_left"), act("Camera", "fault_middle"), act("CAN", "fault_right")];
    case "fault_fixing":
      return [act("Back", "fault_back"), act("Fixed", "fault_fixed"),
              { ...act("Hold to cancel", "fault_cancel"), hold: true }];
    case "handover":
      return [null, act("Start", "start_episode"), act("Hardware issue", "open_issue")];
    case "recording":
      // Middle is "go" everywhere else. Leaving it inert here is how an
      // operator stops ending good takes by muscle memory.
      return [act("Discard", "discard"), null, act("Save", "save")];
    case "review":
      return [act("Usable", "score_3"), act("Good", "score_4"), act("Exemplary", "score_5")];
    case "resetting":
      return [null, act("Next episode", "start_episode"), act("Hardware issue", "open_issue")];
    case "issue_menu": {
      const c = S.issueNode.children;
      return [act(c[0].label, "issue_0"), act(c[1].label, "issue_1"), act(c[2].label, "issue_2")];
    }
    case "rig_down":
      return [act("End session", "end_session"), act("Problem solved", "problem_solved"),
              act("Bug testing", "bug_testing")];
    case "session_ended":
      return [null, act("Restart demo", "restart"), null];
  }
  return [null, null, null];
}
const act = (label, intent) => ({ label, intent });

// --------------------------------------------------------------- intents

function dispatch(intent) {
  switch (intent) {
    case "start_check":
      S.fromStandby = true;
      go("checklist");
      break;

    case "report_fault":  go("fault_class"); break;

    case "fault_left":
    case "fault_middle":
    case "fault_right": {
      S.fault = FAULTS[intent.split("_")[1]];
      go("fault_fixing");
      emit("fault_opened", "rig_shift_checks", S.fault + " reported at shift check",
           { subsystem: S.fault });
      break;
    }
    case "fault_back":
      emit("fault_reclassified", "rig_shift_checks", S.fault + " was the wrong subsystem",
           { subsystem: S.fault });
      S.fault = null; go("fault_class");
      break;
    case "fault_fixed":
      emit("fault_closed", "rig_shift_checks",
           S.fault + " fixed in " + clock(phaseSecs()) + " — not charged",
           { subsystem: S.fault, secondsCharged: 0, secondsSpent: Math.round(phaseSecs()) });
      S.fault = null; go("checklist");
      break;
    case "fault_cancel":
      // Abandoning the report deletes the fault record but keeps the
      // clock — the excluded time is charged back to the operator.
      S.faultSecs = Math.max(0, S.faultSecs - phaseSecs());
      emit("fault_cancelled", "rig_shift_checks",
           "report withdrawn — " + clock(phaseSecs()) + " charged to operator",
           { subsystem: S.fault, secondsCharged: Math.round(phaseSecs()) });
      S.fault = null; go("checklist");
      break;

    case "checklist_pass": {
      // Measured from the start of the shift, less any fault time — the
      // phase clock restarts each time the operator returns from a fault.
      S.checkedAt = S.t;
      // A check run early goes back to Standby: the rig is ready, but
      // nobody is due at it yet. Decided before the event is filed, so
      // the two cannot disagree about which kind of check this was.
      const early = S.fromStandby && !current();
      emit("shift_check", "rig_shift_checks", "checklist passed in " + clock(S.t - S.faultSecs),
           { outcome: early ? "passed_early" : "passed",
             secondsTaken: Math.round(S.t - S.faultSecs) });
      S.fromStandby = false;
      go(early ? "standby" : "handover");
      break;
    }

    case "start_episode":
      S.episode += 1;
      /* Minted before RODA-RS would be told anything, so the video
         directory on disk and the event that reports it name the same
         take. No lookup table, no reconciliation, and an id that exists
         even for a take the recorder failed to start. */
      S.episodeId = uuid();
      go("recording");
      break;

    case "discard":
      emit("episode_discarded", "episodes", "episode " + S.episode + " discarded after " + clock(phaseSecs()),
           { episodeId: S.episodeId, durationSecs: Math.round(phaseSecs()) });
      S.episode -= 1;
      afterEpisode();
      break;

    case "save":
      S.pendingSecs = phaseSecs();
      go("review");
      break;

    case "score_3": case "score_4": case "score_5": {
      const score = Number(intent.slice(-1));
      S.recordedSecs += S.pendingSecs || 0;
      emit("episode_saved", "episodes",
           "episode " + S.episode + " · " + clock(S.pendingSecs || 0) + " · scored " + score + "/5",
           { episodeId: S.episodeId, durationSecs: Math.round(S.pendingSecs || 0), score: score });
      queueVideo(S.episodeId);
      S.pendingSecs = 0;
      afterEpisode();
      break;
    }

    case "open_issue":
      S.reportedAtHandover = S.phase === "handover";
      S.issueNode = ISSUE_TREE;
      go("issue_menu");
      break;

    case "issue_0": case "issue_1": case "issue_2": {
      const child = S.issueNode.children[Number(intent.slice(-1))];
      if (child.children) { S.issueNode = child; render(); break; }
      S.reportedIssue = child;
      go("rig_down");
      emit("rig_down", "rig_downtime_events",
           child.label + (child.needsManager ? " — manager needed" : "") +
           (S.reportedAtHandover ? " · found at handover, charged to previous operator" : ""),
           { issue: child.label,
             issuePath: issuePath(child),
             needsManager: !!child.needsManager,
             chargedTo: S.reportedAtHandover ? "previous_operator" : "rig" });
      break;
    }

    case "problem_solved":
      // downSecs already accrued frame by frame while the rig was down.
      emit("rig_up", "rig_downtime_events", "resolved after " + clock(phaseSecs()) + " down",
           { downSecs: Math.round(phaseSecs()) });
      S.reportedIssue = null;
      go("resetting");
      break;

    case "bug_testing":
      toast("Bug testing — reserved for technicians, undefined for now");
      break;

    case "end_session":
      emit("session_ended", "sessions", "operator ended session with the rig down",
           { endedBy: "operator" });
      go("session_ended");
      break;

    case "restart": boot(); break;
  }
  render();
}

function afterEpisode() {
  // The workspace has to be reset by hand, so the next episode never
  // starts on its own — but the clock starts the moment this one ends.
  if (S.handoverDue) { rotate(); return; }
  go("resetting");
}

function go(phase) { S.phase = phase; S.phaseAt = S.t; }

function rotate() {
  /* The sentence keeps the percentage because the operator reads it.
     The data carries the four numbers it was made of, so the formula can
     be corrected in six months and every shift already filed recomputes
     correctly. A stored ratio cannot be. */
  emit("stint_ended", "rig_productivity_blocks",
       outgoingOperator() + " · " + S.episode + " episodes · " + pct(efficiency()) + " efficiency",
       { episodes: S.episode,
         recordedSecs: Math.round(S.recordedSecs),
         assignedSecs: Math.round(assignedSecs()),
         faultSecs: Math.round(S.faultSecs),
         downSecs: Math.round(S.downSecs) });
  S.stint += 1;
  S.stintAt = S.t;
  syncTurnKey();
  S.episode = 0;
  S.recordedSecs = 0;
  S.faultSecs = 0;
  S.downSecs = 0;
  S.demoWarm = false;
  S.handoverDue = false;
  go("handover");
}

// ----------------------------------------------------------------- clock

const SPEEDS = [1, 12, 30, 60];
let speed = DEMO || 1;
let last = null;

function tick(now) {
  /* start() is async: on a real rig the payload arrives over the network,
     and the first frame routinely beats it. Rescheduling is the last line
     of tick, so a throw here does not just drop one frame — it kills the
     loop for good and freezes the rig on a blank screen. Wait it out, and
     leave `last` alone so the first real frame starts from a standstill. */
  if (!S) { requestAnimationFrame(tick); return; }
  if (last === null) last = now;
  const dt = Math.min(0.25, (now - last) / 1000) * speed;
  last = now;

  if (S.phase !== "session_ended") {
    S.t += dt;
    if (S.phase === "fault_fixing") S.faultSecs += dt;
    if (S.phase === "rig_down")     S.downSecs += dt;

    /* Standby ends when the schedule says somebody is due. A rig that
       was checked early goes straight to the handover; one that was not
       still owes its check. */
    if (S.phase === "standby" && current()) go(S.checkedAt == null ? "checklist" : "handover");

    // The turn boundary never interrupts a take. If the operator is
    // mid-episode the handover waits until the episode lands.
    const c = current();
    if (c && c.turn.from !== turnKey && !S.handoverDue) {
      const busy = S.phase === "recording" || S.phase === "review";
      if (busy) S.handoverDue = true;
      else if (S.phase !== "rig_down" && S.phase !== "issue_menu" &&
               S.phase !== "fault_fixing" && S.phase !== "fault_class") rotate();
    }
  }
  render();
  paintHold();
  requestAnimationFrame(tick);
}

// ---------------------------------------------------------------- render

const $stage     = document.getElementById("stage");
const $pedals    = document.getElementById("pedals");
const $app       = document.getElementById("app");
const $railBlock = document.getElementById("rail-block");
const $cellBlock = document.getElementById("cell-block");
const $railNext  = document.getElementById("rail-next");
const $railThen  = document.getElementById("rail-then");

let lastViewKey = null;
let lastPedalKey = null;

/* The stage is rebuilt only when what it *says* changes; the numbers on
   it are patched in place every frame. Rebuilding at 60 Hz blanked the
   camera canvases and wiped the hold-to-cancel fill mid-press. */
function render() {
  // A web font landing, or a resize, can call this before the payload has
  // arrived and boot() has built S. Nothing to draw yet.
  if (!S) return;
  const behind = isBehind() && urgencyApplies(S.phase);

  // State hooks only — nothing paints off these any more. Reinstating the
  // wall colour is a matter of giving #app.hail / #app.alarm a background.
  $app.className =
    S.phase === "handover" ? "hail" :
    (S.phase === "fault_fixing" || S.phase === "rig_down") ? "alarm" : "";

  /* When nothing is scheduled, none of the three rail cells has an
     answer: no block is running, nobody is being relieved, nobody is
     going anywhere. Leaving the live values there is what produced the
     contradiction Standby exists to remove - a rail reading "End of
     shift" above a rig simply waiting for one.

     Keyed on the schedule, not the phase: an early check runs outside
     the shift too, and the rail was snapping back to "End of shift" the
     moment the operator pressed the pedal. */
  if (!current()) {
    const first = PAYLOAD && PAYLOAD.turns.length ? PAYLOAD.turns[0] : null;
    $railBlock.textContent = "—";
    $cellBlock.classList.toggle("due", false);
    $railNext.textContent = first ? first.operator.name : "—";
    $railThen.textContent = "—";
  } else {
    const due = S.handoverDue || (stintLeft() < 60 && stintLeft() > 0);
    $railBlock.textContent = S.handoverDue ? "Handover due" : clock(stintLeft());
    $cellBlock.classList.toggle("due", due);
    $railNext.textContent = nextOperator();
    $railThen.textContent = nextPeriod();
  }

  /* A name is part of what the stage *says*, on the two screens that
     show one: rotating from one handover into the next leaves every
     other term unchanged, and without this the screen keeps the old
     name up while the rail has already moved on.
     It is deliberately *not* in the key anywhere else. Recording shows
     no name, and the clock crossing a turn boundary mid-take would
     otherwise tear the stage down — and the camera panes with it — for
     a change the operator cannot see. */
  const who = S.phase === "handover"  ? operator()
            : S.phase === "resetting" ? nextOperator()
            : "";
  const key = [S.phase, S.issueNode && S.issueNode.id, S.episode, S.fault,
               S.reportedIssue && S.reportedIssue.id, S.handoverDue, behind,
               who].join("|");
  if (key !== lastViewKey) {
    lastViewKey = key;
    $stage.className = "stage" + (S.phase === "recording" ? " tight" : "");
    $stage.innerHTML = view(behind);
    fitAll();
  }
  refresh(behind);
  if (S.phase === "recording") mountCameras();

  const pedalKey = pedals().map((a) => (a ? a.label + (a.hold ? "!" : "") : "-")).join("|");
  if (pedalKey !== lastPedalKey) { lastPedalKey = pedalKey; paintPedals(); }
}

const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

/* Scale every .fit element to the width of the stage, capped so a short
   name does not grow taller than the room left for it. Measured once per
   rebuild; tabular figures keep their width as the digits tick over. */
function fitAll() {
  const els = $stage.querySelectorAll(".fit");
  if (!els.length) return;
  const capPx = $stage.clientHeight * (els.length > 1 ? 0.4 : 0.6);
  const range = document.createRange();
  els.forEach((el) => {
    el.style.fontSize = "100px";
    const avail = el.clientWidth;
    // scrollWidth reports clientWidth for an overflow:visible block, so it
    // cannot measure text wider than its box. A Range can.
    range.selectNodeContents(el);
    const wide = range.getBoundingClientRect().width;
    if (!wide || !avail) return;
    el.style.fontSize = Math.min(capPx, (100 * avail) / wide * 0.99) + "px";
  });
}

function refresh(behind) {
  const timer = document.getElementById("v-timer");
  switch (S.phase) {
    case "checklist": {
      const left = Math.max(0, CHECKLIST_SECS - phaseSecs());
      if (timer) { timer.textContent = clock(left); timer.classList.toggle("alarm", left === 0); }
      break;
    }
    case "fault_fixing":
    case "rig_down":
      if (timer) timer.textContent = clock(phaseSecs());
      break;
    case "resetting":
      if (timer) {
        timer.textContent = clock(phaseSecs());
        timer.classList.toggle("alarm", phaseSecs() > RESET_URGENT_SECS || behind);
      }
      break;
    case "recording": {
      setText("v-take", clock(phaseSecs()));
      setText("v-rec", clock(S.recordedSecs));
      setText("v-eff", pct(efficiency()));
      const m = document.getElementById("m-eff");
      if (m) m.classList.toggle("behind", isBehind());
      break;
    }
  }
}

function view(behind) {
  switch (S.phase) {
    /* A sign, read from across the room. Everything an operator walking
       up needs to know that the rig is theirs and ready, and nothing to
       do about it. */
    case "standby": {
      const p = PAYLOAD;
      const first = p && p.turns.length ? p.turns[0] : null;
      return `
        <p class="kicker">${RIG_ID} — standby</p>
        <h1 class="headline">${p ? p.shift.label + " · " + p.shift.start : "No shift scheduled"}</h1>
        ${first ? `<p class="lede standby-who">${first.operator.name}</p>` : ""}
        <p class="sub">${S.checkedAt == null
          ? "Not checked yet. Middle pedal checks the rig now, so nobody spends the first minute of the shift on it."
          : "Rig checked · all four passed."}</p>`;
    }

    case "checklist": {
      const left = Math.max(0, CHECKLIST_SECS - phaseSecs());
      return `
        <div class="split">
          <div>
            <p class="kicker">${RIG_ID} — start of shift</p>
            <h1 class="headline">Check the rig</h1>
          </div>
          <p class="figure" id="v-timer" style="font-size:clamp(3rem,11vw,8rem)">${clock(left)}</p>
        </div>
        <div class="checks">${CHECKLIST_ITEMS.map((i) => `<p class="check"><i></i>${i}</p>`).join("")}</div>`;
    }

    case "fault_class":
      return `
        <p class="kicker">${RIG_ID} — shift check</p>
        <h1 class="headline">What is wrong?</h1>
        <p class="sub">Pick the subsystem. The time you spend fixing it is not charged to you.</p>`;

    case "fault_fixing":
      return `
        <p class="kicker">Fixing — not counted against you</p>
        <h1 class="name" style="font-size:clamp(3rem,13vw,10rem)">${S.fault}</h1>
        <p class="figure fit" id="v-timer">${clock(phaseSecs())}</p>
        <p class="sub">Logged so a fault that keeps coming back gets fixed properly.</p>`;

    case "handover":
      return `
        <p class="kicker">${RIG_ID} — your rig now</p>
        <h1 class="name fit">${operator()}</h1>
        <p class="lede">Press the middle pedal to start</p>`;

    case "recording":
      return `
        <div class="cams">${CAMERAS.map((c, i) => `
          <figure class="cam"><canvas data-cam="${i}" width="320" height="200"></canvas>
            <figcaption><span>${c}</span><span class="rec">Rec</span></figcaption>
          </figure>`).join("")}
        </div>
        <div class="metrics">
          <span class="metric lead"><em class="kicker">Episode</em><b>${S.episode}</b></span>
          <span class="metric"><em class="kicker">This take</em><b id="v-take">${clock(phaseSecs())}</b></span>
          <span class="metric"><em class="kicker">Recorded</em><b id="v-rec">${clock(S.recordedSecs)}</b></span>
          <span class="metric" id="m-eff"><em class="kicker">Efficiency</em><b id="v-eff">${pct(efficiency())}</b></span>
        </div>`;

    case "review":
      // The three choices are already along the bottom of every screen.
      // Repeating them as cards is the opposite of what this app is for —
      // an operator does this hundreds of times a shift in under five
      // seconds, and reads none of it after the first day.
      return `
        <p class="kicker">Episode ${S.episode} — score it</p>
        <p class="figure fit">${clock(S.pendingSecs || 0)}</p>
        <p class="sub">Exemplary is about one take in ten. Anything worse than usable
           should have been discarded during the take.</p>`;

    case "resetting": {
      const urgent = phaseSecs() > RESET_URGENT_SECS || behind;
      return `
        <p class="kicker">Reset the workspace</p>
        <p class="figure fit${urgent ? " alarm" : ""}" id="v-timer">${clock(phaseSecs())}</p>
        ${S.handoverDue
          ? `<p class="lede">${nextOperator()} is waiting — reset and step away</p>`
          : `<p class="lede">Middle pedal the moment it is ready</p>`}`;
    }

    case "issue_menu":
      return `
        <p class="kicker">${S.issueNode === ISSUE_TREE ? "Hardware issue" : "Hardware issue — " + S.issueNode.label}</p>
        <h1 class="headline">${S.issueNode === ISSUE_TREE ? "What broke?" : "Narrow it down"}</h1>
        <p class="sub">Keep going right until it fits.</p>`;

    case "rig_down":
      return `
        <p class="kicker">Rig down — your time has stopped</p>
        <h1 class="name" style="font-size:clamp(2.6rem,10vw,7rem)">${S.reportedIssue.label}</h1>
        <p class="figure fit" id="v-timer">${clock(phaseSecs())}</p>
        <p class="sub">${S.reportedIssue.needsManager
          ? "A manager has to look at this one."
          : S.reportedAtHandover
            ? "Found at handover — charged to the previous operator, not you."
            : "Downtime is charged to the rig, not to you."}</p>`;

    case "session_ended":
      return `
        <p class="kicker">${RIG_ID}</p>
        <h1 class="headline">Session ended</h1>
        <p class="sub">Downtime and episodes are queued for upload.</p>`;
  }
  return "";
}

// --------------------------------------------------------------- pedal UI

let holding = null;    // { intent, startedAt }

function paintPedals() {
  const map = pedals();
  const names = ["Left", "Middle", "Right"];
  $pedals.innerHTML = map.map((a, i) => `
    <button class="pedal${a ? "" : " inert"}" data-i="${i}" ${a ? "" : "disabled"}>
      ${a && a.hold ? '<span class="hold"></span>' : ""}
      <em>${names[i]}</em>
      <strong>${a ? a.label : "—"}</strong>
    </button>`).join("");
}

function paintHold() {
  if (!holding) return;
  const bar = $pedals.querySelector(".hold");
  if (!bar) return;
  const p = Math.min(1, (performance.now() - holding.startedAt) / HOLD_MS);
  bar.style.width = (p * 100) + "%";
  if (p >= 1) { const it = holding.intent; holding = null; dispatch(it); }
}

function press(i) {
  const a = pedals()[i];
  if (!a) return;
  const btn = $pedals.querySelector(`[data-i="${i}"]`);
  if (a.hold) { holding = { intent: a.intent, startedAt: performance.now() }; return; }
  if (btn) { btn.classList.add("hit"); setTimeout(() => btn.classList.remove("hit"), 110); }
  dispatch(a.intent);
}
function release(i) {
  const a = pedals()[i];
  if (a && a.hold && holding) {
    holding = null;
    const bar = $pedals.querySelector(".hold");
    if (bar) bar.style.width = "0";
  }
}

/* A tap fires on `click` and a hold is tracked with mousedown/up, rather
   than everything hanging off pointer events — synthetic and assistive
   clicks reliably produce `click`, and they do not always produce
   `pointerdown`. On the rig the pedals arrive as key codes anyway. */
function pedalAt(target) {
  const b = target.closest && target.closest(".pedal");
  if (!b || b.disabled) return null;
  const i = Number(b.dataset.i);
  const a = pedals()[i];
  return a ? { i, a, btn: b } : null;
}

$pedals.addEventListener("click", (e) => {
  const hit = pedalAt(e.target);
  if (hit && !hit.a.hold) press(hit.i);
});
$pedals.addEventListener("mousedown", (e) => {
  const hit = pedalAt(e.target);
  if (hit && hit.a.hold) press(hit.i);
});
$pedals.addEventListener("mouseup", () => { if (holding) release(2); });
$pedals.addEventListener("mouseleave", () => { if (holding) release(2); });
$pedals.addEventListener("touchstart", (e) => {
  const hit = pedalAt(e.target);
  if (hit && hit.a.hold) { e.preventDefault(); press(hit.i); }
}, { passive: false });
$pedals.addEventListener("touchend", () => { if (holding) release(2); });

const KEYS = { "1": 0, "2": 1, "3": 2 };
addEventListener("keydown", (e) => {
  if (e.repeat) return;
  if (e.key in KEYS) { e.preventDefault(); press(KEYS[e.key]); return; }
  const k = e.key.toLowerCase();
  if (k === "h") {
    const c = current();
    if (c) S.t += c.minutesLeft * 60;
    toast("Jumped to the handover");
  }
  if (k === "e") { S.demoWarm = true; toast("Efficiency warm-up skipped — the floor is live now"); }
  if (k === "r") { boot(); toast("Shift restarted"); }
  if (k === "d") toggleDrawer();
  if (k === "?" || k === "/") toggleDrawer();
});
addEventListener("keyup", (e) => { if (e.key in KEYS) release(KEYS[e.key]); });

// ----------------------------------------------------------------- cameras

/* Not video — a cheap moving field so the panes read as live without
   pretending to be a feed the demo does not have. */
const camState = [0, 1, 2].map((i) => ({ seed: i * 37 }));
function mountCameras() {
  $stage.querySelectorAll("canvas[data-cam]").forEach((cv) => {
    const ctx = cv.getContext("2d");
    const i = Number(cv.dataset.cam);
    const t = S.t * 0.6 + camState[i].seed;
    const g = ctx.createLinearGradient(0, 0, cv.width, cv.height);
    g.addColorStop(0, "#0e1013");
    g.addColorStop(1, "#1b2027");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, cv.width, cv.height);
    for (let n = 0; n < 3; n++) {
      const x = cv.width  * (0.5 + 0.32 * Math.sin(t * 0.7 + n * 2.1));
      const y = cv.height * (0.5 + 0.26 * Math.cos(t * 0.5 + n * 1.3));
      const r = 26 + n * 16;
      const rg = ctx.createRadialGradient(x, y, 0, x, y, r);
      rg.addColorStop(0, `rgba(190,205,220,${0.30 - n * 0.07})`);
      rg.addColorStop(1, "rgba(190,205,220,0)");
      ctx.fillStyle = rg;
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    }
  });
}

// ------------------------------------------------------------------- log

const logLines = [];

/* Every event the rig sends back, in the shape packages/schema/event.js
   validates. Two things this has to get right:

   The id is minted here, at the moment the pedal was pressed, before
   anything downstream is told. That is what lets the uploader resend a
   batch blindly, forever, without ever asking whether the last attempt
   landed - ingest dedupes on (rigId, eventId).

   Everything outside `data` comes from the schedule the desk pushed, not
   from the rig's own reckoning. The payload is the dimension table for
   the whole event stream, so a rig that made up its own shift date would
   quietly orphan every row it filed. */
const envelopes = [];
let seq = 0;

function uuid() {
  if (typeof crypto === "object" && crypto.randomUUID) return crypto.randomUUID();
  // Older webviews: still a v4-shaped id, still unique enough to dedupe on.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function envelope(event, bucket, data) {
  const c = current();
  return {
    eventId: uuid(),
    seq: seq++,
    at: new Date().toISOString(),
    rigId: RIG_ID,
    shiftDate: PAYLOAD ? PAYLOAD.shift.date : null,
    shiftLabel: PAYLOAD ? PAYLOAD.shift.label : null,
    // Null on standby: nothing is scheduled, but the rig still reports.
    turnFrom: c ? c.turn.from : null,
    operatorId: c ? c.turn.operator.id : null,
    bucket: bucket,
    event: event,
    data: data || {},
  };
}

/* `detail` is the sentence on the operator's drawer log and stays exactly
   as it was - it is for the person standing at the rig, not the database,
   and it is good. `data` is the parallel machine-readable half. */
function emit(event, bucket, detail, data) {
  const e = envelope(event, bucket, data);
  envelopes.push(e);
  // Held until the server acknowledges it. A rig with no network keeps
  // working and keeps filing; the outbox grows and drains later.
  outbox.push(e);
  /* Journal before network, always. The uploader runs on a timer and is
     never the thing holding an event; this is. */
  onJournal(() => journal.appendEvent(e), "an event");
  logLines.push({ at: clock(S ? S.t : 0), event, bucket, detail });
  const el = document.getElementById("log");
  el.innerHTML = logLines.slice(-40).map((l) =>
    `<p><time>${l.at}</time><b>${l.event} <span class="bucket">→ ${l.bucket}</span><br>${l.detail}</b></p>`).join("");
}

/* Everything the rig has ever filed this session. Exposed so the headless
   tests can assert the shape the backend receives, rather than the
   sentence the operator reads. */
window.rigEvents = () => envelopes.slice();

// ------------------------------------------------------------ credentials

/* The machine authenticates; the operator never does.

   That is the whole of the rig's auth story and it is what lets this
   screen keep having no login. Ansible places a token on the machine
   beside `/etc/rig/id`, the page is served with it, and every call the
   rig makes carries it. Nobody standing at the rig types anything.

   Only same-origin calls get the header. A presigned upload URL points
   at the object store, is already signed, and is not ours to add
   credentials to - sending a bearer token to somebody else's bucket is
   how a token ends up in somebody else's logs. */

const RIG_TOKEN = (typeof window !== "undefined" && window.RIG_TOKEN) || "";

function ours(url) {
  const u = String(url);
  if (u.startsWith("/")) return true;                    // rooted at us
  if (!/^[a-z][a-z0-9+.-]*:/i.test(u)) return true;      // no scheme: relative
  /* Absolute, with a scheme. Ours only if it names our own origin - and
     if we cannot tell what that is, the answer is no. An earlier version
     fell back to `location.origin || ""`, and startsWith("") is true of
     every string, so an unknown origin quietly sent the floor's token to
     every host the rig was pointed at. */
  const origin = (typeof location === "object" && location.origin) || null;
  return !!origin && (u === origin || u.startsWith(origin + "/"));
}

function withToken(url, init) {
  if (!RIG_TOKEN || !ours(url)) return init;
  const opts = Object.assign({}, init);
  opts.headers = Object.assign({}, opts.headers, {
    Authorization: "Bearer " + RIG_TOKEN,
  });
  return opts;
}

/* Everything the rig sends goes through here, so a route added later
   cannot quietly be the one that forgot to authenticate. */
function api(url, init) {
  return fetch(url, withToken(url, init));
}

// ----------------------------------------------------------- the journal

/* The rig tells the operator, on the session-ended screen:

       Downtime and episodes are queued for upload.

   Until this existed that sentence was aspirational. The outbox was an
   array, so a reload, a crash, a closed lid or a browser deciding to
   discard a background tab took every event since boot with it - and the
   screen said they were safe the whole time. A promise on a screen that
   the code does not keep is worse than no promise.

   The plan writes this as `/var/lib/rig/journal.ndjson`, flushed before
   the UI advances, with the uploader a separate process reading from a
   checkpoint. That is the Tauri shape and it is the right one. This is
   the same property in the medium a browser actually has: IndexedDB,
   which survives a reload, holds Blobs, and is measured in gigabytes
   rather than the five megabytes of localStorage.

   One honest difference. A browser cannot write synchronously, so "on
   disk before the UI advances" becomes "handed to the store before the
   network is touched". A hard power cut in the few milliseconds before
   that transaction commits can still lose the last event. Closing that
   window needs a synchronous write, which needs Tauri. Everything larger
   than that window - which is every failure an operator will actually
   have - is covered.

   `window.RIG_JOURNAL` is the seam. Tauri injects a disk-backed one and
   nothing below this line changes; the tests inject one that survives a
   remount, which is what a reload is. */

const memoryJournal = {
  durable: false,
  async load() { return { events: [], videos: [] }; },
  async appendEvent() {},
  async forgetEvents() {},
  async putVideo() {},
  async forgetVideo() {},
};

function browserJournal() {
  const NAME = "rigs-rig-journal";
  const VERSION = 1;
  let opening = null;

  function db() {
    if (opening) return opening;
    opening = new Promise((resolve, reject) => {
      const req = indexedDB.open(NAME, VERSION);
      req.onupgradeneeded = () => {
        const d = req.result;
        /* Keyed on what already makes each row unique. eventId is what
           the server dedupes on, so re-appending the same event cannot
           produce two of it here either. */
        if (!d.objectStoreNames.contains("events")) {
          d.createObjectStore("events", { keyPath: "eventId" })
           .createIndex("rig", "rigId");
        }
        if (!d.objectStoreNames.contains("videos")) {
          d.createObjectStore("videos", { keyPath: "key" })
           .createIndex("rig", "rigId");
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return opening;
  }

  /* One transaction, resolved on complete rather than on the request -
     a request that succeeded inside a transaction that then aborted did
     not happen, and treating it as done would drop the only copy. */
  function tx(store, mode, fn) {
    return db().then((d) => new Promise((resolve, reject) => {
      const t = d.transaction(store, mode);
      let value;
      const req = fn(t.objectStore(store));
      if (req) req.onsuccess = () => { value = req.result; };
      t.oncomplete = () => resolve(value);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error || new Error("transaction aborted"));
    }));
  }

  return {
    durable: true,
    async load(rigId) {
      const events = (await tx("events", "readonly",
        (s) => s.index("rig").getAll(rigId))) || [];
      const videos = (await tx("videos", "readonly",
        (s) => s.index("rig").getAll(rigId))) || [];
      /* getAll comes back in key order, which is eventId - meaningless.
         The uploader sends oldest first, so put them back in the order
         they were filed. */
      events.sort((a, b) => a.seq - b.seq);
      return { events, videos };
    },
    appendEvent(ev) { return tx("events", "readwrite", (s) => s.put(ev)); },
    forgetEvents(ids) {
      return tx("events", "readwrite", (s) => { ids.forEach((id) => s.delete(id)); });
    },
    putVideo(item) { return tx("videos", "readwrite", (s) => s.put(item)); },
    forgetVideo(key) { return tx("videos", "readwrite", (s) => s.delete(key)); },
  };
}

let journal = memoryJournal;
if (typeof window !== "undefined" && window.RIG_JOURNAL) {
  journal = window.RIG_JOURNAL;
} else if (typeof indexedDB !== "undefined" && indexedDB) {
  try { journal = browserJournal(); } catch (e) { journal = memoryJournal; }
}

/* Writes are serialised. Two appends racing inside IndexedDB is safe, but
   an append racing the forget that follows an acknowledgement is not: the
   delete can land before the put and leave a row nobody will ever send. */
let journalChain = Promise.resolve();
let journalBroken = false;

function onJournal(fn, what) {
  journalChain = journalChain.then(fn).catch((e) => {
    /* A journal that has stopped working must not stop the rig. The
       operator keeps working and the events stay in memory; what changes
       is that the screen may no longer promise they are safe. */
    if (!journalBroken) {
      journalBroken = true;
      emitLog("journal_failed", "sessions",
              what + " could not be written: " + String((e && e.message) || e));
    }
  });
  return journalChain;
}

/* Read back whatever the last boot did not finish sending. */
async function recoverJournal() {
  let held;
  try {
    held = await journal.load(RIG_ID);
  } catch (e) {
    journalBroken = true;
    return { events: 0, videos: 0 };
  }
  const events = (held && held.events) || [];
  const videos = (held && held.videos) || [];

  if (events.length) {
    /* In front of anything filed this boot, because they are older. */
    outbox.unshift(...events);
    /* seq must not go backwards or repeat: it is the cursor the server
       reads. alignSeq only ever raises it, so doing this first is safe. */
    const highest = events.reduce((m, e) => Math.max(m, e.seq || 0), -1);
    if (highest + 1 > seq) seq = highest + 1;
  }
  videos.forEach((v) => {
    if (v && v.blob) videoQueue.push({ episodeId: v.episodeId, camera: v.camera, blob: v.blob });
  });

  /* Reported, not logged. boot() clears the drawer log and then files
     the shift check, so a line written here is wiped a moment later and
     the operator never sees that there is a backlog at all. */
  return { events: events.length, videos: videos.length };
}

/* For the tests and the drawer. */
window.rigJournal = () => ({ durable: !!journal.durable, broken: journalBroken });

// ---------------------------------------------------------- the uploader

/* Events reach the server from here, and the whole design is one idea:
   the rig keeps them until the server says it has them, and never stops
   trying. Ingest dedupes on (rigId, eventId), so re-sending a batch is
   free and asking "did that land?" is unnecessary. That is what keeps
   this short.

   Nothing here blocks the operator. A rig with no network keeps working
   and keeps filing; the outbox simply grows until the network returns. */

const UPLOAD_BATCH = 100;
const UPLOAD_IDLE_MS = 3000;     // how often to look when there is nothing wrong
const UPLOAD_BACKOFF_MS = 1000;  // doubled per consecutive failure
const UPLOAD_BACKOFF_MAX = 60000;

const outbox = [];               // filed, not yet acknowledged
const rejected = [];             // refused by the server: a bug, not a retry
let uploadFailures = 0;
let uploading = false;
let uploadState = "idle";        // idle | sending | offline | rejected
let uploadError = null;          // why the last attempt failed, for the drawer

/* A rig that restarts begins counting from zero again, which would make
   `seq` go backwards - and seq is the cursor the server reads to know
   what it already holds. So on boot the rig asks what the server has and
   carries on from there. This is the cursor protocol used for the reason
   it exists, rather than only after a network drop. */
async function alignSeq() {
  try {
    const r = await api("/api/rigs/" + encodeURIComponent(RIG_ID) + "/cursor",
                        { cache: "no-store" });
    if (!r.ok) return false;
    const { seq: held } = await r.json();
    if (typeof held === "number" && held + 1 > seq) seq = held + 1;
    return true;
  } catch (e) {
    return false;   // no server: seq starts at 0 and aligns when one appears
  }
}

async function flush() {
  if (uploading || !outbox.length) return;
  uploading = true;
  const batch = outbox.slice(0, UPLOAD_BATCH);
  try {
    const r = await api("/api/rigs/" + encodeURIComponent(RIG_ID) + "/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: batch }),
    });

    if (r.ok) {
      outbox.splice(0, batch.length);
      /* Only now. The server has them, so this rig no longer needs to. */
      onJournal(() => journal.forgetEvents(batch.map((e) => e.eventId)),
                "an acknowledgement");
      uploadFailures = 0;
      uploadState = outbox.length ? "sending" : "idle";
    } else if (r.status === 422) {
      /* The server refused the batch outright. Retrying will refuse it
         again forever and the outbox would never drain, so the batch is
         set aside rather than dropped: the floor keeps working, the
         events are still in memory, and the drawer says so. A rig filing
         events its own schema rejects is a bug in the rig, and it should
         be loud rather than silent. */
      rejected.push(...outbox.splice(0, batch.length));
      /* Dropped from the journal too. A batch the server refuses will be
         refused again on every boot for ever, and a journal that reloads
         events nobody will accept never drains. They stay in memory and
         in the log, which is where a bug in this rig belongs. */
      onJournal(() => journal.forgetEvents(batch.map((e) => e.eventId)),
                "a rejection");
      uploadState = "rejected";
      emitLog("upload_rejected", "sessions",
              batch.length + " events refused by the server — see the log");
    } else {
      throw new Error("HTTP " + r.status);
    }
  } catch (e) {
    uploadFailures += 1;
    uploadState = "offline";
    /* Kept and shown. A rig that cannot reach the server is a rig whose
       events nobody has, and "offline" without a reason is the kind of
       thing that costs an afternoon on a floor. */
    uploadError = String((e && e.message) || e);
    if (uploadFailures === 1) {
      emitLog("upload_failed", "sessions", uploadError);
    }
  } finally {
    uploading = false;
    showMode();
  }
}

function uploadDelay() {
  if (!uploadFailures) return UPLOAD_IDLE_MS;
  return Math.min(UPLOAD_BACKOFF_MS * Math.pow(2, uploadFailures - 1), UPLOAD_BACKOFF_MAX);
}

let uploadTimer = null;
function startUploader() {
  if (uploadTimer) return;
  const tick = async () => {
    await flush();
    uploadTimer = setTimeout(tick, uploadDelay());
  };
  uploadTimer = setTimeout(tick, UPLOAD_IDLE_MS);
}

/* For the tests and the drawer: what the uploader is holding. */
window.rigOutbox = () => ({
  queued: outbox.length,
  rejected: rejected.length,
  failures: uploadFailures,
  state: uploadState,
  error: uploadError,
  nextSeq: seq,
});
window.rigFlush = flush;

// -------------------------------------------------------- the video path

/* Three steps and one rule, the same four lines the server is written to:

     1. the rig finishes a take
     2. it asks where to put the bytes
     3. it puts them
     4. it reports the checksum, and the server verifies what landed

   Only then may the rig let go of its copy.

   Step 4 is the whole design. Everything before it is a retry - safe to
   repeat, safe to interrupt, safe to run twice - and this is the one
   place that must never be optimistic, because past it the only other
   copy is gone.

   There is no camera behind a browser tab, so `videoSource` is the seam:
   it is handed an episode and a camera and returns a Blob, or null when
   there is nothing to send. A page with no source queues nothing, which
   is why running the demo does not post fabricated bytes and does not
   pollute what /floor/video measures. Tauri and RODA-RS supply a real one
   later, and nothing below this line changes when they do. */

const VIDEO_BACKOFF_MS = 2000;
const VIDEO_BACKOFF_MAX = 60000;

let videoSource = null;
const videoQueue = [];           // { episodeId, camera, blob }
const videoSent = [];            // keys the server has released
let videoSending = false;
let videoFailures = 0;
let videoState = "idle";         // idle | sending | waiting | offline
let videoError = null;

/* Cameras are named for a person on screen ("Wrist L") and for a path in
   the store ("wrist-l"). The key has to survive being a filename. */
function camSlug(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

async function sha256Hex(buf) {
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

/* Called when a take is saved. Queued per camera, because that is how the
   store keys them and how a partial upload stays partial rather than
   costing the whole episode. */
function queueVideo(episodeId) {
  if (!videoSource || !episodeId) return;
  CAMERAS.forEach((cam) => {
    const blob = videoSource(episodeId, camSlug(cam));
    if (!blob) return;
    const camera = camSlug(cam);
    videoQueue.push({ episodeId, camera, blob });
    /* The take outlives the tab. Video is the one thing here with no
       second copy anywhere until the server confirms it. */
    onJournal(() => journal.putVideo({
      key: episodeId + "/" + camera, rigId: RIG_ID, episodeId, camera, blob,
    }), "a video");
  });
}

async function sendVideo(item) {
  const base = "/api/rigs/" + encodeURIComponent(RIG_ID) +
               "/episodes/" + encodeURIComponent(item.episodeId);
  const json = { "Content-Type": "application/json" };

  const asked = await api(base + "/video:presign", {
    method: "POST", headers: json,
    body: JSON.stringify({ camera: item.camera }),
  });
  if (asked.status === 404) {
    /* The episode is in the ledger but has not been projected into a row
       yet. Ordinary: the rig saves a take and reaches here in the same
       second, and projection runs on its own timer. Waiting is correct -
       treating it as a failure would give up on a take that is about to
       exist. */
    const e = new Error("episode not projected yet");
    e.waiting = true;
    throw e;
  }
  if (!asked.ok) throw new Error("presign HTTP " + asked.status);
  const where = await asked.json();

  const buf = await item.blob.arrayBuffer();
  /* where.url is absolute for a presigned upload and relative for the
     gateway model; `api` adds the token only to the second. */
  const put = await api(where.url, { method: where.method || "PUT", body: buf });
  if (!put.ok) throw new Error("put HTTP " + put.status);

  const said = await api(base + "/video:complete", {
    method: "POST", headers: json,
    body: JSON.stringify({
      camera: item.camera,
      sha256: await sha256Hex(buf),
      bytes: buf.byteLength,
    }),
  });
  /* 409 is the server saying what landed is not what was sent. The bytes
     are still here, so this goes round again from the PUT rather than
     being set aside - a truncated upload is the commonest real failure
     and it is exactly the one a retry fixes. */
  if (!said.ok) throw new Error("complete HTTP " + said.status);

  const result = await said.json();
  if (!result.safeToDelete) throw new Error("the server did not release the copy");
  return result;
}

async function flushVideo() {
  if (videoSending || !videoQueue.length) return;
  videoSending = true;
  const item = videoQueue[0];
  try {
    const result = await sendVideo(item);
    videoQueue.shift();          // only now: the server has verified it
    onJournal(() => journal.forgetVideo(item.episodeId + "/" + item.camera),
              "a confirmed video");
    videoSent.push(result.key);
    videoFailures = 0;
    videoState = videoQueue.length ? "sending" : "idle";
    videoError = null;
  } catch (e) {
    videoFailures += 1;
    videoState = e && e.waiting ? "waiting" : "offline";
    videoError = String((e && e.message) || e);
    if (videoFailures === 1 && !(e && e.waiting)) {
      emitLog("video_upload_failed", "episodes", videoError);
    }
  } finally {
    videoSending = false;
  }
}

function videoDelay() {
  if (!videoFailures) return VIDEO_BACKOFF_MS;
  return Math.min(VIDEO_BACKOFF_MS * Math.pow(2, videoFailures - 1), VIDEO_BACKOFF_MAX);
}

let videoTimer = null;
function startVideoUploader() {
  if (videoTimer) return;
  const tick = async () => {
    await flushVideo();
    videoTimer = setTimeout(tick, videoDelay());
  };
  videoTimer = setTimeout(tick, VIDEO_BACKOFF_MS);
}

/* For the tests and the drawer. `setVideoSource` is how a recorder is
   attached - the browser has none, a harness supplies a fake one, and
   Tauri will supply a real one. */
window.setVideoSource = (fn) => { videoSource = fn; };
window.rigVideo = () => ({
  queued: videoQueue.length,
  sent: videoSent.slice(),
  failures: videoFailures,
  state: videoState,
  error: videoError,
});
window.rigFlushVideo = flushVideo;

/* The uploader writes to the operator's log without filing an event.
   A refused batch is a fact about this rig's software, not about the
   floor, and it has no business in the ledger. */
function emitLog(event, bucket, detail) {
  logLines.push({ at: clock(S ? S.t : 0), event, bucket, detail });
  const el = document.getElementById("log");
  if (el) {
    el.innerHTML = logLines.slice(-40).map((l) =>
      `<p><time>${l.at}</time><b>${l.event} <span class="bucket">→ ${l.bucket}</span><br>${l.detail}</b></p>`).join("");
  }
}

let toastTimer;
function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), 2400);
}

// ---------------------------------------------------------------- drawer

const $drawer = document.getElementById("drawer");
const $tab = document.getElementById("drawer-tab");
function toggleDrawer(force) {
  const open = force !== undefined ? force : !$drawer.classList.contains("open");
  $drawer.classList.toggle("open", open);
  $tab.setAttribute("aria-expanded", String(open));
}
$tab.addEventListener("click", () => toggleDrawer());
document.getElementById("drawer-close").addEventListener("click", () => toggleDrawer(false));

const $screens = document.getElementById("screens");
$screens.innerHTML = SCREENS.map(([id, label]) =>
  `<button data-screen="${id}">${label}</button>`).join("");
$screens.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  location.hash = b.dataset.screen;
  boot(b.dataset.screen);
});
addEventListener("hashchange", () => boot(location.hash.slice(1)));

const $speeds = document.getElementById("speeds");
$speeds.innerHTML = SPEEDS.map((s) =>
  `<button data-s="${s}" aria-pressed="${s === speed}">${s}×</button>`).join("");
$speeds.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  speed = Number(b.dataset.s);
  [...$speeds.children].forEach((c) => c.setAttribute("aria-pressed", String(Number(c.dataset.s) === speed)));
  showMode();
});

const $rigs = document.getElementById("rigs");
$rigs.innerHTML = window.DEMO_ROSTER.allRigs
  .map((r) => `<button data-rig="${r}">${r}</button>`).join("");
$rigs.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  window.switchRig(b.dataset.rig);
  [...$rigs.children].forEach((c) =>
    c.setAttribute("aria-pressed", String(c.dataset.rig === b.dataset.rig)));
});

const $live = document.getElementById("live-toggle");
$live.addEventListener("click", () => {
  const on = $live.getAttribute("aria-pressed") !== "true";
  $live.setAttribute("aria-pressed", String(on));
  window.setLiveClock(on);
});

// -------------------------------------------------------------------- go

/* ------------------------------------------------------------ the push

   A deployed rig is handed schedule.json by the desk. If it is not there
   - opened over file://, or before anything has been pushed - the rig
   falls back to generating the same schedule locally from the shared
   engine, so the demo still runs and still agrees with the desk. */

async function loadPayload(rigId) {
  // A single-file build has the push baked in - there is nothing to fetch.
  if (window.PUSHED_SCHEDULE && (!rigId || window.PUSHED_SCHEDULE.rigId === rigId)) {
    SOURCE = "baked";
    return window.PUSHED_SCHEDULE;
  }
  const id = rigId || RIG_ID;
  // The server serves the pushed payload for this rig. If the server is
  // not running, or nothing has been pushed to this rig yet, fall back
  // to the co-located schedule.json (still supported for a plain static
  // deploy) and then to a locally generated schedule so the demo runs.
  try {
    const r = await api("/api/rigs/" + encodeURIComponent(id) + "/schedule.json", { cache: "no-store" });
    if (r.ok) { SOURCE = "server"; return await r.json(); }
  } catch (e) { /* server not up */ }
  try {
    const r = await fetch("schedule.json", { cache: "no-store" });
    if (r.ok) {
      const p = await r.json();
      if (!rigId || p.rigId === rigId) { SOURCE = "file"; return p; }
    }
  } catch (e) { /* no file, nothing pushed */ }
  SOURCE = "generated";
  return generateLocally(id);
}

/* What the rig is actually running on, in words, whenever that is not
   the ordinary thing. A rig reading a pushed schedule on the wall clock
   says nothing; any other combination says so out loud.

   The rule this enforces: the rig never lies about its own state. A rig
   running a schedule nobody pushed, or an accelerated clock, used to be
   indistinguishable from a correct one - which is exactly how a 30x demo
   clock survived all the way to a deployable build. */
function modeLine() {
  const bits = [];
  if (SOURCE === "generated") bits.push("schedule not pushed");
  else if (SOURCE === "file") bits.push("schedule from file");
  else if (SOURCE === "baked") bits.push("demo build");
  if (DEMO) bits.push("demo clock " + speed + "×");
  else if (!live) bits.push("shift clock " + speed + "×");
  /* A rig that cannot reach the server is still a working rig - it keeps
     recording and keeps filing - but it is not a rig whose events anyone
     has. Saying so is the same rule as the rest of this line. */
  if (uploadState === "offline" && outbox.length) {
    bits.push(outbox.length + " events queued");
  } else if (rejected.length) {
    bits.push(rejected.length + " events refused");
  }
  return bits.join(" · ");
}

function generateLocally(rigId) {
  const R = window.DEMO_ROSTER;
  const cfg = Object.assign({}, R.defaults, { date: new Date().toISOString().slice(0, 10) });
  return RE.rigPayload(RE.buildPlan(cfg, R.groups), rigId);
}

function applyPayload(p) {
  PAYLOAD = p;
  RIG_ID = p.rigId;
  document.getElementById("rail-rig").textContent = p.rigId;
  document.getElementById("rail-task").textContent = p.task;
  document.title = p.rigId + " Pedal Loop";
  showMode();
}

function showMode() {
  const el = document.getElementById("rail-mode");
  if (el) el.textContent = modeLine();
}

function clock(secs) {
  const s = Math.max(0, Math.floor(secs));
  return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}
const pct = (r) => Math.round(r * 100) + "%";

if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(() => { lastViewKey = null; render(); });
}
addEventListener("resize", () => { lastViewKey = null; render(); });

async function start(rigId) {
  applyPayload(await loadPayload(rigId));
  /* Before anything is filed, and before the cursor is read: whatever the
     last boot did not finish sending is still owed to the server, and its
     sequence numbers have to be accounted for before new ones are minted. */
  const owed = await recoverJournal();
  // Ask what the server already holds before filing anything, so `seq`
  // carries on rather than restarting and going backwards.
  await alignSeq();
  boot(location.hash.slice(1));   // boot() reads the turn in progress back itself
  /* After boot, which clears the log. A backlog the operator cannot see
     is the same as no backlog from where they are standing. */
  if (owed && (owed.events || owed.videos)) {
    emitLog("journal_recovered", "sessions",
            owed.events + " events and " + owed.videos +
            " videos were still waiting from before the last restart");
  }
  startUploader();
  startVideoUploader();
}

/* Demo affordance: watch any rig on the floor. A real rig is only ever
   itself - it reads the one payload it was pushed. Tries the server
   first (so a fresh push shows up) then falls back to a local build. */
window.switchRig = async function (rigId) {
  applyPayload(await loadPayload(rigId));
  boot("");
  toast("Now showing " + rigId);
};

window.setLiveClock = function (on) {
  live = on;
  syncTurnKey();   // the clock just jumped; the turn in progress moved with it
  showMode();
  toast(on ? "Following the wall clock" : "Demo clock");
};

start();
requestAnimationFrame(tick);
