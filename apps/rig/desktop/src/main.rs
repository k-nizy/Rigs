// The kiosk shell for one rig.
//
// It does exactly one thing: open a full-screen window on the rig page
// served by the floor. It is not a rewrite of the rig app and must not
// become one - the page in `apps/rig/` stays a browser page, and this
// hosts it in a webview.
//
// ---------------------------------------------------------------------
// Why this loads a URL and not the files next door
//
// The obvious build is to bundle `apps/rig/` into the binary and open
// `index.html`. That is wrong here, and it is wrong in the exact way this
// project has already been burned once.
//
// `index.html` loads `rig-config.js` by a relative path, and that file is
// how a machine learns which rig it is. On a floor the kiosk loads the
// page from the server, so the browser asks the *server* for that file,
// and the server answers per caller from `RIG_ADDRESSES`. Bundle the page
// and the webview reads the placeholder sitting beside it instead - which
// names nobody. The rig then refuses to work, which is at least loud; but
// the tempting fix is to write a real id into the bundled copy, and that
// is twelve machines with one identity again, one layer lower and harder
// to see. `BACKEND-PLAN.md` calls this "the one place where Tauri must
// not help", and it means it.
//
// So: no rig assets are bundled, and this shell has no idea which rig it
// is standing in front of. It cannot leak an identity it does not hold.
//
// ---------------------------------------------------------------------
// What RIG_URL is, and what it is not
//
// `RIG_URL` is the address of the floor's web server - the same value on
// all twelve machines. It is a deployment fact, like the database URL,
// and Ansible sets it when it provisions the host.
//
// It is *not* the rig's identity. Nothing here distinguishes RIG-03 from
// RIG-07; the service does that from the address the request arrives on.
// Anyone tempted to add `RIG_ID` beside this should read the paragraph
// above first.

use rig_desktop::bridge;
use rig_desktop::journal::Journal;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

/// Where the floor serves the rig page, e.g.
/// `http://floor.internal/apps/rig/`. `DEPLOY.md` already says the kiosk
/// points at that path directly rather than being redirected from `/`, so
/// this takes the whole URL rather than an origin plus an assumption
/// about where the page lives.
const RIG_URL: &str = "RIG_URL";

/// Set to anything to get an ordinary window instead of a full-screen
/// one. For working on the shell, where a full-screen window you cannot
/// escape from is its own small punishment. A rig never sets it.
const WINDOWED: &str = "RIG_WINDOWED";

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            // Unset, blank, or unparseable all land in the same place: the
            // local page, which says what is missing. Not a blank window
            // and not a silent retry - a kiosk has no terminal, so an
            // error nobody can see is an error nobody will fix.
            let configured = std::env::var(RIG_URL)
                .ok()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty());

            // `remote` is the address actually loaded, not merely the one
            // configured: an unparseable RIG_URL falls back to the local
            // page, and trusting a URL the webview never opened would
            // grant the journal to an origin nobody is looking at.
            let mut remote: Option<String> = None;
            let target = match configured.as_deref().map(str::parse) {
                Some(Ok(url)) => {
                    remote = configured.clone();
                    WebviewUrl::External(url)
                }
                _ => WebviewUrl::App("index.html".into()),
            };

            // The disk half of the outbox. Opened before the window, so a
            // page cannot call into a journal that is not there yet.
            let dir = bridge::journal_dir();
            let journal = Journal::open(&dir);

            // A shell that cannot write down what it records must not open
            // a rig page at all. rig.js falls back to the browser's own
            // journal when `window.RIG_JOURNAL` is missing, and that
            // fallback reports itself durable too - so an operator would be
            // promised their work was safe by the one machine that had just
            // failed to make it so.
            //
            // So this lands where an unset RIG_URL lands, for the same
            // reason: the local page, saying what is wrong, on the screen.
            // Returning the error instead kills the process before the
            // window exists, and a kiosk showing nothing looks exactly like
            // a slow network.
            let fault = journal
                .as_ref()
                .err()
                .map(|e| format!("{}: {e}", dir.display()));

            let mut window = WebviewWindowBuilder::new(
                app,
                "rig",
                if fault.is_some() { WebviewUrl::App("index.html".into()) } else { target },
            )
            .title("Rig")
            .fullscreen(std::env::var_os(WINDOWED).is_none());

            match journal {
                Ok(open) => {
                    app.manage(open);
                    bridge::allow_remote(app.handle(), remote.as_deref())?;
                    window = window.initialization_script(bridge::init_script());
                }
                // No bridge and no capability: there is nothing behind them.
                Err(_) => {
                    window = window
                        .initialization_script(bridge::fault_script(fault.as_deref().unwrap_or("")));
                }
            }
            window.build()?;

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            bridge::rig_journal_load,
            bridge::rig_journal_append_event,
            bridge::rig_journal_forget_events,
            bridge::rig_journal_put_stint,
            bridge::rig_journal_forget_video,
            bridge::rig_journal_put_video,
            bridge::rig_journal_read_video,
        ])
        .run(tauri::generate_context!())
        .expect("the rig shell could not start");
}
