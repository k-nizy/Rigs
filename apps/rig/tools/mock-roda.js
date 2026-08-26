/* =====================================================================
 * mock-roda.js  -  a camera that does not exist, at the size one would be
 *
 * `BACKEND-PLAN.md` writes `MockRoda` as the Rust half of the RODA-RS
 * adapter: it "satisfies the same trait, writes a real file of plausible
 * size to the real path, and returns real numbers", so that the whole
 * pipeline below it - journal, uploader, ingest, spool, archive - is
 * built and tested before any hardware arrives.
 *
 * This is that idea at the seam a browser actually has. `videoSource` is
 * handed an episode, a camera and the seconds the rig recorded, and
 * returns a Blob. Attach one of these and every stage downstream carries
 * bytes at the volume a real take would.
 *
 * **It is never attached by the page.** `rig.js` says why, and it is the
 * reason this file lives in `tools/` and not in `assets/`: a page that
 * invents bytes pollutes what `/api/floor/video` measures, and that
 * measurement is the thing meant to replace the plan's sizing guess with
 * a fact. A test or a soak attaches this deliberately, against a service
 * somebody is willing to have carry synthetic takes. Nothing else does.
 *
 * The size is not a knob. It is `bytesPerSecond * secondsRecorded`, and
 * the seconds are the ones the rig put in the ledger - so the bytes and
 * the `durationSecs` on the episode row agree, and anything downstream
 * that divides one by the other gets the rate below back rather than a
 * number nobody chose. A test that wants a small take asks for a short
 * one; there is no scale factor to mistake for a measurement later.
 * ===================================================================== */

"use strict";

/* Three 1080p30 cameras at ~7 Mbps each - BACKEND-PLAN.md, "Video, and
   why it does not go straight to the cloud". That is the whole basis of
   the ~9 GB per rig-hour in its sizing table, and of the 245 TB the cold
   tier is provisioned for.

   It is an estimate, and the plan says so twice: the first real episode
   from RODA-RS replaces it, and a different codec or resolution moves
   every row. Which is exactly why it is one named constant here. */
const BYTES_PER_SECOND_PER_CAMERA = 7000000 / 8;   // 875,000

/* Bytes have to differ per episode and per camera, or a mixed-up camera
   key uploads identical content and every checksum agrees with the wrong
   one. Seeded from the key, so the same take always produces the same
   bytes: a retry after a reload re-reads the blob from the journal, and
   a mock that answered differently each call would look like corruption. */
function seedOf(key) {
  let h = 0x811c9dc5;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h || 1;
}

/* One block of xorshift32 noise, tiled to fill the take. Tiling rather
   than generating every byte keeps a 100 MB take cheap, and nothing here
   cares whether it compresses. */
function fillerBlock(seed) {
  const block = new Uint8Array(4096);
  let x = seed;
  for (let i = 0; i < block.length; i++) {
    x ^= (x << 13); x >>>= 0;
    x ^= (x >>> 17);
    x ^= (x << 5);  x >>>= 0;
    block[i] = x & 0xff;
  }
  return block;
}

function ascii(s) {
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i) & 0x7f;
  return out;
}

/* Build a recorder.
 *
 *   mockRoda()                       the plan's rate
 *   mockRoda({ bytesPerSecond: n })  a measured one, when there is one
 *
 * Returns the function `setVideoSource` wants. `stats()` on it reports
 * what it has produced, which is how a soak says what it pushed and how
 * a test catches a caller that forgot to pass the duration. */
function mockRoda(opts) {
  const o = opts || {};
  const rate = o.bytesPerSecond == null ? BYTES_PER_SECOND_PER_CAMERA
                                        : Number(o.bytesPerSecond);
  if (!isFinite(rate) || rate <= 0) throw new Error("bytesPerSecond must be positive");

  let takes = 0, bytes = 0, skipped = 0;

  function source(episodeId, camera, durationSecs) {
    const secs = Number(durationSecs);
    /* Null is the seam's own word for "nothing to send", and a take of
       no seconds recorded nothing. Returning it rather than throwing is
       deliberate: `queueVideo` calls this inside the pedal handler with
       no guard around it, and a recorder that can end an operator's take
       by raising is worse than one that quietly queues nothing. The
       count is how a test notices instead. */
    if (!episodeId || !camera || !isFinite(secs) || secs <= 0) { skipped++; return null; }

    const total = Math.round(rate * secs);
    if (total <= 0) { skipped++; return null; }

    const key = episodeId + "/" + camera;
    /* Readable at the front, so a file pulled out of the spool by hand
       says which take and which camera it belongs to without a lookup. */
    const head = ascii("MOCK-RODA " + key + " " + secs + "s " + total + "B\n");
    const out = new Uint8Array(total);
    const headLen = Math.min(head.length, total);
    out.set(head.subarray(0, headLen), 0);

    const block = fillerBlock(seedOf(key));
    for (let at = headLen; at < total; at += block.length) {
      out.set(block.subarray(0, Math.min(block.length, total - at)), at);
    }

    takes++; bytes += total;
    /* Not video/mp4. These bytes are not a container and saying they are
       would be the one lie in a file whose whole job is to be honestly
       fake. The real adapter supplies the real type with the real take. */
    return new Blob([out], { type: "application/octet-stream" });
  }

  source.bytesPerSecond = rate;
  source.stats = () => ({ takes: takes, bytes: bytes, skipped: skipped });
  return source;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { mockRoda: mockRoda, BYTES_PER_SECOND_PER_CAMERA: BYTES_PER_SECOND_PER_CAMERA };
}
if (typeof window !== "undefined") {
  window.mockRoda = mockRoda;
}
