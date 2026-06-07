#!/usr/bin/env bash
# Run whisper.cpp in server mode so the STT model stays warm in memory.
# Then set voice.whisper_server_url in config.yaml to http://127.0.0.1:<port>/inference
# and the client will POST audio instead of cold-loading the model each time.
#
#   scripts/mac/start_whisper_server.sh [model_path] [port]
#
# Needs the whisper-server binary (brew install whisper-cpp, or build whisper.cpp).
# Download a model first, e.g.:
#   curl -L -o ~/.whisper/ggml-small.en.bin \
#     https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
set -euo pipefail

MODEL="${1:-$HOME/.whisper/ggml-small.en.bin}"
PORT="${2:-8910}"

BIN="$(command -v whisper-server || true)"
if [ -z "$BIN" ]; then
    echo "whisper-server not found on PATH."
    echo "Install with: brew install whisper-cpp   (or build whisper.cpp's server example)"
    exit 1
fi
if [ ! -f "$MODEL" ]; then
    echo "Model not found: $MODEL  (see the download command in this script's header)"
    exit 1
fi

echo "Starting whisper-server on 127.0.0.1:$PORT with $(basename "$MODEL")"
exec "$BIN" -m "$MODEL" --host 127.0.0.1 --port "$PORT"
