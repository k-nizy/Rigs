//! `rig-uploader` - the process that carries the journal to the service.
//!
//! It runs beside the shell on a rig and outlives the page: a reload, a
//! webview crash or a screen being restarted stops the page uploading,
//! and stops nothing here. `BACKEND-PLAN.md` names it in the unit list
//! alongside `rig-app` and `roda-rs`, and `src/upload.rs` explains why it
//! reads the journal without ever writing to it.
//!
//! What this file is, and is not: it is the wiring. The decisions - what
//! to send, when the mark may move, when a take's only copy may be
//! unlinked - all live in `upload.rs`, behind a trait, so they are tested
//! without a socket. Everything here is an HTTP client and a loop.
//!
//! It does not decide which rig it is. It asks the service the same
//! question the page asks, from the same machine and so the same address,
//! and a machine the floor cannot place uploads nothing rather than
//! picking a name.

use std::time::{Duration, Instant};

use rig_desktop::journal::{self, Journal};
use rig_desktop::upload::{config_url, origin_of, parse_config_js, Answer, Floor, Pass, Uploader};

/// Where the floor serves the rig page - the same value the shell reads,
/// set once by whatever provisions the host.
const RIG_URL: &str = "RIG_URL";

/// How long to wait when there is nothing to do. Short, because a rig
/// that has just finished a take should not sit on it.
const IDLE: Duration = Duration::from_secs(2);
/// And when the service cannot be reached: back off to a minute, the
/// same ceiling the page uses.
const BACKOFF_MAX: Duration = Duration::from_secs(60);
/// How long an identity is good for before it is asked again.
///
/// Asked at all because a rig can be re-addressed and a token can be
/// rotated under a running process. Not asked every pass, because that
/// is a request every two seconds from every rig on the floor against
/// the one route that is deliberately unauthenticated - and it answers
/// the same thing all day.
const IDENTITY_TTL: Duration = Duration::from_secs(60);

struct Http {
    agent: ureq::Agent,
}

impl Http {
    fn new() -> Self {
        Http {
            agent: ureq::AgentBuilder::new()
                // A take is tens of megabytes over a floor network. The
                // default is generous enough for the small calls and far
                // too tight for the PUT.
                .timeout_read(Duration::from_secs(300))
                .timeout_write(Duration::from_secs(300))
                .build(),
        }
    }
}

/// A status is an answer, not a failure.
///
/// `upload.rs` decides what each code means - 422 retires a batch, 404 on
/// a presign is an episode that is not projected yet - so a non-2xx must
/// arrive there as data. Only a request that never got an answer at all
/// is an error, because only that one is worth retrying blindly.
fn answered(r: Result<ureq::Response, ureq::Error>) -> Answer {
    match r {
        Ok(resp) => {
            let status = resp.status();
            Ok((status, resp.into_string().unwrap_or_default()))
        }
        Err(ureq::Error::Status(status, resp)) => {
            Ok((status, resp.into_string().unwrap_or_default()))
        }
        Err(e) => Err(e.to_string()),
    }
}

fn bearer(req: ureq::Request, token: Option<&str>) -> ureq::Request {
    match token {
        Some(t) => req.set("Authorization", &format!("Bearer {t}")),
        None => req,
    }
}

impl Floor for Http {
    fn get(&self, url: &str, token: Option<&str>) -> Answer {
        answered(bearer(self.agent.get(url), token).call())
    }

    fn post(&self, url: &str, body: &serde_json::Value, token: Option<&str>) -> Answer {
        answered(bearer(self.agent.post(url), token).send_json(body.clone()))
    }

    fn put(&self, url: &str, bytes: &[u8], token: Option<&str>) -> Answer {
        answered(
            bearer(self.agent.put(url), token)
                .set("Content-Type", "application/octet-stream")
                .send_bytes(bytes),
        )
    }
}

fn say(what: &str) {
    // stderr, which is where systemd reads from. No timestamps: journald
    // adds better ones than anything written here would be.
    eprintln!("rig-uploader: {what}");
}

fn main() {
    let Some(rig_url) = std::env::var(RIG_URL)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
    else {
        say("RIG_URL is not set, so there is no floor to upload to");
        std::process::exit(2);
    };
    let (Some(origin), Some(config)) = (origin_of(&rig_url), config_url(&rig_url)) else {
        say(&format!("RIG_URL is not an address this can use: {rig_url}"));
        std::process::exit(2);
    };

    let dir = journal::dir_from_env();
    let journal = match Journal::open(&dir) {
        Ok(j) => j,
        Err(e) => {
            // The shell says this on the screen because it has one. Here
            // there is only the log, and exiting is right: systemd will
            // restart this, and a directory that appears later is then
            // picked up without anybody being asked to intervene.
            say(&format!("cannot open the journal at {}: {e}", dir.display()));
            std::process::exit(1);
        }
    };

    let floor = Http::new();
    say(&format!("draining {} to {origin}", dir.display()));

    let mut quiet = IDLE;
    let mut nobody = false;
    let mut unreachable = false;
    // Who the floor says this machine is, and when it last said so.
    let mut known: Option<(String, String)> = None;
    let mut asked: Option<Instant> = None;

    loop {
        std::thread::sleep(quiet);

        let stale = asked.map(|t| t.elapsed() >= IDENTITY_TTL).unwrap_or(true);
        if known.is_none() || stale {
            match floor.get(&config, None) {
                Ok((status, body)) if (200..300).contains(&status) => {
                    asked = Some(Instant::now());
                    unreachable = false;
                    let id = parse_config_js(&body);
                    match (id.rig_id, id.token) {
                        (Some(rig), Some(token)) => {
                            if nobody {
                                say(&format!("the floor now places this machine as {rig}"));
                            }
                            nobody = false;
                            known = Some((rig, token));
                        }
                        _ => {
                            // Exactly what the page does with the same
                            // answer: nothing. A machine the floor cannot
                            // place has no work of its own to send, and
                            // guessing a name is the one mistake here that
                            // cannot be undone afterwards.
                            known = None;
                            if !nobody {
                                nobody = true;
                                say(&format!(
                                    "the floor does not place this machine (it saw {}), so nothing will be uploaded",
                                    id.seen_as.as_deref().unwrap_or("no address")
                                ));
                            }
                        }
                    }
                }
                Ok((status, _)) => {
                    say(&format!("the service answered {status} for {config}"));
                    known = None;
                }
                Err(e) => {
                    // A floor that is not up yet is the ordinary state on
                    // a rig that booted first. Said once, not every pass.
                    if !unreachable {
                        unreachable = true;
                        say(&format!("cannot reach the floor: {e}"));
                    }
                    known = None;
                }
            }
        }

        let Some((rig_id, token)) = known.clone() else {
            quiet = BACKOFF_MAX.min(quiet * 2);
            continue;
        };

        let up = Uploader {
            journal: &journal,
            floor: &floor,
            origin: origin.clone(),
            rig_id,
            token,
        };

        match one_pass(&up) {
            Ok(_) => quiet = IDLE,
            Err(e) => {
                say(&e);
                // Ask again next pass: a 401 after a rotated token looks
                // exactly like this, and the answer to it is a new token.
                known = None;
                quiet = BACKOFF_MAX.min(quiet * 2);
            }
        }
    }
}

/// One sweep: events first, then the oldest take.
///
/// Events before video deliberately. An episode has to be in the ledger
/// before the service can project the row a take is presigned against, so
/// sending video first would mean asking for something that cannot exist
/// yet and waiting for it.
fn one_pass<F: Floor>(up: &Uploader<'_, F>) -> Result<bool, String> {
    let mut moved = false;

    loop {
        match up.send_events()? {
            Pass::Idle => break,
            Pass::Sent(n) => {
                moved = true;
                say(&format!("{n} events accepted"));
            }
            Pass::Refused(n, why) => {
                moved = true;
                // Loud on purpose. A rig filing events its own schema
                // rejects is a bug in the rig, and it has just been
                // retired rather than retried for ever.
                say(&format!("the service refused {n} events outright: {why}"));
            }
            Pass::Waiting => break,
        }
    }

    match up.send_video()? {
        Pass::Sent(_) => {
            moved = true;
            say("a take was accepted and released");
        }
        // Not projected yet. Ordinary, and says nothing worth logging on
        // every pass - the take is still here and will go next time.
        Pass::Waiting | Pass::Idle => {}
        Pass::Refused(_, why) => say(&format!("a take was refused: {why}")),
    }

    Ok(moved)
}
