# claude-cam (server)

The MCP server for [Claude Cam](https://github.com/ssjrocks/claude-cam). The Claude Cam Android
app streams its camera to this server and uploads its recordings, and Claude reads both through
MCP tools.

```bash
claude-cam stdio   # MCP on stdin/stdout; Claude Code starts this (the plugin does)
claude-cam serve   # always-on service; Claude Code connects to http://127.0.0.1:8777/mcp
```

## Tools

| Tool | What it does |
| --- | --- |
| `camera_status` | Phone, battery, stream, camera settings, video capabilities (incl. high-speed modes) |
| `camera_frames` | Live frames, or frames from the last ~90 s, or record a few seconds of the stream |
| `camera_snapshot` | Full-resolution photo, optional crop |
| `camera_wait_for_change` | Block until the picture changes and settles; before/after frames |
| `camera_control` | Torch, zoom, exposure, focus, stream fps/size, rotation |
| `camera_message` | Text on the phone screen, optionally waiting for Done |
| `camera_record_video` | Record an MP4 on the phone: high-speed clips (120/240 fps), start/stop recordings, or a video shared from the phone's camera app |
| `camera_video_frames` | Frame-by-frame analysis of a recording: per-frame brightness/change table, dark runs, update rate, dropped frames, active area, contact sheets, crops, exported stills |

Recordings are decoded with [PyAV](https://github.com/PyAV-Org/PyAV) for frame-by-frame analysis.

## Settings

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_CAM_PORT` | `8777` | Port for the phone, the API and `serve` mode's MCP endpoint |
| `CLAUDE_CAM_RECORDINGS` | `~/Videos/Claude Cam` (`~/Movies/…` on macOS) | Where recordings are saved |
| `CLAUDE_CAM_APK` | the repo's `dist/claude-cam.apk` | APK offered on the phone download page; without one it links to the latest GitHub release |

From the network, only the phone socket (`/ws/device`), uploads with a one-time token
(`/upload/<token>`), the landing page and the APK are reachable. Everything else answers on
loopback only. See the main README for setup.
