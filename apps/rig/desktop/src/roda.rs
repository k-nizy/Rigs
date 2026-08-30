//! The one place the rest of the shell touches the teleop layer.
//!
//! RODA-RS owns the robot, the cameras, and the act of recording. The rig
//! page owns *when* - which is what the pedals have always decided - and
//! RODA-RS owns *how*. There are no docs for its API and none were
//! available, so this does not invent one: it writes down the six calls
//! the rig actually needs, and everything above this file is written
//! against those six rather than against RODA-RS.
//!
//! `BACKEND-PLAN.md` puts it plainly: when the real API arrives - CLI,
//! socket, HTTP, library, it does not matter which - one file changes and
//! the rest of the tree does not notice. If anything outside the adapter
//! has to change on that day, the boundary was drawn in the wrong place
//! and it is worth fixing then rather than absorbing.

use std::fmt;
use std::path::PathBuf;

use uuid::Uuid;

/// A camera by the name the floor calls it: `front`, `wrist-l`,
/// `overhead`. The name is not decoration - it becomes the filename, and
/// the upload is keyed by it, so two cameras that swapped names would
/// upload each other's footage and every checksum would still agree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Camera {
    pub name: String,
}

impl Camera {
    pub fn new(name: impl Into<String>) -> Self {
        Camera { name: name.into() }
    }
}

/// One camera's file from one take.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordedCamera {
    pub name: String,
    pub path: PathBuf,
    pub bytes: u64,
}

/// What one take produced.
///
/// **`seconds` is the recorder's number, and it has to be the one that
/// reaches `episode_saved.durationSecs`.** Not a duration the caller
/// measured separately with its own clock.
///
/// That is not a style preference. `/api/floor/video` divides bytes by
/// `durationSecs` to get a rate, and the whole 245 TB sizing table is
/// read back that way. Two clocks that disagree by a few hundred
/// milliseconds turn that division into a number nobody chose, and
/// nothing downstream can tell it happened - the episode is well formed,
/// the bytes are real, the rate is simply wrong.
///
/// The JavaScript half of this seam already made the matching mistake
/// once: `mock-roda.test.js` has a test named for it, because the wiring
/// handed the recorder an episode and a camera and never told it how long
/// the take was.
#[derive(Debug, Clone, PartialEq)]
pub struct Recorded {
    pub episode_id: Uuid,
    /// `<root>/episodes/<episodeId>/`, the directory named by the id the
    /// rig minted at pedal-press.
    pub dir: PathBuf,
    pub seconds: f64,
    pub cameras: Vec<RecordedCamera>,
}

impl Recorded {
    /// Every camera's bytes. What the uploader is about to move, and what
    /// the sizing table is measured from.
    pub fn total_bytes(&self) -> u64 {
        self.cameras.iter().map(|c| c.bytes).sum()
    }
}

/// One camera, as the rig would report it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CameraHealth {
    pub name: String,
    pub ok: bool,
}

/// Whether the teleop layer can work right now.
///
/// Phase 2 maps this onto the fault and rig-down screens the operator
/// already knows, so a camera dropping out raises a screen rather than a
/// log line. It is deliberately three plain facts and not a score: the
/// rig decides what a fault is, and a health struct that pre-judged that
/// would be a second opinion in a system built around having one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Health {
    pub cameras: Vec<CameraHealth>,
    pub arms_ok: bool,
    pub disk_free_bytes: u64,
}

impl Health {
    pub fn ok(&self) -> bool {
        self.arms_ok && self.cameras.iter().all(|c| c.ok)
    }
}

#[derive(Debug)]
pub enum RodaError {
    /// `start_episode` while one is already recording. A bug in the
    /// caller rather than a condition to recover from - two takes at once
    /// means one of them has no id anybody holds.
    AlreadyRecording,
    /// `stop_episode` or `discard_episode` with nothing running.
    NotRecording,
    /// A session with nothing to record from. Refused at `open` rather
    /// than at the first pedal press, so a misprovisioned rig is loud at
    /// start-up instead of halfway through somebody's shift.
    NoCameras,
    /// A rate that is not a positive number of bytes per second.
    Rate(String),
    Io(std::io::Error),
}

impl fmt::Display for RodaError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RodaError::AlreadyRecording => write!(f, "an episode is already recording"),
            RodaError::NotRecording => write!(f, "no episode is recording"),
            RodaError::NoCameras => write!(f, "a session needs at least one camera"),
            RodaError::Rate(m) => write!(f, "{m}"),
            RodaError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for RodaError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            RodaError::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for RodaError {
    fn from(e: std::io::Error) -> Self {
        RodaError::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, RodaError>;

/// Six calls. Nothing above this trait knows RODA-RS exists.
///
/// `open` is an associated function returning `Self` because the plan's
/// `Session` and the implementor are the same thing - there is one
/// session per process, held for the length of a shift.
pub trait Roda: Sized {
    /// Take the cameras for this rig. Called once at start-up.
    fn open(rig_id: &str, cameras: &[Camera]) -> Result<Self>;

    /// Begin recording under an id **the caller already minted**.
    ///
    /// The rig mints a uuid at pedal-press, before this layer is told
    /// anything, and passes it down; the output directory is named by it
    /// and the `episode_saved` event carries it. That is the single most
    /// important join in the system - the file on the SSD and the row in
    /// the ledger name the same thing - and it holds precisely because
    /// this function does not get to choose the id.
    ///
    /// If a real RODA-RS insists on minting its own session id, the
    /// adapter records both and the event carries `rodaSessionId`
    /// alongside. The rig's id stays the primary key, because it exists
    /// even for an episode RODA-RS failed to start, and that is a row
    /// worth having.
    fn start_episode(&mut self, episode_id: Uuid) -> Result<()>;

    /// Stop, and report what was written. See [`Recorded::seconds`].
    fn stop_episode(&mut self) -> Result<Recorded>;

    /// Stop and unlink. An operator rejected the take, so there is
    /// nothing to upload and nothing to keep.
    fn discard_episode(&mut self) -> Result<()>;

    fn health(&self) -> Health;

    /// Release the cameras. Consumes the session, because a closed one
    /// cannot be reopened - the next shift opens a new one.
    fn close(self) -> Result<()>;
}
