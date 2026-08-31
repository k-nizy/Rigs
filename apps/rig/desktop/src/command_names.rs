// The commands the shell exposes to the page, named once.
//
// This file is `include!`d twice on purpose: by `build.rs`, which turns
// each name into an `allow-<name>` permission at build time, and by
// `bridge.rs`, which asks for those permissions when it builds the
// runtime capability. Two lists that had to be kept in step by hand
// would drift, and the way they would fail is the worst kind - the
// binary compiles, the page loads, and one command is silently denied at
// the moment an operator uses it.
//
// Adding a command means adding it here, registering it in
// `generate_handler!`, and putting it on `window.RIG_JOURNAL`. The first
// two are checked by the compiler; the third is not, which is what the
// headless bridge test is for.

pub const COMMANDS: &[&str] = &[
    "rig_journal_load",
    "rig_journal_append_event",
    "rig_journal_forget_events",
    "rig_journal_put_stint",
    "rig_journal_forget_video",
    "rig_journal_put_video",
    "rig_journal_read_video",
];
