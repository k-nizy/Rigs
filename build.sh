#!/usr/bin/env bash
# Rebuild both single-file distributions from the split sources.
# Run after touching anything in shared/, desk/assets/ or rig/assets/.
set -euo pipefail
cd "$(dirname "$0")"
( cd desk && python3 tools/make-single-file.py )
( cd rig  && python3 tools/make-single-file.py )
