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
use tauri::ipc::{CapabilityBuilder, InvokeBody, Request, Response};
use tauri::{Manager, State};

use crate::journal::Journal;

/// Where the journal lives. Answered in `journal.rs`, because the
/// uploader asks the same question and has to get the same answer.
pub fn journal_dir() -> PathBuf {
    crate::journal::dir_from_env()
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

/// Where a take's metadata rides while its bytes are the body.
const VIDEO_META: &str = "x-rig-video";

/// A take, and the bytes of it.
///
/// The only command here that is not JSON. A take is tens of megabytes,
/// and Tauri's JSON channel would carry it as an array of numbers - one
/// decimal string per byte on the way out, one heap-allocated `Value`
/// per byte on the way in. The raw channel hands over the buffer the
/// recorder already had.
///
/// So the bytes are the body and the metadata travels in a header. That
/// header is plain JSON, escaped to ASCII by the page: a header value
/// may carry only visible ASCII, and JSON's own `\uXXXX` escape is both
/// ASCII and understood by serde_json without being asked.
#[tauri::command]
pub fn rig_journal_put_video(request: Request<'_>, journal: State<'_, Journal>) -> Reply<()> {
    let meta = video_meta(
        request
            .headers()
            .get(VIDEO_META)
            .and_then(|h| h.to_str().ok()),
    )?;
    let bytes = match request.body() {
        InvokeBody::Raw(b) => b.as_slice(),
        // JSON here means the take was sent the ordinary way. Refusing is
        // the point: accepting it would write a file of decimal digits
        // and then tell the operator the take was safe.
        InvokeBody::Json(_) => return Err("a video must be sent as bytes, not JSON".into()),
    };
    journal.put_video(&meta, bytes).map_err(oops)
}

/// The bytes of one held take, on demand.
///
/// `load` lists takes without their bytes, so a boot with a full queue
/// costs a directory read rather than several hundred megabytes. The
/// page asks for a take when the uploader reaches it, which is what the
/// `blob` handle in `init_script` is.
#[tauri::command]
pub fn rig_journal_read_video(key: String, journal: State<'_, Journal>) -> Reply<Response> {
    journal.read_video(&key).map(Response::new).map_err(oops)
}

/// The metadata that came with a take, out of its header.
///
/// `key` and `rigId` are checked here rather than left to the journal
/// because they are the two that make a take findable again: without a
/// key nothing can name the file, and without a rigId `load` will never
/// hand it back. A take on disk that no boot will offer to upload is
/// indistinguishable from one that was never recorded.
fn video_meta(header: Option<&str>) -> Reply<Value> {
    let text = header.ok_or("a video arrived with no metadata")?;
    let meta: Value =
        serde_json::from_str(text).map_err(|e| format!("a video's metadata is not JSON: {e}"))?;
    for field in ["key", "rigId"] {
        if meta.get(field).and_then(Value::as_str).is_none() {
            return Err(format!("a video's metadata has no {field}"));
        }
    }
    Ok(meta)
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
  var internals = window.__TAURI_INTERNALS__;
  var call = function (cmd, args) { return internals.invoke(cmd, args || {}); };

  /* A header value may carry only visible ASCII. JSON's own \uXXXX escape
     is ASCII and serde_json reads it back without being asked, so
     escaping the rest costs nothing and settles the question of what a
     service might one day put in a rig's name. */
  function headerJson(o) {
    return JSON.stringify(o).replace(/[\u007f-\uffff]/g, function (c) {
      return "\\u" + ("000" + c.charCodeAt(0).toString(16)).slice(-4);
    });
  }

  /* The bytes of a held take, fetched when the uploader reaches it rather
     than at boot - a full queue is several hundred megabytes and none of
     it is needed yet. rig.js checks that `blob` is there and later awaits
     `blob.arrayBuffer()`; those are the only two things it does with one,
     so this is the whole contract. */
  function heldBytes(key) {
    return {
      arrayBuffer: function () { return call("rig_journal_read_video", { key: key }); }
    };
  }

  window.RIG_JOURNAL = {
    durable: true,
    load: function (rigId) {
      return call("rig_journal_load", { rigId: rigId }).then(function (h) {
        var videos = (h.videos || []).map(function (v) {
          return { key: v.key, rigId: v.rigId, episodeId: v.episodeId,
                   camera: v.camera, blob: heldBytes(v.key) };
        });
        return { events: h.events || [], videos: videos, stint: h.stint || null };
      });
    },
    appendEvent: function (ev) { return call("rig_journal_append_event", { event: ev }); },
    forgetEvents: function (ids) { return call("rig_journal_forget_events", { ids: ids }); },
    putStint: function (row) { return call("rig_journal_put_stint", { row: row }); },
    putVideo: function (item) {
      /* Named rather than copied, so what lands on disk is a decision
         rather than whatever the caller happened to pass - and so the
         blob is left out of it, because the blob is the body. */
      var meta = { key: item.key, rigId: item.rigId,
                   episodeId: item.episodeId, camera: item.camera };
      return item.blob.arrayBuffer().then(function (buf) {
        return internals.invoke("rig_journal_put_video", buf,
                                { headers: { "x-rig-video": headerJson(meta) } });
      });
    },
    forgetVideo: function (key) { return call("rig_journal_forget_video", { key: key }); }
  };
})();
"#
    .to_string()
}

/// What the local page reads when there is no journal to speak of.
///
/// The message is a path and an OS error, and the path comes from the
/// environment, so it is escaped as JSON rather than pasted into the
/// script: a directory with a quote in its name should make an ugly page,
/// not a broken one.
pub fn fault_script(fault: &str) -> String {
    format!(
        "window.RIG_SHELL_FAULT = {};",
        serde_json::to_string(fault).unwrap_or_else(|_| String::from("\"\""))
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    // --------------------------------------------------------- origins

    #[test]
    fn an_address_is_trusted_as_an_origin_not_as_a_path() {
        // The bug this pins: granting the configured URL verbatim grants
        // nothing at all, because a request arrives carrying its origin
        // and never its path. Every command was refused.
        assert_eq!(
            origins("http://floor.internal/apps/rig/"),
            vec!["http://floor.internal/*", "http://floor.internal"]
        );
    }

    #[test]
    fn a_port_is_part_of_the_origin() {
        assert_eq!(
            origins("http://127.0.0.1:8765/apps/rig/index.html"),
            vec!["http://127.0.0.1:8765/*", "http://127.0.0.1:8765"]
        );
    }

    #[test]
    fn an_opaque_origin_is_trusted_with_nothing() {
        // file: and data: both serialise to "null", so trusting that one
        // string would trust every opaque origin at once.
        assert!(origins("file:///home/rig/index.html").is_empty());
        assert!(origins("data:text/html,<b>hi</b>").is_empty());
        assert!(origins("not a url").is_empty());
    }

    // ------------------------------------------------------ video_meta

    fn meta(json: &str) -> Reply<Value> {
        video_meta(Some(json))
    }

    #[test]
    fn a_takes_metadata_arrives_as_json_in_a_header() {
        let m = meta(r#"{"key":"ep-1/wrist-l","rigId":"RIG-03","camera":"wrist-l"}"#).unwrap();
        assert_eq!(m["key"], "ep-1/wrist-l");
        assert_eq!(m["rigId"], "RIG-03");
        assert_eq!(m["camera"], "wrist-l");
    }

    #[test]
    fn the_pages_ascii_escaping_survives_the_trip() {
        // The page escapes everything above 0x7e, because a header value
        // may not carry it. serde_json puts it back without being asked,
        // which is the whole reason that escape was the one chosen.
        let m = meta(r#"{"key":"ep-1/caf\u00e9","rigId":"RIG-\u00d81"}"#).unwrap();
        assert_eq!(m["key"], "ep-1/café");
        assert_eq!(m["rigId"], "RIG-Ø1");
    }

    #[test]
    fn a_take_nothing_could_name_again_is_refused() {
        // Either of these would write a file that no later boot offers to
        // upload - which from every angle looks exactly like a take that
        // was never recorded.
        assert!(meta(r#"{"rigId":"RIG-03"}"#).is_err());
        assert!(meta(r#"{"key":"ep-1/wrist-l"}"#).is_err());
        // A key that is not a string is not a key.
        assert!(meta(r#"{"key":7,"rigId":"RIG-03"}"#).is_err());
    }

    #[test]
    fn metadata_that_is_not_json_is_refused_rather_than_guessed() {
        assert!(meta("ep-1/wrist-l").is_err());
        assert!(video_meta(None).is_err());
    }

    // ----------------------------------------------------- fault_script

    #[test]
    fn a_fault_reaches_the_page_as_a_string_it_can_read() {
        assert_eq!(
            fault_script("/var/lib/rig: Permission denied (os error 13)"),
            "window.RIG_SHELL_FAULT = \"/var/lib/rig: Permission denied (os error 13)\";"
        );
    }

    #[test]
    fn a_path_that_could_break_the_script_does_not() {
        // The path comes from the environment. A quote or a newline in it
        // must make an ugly page rather than a page with no message at all
        // - which is the failure this whole fault page exists to prevent.
        let js = fault_script("/var/\"lib\"/rig
: no");
        assert!(js.starts_with("window.RIG_SHELL_FAULT = \""));
        assert!(js.ends_with("\";"));
        assert!(!js.contains('\n'), "a raw newline would end the statement early");
    }
}
