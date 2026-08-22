#!/usr/bin/env bash
# Serve the whole floor on http://127.0.0.1:8765
#   /                the landing page
#   /apps/desk/      the manager's screen
#   /apps/rig/       one rig's screen
set -euo pipefail
PORT="${1:-8765}"
cd "$(dirname "$0")"
echo "Floor -> http://127.0.0.1:${PORT}/    desk -> /apps/desk/    rig -> /apps/rig/"
exec python3 -m http.server "$PORT" --bind 127.0.0.1
