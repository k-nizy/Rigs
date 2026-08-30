//! The kiosk shell, as a library so its parts can be tested.
//!
//! `main.rs` is the binary: it opens a window on the floor's rig page and
//! nothing else. Everything with behaviour worth pinning lives here.
//!
//! `roda` is the seam to the teleop layer and `mock_roda` is what stands
//! behind it until RODA-RS exists. Nothing above `roda` knows which one
//! it is talking to, which is the whole point - on hardware day one file
//! is added and the rest of this tree does not notice.

//! `journal` is the disk half of the outbox: the synchronous, fsynced
//! write that the browser's IndexedDB cannot offer, and the reason an
//! event the page has called safe survives a power cut.

pub mod journal;
pub mod mock_roda;
pub mod roda;
