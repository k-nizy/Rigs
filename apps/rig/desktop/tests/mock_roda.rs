//! What the stand-in camera has to be true about.
//!
//! These mirror `apps/rig/mock-roda.test.js` deliberately, question for
//! question, because the two mocks stand at two seams of the same system
//! and the numbers have to be the same numbers. The last one in the size
//! section is the one that would catch them drifting apart: it pins the
//! JavaScript mock's actual output, byte for byte.

use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use rig_desktop::mock_roda::{take_bytes, MockRoda, BYTES_PER_SECOND_PER_CAMERA};
use rig_desktop::roda::{Camera, Roda, RodaError};
use uuid::Uuid;

// ------------------------------------------------------------ helpers

/// A directory of our own per test, so two tests cannot see each other's
/// takes and a failure leaves evidence rather than a shared mess.
struct Scratch(PathBuf);

impl Scratch {
    fn new(what: &str) -> Self {
        let p = std::env::temp_dir().join(format!("rig-mock-{what}-{}", Uuid::new_v4()));
        fs::create_dir_all(&p).unwrap();
        Scratch(p)
    }
    fn path(&self) -> &PathBuf {
        &self.0
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// A clock a test drives, so a take is exactly as long as the test says.
/// Wall time would make the one property that matters - bytes over
/// seconds - depend on how busy the machine was.
fn clock() -> (Arc<AtomicU64>, impl Fn() -> f64 + Send + Sync + 'static) {
    let millis = Arc::new(AtomicU64::new(0));
    let read = millis.clone();
    (millis, move || read.load(Ordering::SeqCst) as f64 / 1000.0)
}

fn cams(names: &[&str]) -> Vec<Camera> {
    names.iter().map(|n| Camera::new(*n)).collect()
}

fn fnv64(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        h ^= *b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

/// Record one take of exactly `secs`, and hand back what it wrote.
fn take(roda: &mut MockRoda, ms: &Arc<AtomicU64>, id: Uuid, secs: f64) -> rig_desktop::roda::Recorded {
    ms.store(0, Ordering::SeqCst);
    roda.start_episode(id).unwrap();
    ms.store((secs * 1000.0) as u64, Ordering::SeqCst);
    roda.stop_episode().unwrap()
}

// --------------------------------------------------------- the size

#[test]
fn a_take_is_the_plans_rate_times_the_seconds_recorded() {
    let bytes = take_bytes("ep-1", "front", 10.0, BYTES_PER_SECOND_PER_CAMERA);
    assert_eq!(
        bytes.len() as u64,
        BYTES_PER_SECOND_PER_CAMERA * 10,
        "ten seconds of one 1080p30 camera at ~7 Mbps"
    );
    assert_eq!(bytes.len(), 8_750_000, "and that is 8.75 MB, stated rather than derived");
}

#[test]
fn bytes_and_duration_agree_so_the_rate_reads_back_out() {
    for secs in [1.0, 7.0, 45.0, 300.0] {
        let n = take_bytes("ep-1", "front", secs, BYTES_PER_SECOND_PER_CAMERA).len() as f64;
        assert_eq!(
            (n / secs) as u64,
            BYTES_PER_SECOND_PER_CAMERA,
            "anything dividing bytes by duration must get the rate back, at {secs}s"
        );
    }
}

#[test]
fn three_cameras_for_eight_hours_is_the_shift_the_sizing_table_claims() {
    let shift = BYTES_PER_SECOND_PER_CAMERA * 3 * 8 * 3600;
    let gb = shift as f64 / 1e9;
    assert!(
        gb > 71.0 && gb < 76.0,
        "~72 GB per rig per 8h shift, per BACKEND-PLAN.md - got {gb:.0} GB"
    );
}

#[test]
fn a_measured_rate_replaces_the_estimate_without_touching_anything_else() {
    let scratch = Scratch::new("rate");
    let (ms, now) = clock();
    let mut roda = MockRoda::open_at("RIG-03", &cams(&["front"]), scratch.path())
        .unwrap()
        .with_rate(1000)
        .unwrap()
        .with_clock(now);
    assert_eq!(roda.bytes_per_second(), 1000);

    let rec = take(&mut roda, &ms, Uuid::new_v4(), 10.0);
    assert_eq!(rec.total_bytes(), 10_000);
}

#[test]
fn a_rate_that_is_not_positive_is_refused_at_construction() {
    let scratch = Scratch::new("badrate");
    let err = MockRoda::open_at("RIG-03", &cams(&["front"]), scratch.path())
        .unwrap()
        .with_rate(0)
        .unwrap_err();
    assert!(
        matches!(err, RodaError::Rate(_)),
        "a rate of zero produces empty takes that read as a broken camera three stages later"
    );
    assert!(err.to_string().contains("positive"));
}

// ------------------------------------------------------ the content

#[test]
fn every_camera_and_every_episode_gets_different_bytes() {
    let front = take_bytes("ep-1", "front", 2.0, 4096);
    let wrist = take_bytes("ep-1", "wrist-l", 2.0, 4096);
    let later = take_bytes("ep-2", "front", 2.0, 4096);

    assert_eq!(front.len(), wrist.len(), "same take, same length");
    assert_ne!(
        front, wrist,
        "identical bytes per camera means a mixed-up key checksums as correct"
    );
    assert_ne!(front, later, "and the same for a different episode");
}

#[test]
fn the_same_take_twice_is_the_same_bytes() {
    let once = take_bytes("ep-1", "front", 3.0, 4096);
    let again = take_bytes("ep-1", "front", 3.0, 4096);
    assert_eq!(
        once, again,
        "a retry re-reads the take; a recorder that answered differently \
         each call would read as corruption rather than as a retry"
    );
}

#[test]
fn a_file_pulled_out_of_the_spool_says_which_take_it_is() {
    let bytes = take_bytes("ep-7", "overhead", 2.0, 4096);
    let head = String::from_utf8_lossy(&bytes[..33]).to_string();
    assert_eq!(head, "MOCK-RODA ep-7/overhead 2s 8192B\n");
}

/// The one that would catch the two mocks drifting apart.
///
/// These digests are the JavaScript mock's actual output - same seed,
/// same xorshift, same header - so if either implementation changes, this
/// fails and somebody has to look at both. Regenerate with a short script
/// over `apps/rig/tools/mock-roda.js` if the format is ever changed on
/// purpose; do not "fix" it by editing the constants.
#[test]
fn the_two_mocks_produce_the_same_bytes_for_the_same_take() {
    // (episode, camera, seconds, rate, length, fnv-1a 64 of the content)
    let cases: [(&str, &str, f64, u64, usize, u64); 3] = [
        ("ep-7", "overhead", 2.0, 4096, 8_192, 0x8328_68a6_8183_e755),
        ("ep-1", "front", 3.0, 4096, 12_288, 0xcc0f_91c9_603d_61ab),
        (
            "3f8a1c2e-0b4d-4e7a-9c11-8de2a5f60471",
            "front",
            1.5,
            875_000,
            1_312_500,
            0x8a13_8173_e4b3_0fe9,
        ),
    ];
    for (ep, cam, secs, rate, len, digest) in cases {
        let bytes = take_bytes(ep, cam, secs, rate);
        assert_eq!(bytes.len(), len, "length for {ep}/{cam} at {secs}s");
        assert_eq!(
            fnv64(&bytes),
            digest,
            "content for {ep}/{cam} at {secs}s no longer matches mock-roda.js"
        );
    }
}

// ------------------------------------------------------ the session

#[test]
fn a_take_is_written_under_the_id_the_caller_minted() {
    let scratch = Scratch::new("id");
    let (ms, now) = clock();
    let mut roda = MockRoda::open_at("RIG-03", &cams(&["front", "wrist-l", "overhead"]), scratch.path())
        .unwrap()
        .with_rate(1000)
        .unwrap()
        .with_clock(now);

    let id = Uuid::new_v4();
    let rec = take(&mut roda, &ms, id, 2.0);

    assert_eq!(rec.episode_id, id);
    assert_eq!(
        rec.dir,
        scratch.path().join("episodes").join(id.to_string()),
        "the directory is named by the id the rig minted at pedal-press, \
         which is what makes the file and the ledger row name one thing"
    );
    assert_eq!(rec.cameras.len(), 3, "three cameras, three files");
    for cam in &rec.cameras {
        assert!(cam.path.exists(), "{} was not written", cam.path.display());
        assert_eq!(fs::metadata(&cam.path).unwrap().len(), cam.bytes);
        assert_eq!(cam.bytes, 2000);
    }
}

#[test]
fn two_takes_at_once_is_refused() {
    let scratch = Scratch::new("two");
    let mut roda = MockRoda::open_at("RIG-03", &cams(&["front"]), scratch.path()).unwrap();
    roda.start_episode(Uuid::new_v4()).unwrap();
    let err = roda.start_episode(Uuid::new_v4()).unwrap_err();
    assert!(
        matches!(err, RodaError::AlreadyRecording),
        "two takes at once means one of them has no id anybody holds"
    );
}

#[test]
fn stopping_or_discarding_with_nothing_recording_is_refused() {
    let scratch = Scratch::new("idle");
    let mut roda = MockRoda::open_at("RIG-03", &cams(&["front"]), scratch.path()).unwrap();
    assert!(matches!(roda.stop_episode().unwrap_err(), RodaError::NotRecording));
    assert!(matches!(roda.discard_episode().unwrap_err(), RodaError::NotRecording));
}

#[test]
fn a_discarded_take_leaves_nothing_behind() {
    let scratch = Scratch::new("discard");
    let (ms, now) = clock();
    let mut roda = MockRoda::open_at("RIG-03", &cams(&["front"]), scratch.path())
        .unwrap()
        .with_rate(1000)
        .unwrap()
        .with_clock(now);

    // One take saved, so there is something on disk to be wrong about.
    let kept = take(&mut roda, &ms, Uuid::new_v4(), 1.0);
    assert!(kept.dir.exists());

    let thrown = Uuid::new_v4();
    ms.store(0, Ordering::SeqCst);
    roda.start_episode(thrown).unwrap();
    ms.store(5_000, Ordering::SeqCst);
    roda.discard_episode().unwrap();

    assert!(!roda.episode_dir(thrown).exists(), "the discarded take is gone");
    assert!(kept.dir.exists(), "and the saved one is untouched");
    assert!(!roda.is_recording());
}

#[test]
fn closing_mid_take_leaves_nothing_behind() {
    let scratch = Scratch::new("close");
    let mut roda = MockRoda::open_at("RIG-03", &cams(&["front"]), scratch.path()).unwrap();
    let id = Uuid::new_v4();
    let dir = roda.episode_dir(id);
    roda.start_episode(id).unwrap();
    roda.close().unwrap();
    assert!(
        !dir.exists(),
        "a take running at shutdown is not a take: no episode_saved will \
         ever name it, so its bytes would sit on the SSD unreferenced"
    );
}

#[test]
fn a_take_of_no_seconds_writes_nothing_and_says_so() {
    let scratch = Scratch::new("empty");
    let (ms, now) = clock();
    let mut roda = MockRoda::open_at("RIG-03", &cams(&["front"]), scratch.path())
        .unwrap()
        .with_clock(now);

    let id = Uuid::new_v4();
    ms.store(0, Ordering::SeqCst);
    roda.start_episode(id).unwrap();
    let rec = roda.stop_episode().unwrap();

    assert!(rec.cameras.is_empty(), "nothing was recorded, so nothing is queued");
    assert!(!rec.dir.exists(), "and no directory was made for it");
    assert_eq!(roda.stats().skipped, 1, "silence is asked for, but it must be countable");
    assert_eq!(roda.stats().takes, 0);
}

#[test]
fn stats_report_what_was_actually_produced() {
    let scratch = Scratch::new("stats");
    let (ms, now) = clock();
    let mut roda = MockRoda::open_at("RIG-03", &cams(&["front", "wrist-l"]), scratch.path())
        .unwrap()
        .with_rate(1000)
        .unwrap()
        .with_clock(now);

    take(&mut roda, &ms, Uuid::new_v4(), 2.0);
    take(&mut roda, &ms, Uuid::new_v4(), 3.0);

    let s = roda.stats();
    assert_eq!(s.takes, 2);
    assert_eq!(s.bytes, (2 + 3) * 1000 * 2, "two cameras on each take");
    assert_eq!(s.skipped, 0);
}

#[test]
fn a_session_with_no_cameras_is_refused() {
    let scratch = Scratch::new("nocams");
    let err = MockRoda::open_at("RIG-03", &[], scratch.path()).unwrap_err();
    assert!(
        matches!(err, RodaError::NoCameras),
        "a misprovisioned rig should be loud at start-up, not halfway through a shift"
    );
}

#[test]
fn health_names_every_camera() {
    let scratch = Scratch::new("health");
    let roda =
        MockRoda::open_at("RIG-03", &cams(&["front", "wrist-l", "overhead"]), scratch.path()).unwrap();
    let h = roda.health();
    assert_eq!(
        h.cameras.iter().map(|c| c.name.as_str()).collect::<Vec<_>>(),
        vec!["front", "wrist-l", "overhead"]
    );
    assert!(h.ok(), "a mock with no fault injected is a working rig");
}
