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
    this.style = { _p: {}, setProperty(k, v) { this._p[k] = v; }, width: "" };
    this._on = {};
    const self = this;
    this.classList = {
      add(c) { if (!self.classList.contains(c)) self.className = (self.className ? self.className + " " : "") + c; },
      remove(c) { self.className = self.className.split(/\s+/).filter(x => x && x !== c).join(" "); },
      contains(c) { return self.className.split(/\s+/).includes(c); },
    };
  }
  appendChild(n) { this.children.push(n); return n; }
  /* Real elements have these; a stub without them turns "the script
     moved the caret" into "the screen threw". */
  focus() {}
  blur() {}
  /* The DOM's own name for the same list. The render code asks whether
     it appended anything before showing a line; without this it reads
     undefined and throws. */
  get childNodes() { return this.children; }
  /* The perch asks whether the hero has scrolled away. Headlessly
     nothing scrolls, so the hero is always on screen and the perch
     always hidden - which is the state the tests care about. */
  getBoundingClientRect() { return { top: 0, bottom: 100, height: 100 }; }
  scrollIntoView() { this._scrolledTo = true; }
  querySelector() { return null; }
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

  /* Start hidden if the page says hidden. Everything used to mount
     visible whatever the markup said, so a panel the page ships closed
     read as open - the harness disagreeing with the page about the
     page's own initial state. */
  (html.match(/<[a-zA-Z][^>]*>/g) || []).forEach(tag => {
    const id = (tag.match(/\bid="([^"]+)"/) || [])[1];
    if (id && byId[id] && /\shidden(\s|>|=)/.test(tag)) byId[id].hidden = true;
  });

  const intervals = [];
  const timerFns = [];
  const realSetInterval = global.setInterval;

  global.Date = FakeDate;
  global.setInterval = (fn, ms) => {
    /* Kept so a test can make time pass on purpose rather than waiting
       sixty seconds for the page to re-read its own shift. */
    timerFns.push(fn);
    const id = realSetInterval(fn, ms);
    intervals.push(id);
    return id;
  };
  const docListeners = {};
  global.document = {
    querySelector: () => null,
    createElement: t => new El(t),
    createTextNode: t => { const n = new El("#text"); n.textContent = String(t); return n; },
    getElementById: id => byId[id] || (missingIds.push(id), mk(id)),
    body: new El("body"),
    /* A real document has these, and the page uses one for Escape.
       Without them the script throws at the top level and takes the
       whole screen down - a failure a browser would never have had. */
    addEventListener: (type, fn) => {
      (docListeners[type] = docListeners[type] || []).push(fn);
    },
    removeEventListener: (type, fn) => {
      docListeners[type] = (docListeners[type] || []).filter(f => f !== fn);
    },
  };
  global.window = global;
  global.location = {
    search: opts.search || "",
    /* A reset link is a fragment now, so a test that opens one sets
       this. The server never sees it, which is why the token is here. */
    hash: opts.hash || "",
    pathname: "/apps/my-shift/",
  };
  /* A real one, because the page uses `replaceState` to take a reset
     token out of the address bar - and "did the credential leave the
     URL" is a question only answerable if the stub actually moves. */
  global.history = {
    replaceState(_state, _title, url) {
      const [rest, frag] = String(url).split("#");
      const [path, query] = rest.split("?");
      global.location.pathname = path;
      global.location.search = query ? "?" + query : "";
      global.location.hash = frag ? "#" + frag : "";
    },
  };
  /* The page listens for scroll to keep the perch in step. Nothing
     scrolls headlessly, so this only has to exist. */
  const listeners = {};
  global.addEventListener = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
  global.removeEventListener = () => {};
  global.scrollTo = () => {};
  global.fetch = opts.fetchImpl || (() => Promise.reject(new Error("no server")));

  // A fresh session module per mount - its state is a singleton, and one
  // test's sign-in must not reach the next.
  delete require.cache[require.resolve(path.join(REPO, "packages/session/session.js"))];
  require(path.join(REPO, "packages/session/session.js"));
  require(path.join(REPO, "packages/engine/rotation-engine.js"));

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

    /* A key press at the document, the way a real one arrives. */
    key(name) {
      (docListeners["keydown"] || []).forEach(fn => fn({ key: name }));
    },

    /* What is in the address bar now. */
    url() {
      return global.location.pathname + global.location.search
               + global.location.hash;
    },

    /* Which of the three screens is up, as one word. */
    showing() {
      if (!byId["view-signin"].hidden) return "signin";
      if (!byId["view-denied"].hidden) return "denied";
      if (!byId["view-day"].hidden) return "day";
      return "nothing";
    },

    /* The timeline, read back as plain rows. */
    rows() {
      return byId["timeline"].children.map(li => {
        const body = find(li, "tl-b")[0];
        const sub = find(li, "tl-sub")[0];
        return {
          kind: li.attrs["data-kind"],
          at: li.children[0].textContent,
          what: find(li, "tl-what")[0].textContent,
          rig: (find(li, "mono")[0] || {}).textContent || null,
          mins: (find(li, "tl-mins")[0] || {}).textContent || null,
          sub: sub ? sub.textContent : "",
          height: body && body.style ? body.style._p["--h"] : null,
          hidden: !!li.hidden,
          isNow: li.classList.contains("is-now"),
          isPast: li.classList.contains("is-past"),
        };
      });
    },

    now() {
      const box = byId["now"];
      return {
        kind: box.attrs["data-kind"] || null,
        text: box.textContent,
        label: find(box, "now-k").map(n => n.textContent)[0] || "",
        where: find(box, "now-where").map(n => n.textContent)[0] || "",
        left: find(box, "now-left").map(n => n.textContent)[0] || "",
        unit: find(box, "now-unit").map(n => n.textContent)[0] || "",
        hand: find(box, "now-hand").map(n => n.textContent)[0] || "",
        barWidth: (() => {
          const bar = find(box, "bar")[0];
          const fill = bar && bar.children[0];
          return fill && fill.style ? fill.style.width : null;
        })(),
      };
    },

    next() {
      const box = byId["next"];
      return {
        hidden: box.hidden,
        kind: box.attrs["data-kind"] || null,
        text: box.textContent,
        what: find(box, "next-what").map(n => n.textContent)[0] || "",
        sub: find(box, "next-sub").map(n => n.textContent)[0] || "",
        in: find(box, "next-in").map(n => n.textContent)[0] || "",
      };
    },

    progress() {
      const box = byId["progress"];
      return { hidden: box.hidden, text: box.textContent };
    },

    clock() {
      return {
        hidden: byId["clock"].hidden,
        time: byId["clock-time"].textContent,
        zone: byId["clock-zone"].textContent,
      };
    },

    pastToggle() {
      const b = byId["past-toggle"];
      return { hidden: b.hidden, text: b.textContent,
               expanded: b.attrs["aria-expanded"] };
    },

    /* Only the rows an operator can actually see. */
    visibleRows() { return this.rows().filter(r => !r.hidden); },

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

    /* Make time pass: run the page's own timers once, which is how it
       re-reads its shift and redraws. Reaching into the script's
       internals would test something the browser never does. */
    async tick() {
      timerFns.forEach(fn => fn());
      await this.settle();
    },
    async reload() { await this.tick(); },

    stop() {
      intervals.forEach(clearInterval);
      global.Date = RealDate;
      global.setInterval = realSetInterval;
    },
  };
}

module.exports = { mountMyShift, El, find };
