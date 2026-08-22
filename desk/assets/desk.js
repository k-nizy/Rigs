/* =====================================================================
 * rotation-desk.js
 *
 * The manager's screen. All of the arithmetic lives in
 * rotation-engine.js - this file only holds the roster being edited, the
 * settings on the console, and the code that draws them. If a number
 * looks wrong, the engine is where to look, not here.
 * ===================================================================== */

"use strict";

var RE = window.RotationEngine;

/* Grid sizes offered on the console, in minutes. */
const BLOCK_OPTIONS = [10, 15, 20, 30, 40];

/* The roster the desk opens with, from shared/. A manager edits these
 * in place; the shape is what buildPlan expects. */
const GROUPS = window.DEMO_ROSTER.groups;

const cfg = {
  shift: window.DEMO_ROSTER.defaults.shift,
  date: new Date().toISOString().slice(0, 10),
  blockMin: window.DEMO_ROSTER.defaults.blockMin,
  stintBlocks: window.DEMO_ROSTER.defaults.stintBlocks,
  mode: window.DEMO_ROSTER.defaults.mode,
  pushRig: "RIG-01",
  tab: "ops",
};

/* ===================================================================
 * Rendering
 * =================================================================== */

const $ = id => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

function renderConsole() {
  const seg = $("seg-shift");
  if (!seg.children.length) {
    RE.SHIFTS.forEach(s => {
      const b = el("button", null, s.label);
      b.type = "button";
      b.dataset.shift = s.id;
      b.addEventListener("click", () => { cfg.shift = s.id; render(); });
      seg.appendChild(b);
    });
  }
  [...seg.children].forEach(b => b.setAttribute("aria-pressed", String(b.dataset.shift === cfg.shift)));

  const s = RE.shiftById(cfg.shift);
  $("shift-hint").textContent = RE.hhmm(s.start) + "  to  " + RE.hhmm((s.start + RE.SHIFT_MINUTES) % 1440)
                              + " / whole crew swaps at the boundary";

  [...$("seg-mode").children].forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === cfg.mode)));
  $("mode-hint").textContent = cfg.mode === "hold"
    ? "Same rig until the break. Leaves a short turn at each end of the shift."
    : "Next rig each time. Every turn the same length.";

  const sel = $("in-stint");
  const holdMode = cfg.mode === "hold";
  sel.textContent = "";
  const nB = Math.round(RE.SHIFT_MINUTES / cfg.blockMin);
  const clean = RE.cleanStints(cfg.blockMin);
  for (let L = 1; L <= 8; L++) {
    if (nB % L !== 0) continue;
    const mins = L * cfg.blockMin;
    const o = el("option", null, L + " block" + (L > 1 ? "s" : "") + " = " + mins + " min"
                                + (clean.includes(mins) ? " (fits)" : " (does not fit)"));
    o.value = String(L);
    if (L === RE.stintBlocksFor(cfg)) o.selected = true;
    sel.appendChild(o);
  }
  sel.disabled = holdMode;
  $("stint-hint").textContent = holdMode
    ? "Locked to 3 blocks. Holding one rig forces the 3:1 duty."
    : "Must divide 60 minutes. The options marked (fits) do.";
}

function renderFit(a) {
  $("v-dot").className = "dot " + a.level;
  $("v-text").textContent = a.text;
  $("v-note").textContent = a.note;

  const budget = $("budget");
  budget.textContent = "";
  const rows = [
    ["Work", a.work.join(" / "), 360],
    ["Break", a.brk.join(" / "), 60],
    ["Think", a.think.join(" / "), 60],
  ];
  rows.forEach(([label, value, target]) => {
    const single = value.indexOf("/") === -1 && Number(value) === target;
    const d = el("div", "bud" + (single ? "" : " off"));
    d.appendChild(el("small", null, label));
    d.appendChild(el("b", null, value + " min"));
    budget.appendChild(d);
  });

  const opts = $("opts");
  opts.textContent = "";
  /* Each mode has a different lever, so offer the one that applies.
   * Holding a rig is fixed at three blocks, so its only choice is the grid;
   * rotating is free to pick any time on rig that divides the hour. */
  const holdMode = cfg.mode === "hold";
  const clean = holdMode
    ? RE.cleanBlocks(BLOCK_OPTIONS)
    : RE.cleanStints(cfg.blockMin);

  opts.appendChild(el("p", "eyebrow", holdMode
    ? "Grid sizes that give equal break and think time"
    : "Times on rig that work at " + cfg.blockMin + "-min resolution"));

  if (!clean.length) {
    opts.appendChild(el("span", "none",
      "None. " + cfg.blockMin + " does not divide 60, so nothing built from it can."));
  } else {
    clean.forEach(mins => {
      const b = el("button", "chip", mins + " min");
      b.type = "button";
      b.setAttribute("aria-pressed",
        String(holdMode ? mins === cfg.blockMin : mins === RE.stintMinutes(cfg)));
      b.addEventListener("click", () => {
        if (holdMode) setBlockMin(mins);
        else { cfg.stintBlocks = mins / cfg.blockMin; render(); }
      });
      opts.appendChild(b);
    });
  }

  const checks = $("checks");
  checks.textContent = "";
  a.checks.forEach(c => {
    const cls = c.ok ? "pass" : (c.soft ? "soft" : "fail");
    const row = el("div", "check " + cls);
    row.appendChild(el("i", null, c.ok ? "OK" : (c.soft ? "!" : "X")));
    row.appendChild(el("b", null, c.label));
    row.appendChild(el("span", null, c.note));
    checks.appendChild(row);
  });
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
    head.appendChild(el("span", "eyebrow", "3 rigs / 4 operators"));
    card.appendChild(head);

    const taskWrap = el("div");
    taskWrap.appendChild(el("label", null, "Task for the shift"));
    const task = el("input");
    task.type = "text"; task.value = g.task;
    task.addEventListener("input", () => { g.task = task.value; render(); });
    taskWrap.appendChild(task);
    card.appendChild(taskWrap);

    const rigWrap = el("div");
    rigWrap.appendChild(el("label", null, "Rigs"));
    const rigRow = el("div", "rig-row");
    g.rigs.forEach((r, ri) => {
      const inp = el("input");
      inp.type = "text"; inp.value = r;
      inp.setAttribute("aria-label", "Group " + g.key + " rig " + (ri + 1));
      inp.addEventListener("input", () => { g.rigs[ri] = inp.value; render(); });
      rigRow.appendChild(inp);
    });
    rigWrap.appendChild(rigRow);
    card.appendChild(rigWrap);

    const opWrap = el("div");
    opWrap.appendChild(el("label", null, "Operators"));
    const list = el("div", "op-list");
    g.ops.forEach((o, oi) => {
      const line = el("div", "op-line");
      line.appendChild(el("span", "idx", String(oi + 1)));
      const inp = el("input");
      inp.type = "text"; inp.value = o;
      inp.setAttribute("aria-label", "Group " + g.key + " operator " + (oi + 1));
      inp.addEventListener("input", () => { g.ops[oi] = inp.value; render(); });
      line.appendChild(inp);
      list.appendChild(line);
    });
    opWrap.appendChild(list);
    card.appendChild(opWrap);

    host.appendChild(card);
  });
}

function timeHeader(plan, stubLabel) {
  const nBlocks = plan.nBlocks;
  const thead = el("thead");
  const tr = el("tr");
  const stub = el("th", "stub", stubLabel);
  stub.scope = "col";
  tr.appendChild(stub);
  for (let b = 0; b < nBlocks; b++) {
    const th = el("th", "tick" + (RE.isHourMark(plan, b) ? " hour" : ""), RE.hhmm(RE.blockStart(plan, b)));
    th.scope = "col";
    tr.appendChild(th);
  }
  thead.appendChild(tr);
  return thead;
}

function renderOperatorGrid(plan) {
  const tbl = $("tbl-ops");
  tbl.textContent = "";
  tbl.appendChild(timeHeader(plan, "Operator"));

  const tbody = el("tbody");
  plan.groups.forEach((g, gi) => {
    const band = el("tr", "band");
    band.style.setProperty("--gc", "var(--g" + gi + ")");
    const bh = el("th", "stub", "GROUP " + g.key);
    bh.scope = "row";
    band.appendChild(bh);
    const bd = el("td", null, g.task + "  /  " + g.rigs.join(" / "));
    bd.colSpan = plan.nBlocks;
    band.appendChild(bd);
    tbody.appendChild(band);

    g.rows.forEach((row, oi) => {
      const tr = el("tr");
      tr.style.setProperty("--gc", "var(--g" + gi + ")");
      tr.style.setProperty("--gc-soft", "var(--g" + gi + "-soft)");

      const t = g.totals[oi];
      const th = el("th", "stub");
      th.scope = "row";
      th.appendChild(document.createTextNode(g.ops[oi] || "-"));
      const meta = el("span", "eyebrow", "  " + (t.work / 60).toFixed(1) + "h");
      th.appendChild(meta);
      tr.appendChild(th);

      row.forEach((cell, b) => {
        const hour = RE.isHourMark(plan, b) ? " hour" : "";
        if (cell === RE.BREAK) {
          tr.appendChild(el("td", "cell brk" + hour, "BREAK"));
        } else if (cell === RE.THINK) {
          tr.appendChild(el("td", "cell thk" + hour, "THINK"));
        } else {
          const isStart = b === 0 || row[b - 1] !== cell;
          tr.appendChild(el("td", "cell work" + hour + (isStart ? " start" : ""), g.rigs[cell] || "-"));
        }
      });
      tbody.appendChild(tr);
    });
  });
  tbl.appendChild(tbody);
}

function renderRigGrid(plan) {
  const tbl = $("tbl-rigs");
  tbl.textContent = "";
  tbl.appendChild(timeHeader(plan, "Rig"));

  const tbody = el("tbody");
  plan.groups.forEach((g, gi) => {
    g.rigs.forEach((rig, ri) => {
      const tr = el("tr");
      tr.style.setProperty("--gc", "var(--g" + gi + ")");
      tr.style.setProperty("--gc-soft", "var(--g" + gi + "-soft)");

      const th = el("th", "stub");
      th.scope = "row";
      th.appendChild(document.createTextNode(rig || "-"));
      th.appendChild(el("span", "eyebrow", "  " + g.task));
      tr.appendChild(th);

      let prev = null;
      for (let b = 0; b < plan.nBlocks; b++) {
        const oi = g.rows.findIndex(r => r[b] === ri);
        const label = oi === -1 ? "-" : shortName(g.ops[oi]);
        const isStart = label !== prev;
        prev = label;
        tr.appendChild(el("td",
          "cell work name" + (RE.isHourMark(plan, b) ? " hour" : "") + (isStart ? " start" : ""),
          label));
      }
      tbody.appendChild(tr);
    });
  });
  tbl.appendChild(tbody);
}

function shortName(full) {
  if (!full) return "-";
  const parts = full.trim().split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 9);
  return parts[0][0] + ". " + parts[parts.length - 1].slice(0, 8);
}
function highlightJSON(text) {
  return text
    .replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))
    .replace(/"([^"\\]*)":/g, '<span class="k">"$1"</span>:')
    .replace(/: "([^"\\]*)"/g, ': <span class="s">"$1"</span>')
    .replace(/: (-?\d+(?:\.\d+)?|true|false|null)/g, ': <span class="n">$1</span>');
}

function renderPush(plan) {
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
  sel.addEventListener("change", () => { cfg.pushRig = sel.value; render(); });
  tools.appendChild(sel);

  const copy = el("button", "btn", "Copy for this rig");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    const p = RE.rigPayload(plan, cfg.pushRig);
    try {
      await navigator.clipboard.writeText(JSON.stringify(p, null, 2));
      toast("Copied " + cfg.pushRig + " payload");
    } catch { toast("Clipboard blocked - select the text instead"); }
  });
  tools.appendChild(copy);

  const p = RE.rigPayload(plan, cfg.pushRig);
  const head = $("push-head");
  head.textContent = "";
  if (!p) { $("push-body").textContent = "No such rig."; return; }
  head.appendChild(el("b", null, p.rigId));
  head.appendChild(el("span", null, p.task));
  head.appendChild(el("span", null, p.turns.length + " turns / " + p.shift.start + "-" + p.shift.end));
  $("push-body").innerHTML = highlightJSON(JSON.stringify(p, null, 2));
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("up");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("up"), 2200);
}

/* ===================================================================
 * Wiring
 * =================================================================== */

function renderTabs() {
  [...$("tabs").querySelectorAll("[role=tab]")].forEach(b => {
    const on = b.dataset.tab === cfg.tab;
    b.setAttribute("aria-selected", String(on));
  });
  $("panel-ops").hidden  = cfg.tab !== "ops";
  $("panel-rigs").hidden = cfg.tab !== "rigs";
  $("panel-push").hidden = cfg.tab !== "push";
}

function render() {
  const plan = RE.buildPlan(cfg, GROUPS);
  renderConsole();
  renderFit(RE.auditPlan(plan));
  renderRosters();
  renderTabs();
  if (cfg.tab === "ops")  renderOperatorGrid(plan);
  if (cfg.tab === "rigs") renderRigGrid(plan);
  renderPush(plan);

  $("stat-ops").textContent    = GROUPS.reduce((n, g) => n + g.ops.length, 0);
  $("stat-rigs").textContent   = GROUPS.reduce((n, g) => n + g.rigs.length, 0);
  $("stat-groups").textContent = GROUPS.length;
}

$("in-date").value = cfg.date;
$("in-date").addEventListener("change", e => { cfg.date = e.target.value; render(); });
function setBlockMin(v) {
  const prevMin = RE.stintMinutes(cfg);
  cfg.blockMin = Number(v);
  // Carry the stint across in minutes where possible, else fall back to a
  // clean one - changing the grid should not silently break the schedule.
  const clean = RE.cleanStints(cfg.blockMin);
  const target = clean.includes(prevMin) ? prevMin : (clean[clean.length - 1] ?? cfg.blockMin);
  cfg.stintBlocks = Math.max(1, Math.round(target / cfg.blockMin));
  render();
}
$("in-block").addEventListener("change", e => setBlockMin(e.target.value));
$("in-stint").addEventListener("change", e => { cfg.stintBlocks = Number(e.target.value); render(); });
[...$("seg-mode").children].forEach(b =>
  b.addEventListener("click", () => { cfg.mode = b.dataset.mode; render(); }));
[...$("tabs").querySelectorAll("[role=tab]")].forEach(b =>
  b.addEventListener("click", () => { cfg.tab = b.dataset.tab; render(); }));

render();
