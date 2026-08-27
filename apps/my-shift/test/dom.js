/* =====================================================================
 * test/dom.js
 *
 * Just enough DOM to run my-shift.js under node, the same way the desk
 * and the rig are tested - headlessly, on every `npm test`.
 *
 * The element registry is read out of index.html rather than listed
 * here, so the stub cannot drift from the page: anything the script asks
 * for that the page does not have lands in `missingIds`, which is itself
 * a test.
 * ===================================================================== */

"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const REPO = path.resolve(ROOT, "..", "..");

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
    this.value = "";
    this._on = {};
    const self = this;
    this.classList = {
      add(c) { if (!self.classList.contains(c)) self.className = (self.className ? self.className + " " : "") + c; },
      remove(c) { self.className = self.className.split(/\s+/).filter(x => x && x !== c).join(" "); },
      contains(c) { return self.className.split(/\s+/).includes(c); },
    };
  }
  appendChild(n) { this.children.push(n); return n; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(type, fn) { (this._on[type] = this._on[type] || []).push(fn); }
  set textContent(v) { this.children = []; this._text = v === "" ? null : v; }
  get textContent() {
    if (this._text != null) return this._text;
    return this.children.map(c => c.textContent).join("");
  }
}

function find(node, cls) {
  const out = [];
  const walk = n => {
    if (n.className && n.className.split(/\s+/).includes(cls)) out.push(n);
    (n.children || []).forEach(walk);
  };
  walk(node);
  return out;
}

/* opts: { at: "HH:MM", fetchImpl } */
async function mountMyShift(opts) {
  opts = opts || {};
  const at = (opts.at || "11:12").split(":");
  const FIXED = new Date(2026, 7, 27, Number(at[0]), Number(at[1]), Number(at[2] || 0));
  const RealDate = global.Date;
  class FakeDate extends RealDate {
    constructor(...a) { if (!a.length) super(FIXED.getTime()); else super(...a); }
    static now() { return FIXED.getTime(); }
  }

  const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
  const byId = {};
  const missingIds = [];
  const mk = id => { const e = new El("div"); e.attrs.id = id; byId[id] = e; return e; };
  (html.match(/id="([^"]+)"/g) || []).forEach(m => mk(m.slice(4, -1)));

  const intervals = [];
  const realSetInterval = global.setInterval;

  global.Date = FakeDate;
  global.setInterval = (fn, ms) => { const id = realSetInterval(fn, ms); intervals.push(id); return id; };
  global.document = {
    createElement: t => new El(t),
    createTextNode: t => { const n = new El("#text"); n.textContent = String(t); return n; },
    getElementById: id => byId[id] || (missingIds.push(id), mk(id)),
    body: new El("body"),
  };
  global.window = global;
  global.fetch = opts.fetchImpl || (() => Promise.reject(new Error("no server")));

  // A fresh session module per mount - its state is a singleton, and one
  // test's sign-in must not reach the next.
  delete require.cache[require.resolve(path.join(REPO, "packages/session/session.js"))];
  require(path.join(REPO, "packages/session/session.js"));

  const src = fs.readFileSync(path.join(ROOT, "assets/my-shift.js"), "utf8");
  new Function(src)();

  for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));

  return {
    byId,
    missingIds,
    $: id => byId[id],
    find,

    fire(node, type, patch) {
      Object.assign(node, patch || {});
      (node._on[type] || []).forEach(fn => fn({ target: node }));
    },
    click(node) { this.fire(node, "click"); },

    /* Which of the three screens is up, as one word. */
    showing() {
      if (!byId["view-signin"].hidden) return "signin";
      if (!byId["view-denied"].hidden) return "denied";
      if (!byId["view-day"].hidden) return "day";
      return "nothing";
    },

    /* The timeline, read back as plain rows. */
    rows() {
      return byId["timeline"].children.map(li => ({
        kind: li.attrs["data-kind"],
        at: li.children[0].textContent,
        what: find(li, "tl-what")[0].textContent,
        sub: find(li, "tl-sub")[0].textContent,
        isNow: li.classList.contains("is-now"),
        isPast: li.classList.contains("is-past"),
      }));
    },

    now() {
      const box = byId["now"];
      return {
        kind: box.attrs["data-kind"] || null,
        text: box.textContent,
        left: find(box, "now-left").map(n => n.textContent)[0] || "",
      };
    },

    budget() {
      return {
        hidden: byId["budget"].hidden,
        work: byId["b-work"].textContent,
        break: byId["b-break"].textContent,
        think: byId["b-think"].textContent,
        note: byId["budget-note"].textContent,
      };
    },

    async settle() { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); },

    stop() {
      intervals.forEach(clearInterval);
      global.Date = RealDate;
      global.setInterval = realSetInterval;
    },
  };
}

module.exports = { mountMyShift, El, find };
