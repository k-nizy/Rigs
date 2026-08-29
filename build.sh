#!/usr/bin/env bash
# Rebuild both single-file distributions from the split sources.
# Run after touching anything in packages/ or apps/*/assets/.
set -euo pipefail
cd "$(dirname "$0")"

# Which interpreter, decided by running one rather than by finding one.
#
# `python3` is the right name everywhere CI runs, and on Windows it is a
# Microsoft Store stub that prints an advert and exits 49 without being
# Python at all. `command -v` finds that stub, so whether the name exists
# is not the question - whether it runs is.
#
# Getting this wrong is quiet, which is the reason for the care. `set -e`
# stops the script, the dists are simply not rebuilt, and the next commit
# ships a dist that no longer matches the source it was built from.
# Nothing on the machine says so; CI says so, one push later.
py=""
for candidate in python3 python; do
  if "$candidate" -c 'import sys; sys.exit(sys.version_info[0] != 3)' >/dev/null 2>&1; then
    py="$candidate"
    break
  fi
done
if [ -z "$py" ]; then
  echo "build.sh: no working python3 or python on PATH" >&2
  exit 1
fi

( cd rotation-desk-v1 && "$py" tools/make-single-file.py )
( cd apps/rig  && "$py" tools/make-single-file.py )
