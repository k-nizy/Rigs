/* =====================================================================
 * demo-roster.js
 *
 * The floor both apps open with, and the settings that reproduce the
 * reference sheet exactly: 15-minute blocks, three-block turns, each
 * operator holding one rig until their break.
 *
 * This is demo data. In a deployment the desk gets the roster from
 * whatever holds staff records, and the rig gets nothing but the payload
 * the desk pushes it - it never needs this file. It lives in packages/ so
 * the two apps cannot drift into disagreeing about the example floor.
 * ===================================================================== */

(function (root) {
  "use strict";

  root.DEMO_ROSTER = {
    /* Settings that regenerate the reference sheet cell for cell. */
    defaults: {
      shift: "morning",
      blockMin: 15,
      stintBlocks: 3,
      mode: "hold",
    },

    /* Four groups: three rigs and four operators each, one task per group
     * for the whole shift so an operator works one skill all day. */
    groups: [
      { key: "A", task: "Box transfer - bin to conveyor",
        rigs: ["RIG-01", "RIG-02", "RIG-03"],
        ops:  ["Aleksandr Petrov", "Mei Chen", "Tomas Rivera", "Nadia Haddad"] },
      { key: "B", task: "Cable routing - harness to clip",
        rigs: ["RIG-04", "RIG-05", "RIG-06"],
        ops:  ["Grace Okonkwo", "Ivan Novak", "Priya Raman", "Ben Carter"] },
      { key: "C", task: "Cloth fold - flat to quarters",
        rigs: ["RIG-07", "RIG-08", "RIG-09"],
        ops:  ["Sofia Lindqvist", "Hassan Dawood", "Yuki Tanaka", "Marcus Bell"] },
      { key: "D", task: "Cup stacking - tray to rack",
        rigs: ["RIG-10", "RIG-11", "RIG-12"],
        ops:  ["Lena Vogel", "Diego Salas", "Amara Nwosu", "Kai Nakamura"] },
    ],
  };

  root.DEMO_ROSTER.allRigs = root.DEMO_ROSTER.groups
    .reduce(function (all, g) { return all.concat(g.rigs); }, []);

  if (typeof module === "object" && module.exports) module.exports = root.DEMO_ROSTER;

})(typeof window !== "undefined" ? window : globalThis);
