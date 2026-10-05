---
name: phone-camera
description: See the physical world through the user's phone camera (Claude Cam). Use when the result of your work shows up somewhere software can't read - hardware, microcontrollers (ESP32, Arduino, Raspberry Pi), ESPHome/IoT devices, LEDs, LCD/OLED/e-ink displays, 3D printers, robots and motors, TVs, monitors, kiosks, another device's screen, wiring, labels and serial numbers - or when the user would otherwise have to describe what they see. Covers checking whether the camera is available, asking the user to point their phone, verifying a command's visible effect, reading small text, watching for changes, recording high-speed clips of fast things (animations, GIFs, flicker, glitches) and examining them frame by frame, and recording demo videos. Always ask the user to open the app and wait for them to confirm before using the camera, and tell them straight away if a camera tool fails.
---

# Seeing the real world with Claude Cam

Claude Cam streams the user's phone camera to you through the `claude-cam` MCP server. The user
points their phone at a device, and you can look at it yourself while you work, instead of asking
"what does the screen say now?".

**The two rules that matter most:**
1. **Ask before you use the camera, then wait.** Say in chat what you want to look at and why, ask the
   user to open the app and point the phone, and end your turn until they say it's ready.
2. **Never fail silently.** If a camera tool fails, tell the user in your very next message. Don't
   keep retrying, and don't carry on as if you'd seen the result.

Details below.

Tools (they may be listed as deferred: load them first, e.g. search for "camera"):

| Tool | Use it to |
| --- | --- |
| `camera_status` | See whether a phone is connected, its state, and what it can record (fps, high-speed modes). Call this first |
| `camera_frames` | Get the newest live frame instantly, or several from the last N seconds (about 90 s of history), or wait and record. The live stream is only ~3-10 fps |
| `camera_snapshot` | Take a full-resolution photo; `crop` reads small text, and `use_last` re-crops it without a new photo |
| `camera_wait_for_change` | Block until the picture changes and settles, then return before/after frames |
| `camera_record_video` | Record an MP4: high-speed clips (120/240 fps) for fast things, or start/stop recordings for demos. `from_camera_app` asks the user to film with the phone's own camera app and share it in |
| `camera_video_frames` | Examine a recording frame by frame: per-frame statistics table, contact sheet or separate frames, crop, frame ranges, `save_dir` to export stills |
| `camera_control` | Set torch, zoom, exposure (negative for bright screens), focus point, stream fps/size, rotation |
| `camera_message` | Put text on the phone screen; with `wait_for_done_seconds` it waits for the user to tap Done |

## When to offer it

Offer it when the task has a visible result you can't check from software, especially when you'll
iterate: flashing firmware, changing a display layout, wiring LEDs, tuning a printer, testing a UI on
a physical device. One sentence is enough: "If you open Claude Cam and point your phone at the
board, I can check the LEDs myself after each flash."

Don't look when it isn't relevant to the task. The phone shows the user a "Claude is looking" badge
whenever you read the camera.

## Before you use the camera: tell the user and wait

The user can't see your tool calls as they happen. If you quietly try the camera and it isn't ready,
they only find out much later, by reading the transcript. So:

1. **Ask in chat first, then stop.** Before your first camera call in a task, tell the user what you
   want to look at and why, and ask them to open Claude Cam and point the phone at it. Ask them to
   prop it up if you'll watch for changes or record. Then **end your turn** and wait for their reply.
   Don't call camera tools meanwhile, and don't start the part of the work that needs the camera.
   For example:

   > I'd like to watch the LCD while I flash the firmware, so I can check it myself. Could you open
   > Claude Cam on your phone, prop it up facing the display, and tell me when its top bar says Connected?

2. **When they say it's ready, check once.** Call `camera_status`. If a phone is connected, take one
   look with `camera_frames` to confirm the device is in view and readable. If it isn't framed well,
   say so in chat, or use `camera_message` ("Move a bit closer to the screen", with
   `wait_for_done_seconds: 60`).
3. **If it isn't connected, tell them and wait again.** Quote what `camera_status` said, suggest the
   likely fix (the app is closed, a different Wi-Fi, Claude was restarted and the app hasn't reconnected
   yet), and end your turn. Don't poll `camera_status` in a loop.
4. **The only exception:** if the user has just said the phone is set up and pointed ("I've propped my
   phone up facing the board"), go straight to step 2.

Ask again (briefly) whenever you need the phone pointed somewhere new, or after a long gap.

## Never fail silently

- **If any camera tool fails,** stop and tell the user in your next message. Failures include no phone
  connected, the phone not answering, a stalled stream, a recording or upload error, or a timeout. Say
  what you were trying to do, what went wrong (quote the message), and what they can do about it.
  Then wait for them. At most one quick retry before telling them, never a loop.
- **Don't guess what the device showed.** If you couldn't see it, say "I couldn't check the display
  because the camera wasn't connected", not a guess presented as an observation.
- **Make every camera use visible in your reply.** Say briefly what you looked at or recorded, and what
  you saw ("I recorded 3 s at 240 fps; the LED blinks about every 0.5 s"). The user shouldn't need to
  read your tool calls to find out what happened.
- **Ending your turn to wait for the user is the right move** when the camera isn't ready. Carrying on
  without it, or retrying in silence, isn't.

## Rules: evidence before conclusions

These come from real mistakes. Follow them every time you judge what a device is doing.

1. **A contact sheet is a sample, not the video.** The sheet in a `camera_record_video` result shows 16
   frames spread over hundreds. A brief event (a line crossing the screen, one glitched frame) is
   usually *between* the sampled frames. Never conclude "black frames", "flicker", "missing frames",
   "frozen" or "it works" from a sheet. Examine the actual frames first (see the procedure below).
2. **Know what the content should look like before calling anything a fault.** Dark or empty frames
   are usually the content: a test pattern on a black background, an object that has left the screen,
   a GIF with dark frames. Ask the user what's playing, or look at the source (the GIF, the video, the
   firmware's draw code), before you interpret frames.
3. **Don't invent hardware explanations** such as backlight flicker, black-frame insertion, refresh
   scanning, a dying panel or a camera fault. Only raise one if the per-frame table shows a pattern the
   content can't explain, and even then ask the user before treating it as fact.
4. **The user can see the device.** If they say it looks fine, believe them and re-check how you
   handled the data.
5. **Say what you actually examined**, e.g. "I looked at every frame from #300 to #360, cropped to the
   screen". Then the user can see the gaps in your evidence, and so can you.
6. **Time it with the user.** If the thing to capture has to be started by the user or only runs for a
   while (a video, an animation, a boot), don't assume it's running. Call `camera_message` with
   `wait_for_done_seconds`, e.g. "Start the test video, then tap Done", and record as soon as it
   returns. Tell them what's about to happen (e.g. the preview freezes during a high-speed clip).
7. **The phone is a real device in the user's hands.** If it disconnects, the picture moves, or it goes
   blurry or dark, ask the user whether they did something (pressed home, picked the phone up, the
   screen locked) before you assume a bug. A frozen picture with a stall warning usually means the app
   went to the background.

## Fast things: record, then examine properly

The live stream (`camera_frames`, `camera_wait_for_change`) runs at only ~3-10 fps, so anything faster
than a few changes per second needs a recording.

### 1. Capture

- **Frame rate: at least 4x the content's update rate.** Check `camera_status` for the phone's
  high-speed rates.

  | Content | Record at | Camera frames per content frame |
  | --- | --- | --- |
  | 60 fps video or UI animation | 240 fps | 4 |
  | 24-30 fps GIF or video | 120-240 fps | 4-10 |
  | LED blinking 5-20 times/s | 120 fps | 6-24 |
  | Menus, boot screens, slow changes | 30 fps, or the live stream | - |

- **Length:** cover at least one full cycle of the content (a whole GIF loop, a complete pass of the
  moving object) plus a margin, typically 2-4 s. High-speed clips are capped at 15 s.
- **Setup:** ask the user to prop the phone up and start the content (rule 6). Then call
  `camera_record_video` with `fps: 240, seconds: 3` (and a `name`).
- If the phone has no high-speed mode, `camera_record_video` with `from_camera_app: true` asks the user to
  film in the phone's own Slow motion mode and share the clip in. If the result says it's stored slowed
  down, pass `slowdown`.

### 2. Orient with the text, not the images

Read the result's text report before looking at the sheet:

- **Measured frame rate:** did the camera deliver what you asked for? Dropped frames are listed.
- **"Most of the change happens in one area [x, y, w, h]":** use that region as `crop` from now on.
  Whole-frame numbers dilute a small screen to nothing.
- **Dark-frame runs, with frame numbers**, and the **picture-update rate**. These cover *every* frame,
  so they're the evidence for claims about the whole clip. The sheet isn't.
- **A WARNING that the picture changes in nearly every frame** means the recording is too slow for the
  content. Re-record faster; don't interpret those frames.

### 3. Narrow down: every frame, cropped, with the table

Call `camera_video_frames` on the interesting span:

```
camera_video_frames(
  video="<file from the result>",
  start_frame=<event - 20>, end_frame=<event + 40>,   # about 60 frames around a dark run, update or event
  step=1,                                             # every frame; up to 64 fit on one sheet
  crop=[x, y, w, h],                                  # the active area or the screen
  table=true,                                         # brightness and change% for each frame
)
```

- For a span longer than 64 frames, first use `step` of about span/48 to find where things happen, then
  zoom into that part with `step=1`.
- **Read the table:** a spike in `change%` is the content updating. Low brightness with ~0% change is a
  static dark picture, which is content, not a glitch. A regular brightness pattern that the content
  can't explain is the only thing that might be the display itself (rule 3).
- To read fine detail in particular frames: `layout="separate"`, `max_size=1200-1600`, a tight `crop`,
  up to 8 frames.
- Before claiming anything about the whole clip ("never goes black", "no dropped frames"), run the stats
  over the full range (no start/end). They cover every frame.

### 4. Share the evidence

Give the user frames to check themselves: `save_dir="~/Videos/Claude Cam/<clip>_frames"` on
`camera_video_frames`. To export *every* frame, run
`ffmpeg -i <clip>.mp4 -fps_mode passthrough -start_number 0 <dir>/frame_%04d.jpg`. The
`passthrough` setting keeps file numbers equal to frame numbers; without it ffmpeg duplicates frames to
fill timing gaps. Tell the user the folder and the frame numbers you're talking about.

## Other patterns

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
- **Demo or documentation videos.** Call `camera_record_video` (30 fps, no `seconds`) to start, do the
  work, then call it with `stop: true`. Export stills for docs with `camera_video_frames` `save_dir`.
  Videos are saved in `~/Videos/Claude Cam` (`~/Movies` on macOS).
- **Keep the cost down.** Images cost tokens. Use `max_size` around 512-800 for routine checks, and full
  size or crops only when you need detail. But when you're diagnosing, examining enough frames matters
  more than tokens.

## If the tools aren't available

- Ask whether the user has Claude Cam. Setup: https://github.com/ssjrocks/claude-cam (install the
  plugin, install the Android app, same Wi-Fi as the computer).
- If the server runs as a service, the HTTP API works without MCP:
  `curl -s http://127.0.0.1:8777/api/status`, and
  `curl -s "http://127.0.0.1:8777/api/frame.jpg?max=1024" -o /tmp/cam.jpg` followed by reading the
  image file. `/api/photo.jpg?crop=x,y,w,h` takes a photo.
