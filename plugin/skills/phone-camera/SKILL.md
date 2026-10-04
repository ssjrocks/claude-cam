---
name: phone-camera
description: See the physical world through the user's phone camera (Claude Cam). Use when the result of your work shows up somewhere software can't read - hardware, microcontrollers (ESP32, Arduino, Raspberry Pi), ESPHome/IoT devices, LEDs, LCD/OLED/e-ink displays, 3D printers, robots and motors, TVs, monitors, kiosks, another device's screen, wiring, labels and serial numbers - or when the user would otherwise have to describe what they see. Covers checking whether the camera is available, asking the user to point their phone, verifying a command's visible effect, reading small text, and watching for changes.
---

# Seeing the real world with Claude Cam

Claude Cam streams the user's phone camera to you through the `claude-cam` MCP server. The user
points their phone at a device, and you can look at it yourself while you work, instead of asking
"what does the screen say now?".

Tools (they may be listed as deferred: load them first, e.g. search for "camera"):

| Tool | Use it to |
| --- | --- |
| `camera_status` | See whether a phone is connected and its state. Call this first |
| `camera_frames` | Get the newest live frame instantly, or several from the last N seconds (about 90 s of history), or wait and record |
| `camera_snapshot` | Take a full-resolution photo; `crop` reads small text, and `use_last` re-crops it without a new photo |
| `camera_wait_for_change` | Block until the picture changes and settles, then return before/after frames |
| `camera_control` | Set torch, zoom, exposure (negative for bright screens), focus point, stream fps/size, rotation |
| `camera_message` | Put text on the phone screen; with `wait_for_done_seconds` it waits for the user to tap Done |

## When to offer it

Offer it when the task has a visible result you can't check from software, especially when you'll
iterate: flashing firmware, changing a display layout, wiring LEDs, tuning a printer, testing a UI on
a physical device. One sentence is enough: "If you open Claude Cam and point your phone at the
board, I can check the LEDs myself after each flash."

Don't look when it isn't relevant to the task. The phone shows the user a "Claude is looking" badge
whenever you read the camera.

## Getting started

1. Call `camera_status`.
2. If no phone is connected, ask the user in chat to open the Claude Cam app and point it at the
   device. (You can't reach the phone until the app is open.) Suggest propping it up if you'll be
   watching for changes.
3. Call `camera_frames` to confirm the device is in view and readable. If it isn't, guide the user with
   `camera_message`, e.g. "Move a bit closer to the screen", with `wait_for_done_seconds: 60`.

## Patterns that work

- **Check what a command did.** Take a timestamp, run the command, then wait for the change:
  run `date +%s.%N && <command>`, then call `camera_wait_for_change` with `baseline_at` set to that
  timestamp. The baseline means a change that already happened is still caught. On timeout it tells you
  the largest change it saw and returns the current frame.
- **Small LEDs or one part of a screen.** Pass `region` as `[x, y, w, h]` fractions of the frame (read
  them off a frame you've already seen), with `sensitivity: "high"` for a single small LED.
- **Small text.** Use `camera_snapshot` with `crop` around the text. A full photo is usually ~4000x3000,
  so crops stay sharp. Re-crop with `use_last: true`.
- **Glowing screens look washed out.** Set `camera_control` `exposure` negative (see `camera_status`
  for the range). If focus hunts, focus on a point: `focus: [x, y]`.
- **Something that unfolds over time (a boot sequence, an animation).** Use `camera_frames` with
  `wait_seconds: 10, count: 5`.
- **Keep the cost down.** Images cost tokens. Use `max_size` around 512-800 for routine checks, and full
  size or crops only when you need detail.

## When something looks wrong

The phone is a real device in the user's hands. If it disconnects, the picture moves, or it goes
blurry or dark, **ask the user whether they did something** (pressed home, picked the phone up, the
screen locked) before you assume a bug or start debugging. Most surprises are a person, not a fault.

A frozen picture with a stall warning usually means the app went to the background. Ask the user to
reopen it.

## If the tools aren't available

- Ask whether the user has Claude Cam. Setup: https://github.com/ssjrocks/claude-cam (install the
  plugin, install the Android app, same Wi-Fi as the computer).
- If the server runs as a service, the HTTP API works without MCP:
  `curl -s http://127.0.0.1:8777/api/status`, and
  `curl -s "http://127.0.0.1:8777/api/frame.jpg?max=1024" -o /tmp/cam.jpg` followed by reading the
  image file. `/api/photo.jpg?crop=x,y,w,h` takes a photo.
