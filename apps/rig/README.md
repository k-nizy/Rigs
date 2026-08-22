# Rig 03 — Pedal Loop

The whole operator app for one teleop rig: a single full-screen page, three foot
pedals along the bottom, no login and no task picker. The schedule decides who is
at the rig, so there is nothing left for the operator to choose — only to do.

Built to be read from two metres away by someone holding two robot arms: white
wall, black type, the same on every screen. Nothing on the wall blinks — the
handover and fault states are carried by the words and the pedals, not by a
flashing colour. No dark theme, on purpose.

## Run it

No build step, no dependencies. Serve the folder over HTTP:

```sh
python3 -m http.server 8731 --bind 127.0.0.1
# then open http://127.0.0.1:8731/
```

`file://` also works, but a server is better — the web manifest and the
`#screen` deep links behave properly.

## Deploy it

Static files only. Copy the folder to any static host (Netlify, Vercel, S3 +
CloudFront, nginx, GitHub Pages). Nothing needs to run server-side.

## Layout

```
index.html                    markup: rail, stage, pedals, demo drawer
assets/rig.css                the whole visual system
assets/rig.js                 schedule clock, screen state machine, pedal map, event log
manifest.webmanifest          installs full-screen landscape, for the rig display
favicon.ico                   16/32/48, so a bare /favicon.ico probe never 404s
assets/icon.svg               vector favicon, flips ink on prefers-color-scheme
assets/icon-{light,dark}-{16,32}.png   raster fallback, one per theme
assets/icon-{192,512}.png     installed-app icons
assets/icon-maskable-512.png  same mark inside a launcher's crop-safe zone
assets/apple-touch-icon.png   180x180, opaque — iOS does not do transparency
tools/make-icons.py           regenerates every icon above from one definition
```

## The icon

The mark is the app reduced to its own structure: three pedals on the rail
that runs along the bottom of every screen, middle pedal in the signal
colour because middle is "go" everywhere except during a take.

It is drawn on a 32-unit grid so edges land on whole pixels at 16px and
32px. Proportions were set by the 16px case, not by how it looks large — an
earlier pass with a thinner rail turned into a grey smear at favicon size.

Two themes: `icon.svg` carries a `prefers-color-scheme` rule and flips the
ink from near-black to near-white, so it reads on a dark and a light tab
strip; the PNG fallbacks ship as a light pair and a dark pair, selected by
`media` on the `<link>`. The installed-app icons are a fixed white plate —
a web app manifest has no theme switching, so a plate that sits correctly
on both dark and light launchers is the right answer there.

Regenerate after any change:

```sh
python3 tools/make-icons.py
```

Everything is derived from the geometry constants at the top of that script,
so the vector and the rasters cannot drift apart. Setting `ACCENT` to the
ink value gives a monochrome mark.

## Pedals

| Key | Pedal  |
| --- | ------ |
| `1` | Left   |
| `2` | Middle |
| `3` | Right — hold where the screen says hold |

Middle is "go" on every screen except **Recording**, where it is deliberately
inert so muscle memory cannot end a good take. "Other" is always the right
pedal, so the issue tree is one rule instead of three menus.

## Demo controls

`D` or `?` opens the drawer: jump straight to any of the nine screens, change the
clock speed, and watch the events the rig would send the backend.

| Key | Does |
| --- | ---- |
| `H` | Jump to the next handover |
| `E` | Skip the efficiency warm-up so the floor goes live |
| `R` | Restart the shift |

Every screen is addressable by hash — `#recording`, `#rig-down`, `#handover`,
`#review`, `#resetting`, `#issue-menu`, `#fault-class`, `#fault-fixing`,
`#checklist` — so reviewing the app with someone means jumping to the screen you
want to argue about, not waiting forty-five minutes for a handover.

## The clock

Every duration in `rig.js` is in *shift seconds*. A block is 15 minutes, a stint
at one rig is three blocks. The demo runs that clock at 30× so a 45-minute stint
is watchable; on a real rig `speed` is `1`.

Efficiency is data time over the time the operator has held the rig. Reset,
review and idle land in the denominator — that is the point of the number. Fault
and downtime do not: a loose mount is not the operator's productivity problem.

## Not wired up

The drawer's event log is where a backend would go. Each `emit()` names the
bucket the event belongs in — `episodes`, `rig_shift_checks`,
`rig_downtime_events`, `rig_productivity_blocks`, `sessions`. Nothing is
persisted; a reload starts a fresh shift.
