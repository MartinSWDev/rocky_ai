#!/usr/bin/env bash
# Install Rocky launchd agents on the Mac.
#
#   scripts/mac/install_launchd.sh                  # TTS server (recommended baseline)
#   scripts/mac/install_launchd.sh --whisper        # also warm whisper.cpp server
#   scripts/mac/install_launchd.sh --client         # also auto-start the voice client
#   scripts/mac/install_launchd.sh --whisper --client
#   scripts/mac/install_launchd.sh --remove          # uninstall all Rocky agents
#
# Substitutes paths into the plist templates and loads them with launchctl.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$HOME/.rocky_say" "$HOME/.whisper"

install_one() {
    local label="$1" template="$2"
    local dest="$AGENTS/$label.plist"
    sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$REPO|g" "$template" > "$dest"
    launchctl unload "$dest" 2>/dev/null || true
    launchctl load "$dest"
    echo "Loaded $label  ($dest)"
}

remove_one() {
    local dest="$AGENTS/$1.plist"
    launchctl unload "$dest" 2>/dev/null || true
    rm -f "$dest"
    echo "Removed $1"
}

if [[ " $* " == *" --remove "* ]]; then
    remove_one com.rocky.tts
    remove_one com.rocky.whisper
    remove_one com.rocky.client
    exit 0
fi

install_one com.rocky.tts "$REPO/scripts/mac/com.rocky.tts.plist"
[[ " $* " == *" --whisper "* ]] && install_one com.rocky.whisper "$REPO/scripts/mac/com.rocky.whisper.plist"
[[ " $* " == *" --client "* ]]  && install_one com.rocky.client  "$REPO/scripts/mac/com.rocky.client.plist"

echo "Done. Check status with:  launchctl list | grep rocky"
