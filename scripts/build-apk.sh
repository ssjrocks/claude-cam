#!/usr/bin/env bash
# Build the Claude Cam Android app into dist/claude-cam.apk.
#
#   scripts/build-apk.sh             public build: the app finds the server by mDNS or a typed address
#   scripts/build-apk.sh --bake-ip   also bake this computer's LAN address in as the first guess
#
# Needs the Android SDK (ANDROID_HOME or android/local.properties) and JDK 17+. Release builds are
# signed with the key in android/keystore.properties if present, else with your debug key.
set -euo pipefail
cd "$(dirname "$0")/.."
SERVER=""
if [ "${1:-}" = "--bake-ip" ]; then
  IP="$(ip -4 route get 192.0.2.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
  SERVER="${IP}:${CLAUDE_CAM_PORT:-8777}"
fi
(cd android && ./gradlew --quiet :app:assembleRelease -Pclaudecam.server="$SERVER")
mkdir -p dist
cp android/app/build/outputs/apk/release/app-release.apk dist/claude-cam.apk
echo "Built dist/claude-cam.apk ($(du -h dist/claude-cam.apk | cut -f1))${SERVER:+, default server $SERVER}"
