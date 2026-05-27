#!/bin/sh
# Flatpak launcher: the package lives in /app/lib/clippy.
export PYTHONPATH="/app/lib/clippy:${PYTHONPATH}"
exec python3 -m clippy "$@"
