// The disk journal.
//
// `BACKEND-PLAN.md` Phase 1: "the disk journal, and `emit()` routed
// through it with browser fallback". The browser half already exists -
// `apps/rig/assets/rig.js` writes to IndexedDB - and its own comment
// says what is wrong with it:
//
//     "on disk before the UI advances" becomes "handed to the store
//     before the network is touched". A hard power cut in the few
//     milliseconds before that transaction commits can still lose the
//     last event. Closing that window needs a synchronous write, which
//     needs Tauri.
//
// This is that synchronous write. Every append is followed by an fsync
// before the call returns, so an event the page has been told is safe is
// on the platter rather than in a cache waiting for a transaction that a
// power cut will never commit.
//
// ---------------------------------------------------------------------
// Why append-only, with tombstones
//
// The same reason the backend ledger is append-only: a file that is only
// ever added to cannot be corrupted by a half-finished edit. Rewriting
// the whole outbox on every acknowledgement would put all of it at risk
// every time the server said "got it", which is the most common thing
// that happens here.
//
// So a forget is a line too, and `load` replays the file to find out what
// is still owed. Replaying a file that only grows is not free for ever,
// so `load` also compacts - at boot, which is the one moment when nothing
// is in flight and rewriting is safe.
//
// ---------------------------------------------------------------------
// Why a torn last line is skipped rather than fatal
//
// The last line is the one a power cut interrupts, and a half-written
// line is exactly what "the machine died mid-append" looks like. Skipping
// it loses at most the event being written when the power went - which
// was never acknowledged to anyone - while failing the whole load would
// strand every event before it, which *were* acknowledged as safe.
//
// This is deliberately the opposite of the push server, which refuses to
// start on a state it cannot account for. There, a state nobody can
// explain means twelve rigs might run the wrong day. Here the file is the
// only copy of work already done, and reading all but the last line of it
// is strictly better than reading none of it.

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

/// What a boot is still holding: events not yet acknowledged, takes not
/// yet confirmed, and the stint that was in progress.
#[derive(Debug, Default)]
pub struct Held {
    pub events: Vec<Value>,
    pub videos: Vec<Value>,
    pub stint: Option<Value>,
}

pub struct Journal {
    dir: PathBuf,
}

const EVENTS: &str = "journal.ndjson";
const STINT: &str = "stint.json";
const VIDEO_DIR: &str = "video";
const MARK: &str = "uploaded.json";

/// Where the journal lives. `/var/lib/rig` on a rig, overridable so a
/// developer - and a test - does not need a system directory.
///
/// Here rather than beside either reader, because the shell and the
/// uploader have to agree about it and a second copy of a default path is
/// a second answer waiting to drift.
pub const JOURNAL_DIR_ENV: &str = "RIG_JOURNAL_DIR";
const DEFAULT_DIR: &str = "/var/lib/rig";

pub fn dir_from_env() -> PathBuf {
    std::env::var(JOURNAL_DIR_ENV)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_DIR))
}

impl Journal {
    /// Open (and create) the journal directory. `/var/lib/rig` on a rig,
    /// anywhere for a test.
    pub fn open(dir: impl Into<PathBuf>) -> std::io::Result<Self> {
        let dir = dir.into();
        fs::create_dir_all(dir.join(VIDEO_DIR))?;
        Ok(Self { dir })
    }

    // ------------------------------------------------------------ events

    /// Append one event and return only once it is on the platter.
    pub fn append_event(&self, event: &Value) -> std::io::Result<()> {
        let mut line = Map::new();
        line.insert("op".into(), Value::String("put".into()));
        line.insert("event".into(), event.clone());
        self.append_line(&Value::Object(line))
    }

    /// Record that these events are no longer owed. A line, not an edit.
    pub fn forget_events(&self, ids: &[String]) -> std::io::Result<()> {
        if ids.is_empty() {
            return Ok(());
        }
        let mut line = Map::new();
        line.insert("op".into(), Value::String("del".into()));
        line.insert(
            "ids".into(),
            Value::Array(ids.iter().map(|i| Value::String(i.clone())).collect()),
        );
        self.append_line(&Value::Object(line))
    }

    fn append_line(&self, line: &Value) -> std::io::Result<()> {
        let mut f = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.dir.join(EVENTS))?;
        let mut buf = serde_json::to_vec(line).map_err(std::io::Error::other)?;
        buf.push(b'\n');
        // One write call, so a torn line can only ever be the last one.
        f.write_all(&buf)?;
        // The whole reason this file exists. Without it the page has been
        // told "safe" about bytes still sitting in the kernel's cache.
        f.sync_data()
    }

    // ------------------------------------------------------------ videos

    /// A take, and the metadata naming which take it is. The bytes go to
    /// their own file: video is the one thing here measured in tens of
    /// megabytes, and a line-oriented log is the wrong shape for it.
    pub fn put_video(&self, meta: &Value, bytes: &[u8]) -> std::io::Result<()> {
        let key = meta
            .get("key")
            .and_then(Value::as_str)
            .ok_or_else(|| std::io::Error::other("a video with no key"))?;
        let stem = self.video_stem(key);

        // Bytes first, metadata second. The metadata is what `load` lists,
        // so a crash between the two leaves an unread blob rather than a
        // promise of bytes that are not there. `load` sweeps the orphan.
        write_atomic(&with_ext(&stem, "bin"), bytes)?;
        write_atomic(
            &with_ext(&stem, "json"),
            &serde_json::to_vec(meta).map_err(std::io::Error::other)?,
        )
    }

    pub fn forget_video(&self, key: &str) -> std::io::Result<()> {
        let stem = self.video_stem(key);
        // Metadata first: it is what makes the pair visible to `load`.
        remove_if_present(&with_ext(&stem, "json"))?;
        remove_if_present(&with_ext(&stem, "bin"))
    }

    /// The bytes of one held take, for the page to put back on the queue.
    /// Read on demand rather than returned by `load`, because a boot with
    /// a full queue is hundreds of megabytes and none of it is needed
    /// until the uploader reaches that take.
    pub fn read_video(&self, key: &str) -> std::io::Result<Vec<u8>> {
        fs::read(with_ext(&self.video_stem(key), "bin"))
    }

    fn video_stem(&self, key: &str) -> PathBuf {
        self.dir.join(VIDEO_DIR).join(file_stem(key))
    }

    // ------------------------------------------------------------- stint

    /// The running total for one rig. Keyed on rigId and rewritten in
    /// place, because it is a fact about now rather than a thing that
    /// happened - the same reason the browser journal keeps one row.
    pub fn put_stint(&self, row: &Value) -> std::io::Result<()> {
        let rig = row
            .get("rigId")
            .and_then(Value::as_str)
            .ok_or_else(|| std::io::Error::other("a stint with no rigId"))?;
        let mut all = self.stints();
        all.insert(rig.to_string(), row.clone());
        let body = serde_json::to_vec(&all).map_err(std::io::Error::other)?;
        write_atomic(&self.dir.join(STINT), &body)
    }

    fn stints(&self) -> BTreeMap<String, Value> {
        fs::read(self.dir.join(STINT))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default()
    }

    // -------------------------------------------------------------- mark

    /// How far the uploader has got, as the rig's own `seq`.
    ///
    /// Its own file, and the only thing that writes it is the uploader.
    /// That is the whole point: `journal.ndjson` keeps one writer - this
    /// process - and a second process can still say what it has managed
    /// to hand over without ever touching it.
    ///
    /// By `seq` rather than by a byte offset or a line number, because
    /// `compact` rewrites the file and renames it into place. An offset
    /// would name a different event afterwards, or none. `seq` travels
    /// with the event and does not change.
    ///
    /// The rig id travels with it too. A mark is only ever applied to the
    /// rig it was recorded for, so a journal that somehow held two rigs
    /// could not have one rig's progress drop the other's work.
    pub fn read_mark(&self) -> Option<(String, i64)> {
        let raw = fs::read(self.dir.join(MARK)).ok()?;
        let v: Value = serde_json::from_slice(&raw).ok()?;
        let rig = v.get("rigId").and_then(Value::as_str)?.to_string();
        let seq = v.get("seq").and_then(Value::as_i64)?;
        Some((rig, seq))
    }

    /// Record that the service holds everything up to and including `seq`.
    ///
    /// Only ever moves forward. A mark that went backwards would offer
    /// work the service already has - harmless, because it dedupes - but
    /// a mark that went backwards *by mistake* is indistinguishable from
    /// one that never advanced, and this is the file that decides what
    /// may be thrown away.
    pub fn set_mark(&self, rig_id: &str, seq: i64) -> std::io::Result<()> {
        if let Some((held, at)) = self.read_mark() {
            if held == rig_id && at >= seq {
                return Ok(());
            }
        }
        let body = serde_json::to_vec(&json!({ "rigId": rig_id, "seq": seq }))
            .map_err(std::io::Error::other)?;
        write_atomic(&self.dir.join(MARK), &body)
    }

    // -------------------------------------------------------------- load

    /// What this rig still owes, oldest first. Compacts on the way past.
    pub fn load(&self, rig_id: &str) -> std::io::Result<Held> {
        let (live, lines) = self.replay()?;
        let mark = self.read_mark();

        let mut events: Vec<Value> = live
            .values()
            .filter(|e| e.get("rigId").and_then(Value::as_str) == Some(rig_id))
            // Not what the uploader has already handed over. The page is
            // still an uploader too, so without this a restart would put
            // its outbox back to work the service already holds.
            .filter(|e| match &mark {
                Some((rig, seq)) => {
                    rig != rig_id || e.get("seq").and_then(Value::as_i64).unwrap_or(i64::MAX) > *seq
                }
                None => true,
            })
            .cloned()
            .collect();
        // seq is the cursor the server reads, and rig.js raises its own
        // seq past the highest here, so oldest-first is a requirement
        // rather than a presentational choice.
        events.sort_by_key(|e| e.get("seq").and_then(Value::as_i64).unwrap_or(0));

        self.compact(&live, lines)?;

        Ok(Held {
            events,
            videos: self.videos(rig_id),
            stint: self.stints().get(rig_id).cloned(),
        })
    }

    /// Replay the log into what is still owed, keyed by eventId so that
    /// re-appending the same event cannot produce two of it - the same
    /// rule as the server's unique `(rigId, eventId)`.
    fn replay(&self) -> std::io::Result<(BTreeMap<String, Value>, usize)> {
        let file = match File::open(self.dir.join(EVENTS)) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok((BTreeMap::new(), 0)),
            Err(e) => return Err(e),
        };

        let mut live: BTreeMap<String, Value> = BTreeMap::new();
        let mut lines = 0usize;
        for line in BufReader::new(file).lines() {
            let line = line?;
            if line.trim().is_empty() {
                continue;
            }
            lines += 1;
            // A line that will not parse is the one the power cut caught.
            let parsed: Value = match serde_json::from_str(&line) {
                Ok(v) => v,
                Err(_) => continue,
            };
            match parsed.get("op").and_then(Value::as_str) {
                Some("put") => {
                    if let Some(ev) = parsed.get("event") {
                        if let Some(id) = ev.get("eventId").and_then(Value::as_str) {
                            live.insert(id.to_string(), ev.clone());
                        }
                    }
                }
                Some("del") => {
                    if let Some(ids) = parsed.get("ids").and_then(Value::as_array) {
                        for id in ids.iter().filter_map(Value::as_str) {
                            live.remove(id);
                        }
                    }
                }
                _ => {}
            }
        }
        Ok((live, lines))
    }

    /// Write what is still owed to a fresh file and rename it over.
    ///
    /// Owed means two things now: not acknowledged by the page, and not
    /// already handed over by the uploader. The second is what lets a
    /// separate process drain this file without writing to it.
    fn compact(&self, live: &BTreeMap<String, Value>, lines: usize) -> std::io::Result<()> {
        let mark = self.read_mark();
        let sent = |e: &&Value| match &mark {
            Some((rig, seq)) => {
                e.get("rigId").and_then(Value::as_str) == Some(rig.as_str())
                    && e.get("seq").and_then(Value::as_i64).unwrap_or(i64::MAX) <= *seq
            }
            None => false,
        };
        let mut ordered: Vec<&Value> = live.values().filter(|e| !sent(e)).collect();

        // Nothing to drop means the file already says exactly this, so
        // rewriting it would be a write for no reason. That matters now
        // that the uploader reads this every couple of seconds all day:
        // an idle rig was rewriting its journal forty thousand times a
        // day, which is flash wear bought with nothing. Order is not a
        // reason to rewrite - `load` sorts what it hands back.
        if ordered.len() == lines {
            return Ok(());
        }
        // Ordered by seq so the compacted file reads the way it was
        // written, rather than in eventId order, which means nothing.
        ordered.sort_by_key(|e| e.get("seq").and_then(Value::as_i64).unwrap_or(0));

        let mut body: Vec<u8> = Vec::new();
        for ev in ordered {
            let mut line = Map::new();
            line.insert("op".into(), Value::String("put".into()));
            line.insert("event".into(), ev.clone());
            body.extend(serde_json::to_vec(&Value::Object(line)).map_err(std::io::Error::other)?);
            body.push(b'\n');
        }
        write_atomic(&self.dir.join(EVENTS), &body)
    }

    /// The held takes for one rig, metadata only. A blob whose metadata
    /// never landed is swept rather than reported: nothing can be done
    /// with bytes nobody can name.
    fn videos(&self, rig_id: &str) -> Vec<Value> {
        let dir = self.dir.join(VIDEO_DIR);
        let mut out = Vec::new();
        let mut named: Vec<String> = Vec::new();

        let entries = match fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => return out,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                named.push(stem.to_string());
            }
            let meta: Value = match fs::read(&path).ok().and_then(|b| serde_json::from_slice(&b).ok())
            {
                Some(v) => v,
                None => continue,
            };
            if meta.get("rigId").and_then(Value::as_str) == Some(rig_id) {
                out.push(meta);
            }
        }

        // Bytes with no metadata beside them: a crash between the two
        // writes in `put_video`. Nothing will ever ask for them.
        if let Ok(entries) = fs::read_dir(&dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().and_then(|e| e.to_str()) != Some("bin") {
                    continue;
                }
                let orphan = path
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .map(|s| !named.iter().any(|n| n == s))
                    .unwrap_or(false);
                if orphan {
                    let _ = fs::remove_file(&path);
                }
            }
        }
        out
    }
}

/// A video key is `episodeId + "/" + camera`, so it is not a filename: it
/// has a separator in it, and nothing stops an episode id arriving with a
/// `..` in it. Hex is used rather than a sanitising substitution because
/// it cannot collide - two different keys that both sanitised to
/// `ep_1_front` would be one file, and one take would silently overwrite
/// the other.
fn file_stem(key: &str) -> String {
    key.bytes().map(|b| format!("{b:02x}")).collect()
}

fn with_ext(stem: &Path, ext: &str) -> PathBuf {
    let mut name = stem.file_name().unwrap_or_default().to_os_string();
    name.push(".");
    name.push(ext);
    stem.with_file_name(name)
}

/// Write beside, fsync, then rename over. The rename is atomic, so a
/// reader sees the old file or the new one and never half of either -
/// which is what truncating in place would offer. The directory is synced
/// too, or a power cut can lose the rename itself.
fn write_atomic(path: &Path, body: &[u8]) -> std::io::Result<()> {
    let mut tmp_name = path.file_name().unwrap_or_default().to_os_string();
    tmp_name.push(".writing");
    let tmp = path.with_file_name(tmp_name);
    {
        let mut f = File::create(&tmp)?;
        f.write_all(body)?;
        f.sync_data()?;
    }
    fs::rename(&tmp, path)?;
    if let Some(dir) = path.parent() {
        if let Ok(d) = File::open(dir) {
            let _ = d.sync_all();
        }
    }
    Ok(())
}

fn remove_if_present(path: &Path) -> std::io::Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

// =====================================================================
// What the journal owes, and what it does not
//
// The promise on the session-ended screen is "Downtime and episodes are
// queued for upload". These are about that sentence being true across a
// restart - which is the only interesting thing here, because writing to
// a file that is never read again would pass any test about writing.
// =====================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT: AtomicUsize = AtomicUsize::new(0);

    /// A directory of this test's own. Tests share a process, and a
    /// journal keyed on a fixed path would have them reading each
    /// other's events - which is the bug the backend suite already paid
    /// for twice with its per-run schemas.
    struct Dir(PathBuf);

    impl Dir {
        fn new() -> Self {
            let p = std::env::temp_dir().join(format!(
                "rig-journal-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::SeqCst)
            ));
            let _ = fs::remove_dir_all(&p);
            Dir(p)
        }
        fn open(&self) -> Journal {
            Journal::open(&self.0).expect("journal opens")
        }
    }

    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn event(id: &str, seq: i64, rig: &str) -> Value {
        serde_json::json!({ "eventId": id, "seq": seq, "rigId": rig })
    }

    #[test]
    fn an_event_survives_a_restart() {
        let dir = Dir::new();
        dir.open().append_event(&event("e1", 0, "RIG-03")).unwrap();

        // A second open is a reboot: nothing carries over in memory.
        let held = dir.open().load("RIG-03").unwrap();
        assert_eq!(held.events.len(), 1, "the event was not still owed");
        assert_eq!(held.events[0]["eventId"], "e1");
    }

    #[test]
    fn what_the_server_acknowledged_is_no_longer_owed() {
        let dir = Dir::new();
        let j = dir.open();
        j.append_event(&event("e1", 0, "RIG-03")).unwrap();
        j.append_event(&event("e2", 1, "RIG-03")).unwrap();
        j.forget_events(&["e1".to_string()]).unwrap();

        let held = dir.open().load("RIG-03").unwrap();
        assert_eq!(held.events.len(), 1);
        assert_eq!(held.events[0]["eventId"], "e2", "the wrong one was kept");
    }

    #[test]
    fn what_is_owed_comes_back_oldest_first() {
        let dir = Dir::new();
        let j = dir.open();
        // Filed out of order on purpose: eventId order and seq order must
        // not be allowed to look the same by accident.
        j.append_event(&event("zzz", 0, "RIG-03")).unwrap();
        j.append_event(&event("aaa", 1, "RIG-03")).unwrap();
        j.append_event(&event("mmm", 2, "RIG-03")).unwrap();

        let held = dir.open().load("RIG-03").unwrap();
        let seqs: Vec<i64> = held.events.iter().map(|e| e["seq"].as_i64().unwrap()).collect();
        assert_eq!(seqs, vec![0, 1, 2], "rig.js raises its seq past the last of these");
    }

    #[test]
    fn the_same_event_filed_twice_is_owed_once() {
        let dir = Dir::new();
        let j = dir.open();
        j.append_event(&event("e1", 0, "RIG-03")).unwrap();
        j.append_event(&event("e1", 0, "RIG-03")).unwrap();

        let held = dir.open().load("RIG-03").unwrap();
        assert_eq!(held.events.len(), 1, "the server dedupes on eventId; so does this");
    }

    #[test]
    fn one_rig_never_reads_another_rigs_events() {
        let dir = Dir::new();
        let j = dir.open();
        j.append_event(&event("e1", 0, "RIG-03")).unwrap();
        j.append_event(&event("e2", 1, "RIG-07")).unwrap();

        let held = dir.open().load("RIG-03").unwrap();
        assert_eq!(held.events.len(), 1);
        assert_eq!(held.events[0]["rigId"], "RIG-03");
    }

    #[test]
    fn a_torn_last_line_does_not_strand_the_events_before_it() {
        let dir = Dir::new();
        let j = dir.open();
        j.append_event(&event("e1", 0, "RIG-03")).unwrap();
        j.append_event(&event("e2", 1, "RIG-03")).unwrap();

        // What a power cut mid-append leaves behind.
        let path = dir.0.join(EVENTS);
        let mut raw = fs::read_to_string(&path).unwrap();
        raw.push_str("{\"op\":\"put\",\"event\":{\"eventId\":\"e3\",\"se");
        fs::write(&path, raw).unwrap();

        let held = dir.open().load("RIG-03").unwrap();
        assert_eq!(
            held.events.len(),
            2,
            "the half-written line took the acknowledged ones with it"
        );
    }

    #[test]
    fn the_log_does_not_grow_for_ever() {
        let dir = Dir::new();
        let j = dir.open();
        for i in 0..50 {
            j.append_event(&event(&format!("e{i}"), i, "RIG-03")).unwrap();
        }
        j.forget_events(&(0..50).map(|i| format!("e{i}")).collect::<Vec<_>>())
            .unwrap();

        let before = fs::metadata(dir.0.join(EVENTS)).unwrap().len();
        let held = dir.open().load("RIG-03").unwrap();
        let after = fs::metadata(dir.0.join(EVENTS)).unwrap().len();

        assert!(held.events.is_empty(), "nothing is owed");
        assert!(after < before, "compaction left {after} bytes of {before}");
        assert_eq!(after, 0, "an empty outbox should compact to an empty file");
    }

    #[test]
    fn a_take_comes_back_with_its_bytes() {
        let dir = Dir::new();
        let meta = serde_json::json!({
            "key": "ep-1/front", "rigId": "RIG-03",
            "episodeId": "ep-1", "camera": "front"
        });
        dir.open().put_video(&meta, &[7u8; 2048]).unwrap();

        let j = dir.open();
        let held = j.load("RIG-03").unwrap();
        assert_eq!(held.videos.len(), 1, "the take was not still held");
        assert_eq!(held.videos[0]["camera"], "front");
        assert_eq!(
            j.read_video("ep-1/front").unwrap(),
            vec![7u8; 2048],
            "the bytes came back changed"
        );
    }

    #[test]
    fn a_confirmed_take_is_let_go_of_entirely() {
        let dir = Dir::new();
        let meta = serde_json::json!({ "key": "ep-1/front", "rigId": "RIG-03" });
        let j = dir.open();
        j.put_video(&meta, &[1u8; 64]).unwrap();
        j.forget_video("ep-1/front").unwrap();

        assert!(dir.open().load("RIG-03").unwrap().videos.is_empty());
        // The spool releasing its copy is the point; leaving the bytes
        // behind would be a spool that never frees.
        let left: Vec<_> = fs::read_dir(dir.0.join(VIDEO_DIR))
            .unwrap()
            .flatten()
            .map(|e| e.file_name())
            .collect();
        assert!(left.is_empty(), "bytes left behind: {left:?}");
    }

    #[test]
    fn a_key_cannot_write_outside_the_video_directory() {
        let dir = Dir::new();
        // A key is episodeId + "/" + camera and episodeId comes off the
        // wire. This is the one place a filename is built from it.
        let meta = serde_json::json!({ "key": "../../etc/rig-owned", "rigId": "RIG-03" });
        dir.open().put_video(&meta, &[9u8; 16]).unwrap();

        let escaped = dir.0.join("../../etc/rig-owned.bin");
        assert!(!escaped.exists(), "the key escaped the journal directory");
        assert_eq!(dir.open().read_video("../../etc/rig-owned").unwrap(), vec![9u8; 16]);
    }

    #[test]
    fn two_takes_whose_names_look_alike_are_two_takes() {
        let dir = Dir::new();
        let j = dir.open();
        // These collapse to one filename under any substitution that
        // replaces the separator, and one take would overwrite the other.
        j.put_video(
            &serde_json::json!({ "key": "ep-1/front", "rigId": "R" }),
            &[1u8; 8],
        )
        .unwrap();
        j.put_video(
            &serde_json::json!({ "key": "ep_1_front", "rigId": "R" }),
            &[2u8; 8],
        )
        .unwrap();

        assert_eq!(j.read_video("ep-1/front").unwrap(), vec![1u8; 8]);
        assert_eq!(j.read_video("ep_1_front").unwrap(), vec![2u8; 8]);
        assert_eq!(dir.open().load("R").unwrap().videos.len(), 2);
    }

    #[test]
    fn bytes_nobody_can_name_are_swept() {
        let dir = Dir::new();
        let j = dir.open();
        j.put_video(
            &serde_json::json!({ "key": "ep-1/front", "rigId": "RIG-03" }),
            &[1u8; 8],
        )
        .unwrap();
        // A crash between put_video's two writes.
        fs::remove_file(with_ext(&j.video_stem("ep-1/front"), "json")).unwrap();

        dir.open().load("RIG-03").unwrap();
        assert!(
            !with_ext(&j.video_stem("ep-1/front"), "bin").exists(),
            "an orphaned take stayed on the disk for ever"
        );
    }

    #[test]
    fn the_stint_survives_a_restart_and_is_per_rig() {
        let dir = Dir::new();
        let j = dir.open();
        j.put_stint(&serde_json::json!({ "rigId": "RIG-03", "episode": 4 }))
            .unwrap();
        j.put_stint(&serde_json::json!({ "rigId": "RIG-07", "episode": 9 }))
            .unwrap();
        // Writing it again replaces rather than accumulates.
        j.put_stint(&serde_json::json!({ "rigId": "RIG-03", "episode": 5 }))
            .unwrap();

        let held = dir.open().load("RIG-03").unwrap();
        assert_eq!(held.stint.expect("a stint was held")["episode"], 5);
        assert_eq!(
            dir.open().load("RIG-07").unwrap().stint.unwrap()["episode"],
            9,
            "one rig's stint overwrote another's"
        );
    }

    #[test]
    fn a_rig_with_nothing_owed_loads_cleanly() {
        let dir = Dir::new();
        let held = dir.open().load("RIG-03").unwrap();
        assert!(held.events.is_empty() && held.videos.is_empty() && held.stint.is_none());
    }

    // ------------------------------------------------------ idle writes

    #[test]
    fn an_idle_load_does_not_rewrite_the_log() {
        // The uploader reads this every couple of seconds for the length
        // of a shift. Rewriting a file that already says the right thing
        // is flash wear bought with nothing.
        let dir = Dir::new();
        let j = dir.open();
        j.append_event(&event("e1", 0, "RIG-03")).unwrap();

        let path = dir.0.join(EVENTS);
        let before = fs::metadata(&path).unwrap().modified().unwrap();
        j.load("RIG-03").unwrap();
        j.load("RIG-03").unwrap();
        let after = fs::metadata(&path).unwrap().modified().unwrap();

        assert_eq!(before, after, "an idle rig rewrote its journal for nothing");
    }

    #[test]
    fn a_load_with_something_to_drop_still_rewrites() {
        // And the saving must not have turned compaction off.
        let dir = Dir::new();
        let j = dir.open();
        j.append_event(&event("e1", 0, "RIG-03")).unwrap();
        j.append_event(&event("e2", 1, "RIG-03")).unwrap();
        j.forget_events(&["e1".to_string()]).unwrap();

        let path = dir.0.join(EVENTS);
        let before = fs::read_to_string(&path).unwrap();
        assert!(before.contains("\"e1\""));

        j.load("RIG-03").unwrap();

        let after = fs::read_to_string(&path).unwrap();
        assert!(!after.contains("\"e1\""), "the log never shrinks");
        assert!(after.contains("\"e2\""));
    }

    // ------------------------------------------------- the uploader's mark

    #[test]
    fn the_uploaders_mark_retires_what_it_has_handed_over() {
        let dir = Dir::new();
        let j = dir.open();
        for (id, seq) in [("e1", 0), ("e2", 1), ("e3", 2)] {
            j.append_event(&event(id, seq, "RIG-03")).unwrap();
        }
        // The service holds everything up to and including seq 1.
        j.set_mark("RIG-03", 1).unwrap();

        let held = dir.open().load("RIG-03").unwrap();
        assert_eq!(
            held.events.iter().map(|e| e["eventId"].as_str().unwrap()).collect::<Vec<_>>(),
            vec!["e3"],
            "the page was offered work the service already holds"
        );

        // And it is gone from the file, not merely hidden - otherwise the
        // journal grows for the length of a shift and never shrinks.
        let again = dir.open().load("RIG-03").unwrap();
        assert_eq!(again.events.len(), 1);
        let raw = fs::read_to_string(dir.0.join(EVENTS)).unwrap();
        assert!(!raw.contains("\"e1\""), "a handed-over event was left in the log");
    }

    #[test]
    fn a_mark_belongs_to_one_rig() {
        // One machine is one rig, but the log is not rig-scoped, and a
        // mark that retired another rig's work would be silent and
        // permanent - the worst shape of bug this system has.
        let dir = Dir::new();
        let j = dir.open();
        j.append_event(&event("mine", 5, "RIG-03")).unwrap();
        j.append_event(&event("theirs", 5, "RIG-07")).unwrap();
        j.set_mark("RIG-03", 9).unwrap();

        assert!(dir.open().load("RIG-03").unwrap().events.is_empty());
        assert_eq!(
            dir.open().load("RIG-07").unwrap().events.len(),
            1,
            "one rig's progress retired another rig's work"
        );
    }

    #[test]
    fn a_mark_only_ever_moves_forward() {
        let dir = Dir::new();
        let j = dir.open();
        j.set_mark("RIG-03", 7).unwrap();
        j.set_mark("RIG-03", 3).unwrap();
        assert_eq!(j.read_mark(), Some(("RIG-03".to_string(), 7)),
            "the mark went backwards, which is the one direction it must not");

        j.set_mark("RIG-03", 9).unwrap();
        assert_eq!(j.read_mark(), Some(("RIG-03".to_string(), 9)));
    }

    #[test]
    fn a_rig_the_mark_does_not_name_keeps_everything() {
        let dir = Dir::new();
        let j = dir.open();
        j.append_event(&event("e1", 0, "RIG-03")).unwrap();
        j.set_mark("RIG-07", 99).unwrap();
        assert_eq!(dir.open().load("RIG-03").unwrap().events.len(), 1);
    }

    #[test]
    fn with_no_mark_nothing_changes() {
        let dir = Dir::new();
        let j = dir.open();
        j.append_event(&event("e1", 0, "RIG-03")).unwrap();
        assert_eq!(j.read_mark(), None);
        assert_eq!(dir.open().load("RIG-03").unwrap().events.len(), 1);
    }
}
