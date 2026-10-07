#!/usr/bin/env python3
"""Claude Cam server: a bridge between the Claude Cam phone app and Claude.

The phone app connects to ws://<this host>:8777/ws/device and streams JPEG frames from its
camera. Claude reads them through MCP: either Claude Code starts `claude-cam stdio` itself
(see stdio.py), or `claude-cam serve` runs as a service and Claude Code talks to
http://127.0.0.1:8777/mcp (streamable HTTP). There is also a plain HTTP API under /api/.

Only the phone socket, the landing page and the APK download are reachable from the LAN.
Everything that exposes camera images or controls the phone is loopback-only.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import signal
import socket
import struct
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web
from PIL import Image, ImageFilter, ImageOps

from . import __version__, video
from .common import (
    SENSITIVITY,
    CamError,
    changed_fraction,
    clamp,
    fmt_ts,
    image_block,
    jpeg_bytes,
    parse_region,
    pick_evenly,
    region_box,
    rotated,
    text_block,
)

VERSION = __version__
SERVICE_TYPE = "_claudecam._tcp.local."
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC = PACKAGE_DIR / "static"
DEFAULT_PORT = int(os.environ.get("CLAUDE_CAM_PORT") or 8777)
# Offer a locally built APK when there is one (a repo checkout's dist/); otherwise send the
# phone to the latest GitHub release.
APK_PATH = Path(os.environ.get("CLAUDE_CAM_APK") or PACKAGE_DIR.parents[2] / "dist" / "claude-cam.apk")
RELEASE_APK_URL = "https://github.com/ssjrocks/claude-cam/releases/latest/download/claude-cam.apk"

log = logging.getLogger("claudecam")


# ---------------------------------------------------------------------------------------------
# Frames and image helpers
# ---------------------------------------------------------------------------------------------


@dataclass
class Frame:
    ts: float  # unix time the image was captured (server clock)
    jpeg: bytes
    width: int
    height: int
    kind: str  # "stream" or "photo"
    thumb: Image.Image | None = None  # small blurred greyscale copy for change detection


def make_thumb(jpeg: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(jpeg))
    im.draft("L", (max(1, im.width // 4), max(1, im.height // 4)))
    im = im.convert("L")
    im.thumbnail((320, 320))
    return im.filter(ImageFilter.GaussianBlur(1.5))


def render(frame: Frame, *, max_size: int, crop=None, rotate: int = 0, quality: int = 80) -> tuple[bytes, int, int]:
    """Apply EXIF orientation, extra rotation, crop and downscale. Returns (jpeg, width, height)."""
    im = Image.open(io.BytesIO(frame.jpeg))
    orientation = im.getexif().get(0x0112, 1)
    if orientation == 1 and not rotate and not crop and max(im.size) <= max_size:
        return frame.jpeg, im.width, im.height
    if not crop:
        im.draft("RGB", (max_size, max_size))  # fast JPEG downscale while decoding
    im = ImageOps.exif_transpose(im)
    if rotate:
        im = im.rotate(-rotate, expand=True)
    if crop:
        im = im.crop(region_box(crop, im.size))
    if max(im.size) > max_size:
        im.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=quality)
    return out.getvalue(), im.width, im.height


# ---------------------------------------------------------------------------------------------
# Hub: the connected phone, the frame buffer and request/response plumbing
# ---------------------------------------------------------------------------------------------


@dataclass
class Recording:
    token: str
    name: str
    fps: int
    started_at: float
    done: asyncio.Future  # resolves to the saved Path once the phone has uploaded the file
    info: dict = field(default_factory=dict)  # what the phone reported when it started
    stopped_by: str | None = None
    stop_timer: asyncio.TimerHandle | None = None


TELL_USER = (
    " Tell the user about this in your reply now, and wait for them; don't keep retrying "
    "or carry on as if you'd seen the result."
)


class Device:
    def __init__(self, ws: web.WebSocketResponse, hello: dict, remote: str):
        self.ws = ws
        self.hello = hello
        self.remote = remote
        self.status: dict = {}
        self.connected_at = time.time()
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return f"{self.hello.get('manufacturer', '')} {self.hello.get('model', 'phone')}".strip()

    async def send(self, msg: dict) -> None:
        async with self._lock:
            await self.ws.send_str(json.dumps(msg))


class Hub:
    def __init__(self, buffer_seconds: float, base_fps: float, boost_fps: float, lan_url: str):
        self.device: Device | None = None
        self.last_device: tuple[str, float] | None = None  # (name, disconnected at)
        self.frames: deque[Frame] = deque()
        self.buffer_seconds = buffer_seconds
        self.cond = asyncio.Condition()
        self.pending: dict[str, asyncio.Future] = {}
        self.acks: dict[str, asyncio.Future] = {}
        self.message: dict | None = None  # message currently shown on the phone
        self.last_photo: Frame | None = None
        self.rotate = 0
        self.base_fps = base_fps
        self.boost_fps = boost_fps
        self.boosts = 0
        self.stream_size = 1920  # phones pick the closest size at or below, e.g. 1440x1080
        self.quality = 70
        self.lan_url = lan_url
        self.recording: Recording | None = None
        self.imports: dict[str, str] = {}  # upload token -> file name, for videos shared from the phone
        self.import_waiters: list[asyncio.Future] = []
        # Uploads the server asked for (photos/videos the user took in the app): token -> (path, future)
        self.expected: dict[str, tuple[Path, asyncio.Future]] = {}

    # --- connection ---------------------------------------------------------------------------

    def not_connected_text(self) -> str:
        text = (
            "No phone is connected to Claude Cam. Stop and tell the user in your reply: ask them to open "
            f"the Claude Cam app on their phone (on the same Wi-Fi as this PC; it finds {self.lan_url} by "
            "itself) and point it at what you need to see, then end your turn and wait for them to say it's "
            "connected. Don't poll for it."
        )
        if self.last_device:
            name, at = self.last_device
            text += f" (Last phone: {name}, disconnected {time.time() - at:.0f} s ago.)"
        return text

    def require_device(self) -> Device:
        if not self.device:
            raise CamError(self.not_connected_text())
        return self.device

    async def attach(self, dev: Device) -> None:
        old, self.device = self.device, dev
        log.info("phone connected: %s from %s (app %s)", dev.name, dev.remote, dev.hello.get("app_version"))
        if old:
            await old.ws.close(code=4000, message=b"replaced by a newer connection")
        await dev.send(self.config_msg())
        if self.message:
            await dev.send(self.message)

    async def detach(self, dev: Device) -> None:
        if self.device is not dev:
            return
        self.device = None
        self.last_device = (dev.name, time.time())
        log.info("phone disconnected: %s", dev.name)
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(CamError("The phone disconnected before it answered." + TELL_USER))

    def current_fps(self) -> float:
        return self.boost_fps if self.boosts else self.base_fps

    def config_msg(self) -> dict:
        return {"type": "config", "fps": self.current_fps(), "quality": self.quality, "size": self.stream_size}

    async def safe_send(self, msg: dict) -> None:
        if self.device:
            try:
                await self.device.send(msg)
            except (ConnectionError, RuntimeError) as e:
                log.debug("send failed: %s", e)

    async def push_config(self) -> None:
        await self.safe_send(self.config_msg())

    @asynccontextmanager
    async def boosted(self):
        """Raise the phone's stream rate while Claude is actively watching."""
        self.boosts += 1
        if self.boosts == 1:
            await self.push_config()
        try:
            yield
        finally:
            self.boosts -= 1
            if self.boosts == 0:
                await self.push_config()

    async def activity(self, what: str, seconds: float = 0) -> None:
        """Tell the phone Claude is looking, so the user sees a badge."""
        await self.safe_send({"type": "activity", "what": what, "seconds": seconds})

    async def request(self, msg: dict, timeout: float):
        dev = self.require_device()
        rid = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await dev.send({**msg, "req": rid})
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise CamError(f"The phone did not answer within {timeout:g} s." + TELL_USER) from None
        finally:
            self.pending.pop(rid, None)

    def resolve(self, rid: str | None, value=None, error: str | None = None) -> None:
        fut = self.pending.get(rid or "")
        if fut and not fut.done():
            if error:
                fut.set_exception(CamError(f"The phone reported an error: {error}." + TELL_USER))
            else:
                fut.set_result(value)

    # --- frames -------------------------------------------------------------------------------

    async def add_frame(self, header: dict, jpeg: bytes) -> None:
        now = time.time()
        ts = now - max(0.0, float(header.get("age_ms", 0))) / 1000
        try:
            with Image.open(io.BytesIO(jpeg)) as im:
                w, h = im.size
                if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                    w, h = h, w
        except Exception as e:  # noqa: BLE001 - a corrupt frame should never kill the socket
            log.warning("dropping unreadable %s frame: %s", header.get("kind"), e)
            self.resolve(header.get("req"), error="the photo could not be decoded")
            return
        if header.get("kind") == "photo":
            frame = Frame(ts, jpeg, w, h, "photo")
            self.last_photo = frame
            self.resolve(header.get("req"), frame)
            return
        frame = Frame(ts, jpeg, w, h, "stream", make_thumb(jpeg))
        async with self.cond:
            self.frames.append(frame)
            cutoff = now - self.buffer_seconds
            while self.frames and self.frames[0].ts < cutoff:
                self.frames.popleft()
            self.cond.notify_all()

    def latest(self) -> Frame | None:
        return self.frames[-1] if self.frames else None

    def frame_at(self, ts: float) -> Frame | None:
        """Newest frame captured at or before ts."""
        for f in reversed(self.frames):
            if f.ts <= ts:
                return f
        return None

    def _first_after(self, ts: float) -> Frame | None:
        found = None
        for f in reversed(self.frames):
            if f.ts <= ts:
                break
            found = f
        return found

    async def wait_frame_after(self, ts: float, timeout: float) -> Frame | None:
        """Oldest buffered frame newer than ts, waiting up to timeout for one to arrive."""
        async with self.cond:
            try:
                await asyncio.wait_for(self.cond.wait_for(lambda: self.latest() and self.latest().ts > ts), timeout)
            except TimeoutError:
                return None
            return self._first_after(ts)

    def measured_fps(self) -> float:
        now = time.time()
        recent = [f for f in self.frames if f.ts > now - 3]
        if len(recent) < 2:
            return 0.0
        return (len(recent) - 1) / max(0.001, recent[-1].ts - recent[0].ts)

    def status_dict(self) -> dict:
        now = time.time()
        latest = self.latest()
        d = {
            "server_version": VERSION,
            "time": now,
            "connected": self.device is not None,
            "lan_url": self.lan_url,
            "frames_buffered": len(self.frames),
            "buffer_seconds": self.buffer_seconds,
            "last_frame_age": round(now - latest.ts, 2) if latest else None,
            "stream_resolution": [latest.width, latest.height] if latest else None,
            "fps_target": self.current_fps(),
            "fps_measured": round(self.measured_fps(), 1),
            "rotate": self.rotate,
            "message": self.message["text"] if self.message else None,
        }
        if self.device:
            dev = self.device
            d["device"] = {
                "name": dev.name,
                "remote": dev.remote,
                "connected_for": round(now - dev.connected_at),
                "app_version": dev.hello.get("app_version"),
                "android": dev.hello.get("android"),
                **dev.status,
            }
        elif self.last_device:
            d["last_device"] = {"name": self.last_device[0], "disconnected_ago": round(now - self.last_device[1])}
        return d

    # --- actions shared by MCP tools and the HTTP API -----------------------------------------

    async def take_photo(self, use_last: bool = False) -> Frame:
        if use_last:
            if not self.last_photo:
                raise CamError("There is no earlier photo to reuse; call again without use_last.")
            return self.last_photo
        self.require_device()
        await self.activity("photo")
        return await self.request({"type": "photo"}, timeout=20)

    async def control(self, args: dict) -> dict:
        config_changed = False
        if args.get("rotate") is not None:
            r = int(args["rotate"]) % 360
            if r % 90:
                raise CamError("rotate must be 0, 90, 180 or 270.")
            self.rotate = r
        if args.get("fps") is not None:
            self.base_fps = clamp(float(args["fps"]), 0.5, 15)
            config_changed = True
        if args.get("stream_size") is not None:
            self.stream_size = int(clamp(int(args["stream_size"]), 320, 1920))
            config_changed = True
        phone_args = {k: args[k] for k in ("torch", "zoom", "exposure", "focus") if args.get(k) is not None}
        if "focus" in phone_args:
            focus = phone_args["focus"]
            if isinstance(focus, str):
                focus = focus.strip().lower()
                if focus != "auto":
                    try:
                        focus = [float(v) for v in focus.split(",")]
                    except ValueError:
                        raise CamError('focus must be "auto" or [x, y] fractions of the image.') from None
            if focus != "auto" and not (
                isinstance(focus, (list, tuple)) and len(focus) == 2 and all(0 <= float(v) <= 1 for v in focus)
            ):
                raise CamError('focus must be "auto" or [x, y] fractions of the image (0-1).')
            phone_args["focus"] = focus
        result = {}
        if phone_args:
            result = await self.request({"type": "control", **phone_args}, timeout=8)
            if self.device and isinstance(result.get("state"), dict):
                self.device.status.update(result["state"])
        if config_changed:
            await self.push_config()
        return result

    async def show_message(self, text: str, wait: float, vibrate: bool) -> str:
        dev = self.require_device()
        if not text:
            self.message = None
            await dev.send({"type": "clear_message"})
            return "Cleared the message on the phone."
        mid = uuid.uuid4().hex[:10]
        msg = {"type": "message", "id": mid, "text": text, "ack": wait > 0, "vibrate": vibrate}
        self.message = msg
        await dev.send(msg)
        if wait <= 0:
            return "Message shown on the phone screen."
        fut = asyncio.get_running_loop().create_future()
        self.acks[mid] = fut
        t0 = time.time()
        try:
            await asyncio.wait_for(fut, wait)
            return f"The user tapped Done after {time.time() - t0:.0f} s."
        except TimeoutError:
            return f"The user did not tap Done within {wait:g} s; the message is still showing on the phone."
        finally:
            self.acks.pop(mid, None)

    # --- video recording ------------------------------------------------------------------------

    async def start_recording(self, fps: int, quality: str, limit: float, name: str) -> Recording:
        self.require_device()
        if self.recording and not self.recording.done.done():
            raise CamError("A recording is already running. Stop it first with camera_record_video stop=true.")
        loop = asyncio.get_running_loop()
        rec = Recording(uuid.uuid4().hex, name, fps, time.time(), loop.create_future())
        self.recording = rec
        try:
            rec.info = await self.request(
                {"type": "record_start", "token": rec.token, "fps": fps, "quality": quality, "max_seconds": limit},
                timeout=20,
            )
        except CamError:
            self.recording = None
            raise
        rec.started_at = time.time()
        rec.stop_timer = loop.call_later(limit, lambda: asyncio.ensure_future(self._auto_stop(rec, "the time limit")))
        return rec

    async def _auto_stop(self, rec: Recording, why: str) -> None:
        if self.recording is rec and rec.stopped_by is None and not rec.done.done():
            rec.stopped_by = why
            await self.safe_send({"type": "record_stop", "token": rec.token})

    async def stop_recording(self, wait: float = 900) -> Recording:
        rec = self.recording
        if rec is None:
            raise CamError("Nothing is being recorded. Start a recording with camera_record_video.")
        if rec.stopped_by is None and not rec.done.done():
            rec.stopped_by = "Claude"
            if rec.stop_timer:
                rec.stop_timer.cancel()
            self.require_device()
            await self.request({"type": "record_stop", "token": rec.token}, timeout=15)
        try:
            await asyncio.wait_for(asyncio.shield(rec.done), wait)
        except TimeoutError:
            raise CamError(
                "The phone stopped recording but the video hasn't arrived yet (big files take a while on Wi-Fi). "
                "Call camera_record_video with stop=true again to keep waiting."
            ) from None
        return rec

    def on_record_event(self, data: dict) -> None:
        rec = self.recording
        if rec is None or data.get("token") != rec.token or rec.done.done():
            return
        if data.get("type") == "record_stopped" and rec.stopped_by is None:
            rec.stopped_by = data.get("by") or "the phone"
            if rec.stop_timer:
                rec.stop_timer.cancel()
        elif data.get("type") == "record_error":
            rec.done.set_exception(CamError(f"The phone couldn't record or send the video: {data.get('error')}." + TELL_USER))

    def offer_import(self, data: dict) -> str:
        """The user shared a video to Claude Cam; hand the phone a one-time upload token."""
        token = uuid.uuid4().hex
        self.imports[token] = str(data.get("name") or "video.mp4")
        log.info("phone is sending a shared video: %s (%s bytes)", self.imports[token], data.get("size"))
        return token

    # --- photos and videos the user took in the app --------------------------------------------

    async def list_captures(self) -> list[dict]:
        """What the app is holding for Claude, newest first. The app deletes each one once it's sent."""
        result = await self.request({"type": "captures_list"}, timeout=15)
        return list(result.get("captures") or [])

    async def fetch_capture(self, cap: dict) -> Path:
        """Have the phone upload one capture, and return where it was saved."""
        self.require_device()
        kind = cap.get("kind")
        taken = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime((cap.get("taken_at") or 0) / 1000 or time.time()))
        if kind == "photo":
            path = video.new_photo_path(f"phone-photo-{taken}")
        else:
            path = video.new_recording_path(f"phone-video-{taken}")
        token = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.expected[token] = (path, fut)
        timeout = 60 + float(cap.get("size") or 0) / 1e6 * 3  # ~3 s per MB on slow Wi-Fi
        try:
            await self.device.send({"type": "capture_send", "id": cap.get("id"), "token": token})
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise CamError(f"The phone didn't finish sending the {kind} within {timeout:.0f} s." + TELL_USER) from None
        finally:
            self.expected.pop(token, None)

    def on_upload_failed(self, data: dict) -> None:
        entry = self.expected.get(str(data.get("token")))
        if entry and not entry[1].done():
            entry[1].set_exception(CamError(f"The phone couldn't send it: {data.get('error')}." + TELL_USER))

    def on_import(self, path: Path) -> None:
        for fut in self.import_waiters:
            if not fut.done():
                fut.set_result(path)

    def on_ack(self, mid: str) -> None:
        if self.message and self.message.get("id") == mid:
            self.message = None
        fut = self.acks.get(mid)
        if fut and not fut.done():
            fut.set_result(True)


# ---------------------------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------------------------


REGION_SCHEMA = {
    "type": "array",
    "items": {"type": "number", "minimum": 0, "maximum": 1},
    "minItems": 4,
    "maxItems": 4,
}

INSTRUCTIONS = """\
Claude Cam lets you see live through the camera of the user's phone. Typical use: the user \
points the phone at a device (a screen, LEDs, a dev board, a printer) while you run commands, \
so you can check the physical result yourself instead of asking.

- Ask before you use the camera. Tell the user in chat what you want to look at and why, ask them \
to open the Claude Cam app and point the phone at it, then end your turn and wait until they say \
it's ready. Don't call camera tools before then. (If they've just said it's set up, go ahead.)
- Then check once with camera_status. If no phone is connected, tell them what it said and wait \
again; never poll in a loop.
- Never fail silently. If a camera tool fails (no phone, no answer, stalled stream, recording or \
upload error), tell the user in your very next message what you tried and what went wrong. Don't \
retry repeatedly or carry on as if you'd seen the result. In every reply where you used the camera, \
say what you looked at and what you saw: the user can't see your tool calls as they happen.
- camera_frames is instant (frames from the live stream, also the last ~90 s of history). \
camera_snapshot takes a full-resolution photo and can crop, which is best for reading small text.
- To catch the effect of a command: get a timestamp first (`date +%s.%N`), run the command, then \
call camera_wait_for_change with baseline_at set to that timestamp. It returns before/after \
frames as soon as the picture changes and settles.
- camera_message puts text on the phone screen (e.g. "Move closer to the LCD") and can wait for \
the user to tap Done. camera_control sets torch, zoom, exposure and focus; lower exposure helps \
with glowing screens.
- The live stream is only a few frames per second. For anything faster (animations, GIF playback, \
flicker, blinking LEDs, glitches that last one frame), record a high-speed clip with \
camera_record_video (fps 120 or 240 if the phone supports it, a few seconds) and read the per-frame \
statistics: dark frames and the real update rate are measured, not guessed. Look at individual \
frames with camera_video_frames.
- Contact sheets are samples (16 frames out of hundreds). Before concluding anything (black frames, \
flicker, missing frames, "it works"), examine every frame of the relevant range with \
camera_video_frames (step=1, crop to the screen, table=true), and ask the user what the content should \
look like: dark frames are usually content, not a fault. Don't invent hardware explanations.
- camera_record_video also makes normal 30/60 fps recordings for demos or documentation: start it, \
do the work, stop it. Videos are saved on this computer; camera_video_frames can export stills.
- The user can take photos and videos themselves with the app's shutter and record buttons. When \
they say they've taken one, fetch it with camera_phone_captures.
- Coordinate timing with the user. If what you need to capture has to be started by them or only \
runs for a while (a video, an animation, a boot sequence), call camera_message with \
wait_for_done_seconds asking them to start it, then record as soon as they tap Done. Don't \
assume it's already running.
- Ask the user to prop the phone up steadily when you rely on change detection.
- The phone is a real device in the user's hands. If it disconnects, moves, or the picture goes \
blurry or dark, ask the user whether they did something (pressed home, picked it up, the screen \
locked) before assuming a bug or crash."""

TOOLS = [
    {
        "name": "camera_status",
        "title": "Camera status",
        "description": (
            "Check whether the user's phone is connected to Claude Cam and get its state: battery, "
            "stream resolution and rate, zoom/torch/exposure/focus, how fresh the last frame is, and "
            "any message showing on the phone. Call this first."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "camera_snapshot",
        "title": "Take a photo",
        "description": (
            "Take a fresh full-resolution photo with the phone camera and return it. Takes about "
            "0.5-2 s. Use `crop` to zoom into part of the photo at full sensor detail (e.g. to read "
            "small text on a display); combine with use_last=true to crop the previous photo again "
            "without taking a new one. For a quick look, camera_frames is faster."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_size": {
                    "type": "integer",
                    "description": "Longest edge of the returned image in pixels (default 1568).",
                    "minimum": 160,
                    "maximum": 4000,
                },
                "crop": {
                    **REGION_SCHEMA,
                    "description": "[x, y, width, height] as fractions (0-1) of the photo, origin top-left.",
                },
                "use_last": {
                    "type": "boolean",
                    "description": "Reuse the previous photo instead of taking a new one (for a different crop).",
                },
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "camera_frames",
        "title": "Get live frames",
        "description": (
            "Return frames from the phone's live camera stream (instant; ~1440x1080). With the "
            "defaults it returns the newest frame. count>1 returns frames evenly spaced over the "
            "last window_seconds, to see what just happened (the server keeps ~90 s of history). "
            "Set wait_seconds to first wait and record what happens next; the window then covers "
            "the wait. Each frame is labelled with its capture time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of frames (default 1, max 16).", "minimum": 1, "maximum": 16},
                "window_seconds": {
                    "type": "number",
                    "description": "Time span to spread the frames over (default: wait_seconds, or 5 s when count>1).",
                    "minimum": 0,
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Wait this long before returning, recording what happens (default 0, max 120).",
                    "minimum": 0,
                    "maximum": 120,
                },
                "max_size": {
                    "type": "integer",
                    "description": "Longest edge of each returned image (default 1024).",
                    "minimum": 160,
                    "maximum": 1920,
                },
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "camera_wait_for_change",
        "title": "Wait for the picture to change",
        "description": (
            "Block until the camera picture changes compared with a baseline, then wait for it to "
            "settle and return before/after frames. Use it after running a command to see its "
            "effect as soon as it happens (a display updating, an LED turning on). Pass baseline_at "
            "(unix time from `date +%s.%N` taken before the command) so a change that already "
            "happened is still caught; otherwise the baseline is the newest frame at call time. "
            "Restrict detection to part of the picture with `region`. On timeout it returns the "
            "current frame and the largest change seen."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "number", "description": "Give up after this long (default 30, max 600).", "minimum": 1, "maximum": 600},
                "baseline_at": {"type": "number", "description": "Unix time (seconds) of the 'before' state."},
                "region": {
                    **REGION_SCHEMA,
                    "description": "Only watch this part: [x, y, width, height] as fractions (0-1), origin top-left.",
                },
                "sensitivity": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "low = big changes only (~5% of area), medium (default, ~1.2%), high (~0.3%, e.g. one small LED).",
                },
                "settle_seconds": {
                    "type": "number",
                    "description": "After a change, wait until the picture is still for this long (default 0.6, 0 = return at once).",
                    "minimum": 0,
                    "maximum": 10,
                },
                "include_before": {"type": "boolean", "description": "Also return the baseline frame (default true)."},
                "max_size": {"type": "integer", "description": "Longest edge of returned images (default 1024).", "minimum": 160, "maximum": 1920},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "camera_record_video",
        "title": "Record a video",
        "description": (
            "Record a video with the phone camera and save it as an MP4 on this computer. "
            "(1) High-speed clips to analyse anything faster than the live stream: animations, GIF playback, "
            "screen flicker, blinking LEDs, one-frame glitches. Use fps 120 or 240 (the phone's high-speed mode, "
            "lower resolution) or 60, and a few seconds. The result measures every frame: dark frames, how often "
            "the picture actually updates, dropped frames. It also includes a labelled contact sheet. "
            "(2) Normal recordings for demos or documentation: call without `seconds` to start, do the work, then "
            "call with stop=true. During a 30/60 fps recording the live stream keeps working but photos don't; "
            "high-speed recording pauses the live stream. Inspect any recording with camera_video_frames."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Record this long, then return the finished video (max 15 s above 60 fps, 600 s otherwise). "
                    "Omit to start a recording that runs until you call again with stop=true.",
                    "minimum": 0.5,
                    "maximum": 600,
                },
                "stop": {"type": "boolean", "description": "Stop the running recording and return it."},
                "fps": {
                    "type": "integer",
                    "description": "Frame rate: 30 (default), 60, or 120/240 for high-speed mode. The phone uses the closest "
                    "rate it supports (camera_status lists them) and the result says what it used.",
                    "minimum": 1,
                    "maximum": 960,
                },
                "quality": {"type": "string", "enum": ["720p", "1080p", "2160p"], "description": "Resolution (default 1080p; high-speed mode uses what the phone allows)."},
                "name": {"type": "string", "description": "Short label for the file name, e.g. 'gif-test' or 'setup-demo'."},
                "analyze": {
                    "type": "boolean",
                    "description": "Frame statistics and a contact sheet in the result (default true for videos up to 30 s; longer ones get a 12-frame overview).",
                },
                "crop": {
                    **REGION_SCHEMA,
                    "description": "Analyse only this part of the picture (e.g. the screen you care about): [x, y, width, height] fractions of the upright frame.",
                },
                "from_camera_app": {
                    "type": "boolean",
                    "description": "Instead of recording, ask the user (on the phone) to record with the phone's own Camera app and "
                    "Share it to Claude Cam, then wait for it and analyse it. Use this when the phone's built-in modes beat what "
                    "this app can do, e.g. Samsung Slow motion at 240/960 fps when camera_status shows no high-speed mode.",
                },
                "message": {"type": "string", "description": "With from_camera_app: what to ask the user (default: record, then Share to Claude Cam)."},
                "wait_seconds": {"type": "number", "description": "With from_camera_app: how long to wait for the video (default 300)."},
                "slowdown": {
                    "type": "number",
                    "description": "If the video is stored slowed down (some super-slow-motion exports play 8-32x slower), the factor, "
                    "so times and rates are reported in real time. Check the measured fps against what was recorded.",
                },
            },
        },
    },
    {
        "name": "camera_video_frames",
        "title": "Look inside a recorded video",
        "description": (
            "Examine a recorded video (the latest by default) frame by frame. You get per-frame statistics "
            "(brightness, dark frames, how often the picture changes, dropped frames) and the frames themselves, "
            "as one labelled contact sheet or as separate images. Choose a time range (start_seconds/end_seconds) "
            "or exact frames (start_frame/end_frame), then either `count` evenly spread frames or every Nth frame with "
            "`step` (step 1 shows every frame). Use `crop` to zoom into one part, such as a small LCD; statistics then "
            "cover just that part. `save_dir` writes the shown frames as JPEG files, e.g. stills for documentation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "video": {"type": "string", "description": "File name or path of a recording (default: the latest)."},
                "start_seconds": {"type": "number", "minimum": 0},
                "end_seconds": {"type": "number", "minimum": 0},
                "start_frame": {"type": "integer", "minimum": 0, "description": "First frame number (counted from 0)."},
                "end_frame": {"type": "integer", "minimum": 0, "description": "Last frame number (inclusive)."},
                "count": {"type": "integer", "minimum": 1, "maximum": 64, "description": "How many frames to show, evenly spread (default 16)."},
                "step": {"type": "integer", "minimum": 1, "description": "Show every Nth frame of the range instead (up to `count` frames)."},
                "layout": {"type": "string", "enum": ["sheet", "separate"], "description": "One contact sheet (default) or separate images (max 8)."},
                "crop": {**REGION_SCHEMA, "description": "Zoom into this part: [x, y, width, height] fractions of the upright frame."},
                "max_size": {"type": "integer", "minimum": 160, "maximum": 1920, "description": "Longest edge of separate images (default 1024)."},
                "table": {"type": "boolean", "description": "Include a row per frame (default: when the range has at most 120 frames)."},
                "sensitivity": {"type": "string", "enum": ["low", "medium", "high"], "description": "What counts as a picture update (default medium, about 1.2% of the area)."},
                "save_dir": {"type": "string", "description": "Also save the shown frames as JPEG files in this folder."},
                "slowdown": {"type": "number", "description": "If the video is stored slowed down, the factor; times and rates are then real time."},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "camera_phone_captures",
        "title": "Get photos and videos the user took",
        "description": (
            "Fetch the photos and videos the user took themselves with the Claude Cam app's shutter and record "
            "buttons. Call it when the user says they've taken a picture or video for you, or when camera_status "
            "says some are waiting. The app holds them (not in the phone's gallery) until they're sent here, then "
            "deletes them from the phone. Photos come back as images and are saved in ~/Pictures/Claude Cam. "
            "Videos are saved in ~/Videos/Claude Cam and get the same analysis as camera_record_video. "
            "Look at earlier ones again in those folders."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1, "maximum": 20, "description": "At most this many, newest first (default 10)."},
                "max_size": {"type": "integer", "minimum": 160, "maximum": 4000, "description": "Longest edge of returned photos (default 1568)."},
                "analyze": {"type": "boolean", "description": "Frame statistics and a contact sheet for videos (default true for videos up to 30 s)."},
            },
        },
    },
    {
        "name": "camera_control",
        "title": "Adjust the camera",
        "description": (
            "Change camera settings on the phone: torch (flashlight), zoom ratio, exposure "
            "compensation (negative values help with bright screens), focus (\"auto\" or a point "
            "[x, y] to focus and lock on), stream fps and resolution, and an extra rotation applied "
            "to every image (if the phone lies sideways). Only the given settings change. Returns "
            "the resulting camera state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "torch": {"type": "boolean", "description": "Flashlight on/off."},
                "zoom": {"type": "number", "description": "Zoom ratio, e.g. 1, 2, 3 (0.6 may select the ultra-wide). Clamped to the phone's range."},
                "exposure": {"type": "integer", "description": "Exposure compensation index; 0 = auto. See camera_status for range and step."},
                "focus": {
                    "description": '"auto" for continuous autofocus, or [x, y] fractions of the image to focus and lock on.',
                    "anyOf": [
                        {"type": "string", "enum": ["auto"]},
                        {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1}, "minItems": 2, "maxItems": 2},
                    ],
                },
                "fps": {"type": "number", "description": "Idle stream frame rate (default 3; it boosts to 10 while you wait or record).", "minimum": 0.5, "maximum": 15},
                "stream_size": {"type": "integer", "description": "Long edge of stream frames (default 1920; lower it on a slow network).", "minimum": 320, "maximum": 1920},
                "rotate": {"type": "integer", "enum": [0, 90, 180, 270], "description": "Extra clockwise rotation for all returned images."},
            },
        },
    },
    {
        "name": "camera_message",
        "title": "Show a message on the phone",
        "description": (
            "Show a message on the phone screen, e.g. \"Point the camera at the router LEDs\" or "
            "\"Move closer to the LCD\". The phone vibrates. With wait_for_done_seconds > 0 the "
            "message gets a Done button and this call waits until the user taps it (or the time "
            "runs out). Empty text clears the message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message to show; empty clears it."},
                "wait_for_done_seconds": {"type": "number", "description": "Wait for the user to tap Done (default 0 = don't wait, max 900).", "minimum": 0, "maximum": 900},
                "vibrate": {"type": "boolean", "description": "Vibrate the phone (default true)."},
            },
            "required": ["text"],
        },
    },
]


class Tools:
    def __init__(self, hub: Hub):
        self.hub = hub

    async def call(self, name: str, args: dict) -> list[dict]:
        fn = getattr(self, "t_" + name.removeprefix("camera_"), None) if name.startswith("camera_") else None
        if fn is None:
            raise CamError(f"Unknown tool {name}")
        return await fn(args or {})

    def _render(self, frame: Frame, max_size: int, crop=None, quality=80) -> tuple[bytes, int, int]:
        return render(frame, max_size=max_size, crop=crop, rotate=self.hub.rotate, quality=quality)

    def _stall_note(self) -> str:
        latest = self.hub.latest()
        if self.hub.device and latest and time.time() - latest.ts > 3:
            return (
                f"\nWarning: the live stream has stalled (newest frame is {time.time() - latest.ts:.0f} s old). "
                "The app may be in the background or the screen off. Tell the user in your reply rather than "
                "judging the device from old frames."
            )
        return ""

    async def t_status(self, args: dict) -> list[dict]:
        hub = self.hub
        s = hub.status_dict()
        if not s["connected"]:
            return [text_block(hub.not_connected_text())]
        d = s["device"]
        lines = [
            f"Phone: {d['name']} (Android {d.get('android', '?')}, app {d.get('app_version', '?')}), "
            f"connected {d['connected_for']} s from {d['remote']}.",
        ]
        if "battery" in d:
            lines.append(f"Battery: {d['battery']}%{' (charging)' if d.get('charging') else ''}"
                         + (f", thermal status {d['thermal']}" if d.get("thermal") else ""))
        if s["stream_resolution"]:
            w, h = s["stream_resolution"]
            lines.append(
                f"Stream: {w}x{h}, target {s['fps_target']:g} fps (measured {s['fps_measured']:g}), "
                f"newest frame {s['last_frame_age']:.1f} s old, {s['frames_buffered']} frames buffered "
                f"(last {s['buffer_seconds']:g} s)."
            )
        else:
            lines.append("Stream: no frames received yet (camera starting, or permission not granted).")
        if d.get("camera_ready") is False:
            lines.append("Camera: not running on the phone (permission or app in background).")
        cam = []
        if "zoom" in d:
            cam.append(f"zoom {d['zoom']:.2g}x (range {d.get('zoom_min', 1):.2g}-{d.get('zoom_max', 1):.2g})")
        if "torch" in d:
            cam.append(f"torch {'on' if d['torch'] else 'off'}" + ("" if d.get("has_flash", True) else " (no flash unit)"))
        if "exposure" in d:
            cam.append(f"exposure {d['exposure']} (range {d.get('exposure_min')}..{d.get('exposure_max')}, step {d.get('exposure_step', '?')} EV)")
        if "focus" in d:
            cam.append(f"focus {d['focus']}")
        if cam:
            lines.append("Camera: " + ", ".join(cam) + ".")
        if d.get("orientation") is not None:
            lines.append(f"Phone orientation: {d['orientation']} (images are turned upright to match).")
        if s["rotate"]:
            lines.append(f"Extra rotation applied to images: {s['rotate']} deg.")
        if s["message"]:
            lines.append(f"Message on the phone screen: {s['message']!r}")
        if d.get("video_fps") or d.get("high_speed_fps"):
            v = f"Video: {', '.join(str(f) for f in d.get('video_fps') or [30])} fps"
            if d.get("video_qualities"):
                v += f" at up to {d['video_qualities'][-1]}"
            if d.get("high_speed_fps"):
                v += f"; high-speed {', '.join(str(f) for f in d['high_speed_fps'])} fps"
                if d.get("high_speed_qualities"):
                    v += f" at {'/'.join(d['high_speed_qualities'])}"
            else:
                v += "; no high-speed mode available to apps"
            lines.append(v + ".")
        if len(d.get("cameras") or []) > 1:
            cams = "; ".join(
                f"{c.get('label', c.get('id'))}: {', '.join(str(f) for f in c.get('fps') or [])} fps"
                + (f", high-speed {', '.join(str(f) for f in c['high_speed_fps'])}" if c.get("high_speed_fps") else "")
                for c in d["cameras"]
            )
            lines.append(f"Back cameras: {cams}. Recordings use whichever camera can do the requested frame rate.")
        if d.get("captures_waiting"):
            n = d["captures_waiting"]
            lines.append(
                f"The user has {n} photo(s)/video(s) waiting in the app for you. Fetch them with camera_phone_captures "
                "when they're relevant (ask the user if you're not sure)."
            )
        rec = hub.recording
        if rec and not rec.done.done():
            lines.append(f"Recording now: {time.time() - rec.started_at:.0f} s so far ({self._describe_start(rec)}).")
        recs = video.list_recordings(3)
        if recs:
            lines.append(f"Recordings in {video.recordings_dir()}: latest {recs[0].name}.")
        lines.append(f"Server time: {fmt_ts(s['time'])}")
        return [text_block("\n".join(lines) + self._stall_note())]

    async def t_snapshot(self, args: dict) -> list[dict]:
        max_size = int(clamp(int(args.get("max_size") or 1568), 160, 4000))
        crop = parse_region(args.get("crop"))
        frame = await self.hub.take_photo(bool(args.get("use_last")))
        data, w, h = self._render(frame, max_size, crop, quality=85)
        desc = f"Photo {frame.width}x{frame.height} taken {fmt_ts(frame.ts)}"
        if crop:
            desc += f", cropped to {list(crop)}"
        desc += f", returned at {w}x{h}."
        return [text_block(desc), image_block(data)]

    async def t_frames(self, args: dict) -> list[dict]:
        hub = self.hub
        count = int(clamp(int(args.get("count") or 1), 1, 16))
        wait = float(clamp(float(args.get("wait_seconds") or 0), 0, 120))
        window = args.get("window_seconds")
        max_size = int(clamp(int(args.get("max_size") or 1024), 160, 1920))
        if wait > 0:
            hub.require_device()
            async with hub.boosted():
                await hub.activity("watching", wait)
                await asyncio.sleep(wait)
            await hub.activity("idle")
        else:
            if not hub.device and not hub.frames:
                raise CamError(hub.not_connected_text())
            await hub.activity("looking")
        end = time.time()
        if window is None:
            window = wait if wait > 0 else (5 if count > 1 else 0)
        window = float(clamp(float(window), 0, hub.buffer_seconds))
        if window <= 0:
            latest = hub.latest()
            frames = [latest] if latest else []
        else:
            frames = [f for f in hub.frames if end - window <= f.ts <= end]
        if not frames:
            if hub.device:
                raise CamError("No frames from the phone in that window yet (the camera may still be starting)." + self._stall_note())
            raise CamError(hub.not_connected_text())
        picked = pick_evenly(frames, count)
        header = f"{len(picked)} frame(s)"
        if len(picked) > 1:
            header += f" spanning {picked[-1].ts - picked[0].ts:.1f} s"
        header += f", newest captured {end - picked[-1].ts:.1f} s ago."
        if not hub.device:
            header += " The phone is disconnected; these are buffered frames from before."
        content = [text_block(header + self._stall_note())]
        for i, f in enumerate(picked, 1):
            data, w, h = self._render(f, max_size)
            if len(picked) > 1:
                content.append(text_block(f"[{i}/{len(picked)}] {fmt_ts(f.ts)}"))
            content.append(image_block(data))
        return content

    async def t_wait_for_change(self, args: dict) -> list[dict]:
        hub = self.hub
        hub.require_device()
        timeout = float(clamp(float(args.get("timeout_seconds") or 30), 1, 600))
        region = parse_region(args.get("region"))
        sensitivity = args.get("sensitivity") or "medium"
        if sensitivity not in SENSITIVITY:
            raise CamError("sensitivity must be low, medium or high.")
        threshold = SENSITIVITY[sensitivity]
        settle = args.get("settle_seconds")
        settle = 0.6 if settle is None else float(clamp(float(settle), 0, 10))
        include_before = args.get("include_before", True) is not False
        max_size = int(clamp(int(args.get("max_size") or 1024), 160, 1920))
        baseline_at = args.get("baseline_at")
        rot = hub.rotate
        started = time.time()
        deadline = started + timeout
        notes = []

        def diff(a: Frame, b: Frame) -> float:
            return changed_fraction(rotated(a.thumb, rot), rotated(b.thumb, rot), region)

        async with hub.boosted():
            await hub.activity("watching", timeout)
            try:
                if baseline_at is not None:
                    baseline_at = float(baseline_at)
                    baseline = hub.frame_at(baseline_at)
                    if baseline is None and hub.frames:
                        baseline = hub.frames[0]
                        notes.append(f"baseline_at is older than the buffer; used the oldest frame ({fmt_ts(baseline.ts)}).")
                    elif baseline and baseline_at - baseline.ts > 2:
                        notes.append(f"No frame near baseline_at; the baseline is from {baseline_at - baseline.ts:.1f} s earlier.")
                else:
                    baseline = hub.latest()
                if baseline is None:
                    baseline = await hub.wait_frame_after(0, min(5.0, timeout))
                if baseline is None:
                    raise CamError("No frames from the phone yet (the camera may still be starting)." + self._stall_note())

                cursor, changed, change_frac, max_seen = baseline.ts, None, 0.0, 0.0
                while (remaining := deadline - time.time()) > 0:
                    f = await hub.wait_frame_after(cursor, remaining)
                    if f is None:
                        break
                    cursor = f.ts
                    frac = diff(baseline, f)
                    max_seen = max(max_seen, frac)
                    if frac >= threshold:
                        changed, change_frac = f, frac
                        break

                area = "watched region" if region else "frame"
                if changed is None:
                    latest = hub.latest() or baseline
                    data, _, _ = self._render(latest, max_size)
                    msg = (
                        f"No change within {timeout:g} s: the largest change was {max_seen * 100:.2f}% of the {area} "
                        f"(threshold {threshold * 100:.1f}%, sensitivity {sensitivity}). Current frame {fmt_ts(latest.ts)}:"
                    )
                    return [text_block("\n".join([msg, *notes]) + self._stall_note()), image_block(data)]

                final, settled = changed, settle == 0
                if settle > 0:
                    quiet_since, prev = changed.ts, changed
                    settle_deadline = max(deadline, time.time()) + min(10.0, settle * 5 + 2)
                    while (remaining := settle_deadline - time.time()) > 0:
                        f = await hub.wait_frame_after(cursor, remaining)
                        if f is None:
                            break
                        cursor = f.ts
                        # Gradual changes (fades, exposure ramps) move little per frame, so
                        # "still" needs a much lower bar than "changed".
                        if diff(prev, f) >= threshold / 4:
                            quiet_since = f.ts
                        prev = final = f
                        if f.ts - quiet_since >= settle:
                            settled = True
                            break
            finally:
                await hub.activity("idle")

        lines = [
            f"Change detected {changed.ts - baseline.ts:.2f} s after the baseline, at {fmt_ts(changed.ts)}: "
            f"{change_frac * 100:.1f}% of the {area} changed (threshold {threshold * 100:.1f}%)."
        ]
        if settle > 0:
            lines.append(
                (
                    f"Settled {final.ts - changed.ts:.2f} s later"
                    if settled
                    else f"It kept changing (animation, moving camera?); the 'after' frame is {final.ts - changed.ts:.1f} s after the change"
                )
                + f"; {diff(baseline, final) * 100:.1f}% of the {area} differs from the baseline."
            )
        lines += notes
        content = [text_block("\n".join(lines))]
        if include_before:
            content.append(text_block(f"Before ({fmt_ts(baseline.ts)}):"))
            content.append(image_block(self._render(baseline, max_size)[0]))
        content.append(text_block(f"After ({fmt_ts(final.ts)}):"))
        content.append(image_block(self._render(final, max_size)[0]))
        return content

    async def t_control(self, args: dict) -> list[dict]:
        result = await self.hub.control(args)
        lines = []
        for note in result.get("notes") or []:
            lines.append(f"Note: {note}")
        status = await self.t_status({})
        return [text_block("\n".join(lines + ["Camera state now:", status[0]["text"]]))]

    async def t_record_video(self, args: dict) -> list[dict]:
        hub = self.hub
        if args.get("from_camera_app"):
            return await self._import_from_phone(args)
        if args.get("stop"):
            rec = await hub.stop_recording()
        else:
            fps = int(clamp(int(args.get("fps") or 30), 1, 960))
            limit = 15.0 if fps > 60 else 600.0
            seconds = args.get("seconds")
            seconds = None if seconds is None else float(clamp(float(seconds), 0.5, limit))
            quality = args.get("quality") or "1080p"
            if quality not in ("720p", "1080p", "2160p"):
                raise CamError("quality must be 720p, 1080p or 2160p.")
            name = str(args.get("name") or ("clip" if seconds is not None and seconds <= 30 else "recording"))
            rec = await hub.start_recording(fps, quality, seconds + 2 if seconds else limit, name)
            if seconds is None:
                return [text_block(
                    f"Recording started: {self._describe_start(rec)}. It keeps going until you call "
                    f"camera_record_video with stop=true (at most {limit:g} s). The phone shows a REC badge; "
                    "the user can also stop it by tapping that badge."
                )]
            await asyncio.sleep(seconds)
            rec = await hub.stop_recording()
        stopped = f"Stopped by {rec.stopped_by}." if rec.stopped_by not in (None, "Claude") else None
        return await self._video_report(rec.done.result(), f"Recorded with {self._describe_start(rec)}.", stopped, args)

    async def _import_from_phone(self, args: dict) -> list[dict]:
        hub = self.hub
        hub.require_device()
        wait = float(clamp(float(args.get("wait_seconds") or 300), 10, 1800))
        text = str(args.get("message") or "").strip() or (
            "Please record it with your phone's Camera app (use Slow motion for fast things), "
            "then tap Share and choose Claude Cam."
        )
        fut = asyncio.get_running_loop().create_future()
        hub.import_waiters.append(fut)
        try:
            await hub.show_message(text, 0, True)
            path = await asyncio.wait_for(fut, wait)
        except TimeoutError:
            raise CamError(f"No video was shared to Claude Cam within {wait:g} s. The message is still on the phone." + TELL_USER) from None
        finally:
            hub.import_waiters.remove(fut)
        await hub.show_message("", 0, False)
        return await self._video_report(path, "Shared from the phone's camera app.", None, args)

    def _describe_start(self, rec: Recording) -> str:
        i = rec.info
        parts = [f"{i.get('fps', rec.fps)} fps"]
        if i.get("width"):
            parts.append(f"{i['width']}x{i['height']}")
        if i.get("high_speed"):
            parts.append("high-speed mode")
        if i.get("camera"):
            parts.append(f"{i['camera']}")
        text = ", ".join(parts)
        if i.get("fps") and int(i["fps"]) != rec.fps:
            text += f" (asked for {rec.fps} fps; that's the closest the phone supports)"
        for note in i.get("notes") or []:
            text += f". Note: {note}"
        return text

    async def _video_report(self, path: Path, origin: str, stopped: str | None, args: dict) -> list[dict]:
        crop = parse_region(args.get("crop"))
        slowdown = float(args.get("slowdown") or 1.0)
        info = await asyncio.to_thread(video.probe, path)
        lines = [
            f"Saved {path} ({path.stat().st_size / 1e6:.1f} MB): {info.duration:.2f} s, "
            + (f"{info.frames} frames, " if info.frames else "")
            + f"{info.fps:.1f} fps, {info.width}x{info.height}. {origin}"
        ]
        if slowdown != 1:
            lines.append(f"Treating it as slowed down {slowdown:g}x: times and rates below are real time.")
        if stopped:
            lines.append(stopped)
        analyze = args.get("analyze")
        if analyze is None:
            analyze = info.duration <= video.FULL_SCAN_SECONDS
        content: list[dict] = []
        if analyze and info.duration <= 120 and (info.frames or 0) <= video.MAX_SCAN_FRAMES:
            sc = await asyncio.to_thread(
                video.scan, path, crop=crop, pick=lambda st: video.even_picks(st, 16, None),
                pick_width=None, keep=lambda st, im: im, slowdown=slowdown,
            )
            lines += video.summarize(sc, SENSITIVITY["medium"], crop)
            area_lines, area = ([], None) if crop else video.active_area_lines(sc, SENSITIVITY["medium"])
            lines += area_lines
            if area:
                # Show the active area (with some context) so a small screen is readable on the sheet.
                box = video.widen(area)
                sc.picked = [(st, im.crop(video.region_box(box, im.size))) for st, im in sc.picked]
                where = f"the active area [{', '.join(f'{v:.2f}' for v in box)}]"
            else:
                where = "the crop" if crop else "the video"
            content.append(image_block(jpeg_bytes(self._sheet(sc), 1600, 82)))
            lines.append(
                f"Contact sheet: {len(sc.picked)} of {len(sc.stats)} frames, evenly spaced over {where}. It's only a sample: "
                "brief events fall between these frames, so don't judge from it. Check frame ranges with camera_video_frames "
                "(step=1, crop, table=true) before drawing conclusions."
            )
        else:
            times = [info.duration * i / 11 for i in range(12)]
            _, frames = await asyncio.to_thread(video.sample, path, times, 640)
            items = [(f"{t:.1f} s", im.crop(video.region_box(crop, im.size)) if crop else im) for t, im in frames]
            if items:
                content.append(image_block(jpeg_bytes(video.contact_sheet(items, 320, self._cols(items[0][1])), 1600, 82)))
            lines.append("Overview: 12 frames spread over the video.")
        lines.append(f"Look closer with camera_video_frames (video={path.name!r}): a time or frame range, step=1 for every frame, crop to zoom in.")
        return [text_block("\n".join(lines)), *content]

    @staticmethod
    def _cols(im) -> int:
        return 4 if im.width >= im.height else 6

    def _sheet(self, sc, tile: int | None = None):
        hash_ = "#" if sc.exact_index else "~#"
        items = [(f"{hash_}{s.index}  {s.t * 1000:.1f} ms", im) for s, im in sc.picked]
        cols = self._cols(items[0][1])
        if tile is None:
            tile = 320 if len(items) <= 16 else 240
            if len(items) > 36:
                cols, tile = 8, 200
        return video.contact_sheet(items, tile, cols)

    async def t_video_frames(self, args: dict) -> list[dict]:
        path = video.resolve_video(args.get("video"))
        frame_mode = args.get("start_frame") is not None or args.get("end_frame") is not None
        start = args.get("start_frame" if frame_mode else "start_seconds")
        end = args.get("end_frame" if frame_mode else "end_seconds")
        layout = args.get("layout") or "sheet"
        if layout not in ("sheet", "separate"):
            raise CamError("layout must be sheet or separate.")
        count = int(clamp(int(args.get("count") or 16), 1, 64 if layout == "sheet" else 8))
        step = args.get("step")
        step = int(step) if step else None
        if step is not None and step < 1:
            raise CamError("step must be 1 or more.")
        crop = parse_region(args.get("crop"))
        sensitivity = args.get("sensitivity") or "medium"
        if sensitivity not in SENSITIVITY:
            raise CamError("sensitivity must be low, medium or high.")
        max_size = int(clamp(int(args.get("max_size") or 1024), 160, 1920))
        save_dir = Path(args["save_dir"]).expanduser() if args.get("save_dir") else None
        info = await asyncio.to_thread(video.probe, path)

        if start is None and end is None and info.duration > video.FULL_SCAN_SECONDS:
            times = [info.duration * i / max(1, count - 1) for i in range(count)]
            _, frames = await asyncio.to_thread(video.sample, path, times, 640)
            items = [(f"{t:.1f} s", im.crop(video.region_box(crop, im.size)) if crop else im) for t, im in frames]
            text = (
                f"{info.describe()}. It's longer than {video.FULL_SCAN_SECONDS} s, so here are {len(items)} frames spread "
                "over it. Give start_seconds/end_seconds (or frame numbers) for per-frame statistics."
            )
            return [text_block(text), image_block(jpeg_bytes(video.contact_sheet(items, 320, self._cols(items[0][1])), 1600, 82))]

        saved: list[Path] = []
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)

        def keep(stat, im):
            if save_dir:
                out = save_dir / f"{path.stem}_f{stat.index:05d}.jpg"
                im.convert("RGB").save(out, quality=92)
                saved.append(out)
            if layout == "separate":
                im.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            else:
                im.thumbnail((640, 640), Image.Resampling.LANCZOS)
            return im

        pick_width = None if (save_dir or crop) else (max_size if layout == "separate" else 640)
        sc = await asyncio.to_thread(
            video.scan, path, start=start, end=end, frame_mode=frame_mode, crop=crop,
            pick=lambda st: video.even_picks(st, count, step), pick_width=pick_width, keep=keep,
            slowdown=float(args.get("slowdown") or 1.0),
        )
        first, last = sc.stats[0], sc.stats[-1]
        hash_ = "#" if sc.exact_index else "~#"
        lines = [
            info.describe() + ".",
            f"Range: {hash_}{first.index}-{last.index} ({first.t:.3f}-{last.t:.3f} s), {len(sc.stats)} frames; "
            + (("showing every frame" if step == 1 else f"showing every {step}th frame") if step else "showing frames spread evenly")
            + f" ({len(sc.picked)})" + (" of the crop" if crop else "") + ".",
        ]
        if len(sc.picked) < len(sc.stats):
            lines.append(
                f"The images are {len(sc.picked)} of the {len(sc.stats)} frames in the range; anything between them isn't shown. "
                "Use step=1 on a narrower range to see every frame."
            )
        if len(sc.stats) >= video.MAX_SCAN_FRAMES:
            lines.append(f"(Stopped after {video.MAX_SCAN_FRAMES} frames; narrow the range to go further.)")
        lines += video.summarize(sc, SENSITIVITY[sensitivity], crop)
        if not crop:
            lines += video.active_area_lines(sc, SENSITIVITY[sensitivity])[0]
        if args.get("table", len(sc.stats) <= 120):
            lines.append(video.table(sc))
        if saved:
            lines.append(f"Saved {len(saved)} frames to {save_dir} (e.g. {saved[0].name}).")
        content = [text_block("\n".join(lines))]
        if layout == "sheet":
            content.append(image_block(jpeg_bytes(self._sheet(sc), 1800, 82)))
        else:
            for s_, im in sc.picked:
                content.append(text_block(f"{hash_}{s_.index} at {s_.t * 1000:.1f} ms"))
                content.append(image_block(jpeg_bytes(im, max_size, 85)))
        return content

    async def t_phone_captures(self, args: dict) -> list[dict]:
        hub = self.hub
        hub.require_device()
        count = int(clamp(int(args.get("count") or 10), 1, 20))
        max_size = int(clamp(int(args.get("max_size") or 1568), 160, 4000))
        waiting = await hub.list_captures()
        if not waiting:
            return [text_block(
                "The app isn't holding any photos or videos for you. If the user meant to send one, ask them to take "
                "it with the shutter (photo) or record button in Claude Cam, then tell you."
            )]
        picked = waiting[:count]
        content: list[dict] = []
        summary = [f"{len(waiting)} item(s) were waiting in the app; fetched {len(picked)} (newest first)."]
        if len(waiting) > len(picked):
            summary.append(f"{len(waiting) - len(picked)} more are still on the phone; call again to get them.")
        content.append(text_block(" ".join(summary)))
        for cap in picked:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((cap.get("taken_at") or 0) / 1000))
            path = await hub.fetch_capture(cap)
            if cap.get("kind") == "photo":
                frame = Frame(time.time(), path.read_bytes(), 0, 0, "photo")
                data, w, h = render(frame, max_size=max_size, rotate=0, quality=85)
                content.append(text_block(f"Photo the user took at {when}, saved to {path} (shown at {w}x{h})."))
                content.append(image_block(data))
            else:
                content += await self._video_report(path, f"Video the user took on the phone at {when}.", None, args)
        return content

    async def t_message(self, args: dict) -> list[dict]:
        wait = float(clamp(float(args.get("wait_for_done_seconds") or 0), 0, 900))
        vibrate = args.get("vibrate", True) is not False
        result = await self.hub.show_message(str(args.get("text") or "").strip(), wait, vibrate)
        return [text_block(result)]


# ---------------------------------------------------------------------------------------------
# MCP over streamable HTTP (JSON responses, stateless)
# ---------------------------------------------------------------------------------------------

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class McpEndpoint:
    def __init__(self, call_tool):
        self.call_tool = call_tool  # async (name, arguments) -> content blocks; raises CamError

    async def handle(self, msg) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification (initialized, cancelled, ...) or a stray response
            return None
        params = msg.get("params") or {}
        try:
            result = await self.dispatch(method, params)
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    async def dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "claude-cam", "title": "Claude Cam", "version": VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name", "")
            try:
                content = await self.call_tool(name, params.get("arguments") or {})
                return {"content": content, "isError": False}
            except CamError as e:
                return {"content": [text_block(str(e))], "isError": True}
            except (ValueError, TypeError) as e:
                return {"content": [text_block(f"Invalid arguments: {e}")], "isError": True}
            except Exception as e:  # noqa: BLE001
                log.exception("tool %s failed", name)
                return {"content": [text_block(f"Internal error in {name}: {e}")], "isError": True}
        raise KeyError(method)


# ---------------------------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------------------------

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def is_local(request: web.Request) -> bool:
    remote = request.remote or ""
    return remote in ("127.0.0.1", "::1") or remote.startswith("::ffff:127.")


def origin_ok(request: web.Request) -> bool:
    """Block DNS-rebinding / cross-site requests from web pages to the local-only endpoints."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    try:
        return urlsplit(origin).hostname in LOCAL_HOSTS
    except ValueError:
        return False


@web.middleware
async def local_only(request: web.Request, handler):
    public = request.path in ("/", "/claude-cam.apk", "/ws/device", "/favicon.ico") or request.path.startswith("/upload/")
    if not public and (not is_local(request) or not origin_ok(request)):
        return web.Response(status=403, text="Claude Cam: this endpoint is only available on the server itself.\n")
    return await handler(request)


def build_app(hub: Hub) -> web.Application:
    mcp = McpEndpoint(Tools(hub).call)
    app = web.Application(middlewares=[local_only], client_max_size=4 * 1024 * 1024)

    async def ws_device(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)
        dev: Device | None = None
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    kind = data.get("type")
                    if kind == "hello":
                        dev = Device(ws, data, request.remote or "?")
                        await hub.attach(dev)
                    elif dev is None:
                        continue
                    elif kind == "status":
                        data.pop("type", None)
                        dev.status.update(data)
                    elif kind == "result":
                        if data.get("ok", True):
                            hub.resolve(data.get("req"), data)
                        else:
                            hub.resolve(data.get("req"), error=data.get("error") or "unknown error")
                    elif kind in ("record_stopped", "record_error"):
                        hub.on_record_event(data)
                    elif kind == "upload_failed":
                        hub.on_upload_failed(data)
                    elif kind == "import_offer":
                        await dev.send({"type": "import_ready", "token": hub.offer_import(data)})
                    elif kind == "ack":
                        hub.on_ack(str(data.get("id")))
                elif msg.type == WSMsgType.BINARY and dev is not None:
                    raw = msg.data
                    if len(raw) < 4:
                        continue
                    (hlen,) = struct.unpack(">I", raw[:4])
                    try:
                        header = json.loads(raw[4 : 4 + hlen])
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    await hub.add_frame(header, raw[4 + hlen :])
        finally:
            if dev is not None:
                await hub.detach(dev)
        return ws

    async def mcp_post(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status=400)
        if isinstance(body, list):
            results = [r for r in [await mcp.handle(m) for m in body] if r is not None]
            return web.json_response(results) if results else web.Response(status=202)
        result = await mcp.handle(body)
        return web.json_response(result) if result is not None else web.Response(status=202)

    async def mcp_other(request: web.Request) -> web.Response:
        return web.Response(status=405, headers={"Allow": "POST"})

    async def landing(request: web.Request) -> web.Response:
        html = (STATIC / "index.html").read_text()
        apk = (
            f'<a class="button" href="/claude-cam.apk">Download the app ({APK_PATH.stat().st_size / 1e6:.1f} MB, '
            f'built {time.strftime("%Y-%m-%d %H:%M", time.localtime(APK_PATH.stat().st_mtime))})</a>'
            if APK_PATH.exists()
            else f'<a class="button" href="{RELEASE_APK_URL}">Download the app from GitHub</a>'
        )
        state = f"Phone connected: {hub.device.name}" if hub.device else "No phone connected"
        local = '<p><a href="/live">Open the live view</a> (only on this PC)</p>' if is_local(request) else ""
        html = html.replace("{{APK}}", apk).replace("{{STATE}}", state).replace("{{LOCAL}}", local)
        html = html.replace("{{LAN_URL}}", hub.lan_url)
        return web.Response(text=html, content_type="text/html")

    async def apk(request: web.Request) -> web.StreamResponse:
        if not APK_PATH.exists():
            raise web.HTTPFound(RELEASE_APK_URL)
        return web.FileResponse(
            APK_PATH,
            headers={
                "Content-Type": "application/vnd.android.package-archive",
                "Content-Disposition": 'attachment; filename="claude-cam.apk"',
            },
        )

    async def live(request: web.Request) -> web.Response:
        return web.FileResponse(STATIC / "live.html")

    async def api_status(request: web.Request) -> web.Response:
        return web.json_response(hub.status_dict())

    def jpeg_response(data: bytes, frame: Frame) -> web.Response:
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Captured-At": f"{frame.ts:.3f}"})

    async def api_photo(request: web.Request) -> web.Response:
        q = request.query
        try:
            frame = await hub.take_photo(q.get("last") in ("1", "true"))
            data, _, _ = render(frame, max_size=int(q.get("max", 1568)), crop=parse_region(q.get("crop")), rotate=hub.rotate, quality=85)
        except CamError as e:
            return web.Response(status=503, text=str(e) + "\n")
        return jpeg_response(data, frame)

    async def api_frame(request: web.Request) -> web.Response:
        q = request.query
        ago = float(q.get("ago", 0))
        frame = hub.frame_at(time.time() - ago) if ago > 0 else hub.latest()
        if frame is None:
            return web.Response(status=503, text=(hub.not_connected_text() if not hub.device else "No frames yet.") + "\n")
        data, _, _ = render(frame, max_size=int(q.get("max", 1280)), crop=parse_region(q.get("crop")), rotate=hub.rotate)
        return jpeg_response(data, frame)

    async def api_control(request: web.Request) -> web.Response:
        try:
            result = await hub.control(await request.json())
        except CamError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=503)
        return web.json_response({"ok": True, **result, "status": hub.status_dict()})

    async def api_message(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            result = await hub.show_message(str(body.get("text", "")).strip(), float(body.get("wait", 0)), body.get("vibrate", True) is not False)
        except CamError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=503)
        return web.json_response({"ok": True, "result": result})

    async def stream_mjpeg(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame", "Cache-Control": "no-store"})
        await resp.prepare(request)
        last = 0.0
        try:
            while True:
                frame = await hub.wait_frame_after(last, 30)
                if frame is None:
                    continue
                last = hub.latest().ts if hub.latest() else frame.ts  # skip ahead if we fell behind
                frame = hub.latest() or frame
                data, _, _ = render(frame, max_size=1280, rotate=hub.rotate)
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(data) + data + b"\r\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    app.router.add_get("/", landing)
    app.router.add_get("/claude-cam.apk", apk)
    app.router.add_get("/ws/device", ws_device)
    app.router.add_post("/mcp", mcp_post)
    app.router.add_route("GET", "/mcp", mcp_other)
    app.router.add_route("DELETE", "/mcp", mcp_other)
    app.router.add_get("/live", live)
    app.router.add_get("/stream.mjpg", stream_mjpeg)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/photo.jpg", api_photo)
    app.router.add_get("/api/frame.jpg", api_frame)
    app.router.add_post("/api/control", api_control)
    app.router.add_post("/api/message", api_message)

    async def upload(request: web.Request) -> web.Response:
        """The phone posts a finished recording here, with the token from record_start."""
        token = request.match_info["token"]
        rec = hub.recording
        imported = hub.imports.pop(token, None)
        expected = hub.expected.get(token)
        if expected is not None:
            path = expected[0]
        elif imported is not None:
            original = Path(imported)
            path = video.new_recording_path(f"from-phone-{original.stem}", None, original.suffix.lower() or ".mp4")
        elif rec is not None and token == rec.token and not rec.done.done():
            path = video.new_recording_path(rec.name, int((rec.info or {}).get("fps") or rec.fps))
        else:
            raise web.HTTPForbidden(text="unknown or finished upload\n")
        part = path.with_name(path.name + ".part")
        size = 0
        try:
            with open(part, "wb") as fh:
                async for chunk in request.content.iter_chunked(1 << 20):
                    fh.write(chunk)
                    size += len(chunk)
            if size == 0:
                raise ValueError("empty upload")
            part.replace(path)
        except Exception as e:  # noqa: BLE001
            part.unlink(missing_ok=True)
            log.warning("recording upload failed: %s", e)
            raise web.HTTPBadRequest(text=f"upload failed: {e}\n") from None
        log.info("saved from the phone: %s (%.1f MB)", path, size / 1e6)
        if expected is not None:
            if not expected[1].done():
                expected[1].set_result(path)
        elif imported is not None:
            hub.on_import(path)
        elif not rec.done.done():
            rec.done.set_result(path)
        return web.json_response({"ok": True, "saved": path.name})

    async def api_recordings(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "folder": str(video.recordings_dir()),
                "recordings": [
                    {"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime}
                    for p in video.list_recordings()
                ],
            }
        )

    async def recording_file(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        path = video.recordings_dir() / name
        if "/" in name or "\\" in name or path.suffix.lower() not in (".mp4", ".mov", ".3gp", ".mkv", ".webm") or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    app.router.add_post("/upload/{token}", upload)
    app.router.add_get("/api/recordings", api_recordings)
    app.router.add_get("/recordings/{name}", recording_file)

    async def close_device(app: web.Application) -> None:
        if hub.device:
            await hub.device.ws.close(code=1001, message=b"server shutting down")

    app.on_shutdown.append(close_device)
    return app


# ---------------------------------------------------------------------------------------------
# mDNS advertisement so the app finds the server without typing an address
# ---------------------------------------------------------------------------------------------


def lan_ip() -> str:
    """Source address of the default route (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def start_mdns(ip: str, port: int):
    try:
        from zeroconf import IPVersion, ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
    except ImportError:
        log.warning("zeroconf not installed; the app will need the server address typed in")
        return None
    host = socket.gethostname().split(".")[0].lower()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"Claude Cam on {host}"[:60] + "." + SERVICE_TYPE,
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={"path": "/ws/device", "version": VERSION},
        server=f"claudecam-{host}.local.",
    )
    try:
        azc = AsyncZeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)
        await azc.async_register_service(info, allow_name_change=True)
    except Exception as e:  # noqa: BLE001
        log.warning("mDNS advertisement failed (%s); the app will need the address typed in", e)
        return None
    log.info("advertising %s on %s:%d via mDNS", SERVICE_TYPE, ip, port)
    return azc, info


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    advertise_ip: str | None = None
    mdns: bool = True
    buffer_seconds: float = 90
    fps: float = 3
    boost_fps: float = 10


class PhoneServer:
    """The phone socket, HTTP API and MCP endpoint, running in the current event loop."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ip = settings.advertise_ip or lan_ip()
        self.hub = Hub(settings.buffer_seconds, settings.fps, settings.boost_fps, f"http://{self.ip}:{settings.port}")
        self.tools = Tools(self.hub)
        self.runner: web.AppRunner | None = None
        self.mdns = None

    async def start(self) -> None:
        """Bind the port (OSError if it is taken) and advertise the server over mDNS."""
        runner = web.AppRunner(build_app(self.hub), access_log=None, shutdown_timeout=2)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.settings.host, self.settings.port).start()
        except OSError:
            await runner.cleanup()
            raise
        self.runner = runner
        if self.settings.mdns:
            self.mdns = await start_mdns(self.ip, self.settings.port)
        log.info(
            "Claude Cam %s listening on %s:%d (phone: %s, MCP: http://127.0.0.1:%d/mcp)",
            VERSION, self.settings.host, self.settings.port, self.hub.lan_url, self.settings.port,
        )

    async def stop(self) -> None:
        if self.mdns:
            azc, info = self.mdns
            self.mdns = None
            try:
                await azc.async_unregister_service(info)
                await azc.async_close()
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass
        if self.runner:
            await self.runner.cleanup()
            self.runner = None


async def serve(settings: Settings) -> None:
    server = PhoneServer(settings)
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    try:
        await stop.wait()
    finally:
        await server.stop()


def main() -> None:
    frozen = getattr(sys, "frozen", False)
    if frozen and sys.platform == "win32" and len(sys.argv) == 1:
        # Someone double-clicked the program from the Windows installer; it runs inside Claude.
        from .installer import message_box

        message_box(
            "Claude Cam runs inside Claude, so there's nothing to open here.\n\n"
            "Open Claude, and open the Claude Cam app on your phone. "
            "To remove Claude Cam, use Settings > Apps."
        )
        return
    p = argparse.ArgumentParser(prog="claude-cam", description="Let Claude see through your phone's camera.")
    p.add_argument(
        "mode",
        nargs="?",
        choices=["serve", "stdio", "uninstall"],
        default="serve",
        help="serve: run as a service (Claude connects to http://127.0.0.1:PORT/mcp). "
        "stdio: speak MCP on stdin/stdout, for Claude Code to start. "
        "uninstall: remove a Windows-installer setup (default: serve)",
    )
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="port for the phone and the API (default 8777, or $CLAUDE_CAM_PORT)")
    p.add_argument("--advertise-ip", help="LAN address to advertise over mDNS (default: the default-route address)")
    p.add_argument("--no-mdns", action="store_true", help="don't advertise over mDNS")
    p.add_argument("--buffer-seconds", type=float, default=90, help="how much stream history to keep")
    p.add_argument("--fps", type=float, default=3, help="idle stream frame rate")
    p.add_argument("--boost-fps", type=float, default=10, help="frame rate while Claude is watching")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--yes", action="store_true", help="uninstall: don't ask or show messages")
    p.add_argument("--no-firewall", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-registry", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.mode == "uninstall":
        from .installer import uninstall

        sys.exit(uninstall(assume_yes=args.yes, firewall=not args.no_firewall, registry=not args.no_registry))
    # stdout carries the MCP protocol in stdio mode, so logs always go to stderr. A windowed build
    # started without one gets a log file instead.
    stream = sys.stderr or open(Path.home() / ".claude-cam.log", "a", encoding="utf-8")  # noqa: SIM115
    logging.basicConfig(stream=stream, level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    settings = Settings(args.host, args.port, args.advertise_ip, not args.no_mdns, args.buffer_seconds, args.fps, args.boost_fps)
    try:
        if args.mode == "stdio":
            from .stdio import run_stdio

            asyncio.run(run_stdio(settings))
        else:
            asyncio.run(serve(settings))
    except OSError as e:
        log.error("Could not listen on port %d: %s", settings.port, e)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
