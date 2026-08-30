//! A camera that does not exist, at the size a real one would be.
//!
//! This is the native half of a mock the browser seam already has.
//! `apps/rig/tools/mock-roda.js` stands in for the camera inside the
//! page; this stands in for RODA-RS underneath the shell. They are the
//! same idea at two different seams, and they deliberately produce **the
//! same bytes for the same take** - a repository whose founding rule is
//! that the desk and the rig never compute different answers should not
//! ship two mocks that disagree about what a take of *n* seconds is.
//!
//! It exists so the whole pipeline below it - journal, uploader, ingest,
//! spool, archive - is built and exercised before any hardware arrives,
//! and hardware day is a swap rather than an integration. Every byte the
//! video path has carried so far has been a couple of kilobytes of
//! filler, which proves the plumbing and proves nothing about the volume.
//!
//! **The size is not a knob.** It is `bytes_per_second * seconds`, and
//! the seconds are the ones that reach the ledger, so anything dividing
//! bytes by `durationSecs` gets the rate back rather than a number nobody
//! chose. A test that wants a small take asks for a short one; there is
//! no scale factor here to be mistaken for a measurement later.

use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use uuid::Uuid;

use crate::roda::{
    Camera, CameraHealth, Health, Recorded, RecordedCamera, Roda, RodaError, Result,
};

/// Three 1080p30 cameras at ~7 Mbps each, per camera, per second.
///
/// `BACKEND-PLAN.md` builds its whole sizing table on this: ~9 GB per
/// rig-hour, ~2.6 TB a day across the floor, and the 245 TB the cold tier
/// is provisioned for. The plan says twice that it is an estimate - the
/// first real episode from RODA-RS replaces it, and a different codec or
/// resolution moves every row - which is exactly why it is one named
/// constant rather than a number sprinkled about.
pub const BYTES_PER_SECOND_PER_CAMERA: u64 = 7_000_000 / 8; // 875,000

/// Where the plan puts takes on a rig's SSD.
pub const DEFAULT_ROOT: &str = "/var/lib/rig";

/// What a mock take is filled with, tiled to length.
const BLOCK: usize = 4096;

/// FNV-1a over the key, so the same take always produces the same bytes.
///
/// The same function as the JavaScript mock's, arithmetic and all. Bytes
/// have to differ per episode *and* per camera, or a mixed-up camera key
/// uploads identical content and every checksum agrees with the wrong
/// one. And they have to be stable, because a retry after a reload
/// re-reads the take and a recorder that answered differently each time
/// would read as corruption rather than as a retry.
fn seed_of(key: &str) -> u32 {
    let mut h: u32 = 0x811c_9dc5;
    // The keys here are a uuid, a slash and a camera name - all ASCII -
    // so bytes and JavaScript's UTF-16 code units are the same numbers.
    for b in key.bytes() {
        h ^= b as u32;
        h = h.wrapping_mul(0x0100_0193);
    }
    if h == 0 {
        1
    } else {
        h
    }
}

/// One block of xorshift32 noise. Tiled rather than generated per byte,
/// which keeps a 200 MB take cheap; nothing here cares whether it
/// compresses.
fn filler_block(seed: u32) -> [u8; BLOCK] {
    let mut block = [0u8; BLOCK];
    let mut x = seed;
    for slot in block.iter_mut() {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        *slot = (x & 0xff) as u8;
    }
    block
}

/// JavaScript prints an integral Number without a decimal point, and the
/// header below has to read the same from either mock.
fn js_number(n: f64) -> String {
    if n.fract() == 0.0 && n.abs() < 1e21 {
        format!("{}", n as i64)
    } else {
        format!("{n}")
    }
}

/// The bytes of one camera's take: a readable header, then filler.
///
/// The header is there so a file pulled out of the spool by hand says
/// which take and which camera it belongs to without a lookup.
pub fn take_bytes(episode_id: &str, camera: &str, seconds: f64, rate: u64) -> Vec<u8> {
    let total = (rate as f64 * seconds).round() as i64;
    if total <= 0 {
        return Vec::new();
    }
    let total = total as usize;

    let key = format!("{episode_id}/{camera}");
    let head = format!(
        "MOCK-RODA {key} {}s {total}B\n",
        js_number(seconds)
    );

    let mut out = vec![0u8; total];
    let head = head.as_bytes();
    let head_len = head.len().min(total);
    out[..head_len].copy_from_slice(&head[..head_len]);

    let block = filler_block(seed_of(&key));
    let mut at = head_len;
    while at < total {
        let n = BLOCK.min(total - at);
        out[at..at + n].copy_from_slice(&block[..n]);
        at += n;
    }
    out
}

/// What this mock actually produced, so a soak can say what it pushed and
/// a test can catch a caller that forgot to pass a duration.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Stats {
    pub takes: u64,
    pub bytes: u64,
    /// Takes that recorded nothing. Silence is what the seam asks for,
    /// but it has to be countable.
    pub skipped: u64,
}

struct Recording {
    episode_id: Uuid,
    started_at: f64,
}

/// A stand-in for RODA-RS that writes real files of plausible size.
pub struct MockRoda {
    rig_id: String,
    cameras: Vec<Camera>,
    root: PathBuf,
    bytes_per_second: u64,
    /// Monotonic seconds. Injected so a test can produce an exact
    /// duration; a mock whose take size depended on how fast the test
    /// machine ran would be untestable in the one property that matters.
    now: Box<dyn Fn() -> f64 + Send + Sync>,
    recording: Option<Recording>,
    stats: Stats,
}

/// Written by hand because the injected clock is a boxed closure and
/// cannot be derived. Worth having rather than dropping: `unwrap_err` on
/// a constructor needs it, and a rig that logs its recorder should say
/// which cameras and what rate rather than printing an address.
impl fmt::Debug for MockRoda {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("MockRoda")
            .field("rig_id", &self.rig_id)
            .field("cameras", &self.cameras)
            .field("root", &self.root)
            .field("bytes_per_second", &self.bytes_per_second)
            .field("recording", &self.recording.as_ref().map(|r| r.episode_id))
            .field("stats", &self.stats)
            .finish_non_exhaustive()
    }
}

impl MockRoda {
    /// Open against a chosen root. `Roda::open` uses [`DEFAULT_ROOT`].
    pub fn open_at(rig_id: &str, cameras: &[Camera], root: impl Into<PathBuf>) -> Result<Self> {
        if cameras.is_empty() {
            return Err(RodaError::NoCameras);
        }
        let start = std::time::Instant::now();
        Ok(MockRoda {
            rig_id: rig_id.to_string(),
            cameras: cameras.to_vec(),
            root: root.into(),
            bytes_per_second: BYTES_PER_SECOND_PER_CAMERA,
            now: Box::new(move || start.elapsed().as_secs_f64()),
            recording: None,
            stats: Stats::default(),
        })
    }

    /// A measured rate replaces the plan's estimate without touching
    /// anything else. Refused at construction if it is not positive,
    /// because a rate of zero produces empty takes that look like a
    /// broken camera three stages downstream.
    pub fn with_rate(mut self, bytes_per_second: u64) -> Result<Self> {
        if bytes_per_second == 0 {
            return Err(RodaError::Rate(
                "bytesPerSecond must be positive".to_string(),
            ));
        }
        self.bytes_per_second = bytes_per_second;
        Ok(self)
    }

    /// Drive the clock from a test instead of the wall.
    pub fn with_clock(mut self, now: impl Fn() -> f64 + Send + Sync + 'static) -> Self {
        self.now = Box::new(now);
        self
    }

    pub fn bytes_per_second(&self) -> u64 {
        self.bytes_per_second
    }

    pub fn stats(&self) -> Stats {
        self.stats
    }

    pub fn rig_id(&self) -> &str {
        &self.rig_id
    }

    pub fn episode_dir(&self, episode_id: Uuid) -> PathBuf {
        self.root.join("episodes").join(episode_id.to_string())
    }

    pub fn is_recording(&self) -> bool {
        self.recording.is_some()
    }
}

impl Roda for MockRoda {
    fn open(rig_id: &str, cameras: &[Camera]) -> Result<Self> {
        Self::open_at(rig_id, cameras, DEFAULT_ROOT)
    }

    fn start_episode(&mut self, episode_id: Uuid) -> Result<()> {
        if self.recording.is_some() {
            return Err(RodaError::AlreadyRecording);
        }
        self.recording = Some(Recording {
            episode_id,
            started_at: (self.now)(),
        });
        Ok(())
    }

    fn stop_episode(&mut self) -> Result<Recorded> {
        let rec = self.recording.take().ok_or(RodaError::NotRecording)?;
        let seconds = ((self.now)() - rec.started_at).max(0.0);
        let dir = self.episode_dir(rec.episode_id);
        let id = rec.episode_id.to_string();

        // A take of no seconds recorded nothing, and that is not an
        // error. This is called from the pedal handler; a recorder that
        // can end an operator's take by failing is worse than one that
        // quietly produces nothing and says so in the count.
        if (self.bytes_per_second as f64 * seconds).round() as i64 <= 0 {
            self.stats.skipped += 1;
            return Ok(Recorded {
                episode_id: rec.episode_id,
                dir,
                seconds,
                cameras: Vec::new(),
            });
        }

        fs::create_dir_all(&dir)?;
        let mut written = Vec::with_capacity(self.cameras.len());
        for cam in &self.cameras {
            let bytes = take_bytes(&id, &cam.name, seconds, self.bytes_per_second);
            // The plan's path, `.mp4` and all, because the uploader and
            // the spool are keyed by it and hardware day should change
            // the contents of these files and nothing else. The bytes are
            // not a container and do not pretend to be: the first line of
            // every one of them says MOCK-RODA in plain ASCII.
            let path = dir.join(format!("{}.mp4", cam.name));
            fs::write(&path, &bytes)?;
            self.stats.bytes += bytes.len() as u64;
            written.push(RecordedCamera {
                name: cam.name.clone(),
                path,
                bytes: bytes.len() as u64,
            });
        }
        self.stats.takes += 1;

        Ok(Recorded {
            episode_id: rec.episode_id,
            dir,
            seconds,
            cameras: written,
        })
    }

    fn discard_episode(&mut self) -> Result<()> {
        let rec = self.recording.take().ok_or(RodaError::NotRecording)?;
        remove_episode(&self.episode_dir(rec.episode_id))
    }

    fn health(&self) -> Health {
        Health {
            cameras: self
                .cameras
                .iter()
                .map(|c| CameraHealth {
                    name: c.name.clone(),
                    ok: true,
                })
                .collect(),
            arms_ok: true,
            disk_free_bytes: free_bytes(&self.root),
        }
    }

    fn close(mut self) -> Result<()> {
        // A take still running when the shell shuts down is not a take.
        // No `episode_saved` will ever name it, so keeping the files
        // would leave bytes on the SSD that nothing references and
        // nothing will ever clean up - and the SSD is self-managing only
        // because every file on it is spoken for.
        if let Some(rec) = self.recording.take() {
            remove_episode(&self.episode_dir(rec.episode_id))?;
        }
        Ok(())
    }
}

fn remove_episode(dir: &Path) -> Result<()> {
    match fs::remove_dir_all(dir) {
        Ok(()) => Ok(()),
        // Nothing was written yet, which is the ordinary case: this mock
        // writes at stop, where RODA-RS writes throughout.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(RodaError::Io(e)),
    }
}

/// Free space, for `Health`. Unknown rather than a guess when the
/// platform will not say - reporting a number nobody measured is how a
/// disk-full alert ends up firing on the wrong rig.
#[cfg(unix)]
fn free_bytes(path: &Path) -> u64 {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let Ok(c) = CString::new(path.as_os_str().as_bytes()) else {
        return 0;
    };
    // SAFETY: `c` is a valid NUL-terminated path and `stat` is only read
    // after statvfs reports success.
    unsafe {
        let mut stat: libc::statvfs = std::mem::zeroed();
        if libc::statvfs(c.as_ptr(), &mut stat) == 0 {
            (stat.f_bavail as u64).saturating_mul(stat.f_frsize as u64)
        } else {
            0
        }
    }
}

#[cfg(not(unix))]
fn free_bytes(_path: &Path) -> u64 {
    0
}
