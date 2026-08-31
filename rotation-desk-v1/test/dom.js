/* =====================================================================
 * test/dom.js
 *
 * Just enough DOM to run desk.js under node, so the manager's screen can
 * be tested the way the engine is - headlessly, on every `npm test`.
 *
 * The element registry is built by reading the ids out of index.html
 * rather than being listed here, so the stub cannot quietly drift from
 * the page. Anything desk.js asks for that the page does not have is
 * recorded in `missingIds`, which is itself a test: a typo'd id would
 * otherwise fail silently in a browser.
 * ===================================================================== */

"use strict";

const fs   = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const REPO = path.resolve(ROOT, "..");

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.className = "";
    this._text = null;
    this.attrs = {};
    this.dataset = {};
    this.hidden = false;
    this.open = false;
    this.disabled = false;
    this.value = "";
    this.scope = "";
    this.colSpan = 1;
    this.style = { setProperty() {}, width: "" };
    this._on = {};
    const self = this;
    this.classList = {
      add(c) { if (!self.classList.contains(c)) self.className = (self.className ? self.className + " " : "") + c; },
      remove(c) { self.className = self.className.split(/\s+/).filter(x => x && x !== c).join(" "); },
      toggle(c, on) { on ? this.add(c) : this.remove(c); },
      contains(c) { return self.className.split(/\s+/).includes(c); },
    };
  }
  appendChild(n) { this.children.push(n); return n; }
  /* Real elements have these. A stub without them turns "the script
     moved the caret" into "the whole screen threw", which is a failure
     the browser would never have had. */
  focus() {}
  blur() {}
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(type, fn) { (this._on[type] = this._on[type] || []).push(fn); }
  querySelectorAll(sel) {
    const [k, v] = sel.replace(/[[\]]/g, "").split("=");
    const out = [];
    const walk = n => {
      if (n.attrs && n.attrs[k] === v) out.push(n);
      (n.children || []).forEach(walk);
    };
    walk(this);
    return out;
  }
  set textContent(v) { this.children = []; this._text = v === "" ? null : v; }
  get textContent() {
    if (this._text != null) return this._text;
    return this.children.map(c => c.textContent).join("");
  }
  set innerHTML(v) { this._html = v; this._text = "[html]"; }
  get innerHTML() { return this._html || ""; }
}

/* ---------------------------------------------------------- helpers */

function find(node, cls) {
  const out = [];
  const walk = n => {
    if (n.className && n.className.split(/\s+/).includes(cls)) out.push(n);
    (n.children || []).forEach(walk);
  };
  walk(node);
  return out;
}

function textIn(node, cls) {
  const hit = find(node, cls);
  return hit.length ? hit[0].textContent : "";
}

/* =====================================================================
 * mount
 * ===================================================================== */

/* opts: { at: "HH:MM", date: [y, m, d], fetchImpl }
 * Returns once desk.js has booted and its first (async) floor read has
 * settled. */
async function mountDesk(opts) {
  opts = opts || {};
  const at = (opts.at || "10:37").split(":");
  const ymd = opts.date || [2026, 7, 23];

  const FIXED = new Date(ymd[0], ymd[1], ymd[2], Number(at[0]), Number(at[1]), Number(at[2] || 0));
  const RealDate = global.Date;
  class FakeDate extends RealDate {
    constructor(...a) { if (!a.length) super(FIXED.getTime()); else super(...a); }
    static now() { return FIXED.getTime(); }
  }

  // ---- the registry, read out of the page itself
  const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
  const byId = {};
  const missingIds = [];

  const mk = id => { const e = new El("div"); e.attrs.id = id; byId[id] = e; return e; };
  (html.match(/id="([^"]+)"/g) || []).forEach(m => mk(m.slice(4, -1)));

  /* Start hidden if the page says hidden.
   *
   * Everything used to mount visible whatever the markup said, so a
   * panel the page ships closed read as open until some boot path
   * happened to close it - and a panel that nothing closes, because the
   * markup already had, read as open forever. That is the harness
   * disagreeing with the page about the page's own initial state. */
  (html.match(/<[a-zA-Z][^>]*>/g) || []).forEach(tag => {
    const id = (tag.match(/\bid="([^"]+)"/) || [])[1];
    if (id && byId[id] && /\shidden(\s|>|=)/.test(tag)) byId[id].hidden = true;
  });

  // the buttons the page ships with, in the order it ships them
  (html.match(/data-mode="(\w+)"/g) || []).forEach(m => {
    const b = new El("button");
    b.dataset.mode = m.slice(11, -1);
    b.attrs.role = "tab";
    byId["modes"].appendChild(b);
  });
  (html.match(/data-tab="(\w+)"/g) || []).forEach(m => {
    const b = new El("button");
    b.dataset.tab = m.slice(10, -1);
    b.attrs.role = "tab";
    byId["tabs"].appendChild(b);
  });

  // ---- globals desk.js expects
  const intervals = [], timeouts = [];
  const realSetInterval = global.setInterval;
  const realSetTimeout  = global.setTimeout;

  global.Date = FakeDate;
  global.setInterval = (fn, ms) => { const id = realSetInterval(fn, ms); intervals.push(id); return id; };
  global.setTimeout  = (fn, ms) => { const id = realSetTimeout(fn, ms);  timeouts.push(id);  return id; };
  /* Document-level listeners, because a real document has them and the
     desk uses one for the Escape key. Without this the script threw at
     the top level and took the whole screen with it - which is what a
     browser missing anything the script assumes would also do, so the
     stub having the method is the accurate stand-in rather than a
     convenience. Kept addressable so a test can send a key. */
  const docListeners = {};
  global.document = {
    createElement: t => new El(t),
    getElementById: id => byId[id] || (missingIds.push(id), mk(id)),
    body: new El("body"),
    addEventListener: (type, fn) => {
      (docListeners[type] = docListeners[type] || []).push(fn);
    },
    removeEventListener: (type, fn) => {
      docListeners[type] = (docListeners[type] || []).filter(f => f !== fn);
    },
  };
  global.__docListeners = docListeners;
  global.window = global;
  global.location = { search: opts.search || "", hash: "" };
  global.fetch = opts.fetchImpl || (() => Promise.reject(new Error("no server")));

  require(path.join(REPO, "packages/session/session.js"));
  require(path.join(REPO, "packages/engine/rotation-engine.js"));
  const roster = require(path.join(REPO, "packages/demo-roster/demo-roster.js"));
  global.DEMO_ROSTER = structuredClone(roster);   // never let one test's edits reach the next

  const src = fs.readFileSync(path.join(ROOT, "assets/desk.js"), "utf8");
  new Function(src)();

  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  const desk = {
    byId,
    missingIds,
    RE: global.RotationEngine,
    roster: global.DEMO_ROSTER,
    at: FIXED,

    $: id => byId[id],
    find,
    textIn,

    fire(node, type, patch) {
      Object.assign(node, patch || {});
      (node._on[type] || []).forEach(fn => fn({ target: node }));
    },
    click(node) { this.fire(node, "click"); },

    /* A key press at the document, the way a real one arrives. */
    key(name) {
      (docListeners["keydown"] || []).forEach(fn => fn({ key: name }));
    },

    mode(name) { this.click(byId["modes"].children.find(b => b.dataset.mode === name)); },

    /* The tabs a manager can actually reach, in order. */
    visibleTabs() {
      return byId["tabs"].children.filter(b => b.dataset.tab && !b.hidden).map(b => b.dataset.tab);
    },
    tab(name)  { this.click(byId["tabs"].children.find(b => b.dataset.tab === name)); },
    shift(id)  { this.click(byId["seg-shift"].children.find(b => b.dataset.shift === id)); },

    /* the schedule grid, read back as plain text */
    grid(which) {
      const tbl = byId[which === "rigs" ? "tbl-rigs" : "tbl-ops"];
      const thead = tbl.children.find(c => c.tagName === "thead");
      const tbody = tbl.children.find(c => c.tagName === "tbody");
      return {
        ticks: thead.children[0].children.slice(1).map(c => c.textContent),
        corner: thead.children[0].children[0].textContent,
        bands: tbody.children.filter(r => r.classList.contains("band")),
        rows: tbody.children.filter(r => !r.classList.contains("band")).map(r => ({
          label: r.children[0].textContent,
          cells: r.children.slice(1).map(c => ({
            text: c.textContent,
            cls: c.className,
            start: c.classList.contains("start"),
          })),
        })),
      };
    },

    /* A destination chip, read back as its three parts. */
    dest(node) {
      const chip = find(node, "dest")[0];
      if (!chip) return null;
      return {
        label: chip.children[1] ? chip.children[1].textContent : "",
        at: chip.children[2] ? chip.children[2].textContent : "",
        kind: chip.className.split(/\s+/).filter(c => c !== "dest").join(" "),
      };
    },

    /* the Live board, read back as plain text */
    board() {
      const self = this;
      // `.dest.to-rig` shares no class token with the `.rig` row, so the
      // row list below cannot pick up a destination chip by accident.
      return find(byId["board"], "gcard").map(card => {
        const offRow = find(card, "offrow")[0];
        return {
          key: textIn(card, "grp-key"),
          task: textIn(card, "gtask"),
          rigs: find(card, "rig").map(r => ({
            rigId: textIn(r, "rig-id"),
            op: textIn(r, "op"),
            left: textIn(r, "left"),
            leftCls: (find(r, "left")[0] || { className: "" }).className,
            width: (find(r, "rig-bar")[0].children[0] || { style: {} }).style.width,
            dest: self.dest(r),
            relief: textIn(r, "relief"),
          })),
          off: {
            tag: textIn(card, "off-tag"),
            name: textIn(card, "off-name"),
            dest: offRow ? self.dest(offRow) : null,
            left: offRow ? textIn(offRow, "left") : "",
          },
        };
      });
    },

    /* Every timer this mount started, stopped - a toast fading after the
     * test has finished would otherwise fire into a torn-down stub. */
    stop() {
      intervals.forEach(clearInterval);
      timeouts.forEach(clearTimeout);
      global.Date = RealDate;
      global.setInterval = realSetInterval;
      global.setTimeout = realSetTimeout;
    },
  };

  return desk;
}

module.exports = { mountDesk, El, find, textIn };
