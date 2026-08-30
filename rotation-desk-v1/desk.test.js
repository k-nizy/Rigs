/* =====================================================================
 * desk.test.js  -  the manager's screen, tested headlessly
 *
 * The engine has its own tests and the sheet has its own transcription.
 * This file tests the screen: that it boots, that picking a shift shows
 * that shift and nothing else, that the grid really is names-down-the-
 * side, that every cell agrees with the engine, that Live counts down
 * to the right minute, and that the push sends twelve valid payloads.
 * ===================================================================== */

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { mountDesk } = require("./test/dom.js");
const { validate } = require(path.resolve(__dirname, "../packages/schema/payload.js"));

/* Runs a test with a freshly mounted desk and always tears it down, so
 * one test's stray interval or edited roster cannot reach the next. */
function withDesk(opts, fn) {
  return async () => {
    const desk = await mountDesk(opts);
    try { await fn(desk); } finally { desk.stop(); }
  };
}

/* ------------------------------------------------------------ boot */

test("boots, and asks the page for nothing the page does not have",
  withDesk({}, async desk => {
    assert.deepEqual(desk.missingIds, [],
      "desk.js looked up ids that are not in index.html: " + desk.missingIds.join(", "));
  }));

test("opens on Live, with Plan hidden",
  withDesk({}, async desk => {
    assert.equal(desk.$("view-live").hidden, false);
    assert.equal(desk.$("view-plan").hidden, true);
    assert.equal(desk.$("modes").children[0].getAttribute("aria-selected"), "true");
    assert.equal(desk.$("modes").children[1].getAttribute("aria-selected"), "false");
  }));

test("the two modes are exclusive - never both, never neither",
  withDesk({}, async desk => {
    desk.mode("plan");
    assert.equal(desk.$("view-live").hidden, true);
    assert.equal(desk.$("view-plan").hidden, false);

    desk.mode("live");
    assert.equal(desk.$("view-live").hidden, false);
    assert.equal(desk.$("view-plan").hidden, true);
  }));

/* ------------------------------------------------- one shift only */

const SHIFTS = [
  { id: "morning", label: "Morning", start: "08:00", end: "16:00" },
  { id: "day",     label: "Day",     start: "16:00", end: "00:00" },
  { id: "night",   label: "Night",   start: "00:00", end: "08:00" },
];

SHIFTS.forEach(s => {
  test("picking " + s.label + " shows " + s.label + " and nothing else",
    withDesk({}, async desk => {
      desk.mode("plan");
      desk.shift(s.id);

      // exactly one button is pressed, and it is that one
      const pressed = desk.$("seg-shift").children
        .filter(b => b.getAttribute("aria-pressed") === "true")
        .map(b => b.dataset.shift);
      assert.deepEqual(pressed, [s.id], "one shift selected, and only one");

      // said in words, once
      const chosen = desk.$("chosen").textContent;
      assert.ok(chosen.startsWith(s.label + " shift"), "the line reads: " + chosen);
      assert.ok(chosen.includes(s.start) && chosen.includes(s.end), "with its hours: " + chosen);

      SHIFTS.filter(o => o.id !== s.id).forEach(other => {
        assert.ok(!chosen.includes(other.label), chosen + " must not mention " + other.label);
        assert.ok(!desk.$("sheet-title").textContent.includes(other.label),
          "the sheet is titled for " + s.label + " only");
      });

      // and every column of the grid falls inside that shift
      const ticks = desk.grid("ops").ticks;
      assert.equal(ticks.length, 32, "32 quarter-hours");
      assert.equal(ticks[0], s.start, "the grid starts at " + s.start);

      const startMin = Number(s.start.slice(0, 2)) * 60;
      ticks.forEach((t, i) => {
        const want = (startMin + i * 15) % 1440;
        const got = Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
        assert.equal(got, want, "column " + i + " should be inside the " + s.label + " shift");
      });
    }));
});

test("changing the shift changes Live too",
  withDesk({}, async desk => {
    const before = desk.$("now-shift").textContent;
    desk.mode("plan");
    desk.shift("night");
    desk.mode("live");
    const after = desk.$("now-shift").textContent;

    assert.ok(before.includes("Morning"), before);
    assert.ok(after.includes("Night"), after);
    assert.ok(!after.includes("Morning"), after);
  }));

/* --------------------------------------- names down, time across */

test("the grid is names down the side and time across the top",
  withDesk({}, async desk => {
    desk.mode("plan");
    const g = desk.grid("ops");

    assert.equal(g.corner, "Operator", "the corner names the row axis");
    assert.equal(g.ticks.length, 32, "32 columns of time");
    assert.ok(/^\d\d:\d\d$/.test(g.ticks[0]), "columns are clock times, got " + g.ticks[0]);

    assert.equal(g.bands.length, 4, "four group bands");
    assert.equal(g.rows.length, 16, "sixteen operators down the side");

    const first = g.rows[0].label;
    assert.ok(first.includes("Aleksandr Petrov"), "rows are labelled by name, got " + first);
    assert.ok(first.includes("Op 1"), "with their number, got " + first);
    assert.ok(first.includes("6h"), "and their hours, got " + first);
  }));

test("the rig sheet is the same table with rigs down the side",
  withDesk({}, async desk => {
    desk.mode("plan");
    const g = desk.grid("rigs");
    assert.equal(g.corner, "Rig");
    assert.equal(g.rows.length, 12, "twelve rigs");
    assert.equal(g.ticks.length, 32);
    assert.ok(g.rows[0].label.includes("RIG-01"), g.rows[0].label);
  }));

/* --------------------------------------- the cells match the engine */

test("every cell of the operator grid is what the engine says",
  withDesk({}, async desk => {
    desk.mode("plan");
    const RE = desk.RE;
    const p = RE.buildPlan(
      { shift: "morning", date: "2026-08-23", blockMin: 15, stintBlocks: 3, mode: "hold" },
      desk.roster.groups);

    const rows = desk.grid("ops").rows;
    let r = 0;
    p.groups.forEach(group => {
      group.ops.forEach((op, oi) => {
        const row = rows[r++];
        for (let b = 0; b < p.nBlocks; b++) {
          const cell = group.rows[oi][b];
          const want = cell === RE.BREAK ? "Break"
                     : cell === RE.THINK ? "Think"
                     : group.rigs[cell];
          assert.equal(row.cells[b].text, want,
            op + " at block " + b + " should read " + want);
        }
      });
    });
    assert.equal(r, 16);
  }));

test("every cell of the rig grid is what the engine says",
  withDesk({}, async desk => {
    desk.mode("plan");
    const RE = desk.RE;
    const p = RE.buildPlan(
      { shift: "morning", date: "2026-08-23", blockMin: 15, stintBlocks: 3, mode: "hold" },
      desk.roster.groups);

    const rows = desk.grid("rigs").rows;
    let r = 0;
    p.groups.forEach(group => {
      group.rigs.forEach((rig, ri) => {
        const row = rows[r++];
        for (let b = 0; b < p.nBlocks; b++) {
          const oi = RE.holderAt(group, ri, b);
          assert.notEqual(oi, -1, rig + " is never unmanned (block " + b + ")");
          const parts = group.ops[oi].split(" ");
          const want = parts[0][0] + ". " + parts[parts.length - 1];
          assert.equal(row.cells[b].text, want, rig + " at block " + b);
        }
      });
    });
    assert.equal(r, 12);
  }));

test("the handover bar falls exactly where the name changes",
  withDesk({}, async desk => {
    desk.mode("plan");
    const rows = desk.grid("rigs").rows;
    rows.forEach(row => {
      row.cells.forEach((c, b) => {
        const changed = b === 0 || row.cells[b - 1].text !== c.text;
        assert.equal(c.start, changed,
          row.label + " block " + b + ": bar should be " + (changed ? "on" : "off"));
      });
    });
  }));

/* ----------------------------------------------------- editing */

test("typing a new name reaches the grid and the board",
  withDesk({}, async desk => {
    desk.mode("plan");
    const card = desk.find(desk.$("rosters"), "grp")[0];
    const nameInput = desk.find(card, "op-line")[0].children[1];

    desk.fire(nameInput, "input", { value: "Zoe Bright" });

    assert.ok(desk.grid("ops").rows[0].label.includes("Zoe Bright"),
      "the grid row is relabelled");

    desk.mode("live");
    const onFloor = desk.board().flatMap(g => g.rigs.map(r => r.op).concat(g.off.name));
    assert.ok(onFloor.includes("Zoe Bright"), "Live picks it up while nothing is pushed");
  }));

test("typing a new task reaches the band and the board",
  withDesk({}, async desk => {
    desk.mode("plan");
    const card = desk.find(desk.$("rosters"), "grp")[0];
    const taskInput = card.children[1].children[1];

    desk.fire(taskInput, "input", { value: "Peg sorting" });

    assert.ok(desk.grid("ops").bands[0].textContent.includes("Peg sorting"));
    desk.mode("live");
    assert.equal(desk.board()[0].task, "Peg sorting");
  }));

test("the tabs are exclusive - never two panels, never none",
  withDesk({ search: "?dev" }, async desk => {
    desk.mode("plan");
    const panels = ["panel-ops", "panel-rigs", "panel-push"];
    ["ops", "rigs", "push"].forEach((t, i) => {
      desk.tab(t);
      panels.forEach((id, j) => {
        assert.equal(desk.$(id).hidden, i !== j, t + " tab: " + id);
      });
    });
  }));

/* The payload tab is the exact JSON the floor is sent - the contract the
 * desk, the server and the rig all depend on. Worth keeping and worth
 * hiding: a manager who opens it learns nothing from "blockMinutes": 15,
 * and the sheet has an operator grid and a rig grid and no third thing. */

/* A stylesheet guard, not a DOM one. `hidden` on #view-live, #view-plan
 * and the sheet panels was defeated for a long time by `section {
 * display: flex }` - an author rule outranks the browser's built-in
 * [hidden] { display: none }, so both modes painted at once and the Rig
 * sheet stacked on top of the Payload panel. Every test in this file
 * passed throughout, because the stub tracks the hidden property and has
 * no stylesheet. Only a browser could see it, so this asserts the rule
 * that fixes it is still in the file. */
test("hidden actually hides - the CSS rule that makes the property work",
  async () => {
    const css = require("node:fs").readFileSync(
      require("node:path").join(__dirname, "assets/desk.css"), "utf8");
    assert.match(css, /\[hidden\]\s*\{\s*display:\s*none\s*!important/,
      "without this rule `section { display: flex }` wins and nothing is ever hidden");
  });

test("a manager sees the sheet, and only the sheet",
  withDesk({}, async desk => {
    desk.mode("plan");
    assert.deepEqual(desk.visibleTabs(), ["ops", "rigs"],
      "the payload tab is a developer surface and should not be on a manager's screen");
  }));

test("?dev reveals the payload tab",
  withDesk({ search: "?dev" }, async desk => {
    desk.mode("plan");
    assert.deepEqual(desk.visibleTabs(), ["ops", "rigs", "push"]);
  }));

test("a manager cannot be stranded on a tab they cannot see",
  withDesk({}, async desk => {
    desk.mode("plan");
    desk.tab("push");                       // as a stale link or a stray click would
    assert.equal(desk.$("panel-push").hidden, true, "the payload panel opened for a manager");
    assert.equal(desk.$("panel-ops").hidden, false, "and left them looking at nothing");
  }));

test("the payload tab still shows the real payload when asked for",
  withDesk({ search: "?dev" }, async desk => {
    desk.mode("plan");
    desk.tab("push");
    assert.equal(desk.$("panel-push").hidden, false);
    assert.match(desk.$("push-head").textContent, /RIG-01/);
    assert.match(desk.$("push-body").innerHTML, /rigId/);
  }));

/* -------------------------------------------------------- the check */

test("the check reads as one line, and stays shut when it passes",
  withDesk({}, async desk => {
    desk.mode("plan");
    assert.equal(desk.$("v-text").textContent, "Everything checks out");
    assert.equal(desk.$("fit").open, false, "no reason to open it");
    assert.match(desk.$("v-sub").textContent, /360 min work · 60 break · 60 think, each/);
    assert.equal(desk.$("fit").className, "fit ok");
  }));

/* ----------------------------------------------------------- Live */

test("Live mid-shift: the right people, counting down to the right minute",
  withDesk({ at: "10:37:22" }, async desk => {
    const board = desk.board();
    assert.equal(board.length, 4, "four groups");
    assert.equal(board[0].key, "GROUP A");

    // 10:37 is 2h37m in - block 10, which the reference sheet writes
    // 2:30 - Rig1/Rig2/Rig3 held by Op4, Op1, Op3, with Op2 on Break.
    const a = board[0];
    assert.deepEqual(a.rigs.map(r => r.rigId), ["RIG-01", "RIG-02", "RIG-03"]);
    assert.deepEqual(a.rigs.map(r => r.op),
      ["Nadia Haddad", "Aleksandr Petrov", "Tomas Rivera"]);
    assert.equal(a.off.tag, "Break");
    assert.equal(a.off.name, "Mei Chen");

    // turns end at 11:00, 10:45 and 11:15
    assert.deepEqual(a.rigs.map(r => r.left), ["22:38", "7:38", "37:38"]);
    assert.deepEqual(a.rigs.map(r => r.dest.label), ["THINK", "BREAK", "THINK"]);
    assert.deepEqual(a.rigs.map(r => r.dest.at), ["11:00", "10:45", "11:15"]);
    assert.deepEqual(a.rigs.map(r => r.relief),
      ["A. Petrov takes over", "M. Chen takes over", "N. Haddad takes over"]);

    // Chen is off, and the board says where she walks back to: RIG-02,
    // at 10:45, which is the moment RIG-02's own countdown reaches zero.
    assert.equal(a.off.dest.label, "RIG-02");
    assert.equal(a.off.dest.at, "10:45");
    assert.equal(a.off.left, "7:38");

    assert.match(desk.$("upnext").textContent,
      /^Next handover in 7:38 · RIG-02 · Aleksandr Petrov goes to break, Mei Chen takes over$/);
    assert.equal(desk.$("banner").hidden, true);
  }));

test("Live warns as a turn runs out, and only then",
  withDesk({ at: "10:41:00" }, async desk => {
    // RIG-02's turn ends at 10:45 - four minutes out, so amber
    const a = desk.board()[0];
    assert.equal(a.rigs[1].left, "4:00");
    assert.ok(a.rigs[1].leftCls.includes("warn"), a.rigs[1].leftCls);
    assert.ok(!a.rigs[0].leftCls.includes("warn"), "the other two are not warned");
    assert.ok(!a.rigs[0].leftCls.includes("crit"));
  }));

test("Live goes critical in the last minute",
  withDesk({ at: "10:44:30" }, async desk => {
    const a = desk.board()[0];
    assert.equal(a.rigs[1].left, "0:30");
    assert.ok(a.rigs[1].leftCls.includes("crit"), a.rigs[1].leftCls);
  }));

test("before the shift, Live says so and shows the opening line-up",
  withDesk({ at: "07:12:00" }, async desk => {
    assert.equal(desk.$("banner").hidden, false);
    assert.match(desk.$("banner").textContent,
      /^Morning shift starts at 08:00 - in 48m\. Showing the opening line-up\.$/);

    const a = desk.board()[0];
    assert.deepEqual(a.rigs.map(r => r.op),
      ["Aleksandr Petrov", "Mei Chen", "Tomas Rivera"]);
    // the 45/30/15 opening turns a simultaneous crew change forces
    assert.deepEqual(a.rigs.map(r => r.left), ["45 min", "30 min", "15 min"]);
    assert.deepEqual(a.rigs.map(r => r.width), ["0.0%", "0.0%", "0.0%"]);
    assert.equal(a.off.name, "Nadia Haddad");
    assert.equal(desk.$("upnext").textContent, "The shift has not started.");

    // she is told where she joins, and the chip carries the time - so
    // there is no second copy of the same clock beside it
    assert.equal(a.off.dest.label, "RIG-03");
    assert.equal(a.off.dest.at, "08:15");
    assert.equal(a.off.left, "", "nothing to count down to before the shift");
  }));

test("in the last turn of the shift, nobody is promised a relief",
  withDesk({ at: "15:52:00" }, async desk => {
    desk.board()[0].rigs.forEach(r => {
      assert.equal(r.dest.label, "OFF SHIFT");
      assert.equal(r.dest.kind, "end");
      assert.equal(r.relief, "", "there is nobody to hand over to");
    });
    // and the line at the bottom stops calling it a handover
    assert.match(desk.$("upnext").textContent,
      /^Shift ends in \d+:\d\d · everyone comes off together\.$/);
    assert.match(desk.$("now-shift").textContent, /8m left/);
  }));

/* --------------------------------------------- where people are going */

test("every row says where that person is going, and when",
  withDesk({ at: "11:04:56" }, async desk => {
    desk.board().forEach(g => {
      g.rigs.forEach(r => {
        assert.ok(r.dest, r.rigId + " has a destination");
        assert.match(r.dest.at, /^\d\d:\d\d$/, r.rigId + " says when: " + r.dest.at);
        assert.ok(["BREAK", "THINK"].includes(r.dest.label) || /^RIG-\d\d$/.test(r.dest.label),
          r.rigId + " goes somewhere real, got " + r.dest.label);
      });
    });
  }));

test("the off operator is told which rig they walk back to, and it agrees with that rig",
  withDesk({ at: "11:04:56" }, async desk => {
    const board = desk.board();
    assert.equal(board.length, 4);

    board.forEach(g => {
      assert.ok(g.off.dest, g.key + ": the off operator has a destination");
      assert.match(g.off.dest.label, /^RIG-\d\d$/, g.key + ": they go back to a rig");

      /* The bug this replaced: the off row counted in whole minutes while
       * the rig beside it counted in seconds, so one instant showed two
       * numbers. Both now come from the same subtraction. */
      const rig = g.rigs.find(r => r.rigId === g.off.dest.label);
      assert.ok(rig, g.key + ": " + g.off.dest.label + " is one of this group's rigs");
      assert.equal(g.off.left, rig.left,
        g.key + ": " + g.off.name + " returns to " + rig.rigId
        + " - the two countdowns must agree");
      assert.equal(g.off.dest.at, rig.dest.at,
        g.key + ": and so must the two clock times");
    });
  }));

test("a destination is painted for what it is",
  withDesk({ at: "11:04:56" }, async desk => {
    const kindOf = { BREAK: "brk", THINK: "thk" };
    desk.board().forEach(g => {
      g.rigs.concat([{ rigId: "off", dest: g.off.dest }]).forEach(r => {
        if (!r.dest) return;
        const want = kindOf[r.dest.label] || "to-rig";
        assert.equal(r.dest.kind, want,
          r.dest.label + " should be painted " + want + ", got " + r.dest.kind);
      });
    });
  }));

test("the rig chip is not styled as a rig row",
  withDesk({ at: "11:04:56" }, async desk => {
    /* `.dest.rig` would also match the `.rig` row selector and inherit
     * the row's padding and bottom border. The class is `to-rig` for
     * exactly that reason, and this is what keeps it that way. */
    const chip = desk.board()[0].off.dest;
    assert.equal(chip.kind, "to-rig");
    assert.ok(!chip.kind.split(/\s+/).includes("rig"),
      "the destination chip must not carry the bare `rig` class");
  }));

test("the progress bar tracks how far through a turn we are",
  withDesk({ at: "10:37:22" }, async desk => {
    // RIG-02 is 37m38s into a 45-minute turn
    const w = Number(desk.board()[0].rigs[1].width.replace("%", ""));
    assert.ok(w > 82 && w < 84, "expected about 83%, got " + w);
  }));

test("with no server, Live says it is showing the plan",
  withDesk({}, async desk => {
    assert.equal(desk.$("now-src").className, "src plan");
    assert.match(desk.$("now-src").textContent, /Not pushed/);
  }));

test("with a server, Live says it is showing the floor",
  withDesk({
    at: "10:37:22",
    fetchImpl: (url) => {
      if (String(url) === "/api/state") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({
          pushedAt: "2026-08-23T09:58:00.000Z",
          rigs: PUSHED.map(p => p.rigId),
        }) });
      }
      const id = decodeURIComponent(String(url).split("/")[3]);
      const one = PUSHED.find(p => p.rigId === id);
      return Promise.resolve({ ok: true, json: () => Promise.resolve(one) });
    },
  }, async desk => {
    assert.equal(desk.$("now-src").className, "src floor");
    assert.match(desk.$("now-src").textContent, /On the floor/);
    // the pushed floor has different people on it than the desk's roster
    assert.equal(desk.board()[0].rigs[0].op, "Pushed Person 4");
  }));

/* ------------------------------------------------------- the push */

test("Push to floor sends every rig, for every shift of the day",
  withDesk({
    fetchImpl: (url, init) => {
      if (String(url) === "/api/state") return Promise.reject(new Error("empty"));
      if (String(url) === "/api/push") {
        sent = JSON.parse(init.body).payloads;
        return Promise.resolve({ ok: true, json: () => Promise.resolve({
          ok: true, count: sent.length, pushedAt: "2026-08-23T10:00:00.000Z",
        }) });
      }
      return Promise.reject(new Error("no"));
    },
  }, async desk => {
    desk.mode("plan");
    desk.click(desk.$("btn-push"));
    await new Promise(r => setImmediate(r));
    await new Promise(r => setImmediate(r));

    /* Twelve rigs across three shifts. A payload covers one shift, so
       pushing only the one on screen is what left a rig holding a
       finished Morning schedule at 16:00 with nothing newer to pick up. */
    assert.equal(sent.length, 36, "twelve rigs, three shifts");
    sent.forEach(p => {
      const v = validate(p);
      assert.ok(v.ok, p.rigId + " failed validation: " + JSON.stringify(v.errors));
    });

    const rigs = [...new Set(sent.map(p => p.rigId))].sort();
    assert.deepEqual(rigs,
      ["RIG-01", "RIG-02", "RIG-03", "RIG-04", "RIG-05", "RIG-06",
       "RIG-07", "RIG-08", "RIG-09", "RIG-10", "RIG-11", "RIG-12"]);

    const shifts = [...new Set(sent.map(p => p.shift.label))].sort();
    assert.deepEqual(shifts, ["Day", "Morning", "Night"],
      "the whole day has to go up, or the floor stops at the first boundary");

    // Every rig gets every shift, and no shift twice.
    for (const r of rigs) {
      const mine = sent.filter(p => p.rigId === r).map(p => p.shift.label).sort();
      assert.deepEqual(mine, ["Day", "Morning", "Night"], r + " is missing a shift");
    }

    // It still fits the contract: the push route accepts at most 64.
    assert.ok(sent.length <= 64, "a push larger than the route accepts");

    assert.match(desk.$("push-note").textContent, /^Pushed 12 rigs, 3 shifts, at /);
  }));

test("a push the server rejects says so, and does not pretend",
  withDesk({
    fetchImpl: (url) => {
      if (String(url) === "/api/push") {
        return Promise.resolve({ ok: false, status: 422, json: () => Promise.resolve({
          error: "one or more payloads failed validation",
        }) });
      }
      return Promise.reject(new Error("no"));
    },
  }, async desk => {
    desk.mode("plan");
    desk.click(desk.$("btn-push"));
    await new Promise(r => setImmediate(r));
    await new Promise(r => setImmediate(r));

    assert.match(desk.$("push-note").textContent, /^Rejected: /);
    assert.equal(desk.$("btn-push").disabled, false, "the button comes back");
  }));

test("a push with no server at all says that instead",
  withDesk({}, async desk => {
    desk.mode("plan");
    desk.click(desk.$("btn-push"));
    await new Promise(r => setImmediate(r));
    await new Promise(r => setImmediate(r));
    assert.equal(desk.$("push-note").textContent, "Could not reach the server.");
  }));

/* A floor that is deliberately not the desk's roster, so a test can tell
 * which of the two Live is reading. */
let sent = null;
const PUSHED = (() => {
  global.window = global;
  require(path.resolve(__dirname, "../packages/engine/rotation-engine.js"));
  const RE = global.RotationEngine;
  const groups = [{
    key: "A", task: "Pushed task",
    rigs: ["RIG-01", "RIG-02", "RIG-03"],
    ops: ["Pushed Person 1", "Pushed Person 2", "Pushed Person 3", "Pushed Person 4"],
  }];
  const p = RE.buildPlan(
    { shift: "morning", date: "2026-08-23", blockMin: 15, stintBlocks: 3, mode: "hold" }, groups);
  return groups[0].rigs.map(r => RE.rigPayload(p, r));
})();


/* --------------------------------------------- a rota that has run out

   The badge read "On the floor - pushed 4:12 PM" all night. Every word of
   that was true and none of it was useful: by 00:05 the sheet it named
   had expired, all twelve rigs were sitting in Standby, and the one
   screen a manager would open to find that out was quietly reassuring
   them that the floor was running. */

const EXPIRED = (() => {
  global.window = global;
  require(path.resolve(__dirname, "../packages/engine/rotation-engine.js"));
  const RE = global.RotationEngine;
  const groups = [{
    key: "A", task: "Pushed task",
    rigs: ["RIG-01", "RIG-02", "RIG-03"],
    ops: ["Pushed Person 1", "Pushed Person 2", "Pushed Person 3", "Pushed Person 4"],
  }];
  const p = RE.buildPlan(
    { shift: "morning", date: "2026-08-22", blockMin: 15, stintBlocks: 3, mode: "hold" }, groups);
  return groups[0].rigs.map(r => RE.rigPayload(p, r));
})();

const servedBy = (payloads, pushedAt) => (url) => {
  if (String(url) === "/api/state") {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({
      pushedAt: pushedAt, rigs: payloads.map(x => x.rigId) }) });
  }
  const id = decodeURIComponent(String(url).split("/")[3]);
  return Promise.resolve({ ok: true, json: () => Promise.resolve(
    payloads.find(x => x.rigId === id)) });
};

test("Live says plainly when nothing on the floor covers right now",
  withDesk({ at: "10:37:22", fetchImpl: servedBy(EXPIRED, "2026-08-22T09:58:00.000Z") },
    async desk => {
      assert.equal(desk.$("now-src").className, "src dry",
        "a floor holding a rota that expired yesterday was badged as running");
      assert.match(desk.$("now-src").textContent, /Nothing scheduled for now/,
        "the badge did not say the floor had run dry");
    }));

test("it still names when the last push happened, so it can be judged",
  withDesk({ at: "10:37:22", fetchImpl: servedBy(EXPIRED, "2026-08-22T09:58:00.000Z") },
    async desk => {
      assert.match(desk.$("now-src").textContent, /last push/,
        "a manager needs to know how stale it is, not just that it is stale");
    }));

/* The badge was fixed. The rest of the screen was not, and it is drawn
   from a different clock.

   `liveState()` measures everything as minutes since the top of the
   shift, modulo a day: `nowRel = (now - start + 1440) % 1440`. There is
   no date in that, so it cannot tell "before this shift starts" from
   "after it ended" - and on a sheet whose hours happen to contain the
   current time of day it reports the shift as RUNNING, a day late, with
   live countdowns beside a badge that says nothing is scheduled.

   The desk already holds the right answer: `RE.coversAt()` is what the
   badge asks. These make the rest of the screen ask it too. */

test("a sheet that expired yesterday is not counted down as if it were running",
  withDesk({ at: "10:37:22", fetchImpl: servedBy(EXPIRED, "2026-08-22T09:58:00.000Z") },
    async desk => {
      /* 10:37 falls inside 08:00-16:00 as a time of day, so the modular
         arithmetic calls this shift running. It ended a day ago. */
      assert.doesNotMatch(desk.$("upnext").textContent, /Next handover in/,
        "the desk counted down a handover on a shift that ended yesterday, "
        + "beside a badge saying nothing is scheduled");
    }));

test("nor described as one that has not started",
  withDesk({ at: "10:37:22", fetchImpl: servedBy(EXPIRED, "2026-08-22T09:58:00.000Z") },
    async desk => {
      const banner = desk.$("banner").textContent;
      assert.doesNotMatch(banner, /starts at/,
        "a finished shift was announced as one that is about to start: " + banner);
      assert.doesNotMatch(desk.$("upnext").textContent, /has not started/,
        "and the line under the board said it had not started");
    }));

test("it says the shift has ended, which is the one thing that is true",
  withDesk({ at: "10:37:22", fetchImpl: servedBy(EXPIRED, "2026-08-22T09:58:00.000Z") },
    async desk => {
      const said = desk.$("banner").textContent + " " + desk.$("upnext").textContent;
      assert.match(said, /ended/,
        "the screen never said the shift was over - it is the only honest "
        + "description of a sheet whose window has closed");
    }));

test("yesterday's Day sheet read at 02:19 is not a shift about to start",
  (async () => {
    /* Exactly what was on screen: the floor had only the 29th, the server
       handed back the shift that finished most recently, and the desk
       announced "Day shift starts at 16:00 - in 13h 44m" for one that ran
       yesterday afternoon and ended at midnight. The badge beside it read
       "Nothing scheduled for now". */
    const YESTERDAY_DAY = (() => {
      global.window = global;
      require(path.resolve(__dirname, "../packages/engine/rotation-engine.js"));
      const RE = global.RotationEngine;
      const groups = [{
        key: "A", task: "Pushed task",
        rigs: ["RIG-01", "RIG-02", "RIG-03"],
        ops: ["Pushed Person 1", "Pushed Person 2", "Pushed Person 3", "Pushed Person 4"],
      }];
      const p = RE.buildPlan(
        { shift: "day", date: "2026-08-22", blockMin: 15, stintBlocks: 3, mode: "hold" }, groups);
      return groups[0].rigs.map(r => RE.rigPayload(p, r));
    })();

    const desk = await mountDesk({
      at: "02:19:00", fetchImpl: servedBy(YESTERDAY_DAY, "2026-08-22T02:35:00.000Z") });
    try {
      const banner = desk.$("banner").textContent;
      assert.doesNotMatch(banner, /starts at/,
        "a shift that ended at midnight was announced as starting in 13h: " + banner);
      assert.doesNotMatch(desk.$("upnext").textContent, /has not started/,
        "and the board said it had not started");
    } finally { desk.stop(); }
  }));

test("a sheet whose shift really has not started still says so",
  (async () => {
    /* The control, and the case the old arithmetic got right. Tomorrow's
       Morning sheet, read the evening before. */
    const AHEAD = (() => {
      global.window = global;
      require(path.resolve(__dirname, "../packages/engine/rotation-engine.js"));
      const RE = global.RotationEngine;
      const groups = [{
        key: "A", task: "Pushed task",
        rigs: ["RIG-01", "RIG-02", "RIG-03"],
        ops: ["Pushed Person 1", "Pushed Person 2", "Pushed Person 3", "Pushed Person 4"],
      }];
      const p = RE.buildPlan(
        { shift: "morning", date: "2026-08-24", blockMin: 15, stintBlocks: 3, mode: "hold" },
        groups);
      return groups[0].rigs.map(r => RE.rigPayload(p, r));
    })();

    const desk = await mountDesk({
      at: "18:20:00", fetchImpl: servedBy(AHEAD, "2026-08-23T18:00:00.000Z") });
    try {
      assert.match(desk.$("banner").textContent, /starts at 08:00/,
        "a sheet for tomorrow morning should still count down to it");
      assert.match(desk.$("upnext").textContent, /has not started/);
    } finally { desk.stop(); }
  }));

test("a floor that is genuinely running is still badged as running",
  withDesk({ at: "10:37:22", fetchImpl: servedBy(PUSHED, "2026-08-23T09:58:00.000Z") },
    async desk => {
      /* The control. Without it the test above would pass on a desk that
         had simply stopped believing in the floor altogether. */
      assert.equal(desk.$("now-src").className, "src floor");
      assert.match(desk.$("now-src").textContent, /On the floor/);
    }));


/* ------------------------------------------- whose clock the desk reads

   Every "HH:MM" on this screen is time where the rigs are, and the
   payload carries the zone the desk wrote when it pushed. The desk was
   reading them against whatever browser happened to be looking, so a desk
   opened from another zone announced "the shift has not started" while
   the floor was hours into it - and the twelve cards showed the opening
   line-up rather than who was actually on.

   The backend was fixed for this, then the rig, then this. Same mistake,
   three places, because each one had its own idea of "now". */

function pushedIn(tz) {
  return PUSHED.map(p => {
    const copy = JSON.parse(JSON.stringify(p));
    copy.shift.tz = tz;
    return copy;
  });
}

/* These two zones used to be written down as Etc/GMT-2 and Etc/GMT-3,
   with a comment saying UTC+2 was "the zone the test machine runs in".
   That was true of one machine. CI runs in UTC and failed both of them
   the first time it ran, which is the whole argument for having CI: a
   test about reading the right clock could only pass on the clock it was
   written on.

   So the zones are derived from wherever this is running. The harness
   builds its fixed instant with `new Date(y, m, d, H, M)`, which is local
   wall time, so "the same building" is this machine's own zone and "one
   hour east" is the next whole hour beyond it - a real difference in any
   zone, including the half-hour ones, and never zero. */
const CLOCK_AT = "10:37:22";
const FIXED_AT = new Date(2026, 7, 23, 10, 37, 22);
const LOCAL_MIN = -FIXED_AT.getTimezoneOffset();          // minutes east of UTC

/* Etc/GMT-N is UTC+N and Etc/GMT+N is UTC-N - the sign is inverted, and
   getting that wrong builds names like "Etc/GMT--4" on any machine west
   of UTC. The range stops at Etc/GMT-14, so a floor at UTC+14 has no
   whole hour east of it and takes the one west instead; all that matters
   is that the two zones differ. */
const etcGMT = (h) => (h === 0 ? "Etc/GMT" : h > 0 ? "Etc/GMT-" + h : "Etc/GMT+" + -h);
const OTHER_HOURS = Math.floor(LOCAL_MIN / 60) + 1 <= 14
  ? Math.floor(LOCAL_MIN / 60) + 1
  : Math.ceil(LOCAL_MIN / 60) - 1;

const SAME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;
const OTHER_ZONE = etcGMT(OTHER_HOURS);
const OTHER_SHIFT = OTHER_HOURS * 60 - LOCAL_MIN;         // never 0

/* "10:37" plus n minutes, which is all the assertions below need. */
function clockPlus(minutes) {
  const local = new Date(FIXED_AT);
  local.setMinutes(local.getMinutes() + minutes);
  return String(local.getHours()).padStart(2, "0") + ":"
       + String(local.getMinutes()).padStart(2, "0");
}

test("the clock on the desk is the floor's, not the browser's",
  withDesk({ at: CLOCK_AT, fetchImpl: servedBy(pushedIn(OTHER_ZONE), "2026-08-23T09:58:00.000Z") },
    async desk => {
      const onThatFloor = clockPlus(OTHER_SHIFT);
      assert.notEqual(onThatFloor, clockPlus(0),
        "the two zones must differ or this test proves nothing");
      assert.equal(desk.$("now-time").textContent, onThatFloor,
        "the desk showed its own clock; on that floor it is " + onThatFloor);
    }));

test("a desk in the same building as the floor is unchanged",
  withDesk({ at: CLOCK_AT, fetchImpl: servedBy(pushedIn(SAME_ZONE), "2026-08-23T09:58:00.000Z") },
    async desk => {
      /* The normal case, and the reason this was never noticed. */
      assert.equal(desk.$("now-time").textContent, clockPlus(0));
    }));

test("the board shows who is on now, by the floor's clock",
  withDesk({ at: "10:37:22", fetchImpl: servedBy(pushedIn(OTHER_ZONE), "2026-08-23T09:58:00.000Z") },
    async desk => {
      /* The clock is only the visible half. What matters is that the
         twelve cards name the operators who are actually at the rigs. */
      const here = desk.board()[0].rigs.map(r => r.op).join(",");
      assert.ok(here.length, "the board is empty");
      assert.match(desk.$("now-src").className, /floor/,
        "a running floor was badged as not running");
    }));
