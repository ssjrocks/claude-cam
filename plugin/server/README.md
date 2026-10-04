# claude-cam (server)

The MCP server for [Claude Cam](https://github.com/ssjrocks/claude-cam). The Claude Cam Android
app streams its camera to this server, and Claude reads it through MCP tools.

```bash
claude-cam stdio   # MCP on stdin/stdout; Claude Code starts this (the plugin does)
claude-cam serve   # always-on service; Claude Code connects to http://127.0.0.1:8777/mcp
```

Recordings are decoded with PyAV for frame-by-frame analysis. The phone connects to port 8777 (set `CLAUDE_CAM_PORT` to change it). See the main README for setup.
