/* =====================================================================
 * test/dom.js
 *
 * Just enough DOM to run rig.js under node, so the operator's screen can
 * be tested the way the engine is - headlessly, on every `npm test`.
 *
 * Same discipline as the desk's stub next door: the element registry is
 * built by reading the ids out of index.html rather than being listed
 * here, so it cannot quietly drift from the page, and anything rig.js
 * asks for that the page does not have lands in `missingIds`.
 *
 * The rig needs more of a browser than the desk does - it runs a
 * requestAnimationFrame clock, rebuilds the stage through innerHTML and
 * then reads ids back out of it, measures text with a Range, and paints
 * canvases. All of that is stubbed to the depth rig.js actually uses and
 * no further.
 * ===================================================================== */

"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const REPO = path.resolve(ROOT, "../..");

/* The registry the current mount is filling. innerHTML writes ids into
   it, which is what makes getElementById find a node the stage just
   drew - exactly as a browser does, and what lets refresh() patch the
   timers in place. */
let REGISTRY = null;

/* ------------------------------------------------------------ markup */

const VOID = new Set(["br", "img", "input", "hr", "meta", "link"]);

/* A parser for the markup rig.js generates, and nothing more: tags,
   attributes, text. It does not need to survive arbitrary HTML. */
function parseHTML(html) {
  const root = new El("#frag");
  const stack = [root];
  const re = /<\/?([a-zA-Z][\w-]*)((?:\s+[\w:-]+(?:=(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[4] != null) {
      if (m[4].trim()) {
        const t = new El("#text");
        t._text = m[4].replace(/\s+/g, " ");
        stack[stack.length - 1].appendChild(t);
      }
      continue;
    }
    if (m[0][1] === "/") { if (stack.length > 1) stack.pop(); continue; }
    const el = new El(m[1].toLowerCase());
    const ar = /([\w:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
    let a;
    while ((a = ar.exec(m[2] || ""))) {
      const k = a[1];
      const v = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : "";
      el.setAttribute(k, v);
      if (k === "disabled") el.disabled = true;
      if (k.startsWith("data-")) el.dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
    }
    stack[stack.length - 1].appendChild(el);
    if (!VOID.has(m[1].toLowerCase()) && !m[3]) stack.push(el);
  }
  return root.children;
}

/* #id, .class, tag and [attr="v"], in any combination - the selectors
   rig.js actually passes to querySelector and closest. */
function matches(el, sel) {
  const parts = String(sel).trim().match(/(^[a-zA-Z][\w-]*)|(\.[\w-]+)|(#[\w-]+)|(\[[^\]]+\])/g) || [];
  return parts.every((p) => {
    if (p[0] === ".") return el.className.split(/\s+/).includes(p.slice(1));
    if (p[0] === "#") return el.attrs.id === p.slice(1);
    if (p[0] === "[") {
      const mm = p.slice(1, -1).match(/^([\w:-]+)(?:=["']?([^"']*)["']?)?$/);
      if (!mm) return false;
      return mm[2] === undefined ? el.attrs[mm[1]] !== undefined : el.attrs[mm[1]] === mm[2];
    }
    return el.tagName === p.toLowerCase();
  });
}

/* The camera panes are painted, never read back. */
const CTX = new Proxy({}, {
  get: (_, k) => (k === "createLinearGradient" || k === "createRadialGradient"
    ? () => ({ addColorStop() {} })
    : () => {}),
});

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.className = "";
    this._text = null;
    this.attrs = {};
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.parent = null;
    this.value = "";
    this.style = { setProperty() {}, width: "", fontSize: "" };
    this.clientWidth = 400;
    this.clientHeight = 600;
    this._on = {};
    const self = this;
    this.classList = {
      add(c) { if (!this.contains(c)) self.className = (self.className ? self.className + " " : "") + c; },
      remove(c) { self.className = self.className.split(/\s+/).filter((x) => x && x !== c).join(" "); },
      toggle(c, on) {
        if (on === undefined) return this.contains(c) ? this.remove(c) : this.add(c);
        return on ? this.add(c) : this.remove(c);
      },
      contains(c) { return self.className.split(/\s+/).includes(c); },
    };
  }
  appendChild(n) { n.parent = this; this.children.push(n); return n; }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === "class") this.className = String(v);
  }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this._on[t] = this._on[t] || []).push(fn); }
  removeEventListener() {}
  closest(sel) {
    let n = this;
    while (n) { if (n.tagName && n.tagName[0] !== "#" && matches(n, sel)) return n; n = n.parent; }
    return null;
  }
  _walk(out) { for (const c of this.children) { out.push(c); c._walk(out); } return out; }
  querySelectorAll(sel) {
    return this._walk([]).filter((n) => n.tagName && n.tagName[0] !== "#" && matches(n, sel));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getContext() { return CTX; }
  set textContent(v) { this.children = []; this._text = v === "" ? null : String(v); }
  get textContent() {
    if (this._text != null) return this._text;
    return this.children.map((c) => c.textContent).join("");
  }
  set innerHTML(v) {
    this._html = v;
    this.children = [];
    this._text = null;
    parseHTML(v).forEach((n) => this.appendChild(n));
    /* ids drawn by the stage become findable, and ids the last draw put
       there stop being findable - both as a browser does. Keeping the
       old ones alive would hide rig.js writing to a detached node. */
    if (REGISTRY) {
      (this._ownIds || []).forEach((id) => { delete REGISTRY[id]; });
      this._ownIds = [];
      this._walk([]).forEach((n) => {
        if (n.attrs.id) { REGISTRY[n.attrs.id] = n; this._ownIds.push(n.attrs.id); }
      });
    }
  }
  get innerHTML() { return this._html || ""; }
}

/* ---------------------------------------------------------- the screens
 *
 * The pedal map identifies the screen, which is the operator's whole
 * contract with the app: three labels along the bottom and nothing else
 * to read. Asserting through it means a test says "we are on Recording"
 * in the same terms the operator would.
 */
const BY_PEDALS = {
  "Problem|All good|—": "checklist",
  "Gripper|Camera|CAN": "fault_class",
  "Back|Fixed|Hold to cancel": "fault_fixing",
  "—|Start|Hardware issue": "handover",
  "Discard|—|Save": "recording",
  "Usable|Good|Exemplary": "review",
  "—|Next episode|Hardware issue": "resetting",
  "End session|Problem solved|Bug testing": "rig_down",
  "—|Restart demo|—": "session_ended",
  "—|Check the rig|—": "standby",
  "—|Check again|—": "standby",
  /* The only screen in the app with nothing to press at all, which is
     what makes it recognisable here: a rig that was never told which rig
     it is must not be offered a way to start work. */
  "—|—|—": "no_identity",
};

/* =====================================================================
 * mount
 * ===================================================================== */

/* opts: { at, hash, pushed, fetchImpl, settle }
 *
 * Resolves once rig.js has booted and its (async) payload read has
 * settled - unless `settle: false`, which hands the mount back mid-flight
 * so a test can watch what the frame loop does while the payload is
 * still in the air. */
async function mountRig(opts) {
  opts = opts || {};

  const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
  const byId = {};
  const missingIds = [];
  const mk = (id) => { const e = new El("div"); e.attrs.id = id; byId[id] = e; return e; };
  (html.match(/id="([^"]+)"/g) || []).forEach((m) => mk(m.slice(4, -1)));
  REGISTRY = byId;

  const at = (opts.at || "10:37").split(":").map(Number);
  const FIXED = new Date(2026, 7, 23, at[0], at[1], at[2] || 0);
  const RealDate = global.Date;
  class FakeDate extends RealDate {
    constructor(...a) { if (!a.length) super(FIXED.getTime()); else super(...a); }
    static now() { return FIXED.getTime(); }
  }

  const timeouts = [];
  const realSetTimeout = global.setTimeout;
  const realDoc = global.document;
  const realFetch = global.fetch;
  const realRAF = global.requestAnimationFrame;
  const realAdd = global.addEventListener;
  const realPerf = global.performance;

  const listeners = {};
  let queue = [];
  let running = true;
  let nowMs = 0;

  global.Date = FakeDate;
  global.window = global;
  global.location = {
    hash: opts.hash ? "#" + opts.hash : "",
    search: opts.search || "",
    /* Real pages have one, and code that decides what is same-origin has
       to be exercised against a location that behaves like a browser's. */
    origin: opts.origin || "http://localhost:8000",
  };
  /* Only `now` is faked. Inheriting the rest matters: node's own fetch
     reaches for performance.markResourceTiming, so a stub that replaces
     the whole object breaks any test that talks to a real server. */
  global.performance = Object.create(realPerf || Object.prototype);
  global.performance.now = () => nowMs;
  global.requestAnimationFrame = (fn) => { if (running) queue.push(fn); return queue.length; };
  global.setTimeout = (fn, ms) => { const id = realSetTimeout(fn, ms); timeouts.push(id); return id; };
  global.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  global.document = {
    createElement: (t) => new El(t),
    createRange: () => ({ selectNodeContents() {}, getBoundingClientRect: () => ({ width: 200 }) }),
    getElementById: (id) => byId[id] || (missingIds.push(id), mk(id)),
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    body: new El("body"),
    fonts: null,
  };
  global.fetch = opts.fetchImpl || (() => Promise.reject(new Error("no server")));

  require(path.join(REPO, "packages/engine/rotation-engine.js"));
  const roster = require(path.join(REPO, "packages/demo-roster/demo-roster.js"));
  global.DEMO_ROSTER = structuredClone(roster);   // never let one test's edits reach the next
  global.PUSHED_SCHEDULE = opts.pushed || undefined;
  /* The journal. A test that hands the same one to two mounts is
     simulating a reload, which is the whole point of it existing. */
  global.RIG_JOURNAL = opts.journal || undefined;
  /* The token Ansible places on the machine. */
  global.RIG_TOKEN = opts.token || undefined;
  /* What the service writes into the rig-config.js it serves this
     machine. `seenAs` is the address it was recognised by, and is only
     ever shown on the no-identity screen. */
  global.RIG_ID = opts.rigId || undefined;
  global.RIG_SEEN_AS = opts.seenAs || undefined;

  const errors = [];
  new Function(fs.readFileSync(path.join(ROOT, "assets/rig.js"), "utf8"))();

  const settle = async () => { for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r)); };
  if (opts.settle !== false) await settle();

  const rig = {
    byId, missingIds, errors, listeners,
    $: (id) => byId[id],
    settle,

    /* Advance the rAF clock. Returns how many frames actually ran - if
       that is short of what was asked for, the loop has died. */
    frames(n, ms) {
      ms = ms === undefined ? 16 : ms;
      let ran = 0;
      for (let i = 0; i < n; i++) {
        const q = queue;
        queue = [];
        if (!q.length) return ran;
        nowMs += ms;
        for (const fn of q) { try { fn(nowMs); } catch (e) { errors.push(e); } }
        ran++;
      }
      return ran;
    },

    /* The three pedals, as the operator reads them. */
    pedals() {
      return byId["pedals"].querySelectorAll(".pedal").map((b) => {
        const s = b.querySelector("strong");
        return s ? s.textContent.trim() : "—";
      });
    },
    /* Which screen those pedals mean. */
    screen() {
      const k = this.pedals().join("|");
      if (BY_PEDALS[k]) return BY_PEDALS[k];
      // the issue tree relabels all three every level, so it is its own case
      if (byId["stage"].textContent.includes("Hardware issue")) return "issue_menu";
      return "unknown(" + k + ")";
    },

    press(n) { (listeners.keydown || []).forEach((fn) => fn({ key: String(n), repeat: false, preventDefault() {} })); },
    hold(n) { this.press(n); },
    lift(n) { (listeners.keyup || []).forEach((fn) => fn({ key: String(n) })); },
    demoKey(k) { (listeners.keydown || []).forEach((fn) => fn({ key: k, repeat: false, preventDefault() {} })); },
    hashTo(h) {
      global.location.hash = "#" + h;
      (listeners.hashchange || []).forEach((fn) => fn({}));
    },

    stage() { return byId["stage"].textContent.replace(/\s+/g, " ").trim(); },

    /* How many times an element has been torn down and redrawn. The
       stage is rebuilt only when what it *says* changes, and a rebuild
       during a take takes the camera panes with it - so "how often" is
       a property worth being able to assert. */
    countRebuilds(id) {
      const el = byId[id];
      const desc = Object.getOwnPropertyDescriptor(El.prototype, "innerHTML");
      let n = 0;
      Object.defineProperty(el, "innerHTML", {
        configurable: true,
        get: desc.get,
        set(v) { n++; desc.set.call(this, v); },
      });
      return () => n;
    },
    rail() {
      return {
        rig: byId["rail-rig"].textContent,
        task: byId["rail-task"].textContent,
        block: byId["rail-block"].textContent,
        next: byId["rail-next"].textContent,
        then: byId["rail-then"].textContent,
        due: byId["cell-block"].classList.contains("due"),
      };
    },
    log() { return byId["log"].querySelectorAll("p").map((p) => p.textContent.replace(/\s+/g, " ").trim()); },

    /* The envelopes the uploader would drain - what the backend actually
       receives, as opposed to the sentence the operator reads. */
    events() { return global.rigEvents ? global.rigEvents() : []; },
    eventsOf(name) { return this.events().filter((e) => e.event === name); },

    /* What the uploader is holding, and a way to drain it on demand
       rather than waiting for its timer. */
    outbox() { return global.rigOutbox ? global.rigOutbox() : null; },
    async upload() { if (global.rigFlush) await global.rigFlush(); await this.settle(); },

    /* The video path. A browser tab has no camera, so a test attaches a
       recorder of its own and drives the three steps by hand rather than
       waiting on the uploader's timer. */
    setVideoSource(fn) { if (global.setVideoSource) global.setVideoSource(fn); },
    journal() { return global.rigJournal ? global.rigJournal() : null; },

    /* Ask for a fresh schedule now rather than waiting on the timer. */
    async resync() { if (global.rigResync) await global.rigResync(); await this.settle(); },
    /* What the injected journal is still holding - the durable half of
       the outbox, as opposed to the in-memory one. */
    journalHeld() {
      return opts.journal && opts.journal.heldEvents ? opts.journal.heldEvents() : [];
    },
    journalVideos() {
      return opts.journal && opts.journal.heldVideos ? opts.journal.heldVideos() : [];
    },
    video() { return global.rigVideo ? global.rigVideo() : null; },
    async uploadVideo(times) {
      for (let i = 0; i < (times || 1); i++) {
        if (global.rigFlushVideo) await global.rigFlushVideo();
        await this.settle();
      }
    },
    logOf(event) { return this.log().filter((l) => l.includes(event)); },

    /* Every timer and every frame this mount started, stopped. */
    stop() {
      running = false;
      queue = [];
      timeouts.forEach(clearTimeout);
      REGISTRY = null;
      global.Date = RealDate;
      global.setTimeout = realSetTimeout;
      global.document = realDoc;
      global.fetch = realFetch;
      global.requestAnimationFrame = realRAF;
      global.addEventListener = realAdd;
      global.performance = realPerf;
    },
  };

  return rig;
}

module.exports = { mountRig, El, matches, BY_PEDALS };
