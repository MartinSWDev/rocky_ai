#!/usr/bin/env bash
# Convenience launcher for the Rocky voice client on the Mac.
#
#   scripts/mac/start_rocky.sh           # text mode
#   scripts/mac/start_rocky.sh --voice   # wake-word voice mode
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

# Make sure the rocky_say TTS server is up (fast path). Harmless if already running.
if ! curl -s -o /dev/null "http://127.0.0.1:59720" 2>/dev/null; then
    echo "Starting rocky_say TTS server..."
    rocky_say --server start >/dev/null 2>&1 || true
fi

exec python3 rocky_client.py "$@"
