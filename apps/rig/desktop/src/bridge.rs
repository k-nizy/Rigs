// The bridge: how the page reaches the disk journal.
//
// `apps/rig/assets/rig.js` already names the seam and says who is meant
// to fill it:
//
//     `window.RIG_JOURNAL` is the seam. Tauri injects a disk-backed one
//     and nothing below this line changes; the tests inject one that
//     survives a remount, which is what a reload is.
//
// So this file injects exactly that object and nothing more. It does not
// change how the rig works, it does not know what an event is, and the
// page cannot tell the difference between this and the IndexedDB one
// except that this one survives a power cut.
//
// ---------------------------------------------------------------------
// Why the capability is built at runtime
//
// Tauri gates IPC by execution context. A page served from a remote
// origin - which every real rig page is, because the kiosk loads it from
// the floor - is denied every command unless a capability names its URL.
// Capabilities are normally static JSON compiled into the binary.
//
// That cannot work here. `RIG_URL` is a deployment fact, the same value
// on all twelve machines, set by Ansible when it provisions the host. It
// is not known when this binary is built, and a wildcard capability that
// allowed any origin would mean any page the shell was ever pointed at
// could write to this rig's journal.
//
// So the capability is built when the shell starts, from the one URL it
// was configured with, and nothing else is granted. If `RIG_URL` is
// unset the page is local, `local: true` covers it, and no remote origin
// is trusted at all.
//
// ---------------------------------------------------------------------
// Why the shell still does not know which rig it is
//
// It does not. `rigId` arrives as an argument on every call, from the
// page, which got it from the service. The journal keys on it and hands
// back only that rig's rows. Nothing here reads `RIG_ID`, and nothing
// should - `main.rs` explains at length why a shell that can name a rig
// is a shell that can name the wrong one, and a journal is not an
// exception to it.

use std::path::PathBuf;

use serde_json::{json, Value};
use tauri::ipc::CapabilityBuilder;
use tauri::{Manager, State};

use crate::journal::Journal;

/// Where the journal lives. `/var/lib/rig` on a rig, overridable so a
/// developer - and the headless test - does not need a system directory.
const JOURNAL_DIR: &str = "RIG_JOURNAL_DIR";
const DEFAULT_DIR: &str = "/var/lib/rig";

pub fn journal_dir() -> PathBuf {
    std::env::var(JOURNAL_DIR)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_DIR))
}

// Errors come back as strings because the page has exactly one thing it
// can do with them, and it already does it: rig.js catches a journal
// failure, sets `journalBroken`, and stops promising the operator that
// work is safe. A structured error would give it nothing more to say.
type Reply<T> = Result<T, String>;

fn oops(e: impl std::fmt::Display) -> String {
    e.to_string()
}

#[tauri::command]
pub fn rig_journal_load(rig_id: String, journal: State<'_, Journal>) -> Reply<Value> {
    let held = journal.load(&rig_id).map_err(oops)?;
    // The shape `rig.js` expects from every journal: events oldest
    // first, the takes still held, and the stint in progress.
    Ok(json!({
        "events": held.events,
        "videos": held.videos,
        "stint": held.stint,
    }))
}

#[tauri::command]
pub fn rig_journal_append_event(event: Value, journal: State<'_, Journal>) -> Reply<()> {
    journal.append_event(&event).map_err(oops)
}

#[tauri::command]
pub fn rig_journal_forget_events(ids: Vec<String>, journal: State<'_, Journal>) -> Reply<()> {
    journal.forget_events(&ids).map_err(oops)
}

#[tauri::command]
pub fn rig_journal_put_stint(row: Value, journal: State<'_, Journal>) -> Reply<()> {
    journal.put_stint(&row).map_err(oops)
}

#[tauri::command]
pub fn rig_journal_forget_video(key: String, journal: State<'_, Journal>) -> Reply<()> {
    journal.forget_video(&key).map_err(oops)
}

// The commands the shell exposes, named once and shared with `build.rs`.
// See `command_names.rs` for why it is included rather than duplicated.
include!("command_names.rs");

/// Trust exactly the origin this shell was pointed at, and nothing else.
///
/// `url` is the address the webview actually opened, so the origin that
/// is trusted and the origin that is loaded cannot disagree. `None` is
/// the bundled fallback page, which needs the capability just as much:
/// declaring an app manifest in `build.rs` turned the ACL on for local
/// pages too, so "no remote" means a local-only grant, not no grant.
pub fn allow_remote(app: &tauri::AppHandle, url: Option<&str>) -> tauri::Result<()> {
    let mut cap = CapabilityBuilder::new("rig-journal").window("rig");
    if let Some(url) = url {
        // The ORIGIN, not the address. Tauri matches a request against
        // the page's origin - `http://floor.internal/` - and never its
        // path, so granting the configured URL verbatim grants nothing
        // and the page is refused every command. Both spellings are
        // registered because which one matches is urlpattern's business
        // and not something this should depend on.
        //
        // The widening is real and worth naming: every page on the
        // floor's origin can reach this rig's journal, not only
        // /apps/rig/. It is not a meaningful loss - anyone who can serve
        // a page from that origin already serves the rig page itself,
        // and the journal is local to this machine - but it is a wider
        // grant than it looks, and Tauri offers no narrower one.
        for pattern in origins(url) {
            cap = cap.remote(pattern);
        }
    }
    for c in COMMANDS {
        // `build.rs` autogenerates one `allow-<slug>` permission per
        // command, slugified - rig_journal_load becomes
        // allow-rig-journal-load.
        cap = cap.permission(format!("allow-{}", c.replace('_', "-")));
    }
    app.add_capability(cap)
}

/// The origin of a configured address, in the spellings a capability may
/// need. `http://floor.internal/apps/rig/` gives `http://floor.internal`
/// and `http://floor.internal/*`.
fn origins(url: &str) -> Vec<String> {
    match tauri::Url::parse(url) {
        Ok(u) => {
            let o = u.origin().ascii_serialization();
            // "null" is what an opaque origin serialises to - a data: or
            // file: URL. Trusting that would trust every opaque origin.
            if o == "null" {
                Vec::new()
            } else {
                vec![format!("{o}/*"), o]
            }
        }
        Err(_) => Vec::new(),
    }
}

/// The object `rig.js` looks for, defined before any page script runs.
///
/// `durable: true` is the claim this whole file exists to make good on:
/// the page uses it to decide whether it may tell an operator their work
/// is safe. It is only true because every append below is fsynced before
/// its promise resolves.
pub fn init_script() -> String {
    // `__TAURI_INTERNALS__.invoke` rather than the `@tauri-apps/api`
    // module, because the page is an ordinary script served by the floor
    // and has no bundler to import one with.
    r#"
(function () {
  var call = function (cmd, args) {
    return window.__TAURI_INTERNALS__.invoke(cmd, args || {});
  };
  window.RIG_JOURNAL = {
    durable: true,
    load: function (rigId) {
      return call("rig_journal_load", { rigId: rigId }).then(function (h) {
        return { events: h.events || [], videos: h.videos || [], stint: h.stint || null };
      });
    },
    appendEvent: function (ev) { return call("rig_journal_append_event", { event: ev }); },
    forgetEvents: function (ids) { return call("rig_journal_forget_events", { ids: ids }); },
    putStint: function (row) { return call("rig_journal_put_stint", { row: row }); },
    forgetVideo: function (key) { return call("rig_journal_forget_video", { key: key }); },
    /* Not yet on disk. Video is tens of megabytes a take and wants the
       raw request body rather than a JSON array of numbers, so it is a
       separate piece of work. Until then the page keeps the take in
       memory for this boot, which is what it did before this file
       existed - no worse, and it must not silently claim otherwise. */
    putVideo: function () { return Promise.resolve(); }
  };
})();
"#
    .to_string()
}
