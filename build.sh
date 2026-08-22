#!/usr/bin/env bash
# Rebuild both single-file distributions from the split sources.
# Run after touching anything in packages/ or apps/*/assets/.
set -euo pipefail
cd "$(dirname "$0")"
( cd apps/desk && python3 tools/make-single-file.py )
( cd apps/rig  && python3 tools/make-single-file.py )
