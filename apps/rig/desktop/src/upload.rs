//! Draining the journal to the service, from a process of its own.
//!
//! Until now uploading happened inside the page: `rig.js` posts its
//! outbox and its takes, and forgets a row once the service says it holds
//! it. That works while the page is alive, and stops the moment it is
//! not - a reload, a webview crash, a screen being restarted. The work is
//! safe on disk either way, but nothing is carrying it.
//!
//! So this is the same job, done by something whose lifetime is not the
//! page's. `BACKEND-PLAN.md` asks for it in those words:
//!
//! > The uploader is a separate process reading that file from a
//! > checkpoint; if it dies, crashes, or is stopped for a week, not one
//! > event is lost, because it was never the thing holding them.
//!
//! ---------------------------------------------------------------------
//! Two processes, one file, and no locking
//!
//! `journal.ndjson` is rewritten by `Journal::compact` - read it all,
//! write a fresh one, rename it over. A second process writing to that
//! file would lose whatever landed during the rewrite, and a bookmark
//! kept as a byte offset would name a different event afterwards.
//!
//! So this never writes to it. It records how far it has got in a file of
//! its own, keyed by the rig's `seq`, which travels with the event and
//! does not change however often the log is rewritten. The shell stays
//! the only writer, and drops what is at or below that mark next time it
//! compacts. There is nothing to lock because nothing is shared.
//!
//! Video needs no such care: each take is already its own pair of files,
//! so a confirmed take is deleted here directly. That is the plan's
//! checksum-then-unlink rule, and the unlink is the last step for the
//! reason it has always been - past it there is no other copy.
//!
//! ---------------------------------------------------------------------
//! It does not decide which rig it is, either
//!
//! Same rule as the shell and for the same reason. It asks the service
//! the question the page asks - `rig-config.js`, answered per caller from
//! `RIG_ADDRESSES` - and it runs on the rig, so it calls from the rig's
//! address and is told the same thing the page is told. A machine the
//! floor cannot place is told it is nobody, and then this uploads
//! nothing rather than picking a name.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::journal::Journal;

/// What `rig.js` sends in one POST. Matched deliberately: a batch size is
/// a property of the service's appetite, not of who happens to be doing
/// the sending, and two different answers would be two things to tune.
const BATCH: usize = 100;

// ------------------------------------------------------------- identity

/// What the service says this machine is.
///
/// All three fields are optional because the service answers a caller it
/// cannot place with a real file that names nobody - so that the page
/// loads and can say what is wrong. The same file has to be readable here
/// and mean the same thing.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Identity {
    pub rig_id: Option<String>,
    pub token: Option<String>,
    pub seen_as: Option<String>,
}

/// Read `rig-config.js` without running it.
///
/// The service writes it one assignment per line with `json.dumps` on the
/// right-hand side, so every value is JSON and nothing here needs a
/// JavaScript engine. Anything that does not match that shape is ignored
/// rather than guessed at: a half-understood identity file is how a rig
/// ends up filing work under a name nobody chose.
pub fn parse_config_js(src: &str) -> Identity {
    let mut id = Identity::default();
    for line in src.lines() {
        let line = line.trim();
        let Some(rest) = line.strip_prefix("window.") else {
            continue;
        };
        let Some((name, value)) = rest.split_once('=') else {
            continue;
        };
        let value = value.trim().trim_end_matches(';').trim();
        let parsed: Value = match serde_json::from_str(value) {
            Ok(v) => v,
            Err(_) => continue,
        };
        // `null` is the service saying it cannot place this caller, which
        // is an answer and not a missing one - `as_str` gives None for it
        // either way, which is exactly what should happen.
        let slot = match name.trim() {
            "RIG_ID" => &mut id.rig_id,
            "RIG_TOKEN" => &mut id.token,
            "RIG_SEEN_AS" => &mut id.seen_as,
            _ => continue,
        };
        *slot = parsed.as_str().filter(|s| !s.is_empty()).map(str::to_string);
    }
    id
}

/// Whether this URL is the floor's, and so may carry the rig's token.
///
/// The same rule `rig.js` applies, and it exists for the same reason: a
/// presigned upload URL points at somebody else's object store, is
/// already signed, and is not ours to add credentials to. Sending a
/// bearer token to somebody else's bucket is how a token ends up in
/// somebody else's logs.
pub fn ours(origin: &str, url: &str) -> bool {
    if origin.is_empty() {
        // An origin we cannot name matches nothing. The browser version
        // of this once fell back to the empty string, and every string
        // starts with the empty string, so the token went everywhere.
        return false;
    }
    let origin = origin.trim_end_matches('/');
    url == origin || url.starts_with(&format!("{origin}/"))
}

// ---------------------------------------------------------------- floor

/// A response: the status, and the body if there was one worth keeping.
pub type Answer = Result<(u16, String), String>;

/// The service, as far as this process needs it.
///
/// A trait for the same reason `Roda` is one: the logic below is the part
/// worth testing and it should not need a socket to run. The binary wires
/// in a real HTTP client; the tests wire in a stand-in that can be told
/// to fail.
pub trait Floor {
    fn get(&self, url: &str, token: Option<&str>) -> Answer;
    fn post(&self, url: &str, body: &Value, token: Option<&str>) -> Answer;
    fn put(&self, url: &str, bytes: &[u8], token: Option<&str>) -> Answer;
}

/// What one pass managed to do. Reported rather than logged, so the
/// binary decides what is worth saying out loud and how often.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pass {
    /// Nothing was owed.
    Idle,
    /// This many events or takes went, and the service confirmed them.
    Sent(usize),
    /// The service refused these outright, and they have been retired.
    Refused(usize, String),
    /// The episode is not projected yet. Ordinary, and not a failure.
    Waiting,
}

fn ok(status: u16) -> bool {
    (200..300).contains(&status)
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

/// Percent-encoding for the one place ids reach a URL.
///
/// Ids here are uuids and slugs, so nothing needs encoding today. It is
/// done anyway because the day one does not is the day it is a path
/// separator, and an episode id that escaped its segment would address a
/// different route entirely.
fn seg(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}

// ------------------------------------------------------------- uploader

pub struct Uploader<'a, F: Floor> {
    pub journal: &'a Journal,
    pub floor: &'a F,
    /// Where the floor is, with no trailing slash: `http://floor.internal`.
    pub origin: String,
    pub rig_id: String,
    pub token: String,
}

impl<F: Floor> Uploader<'_, F> {
    fn token_for(&self, url: &str) -> Option<&str> {
        if ours(&self.origin, url) && !self.token.is_empty() {
            Some(self.token.as_str())
        } else {
            None
        }
    }

    fn rig_url(&self, tail: &str) -> String {
        format!("{}/api/rigs/{}{}", self.origin, seg(&self.rig_id), tail)
    }

    /// Hand over one batch of events.
    ///
    /// The mark moves only after the service has answered, and only ever
    /// to the highest `seq` in the batch that was actually accepted. It
    /// is the file that decides what may be thrown away, so it is written
    /// last and never on hope.
    pub fn send_events(&self) -> Result<Pass, String> {
        let held = self.journal.load(&self.rig_id).map_err(|e| e.to_string())?;
        if held.events.is_empty() {
            return Ok(Pass::Idle);
        }
        let batch: Vec<Value> = held.events.into_iter().take(BATCH).collect();

        // Every event must carry a seq, because the mark is expressed in
        // them. One that does not cannot be retired, and sending it would
        // mean either sending it for ever or retiring its neighbours on
        // its behalf. Refuse, loudly, and leave the journal alone.
        let mut highest = i64::MIN;
        for e in &batch {
            match e.get("seq").and_then(Value::as_i64) {
                Some(s) => highest = highest.max(s),
                None => {
                    return Err(format!(
                        "an event with no seq is in the journal: {}",
                        e.get("eventId").and_then(Value::as_str).unwrap_or("unnamed")
                    ))
                }
            }
        }

        let url = self.rig_url("/events");
        let (status, body) = self.floor.post(
            &url,
            &json!({ "events": batch }),
            self.token_for(&url),
        )?;

        if ok(status) {
            self.journal
                .set_mark(&self.rig_id, highest)
                .map_err(|e| e.to_string())?;
            return Ok(Pass::Sent(batch.len()));
        }
        if status == 422 {
            // The service refused the batch outright, so retrying refuses
            // it again for ever and nothing behind it would ever drain.
            // Retired for the same reason `rig.js` drops it from the
            // journal: a rig filing events its own schema rejects is a bug
            // in the rig, and it belongs in a log rather than in a loop.
            self.journal
                .set_mark(&self.rig_id, highest)
                .map_err(|e| e.to_string())?;
            return Ok(Pass::Refused(batch.len(), body));
        }
        Err(format!("events HTTP {status}"))
    }

    /// Hand over the oldest held take, all four steps of it.
    ///
    /// Ask, put, report the checksum, and only then unlink. Everything
    /// before the last step is a retry - safe to repeat, safe to
    /// interrupt, safe to run twice - and the last one is the only place
    /// that must never be optimistic, because past it the rig's copy is
    /// gone.
    pub fn send_video(&self) -> Result<Pass, String> {
        let held = self.journal.load(&self.rig_id).map_err(|e| e.to_string())?;
        let Some(take) = held.videos.first() else {
            return Ok(Pass::Idle);
        };
        let key = take.get("key").and_then(Value::as_str).unwrap_or_default();
        let episode = take
            .get("episodeId")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let camera = take.get("camera").and_then(Value::as_str).unwrap_or_default();
        if key.is_empty() || episode.is_empty() || camera.is_empty() {
            return Err(format!("a held take is not named: {take}"));
        }

        let bytes = match self.journal.read_video(key) {
            Ok(b) => b,
            // The page is still an uploader too, and it confirms and
            // deletes takes of its own. Bytes that are gone are a take
            // somebody else finished, not a fault.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                self.journal.forget_video(key).map_err(|e| e.to_string())?;
                return Ok(Pass::Idle);
            }
            Err(e) => return Err(e.to_string()),
        };

        let base = self.rig_url(&format!("/episodes/{}", seg(episode)));

        let ask = format!("{base}/video:presign");
        let (status, body) = self
            .floor
            .post(&ask, &json!({ "camera": camera }), self.token_for(&ask))?;
        if status == 404 {
            // In the ledger but not projected into a row yet. The rig
            // saves a take and reaches here in the same second, and
            // projection runs on its own timer. Waiting is correct;
            // treating it as a failure would give up on a take that is
            // about to exist.
            return Ok(Pass::Waiting);
        }
        if !ok(status) {
            return Err(format!("presign HTTP {status}"));
        }
        let told: Value =
            serde_json::from_str(&body).map_err(|e| format!("presign said {e}"))?;
        let where_to = told
            .get("url")
            .and_then(Value::as_str)
            .ok_or("presign named no url")?;
        // Absolute for a presigned upload, relative for the gateway model.
        let put_to = if where_to.starts_with('/') {
            format!("{}{}", self.origin, where_to)
        } else {
            where_to.to_string()
        };

        let (status, _) = self
            .floor
            .put(&put_to, &bytes, self.token_for(&put_to))?;
        if !ok(status) {
            return Err(format!("put HTTP {status}"));
        }

        let done = format!("{base}/video:complete");
        let (status, body) = self.floor.post(
            &done,
            &json!({
                "camera": camera,
                "sha256": sha256_hex(&bytes),
                "bytes": bytes.len(),
            }),
            self.token_for(&done),
        )?;
        if !ok(status) {
            // 409 is the service saying what landed is not what was sent.
            // The bytes are still here, so this goes round again rather
            // than being set aside - a truncated upload is the commonest
            // real failure and exactly the one a retry fixes.
            return Err(format!("complete HTTP {status}"));
        }
        let said: Value =
            serde_json::from_str(&body).map_err(|e| format!("complete said {e}"))?;
        if said.get("safeToDelete").and_then(Value::as_bool) != Some(true) {
            return Err("the service did not release the copy".into());
        }

        self.journal.forget_video(key).map_err(|e| e.to_string())?;
        Ok(Pass::Sent(1))
    }
}

// ------------------------------------------------------------------ tests

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::path::PathBuf;

    // ------------------------------------------------------- config.js

    #[test]
    fn a_rig_reads_the_identity_the_service_rendered_for_it() {
        let js = concat!(
            "/* Served by the rigs service, for this machine only. */\n",
            "window.RIG_ID = \"RIG-03\";\n",
            "window.RIG_TOKEN = \"t-abc\";\n",
            "window.RIG_SEEN_AS = \"10.0.0.5\";\n"
        );
        assert_eq!(
            parse_config_js(js),
            Identity {
                rig_id: Some("RIG-03".into()),
                token: Some("t-abc".into()),
                seen_as: Some("10.0.0.5".into()),
            }
        );
    }

    #[test]
    fn a_machine_the_floor_cannot_place_is_told_it_is_nobody() {
        // The service answers an unknown caller with a real file that
        // names nobody, and this has to read that as "no identity"
        // rather than as a parse failure.
        let js = concat!(
            "window.RIG_ID = null;\n",
            "window.RIG_TOKEN = null;\n",
            "window.RIG_SEEN_AS = \"10.0.0.99\";\n"
        );
        let id = parse_config_js(js);
        assert_eq!(id.rig_id, None, "a rig with no identity got one anyway");
        assert_eq!(id.token, None);
        assert_eq!(id.seen_as, Some("10.0.0.99".into()));
    }

    #[test]
    fn nothing_that_is_not_an_assignment_is_read_as_one() {
        let id = parse_config_js("alert('hi'); window.RIG_ID = notjson;\nwindow.OTHER = \"x\";");
        assert_eq!(id, Identity::default());
    }

    // ----------------------------------------------------------- ours

    #[test]
    fn the_token_goes_to_the_floor_and_nowhere_else() {
        let o = "http://floor.internal";
        assert!(ours(o, "http://floor.internal/api/rigs/RIG-03/events"));
        assert!(ours(o, "http://floor.internal"));
        // Somebody else's bucket. This is the whole point of the rule.
        assert!(!ours(o, "https://store.example.com/upload?sig=abc"));
        // The prefix trap: a different host that starts with our origin.
        assert!(!ours(o, "http://floor.internal.evil.example/x"));
        // An origin we cannot name matches nothing at all.
        assert!(!ours("", "http://floor.internal/api"));
    }

    // -------------------------------------------------------- a floor

    #[derive(Default)]
    struct Fake {
        seen: RefCell<Vec<(String, Option<String>)>>,
        posts: RefCell<Vec<(String, Value)>>,
        puts: RefCell<Vec<(String, usize)>>,
        events_status: u16,
        presign_status: u16,
        complete: Value,
    }

    impl Fake {
        fn new() -> Self {
            Fake {
                events_status: 200,
                presign_status: 200,
                complete: json!({ "safeToDelete": true, "key": "k" }),
                ..Default::default()
            }
        }
        fn tokens_sent_to(&self, host_fragment: &str) -> Vec<Option<String>> {
            self.seen
                .borrow()
                .iter()
                .filter(|(u, _)| u.contains(host_fragment))
                .map(|(_, t)| t.clone())
                .collect()
        }
    }

    impl Floor for Fake {
        fn get(&self, url: &str, token: Option<&str>) -> Answer {
            self.seen
                .borrow_mut()
                .push((url.to_string(), token.map(str::to_string)));
            Ok((200, String::new()))
        }
        fn post(&self, url: &str, body: &Value, token: Option<&str>) -> Answer {
            self.seen
                .borrow_mut()
                .push((url.to_string(), token.map(str::to_string)));
            self.posts.borrow_mut().push((url.to_string(), body.clone()));
            if url.ends_with("/events") {
                return Ok((self.events_status, String::new()));
            }
            if url.ends_with("video:presign") {
                return Ok((
                    self.presign_status,
                    json!({ "url": "https://store.example.com/put?sig=abc", "method": "PUT" })
                        .to_string(),
                ));
            }
            Ok((200, self.complete.to_string()))
        }
        fn put(&self, url: &str, bytes: &[u8], token: Option<&str>) -> Answer {
            self.seen
                .borrow_mut()
                .push((url.to_string(), token.map(str::to_string)));
            self.puts.borrow_mut().push((url.to_string(), bytes.len()));
            Ok((200, String::new()))
        }
    }

    struct Dir(PathBuf);
    impl Dir {
        fn new(tag: &str) -> Self {
            let p = std::env::temp_dir().join(format!("rig-upload-{}-{tag}", std::process::id()));
            let _ = std::fs::remove_dir_all(&p);
            Dir(p)
        }
        fn open(&self) -> Journal {
            Journal::open(&self.0).expect("journal opens")
        }
    }
    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn uploader<'a>(j: &'a Journal, f: &'a Fake) -> Uploader<'a, Fake> {
        Uploader {
            journal: j,
            floor: f,
            origin: "http://floor.internal".into(),
            rig_id: "RIG-03".into(),
            token: "t-abc".into(),
        }
    }

    fn event(id: &str, seq: i64) -> Value {
        json!({ "eventId": id, "seq": seq, "rigId": "RIG-03", "kind": "episode" })
    }

    // --------------------------------------------------------- events

    #[test]
    fn events_go_once_and_the_mark_moves_with_them() {
        let dir = Dir::new("events");
        let j = dir.open();
        j.append_event(&event("e1", 0)).unwrap();
        j.append_event(&event("e2", 1)).unwrap();
        let f = Fake::new();

        assert_eq!(uploader(&j, &f).send_events().unwrap(), Pass::Sent(2));
        assert_eq!(j.read_mark(), Some(("RIG-03".into(), 1)));

        // Nothing is owed on the next pass, which is what the mark is for.
        assert_eq!(uploader(&j, &f).send_events().unwrap(), Pass::Idle);
        assert_eq!(f.posts.borrow().len(), 1, "the same events went twice");
    }

    #[test]
    fn the_mark_does_not_move_when_the_service_never_answered() {
        let dir = Dir::new("offline");
        let j = dir.open();
        j.append_event(&event("e1", 0)).unwrap();
        let mut f = Fake::new();
        f.events_status = 503;

        assert!(uploader(&j, &f).send_events().is_err());
        assert_eq!(j.read_mark(), None, "work was retired that nobody has");
        assert_eq!(j.load("RIG-03").unwrap().events.len(), 1);
    }

    #[test]
    fn a_batch_the_service_refuses_outright_is_not_retried_for_ever() {
        let dir = Dir::new("refused");
        let j = dir.open();
        j.append_event(&event("e1", 0)).unwrap();
        let mut f = Fake::new();
        f.events_status = 422;

        match uploader(&j, &f).send_events().unwrap() {
            Pass::Refused(n, _) => assert_eq!(n, 1),
            other => panic!("expected a refusal, got {other:?}"),
        }
        // Retired, or the journal would offer it again on every pass for
        // ever and nothing behind it would drain.
        assert_eq!(j.load("RIG-03").unwrap().events.len(), 0);
    }

    #[test]
    fn an_event_with_no_seq_stops_the_pass_rather_than_retiring_its_neighbours() {
        let dir = Dir::new("noseq");
        let j = dir.open();
        j.append_event(&json!({ "eventId": "bad", "rigId": "RIG-03" }))
            .unwrap();
        let f = Fake::new();

        assert!(uploader(&j, &f).send_events().is_err());
        assert_eq!(j.read_mark(), None);
        assert!(f.posts.borrow().is_empty(), "it was sent anyway");
    }

    // ---------------------------------------------------------- video

    fn a_take(j: &Journal, key: &str, bytes: &[u8]) {
        j.put_video(
            &json!({ "key": key, "rigId": "RIG-03", "episodeId": "ep-1", "camera": "wrist-l" }),
            bytes,
        )
        .unwrap();
    }

    #[test]
    fn a_take_is_unlinked_only_after_the_service_says_it_holds_it() {
        let dir = Dir::new("video");
        let j = dir.open();
        a_take(&j, "ep-1/wrist-l", &[7u8; 2048]);
        let f = Fake::new();

        assert_eq!(uploader(&j, &f).send_video().unwrap(), Pass::Sent(1));
        assert!(j.load("RIG-03").unwrap().videos.is_empty(), "the take is still held");
        assert_eq!(f.puts.borrow()[0].1, 2048);

        // And the checksum reported is of what was actually sent.
        let (_, body) = f
            .posts
            .borrow()
            .iter()
            .find(|(u, _)| u.ends_with("video:complete"))
            .cloned()
            .expect("nothing was reported complete");
        assert_eq!(body["sha256"], sha256_hex(&[7u8; 2048]));
        assert_eq!(body["bytes"], 2048);
    }

    #[test]
    fn a_take_the_service_will_not_release_is_kept() {
        let dir = Dir::new("held");
        let j = dir.open();
        a_take(&j, "ep-1/wrist-l", &[1u8; 16]);
        let mut f = Fake::new();
        f.complete = json!({ "safeToDelete": false });

        assert!(uploader(&j, &f).send_video().is_err());
        assert_eq!(
            j.load("RIG-03").unwrap().videos.len(),
            1,
            "the only copy was deleted on the service's silence"
        );
    }

    #[test]
    fn an_episode_that_is_not_projected_yet_is_waited_for_not_failed() {
        let dir = Dir::new("waiting");
        let j = dir.open();
        a_take(&j, "ep-1/wrist-l", &[1u8; 16]);
        let mut f = Fake::new();
        f.presign_status = 404;

        assert_eq!(uploader(&j, &f).send_video().unwrap(), Pass::Waiting);
        assert_eq!(j.load("RIG-03").unwrap().videos.len(), 1);
    }

    #[test]
    fn a_take_the_page_confirmed_first_is_not_a_fault() {
        // The page is still an uploader too. It confirms a take and
        // deletes the bytes; this may reach the metadata a moment later.
        let dir = Dir::new("raced");
        let j = dir.open();
        a_take(&j, "ep-1/wrist-l", &[1u8; 16]);
        std::fs::remove_file(dir.0.join("video").join(
            "65702d312f77726973742d6c".to_string() + ".bin",
        ))
        .expect("the take's bytes were not where they were expected");
        let f = Fake::new();

        assert_eq!(uploader(&j, &f).send_video().unwrap(), Pass::Idle);
        assert!(j.load("RIG-03").unwrap().videos.is_empty(), "an orphan was left behind");
        assert!(f.puts.borrow().is_empty(), "bytes that were gone were sent");
    }

    #[test]
    fn the_rigs_token_never_reaches_somebody_elses_bucket() {
        let dir = Dir::new("token");
        let j = dir.open();
        a_take(&j, "ep-1/wrist-l", &[1u8; 16]);
        let f = Fake::new();
        uploader(&j, &f).send_video().unwrap();

        assert_eq!(
            f.tokens_sent_to("store.example.com"),
            vec![None],
            "the floor's token was sent to the object store"
        );
        assert!(
            f.tokens_sent_to("floor.internal")
                .iter()
                .all(|t| t.as_deref() == Some("t-abc")),
            "the floor was called without the token it issued"
        );
    }
}
