# The kiosk shell

`BACKEND-PLAN.md` Phase 1, first item: *"Tauri project under
`apps/rig/desktop/`, hosting the existing page unchanged."*

This is that, and nothing else yet. It opens a full-screen window on the
rig page the floor is serving. The rig app itself is untouched — it is
still the browser page in `apps/rig/`, still runs in a plain tab, and
still has to.

## The one decision in here

**It loads a URL. It does not bundle the page.**

The obvious build is to pack `apps/rig/` into the binary and open
`index.html`. That is wrong, in the exact way this project has already
been burned once.

`index.html` loads `rig-config.js` by a relative path, and that file is
how a machine learns which rig it is. On a floor the kiosk loads the page
from the server, so the browser asks the *server* for that file, and the
service answers per caller from `RIG_ADDRESSES`. Bundle the page and the
webview reads the placeholder sitting next to it instead, which names
nobody — and the tempting fix is to write a real id into the bundled
copy, which is twelve machines with one identity again, one layer lower
and harder to see.

`BACKEND-PLAN.md` calls this "the one place where Tauri must **not**
help". So this shell holds no rig assets and does not know which rig it
is in front of. It cannot leak an identity it does not have.

## Configuration

| | |
|---|---|
| `RIG_URL` | where the floor serves the rig page, e.g. `http://floor.internal/apps/rig/` |
| `RIG_WINDOWED` | set to anything for a normal window instead of full screen — for working on the shell, never on a rig |

`RIG_URL` is the **same value on all twelve machines**. It is a
deployment fact, like the database URL, and Ansible sets it when it
provisions the host. It is *not* the rig's identity — nothing here
distinguishes RIG-03 from RIG-07. Anyone tempted to add `RIG_ID` beside
it should read the section above first.

Unset, blank or unparseable all land on `shell/index.html`, which says so
on screen. A kiosk has no terminal and nobody is watching stderr, so the
alternative is a blank white window — which looks exactly like a slow
network and gets power-cycled for an hour before anyone reads a log.

## Running it

Needs a Linux host. The floor runs Linux, and two of the three jobs this
shell exists for are Linux-shaped — see below.

```sh
# prerequisites, once
apt-get install -y build-essential pkg-config libwebkit2gtk-4.1-dev \
                   libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# serve the floor from the repo root, in another terminal
npm run serve

# then
cd apps/rig/desktop
RIG_URL=http://127.0.0.1:8765/apps/rig/ RIG_WINDOWED=1 cargo run
```

Under WSL the window renders through WSLg, which is enough to see the
page and drive the pedals from the keyboard.

## What is deliberately not here yet

One thing, and it is the one no amount of code settles:

- **the pedals as raw HID** — see below

The rest of Phase 1 is here now, and the order they arrived in is the order
they depended on each other:

- **the `Roda` trait and a Rust `MockRoda`** (`src/roda.rs`) — the six
  calls the rig makes to the teleop layer, written down
- **the disk journal** (`src/journal.rs`, `src/bridge.rs`) — the page
  writes to `/var/lib/rig/journal.ndjson` through Tauri's IPC, which
  needed the remote-domain capability wiring the earlier version of this
  README warned about
- **the uploader as its own process** (`src/upload.rs`,
  `src/bin/rig-uploader.rs`) — a second binary that reads the journal and
  never writes to it, so the shell stays the only writer and no locking
  is needed. `deploy/systemd/rig-uploader.service` is its unit, and it is
  the only unit in that directory that installs on a rig rather than on
  the server.

**The pedals are the part this cannot answer.** Today they are the keys
`1`/`2`/`3` and the webview handles them. On a rig they are a pedal board
read as raw HID below the browser, which is `evdev` — Linux only, and
untestable without the hardware. The shell builds and runs here; that
particular claim stays unproven until there is a rig.
