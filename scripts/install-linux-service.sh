#!/usr/bin/env bash
# Linux only, optional: run Claude Cam as an always-on systemd user service instead of letting
# Claude Code start it, and register it with Claude Code as an HTTP MCP server. Useful if you
# want the phone connected (and the live view at http://127.0.0.1:8777/live) without Claude open.
# Don't combine with the plugin: use one or the other.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
PORT="${CLAUDE_CAM_PORT:-8777}"
VENV="$DIR/plugin/server/.venv"

echo "== Python environment"
if command -v uv >/dev/null; then
  uv venv -q --allow-existing "$VENV"
  uv pip install -q -p "$VENV/bin/python" -e plugin/server
else
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -e plugin/server
fi

echo "== systemd user service"
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/claude-cam.service <<UNIT
[Unit]
Description=Claude Cam server (phone camera bridge for Claude)
After=network-online.target

[Service]
ExecStart=$VENV/bin/claude-cam serve --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now claude-cam.service
systemctl --user restart claude-cam.service

echo "== Claude Code MCP registration"
if command -v claude >/dev/null; then
  claude mcp get claude-cam >/dev/null 2>&1 || claude mcp add --transport http --scope user claude-cam "http://127.0.0.1:$PORT/mcp"
else
  echo "claude CLI not found; register by hand: claude mcp add --transport http --scope user claude-cam http://127.0.0.1:$PORT/mcp"
fi
echo "Done. Live view: http://127.0.0.1:$PORT/live"
