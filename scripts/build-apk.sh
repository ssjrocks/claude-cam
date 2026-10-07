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
# Extra Gradle properties, e.g. GRADLE_EXTRA="-Pclaudecam.updateApi=http://10.0.2.2:8790/latest" for testing.
(cd android && ./gradlew --quiet :app:assembleRelease -Pclaudecam.server="$SERVER" ${GRADLE_EXTRA:-})
mkdir -p dist
cp android/app/build/outputs/apk/release/app-release.apk dist/claude-cam.apk

# The version file each release carries, so the in-app updater knows which app version is attached.
SDK="${ANDROID_HOME:-$(sed -n 's/^sdk.dir=//p' android/local.properties)}"
AAPT="$(ls -d "$SDK"/build-tools/*/ | sort -V | tail -1)aapt"
BADGING="$("$AAPT" dump badging dist/claude-cam.apk | head -1)"
CODE="$(echo "$BADGING" | sed -n "s/.*versionCode='\([0-9]*\)'.*/\1/p")"
NAME="$(echo "$BADGING" | sed -n "s/.*versionName='\([^']*\)'.*/\1/p")"
SHA="$(sha256sum dist/claude-cam.apk | cut -d' ' -f1)"
printf '{\n  "versionCode": %s,\n  "versionName": "%s",\n  "sha256": "%s",\n  "size": %s\n}\n' \
  "$CODE" "$NAME" "$SHA" "$(stat -c %s dist/claude-cam.apk)" > dist/claude-cam.json
echo "Built dist/claude-cam.apk ($(du -h dist/claude-cam.apk | cut -f1), app $NAME / $CODE)${SERVER:+, default server $SERVER}"
