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
import base64
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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web
from PIL import Image, ImageChops, ImageFilter, ImageOps

VERSION = "1.0.0"
SERVICE_TYPE = "_claudecam._tcp.local."
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC = PACKAGE_DIR / "static"
DEFAULT_PORT = int(os.environ.get("CLAUDE_CAM_PORT") or 8777)
# Offer a locally built APK when there is one (a repo checkout's dist/); otherwise send the
# phone to the latest GitHub release.
APK_PATH = Path(os.environ.get("CLAUDE_CAM_APK") or PACKAGE_DIR.parents[2] / "dist" / "claude-cam.apk")
RELEASE_APK_URL = "https://github.com/ssjrocks/claude-cam/releases/latest/download/claude-cam.apk"

# A pixel of the blurred greyscale thumbnail must move this many grey levels to count as
# changed. Sensor noise and small auto-exposure drift stay below it.
PIXEL_DELTA = 22
# Fraction of the watched area that must change for camera_wait_for_change to fire.
SENSITIVITY = {"low": 0.05, "medium": 0.012, "high": 0.003}

log = logging.getLogger("claudecam")


class CamError(Exception):
    """An error that is shown to Claude as a tool error."""


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


def parse_region(value) -> tuple[float, float, float, float] | None:
    """[x, y, w, h] as fractions of the image (0-1), origin top-left."""
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        value = [float(v) for v in value.split(",")]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise CamError("A region/crop must be [x, y, width, height] as fractions of the image (0-1).")
    x, y, w, h = (float(v) for v in value)
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
        raise CamError("Region/crop values must be fractions: 0 <= x,y < 1 and 0 < width,height <= 1.")
    return x, y, w, h


def region_box(region, size) -> tuple[int, int, int, int]:
    x, y, w, h = region
    width, height = size
    left, top = int(x * width), int(y * height)
    right = max(left + 1, min(width, round((x + w) * width)))
    bottom = max(top + 1, min(height, round((y + h) * height)))
    return left, top, right, bottom


def changed_fraction(a: Image.Image, b: Image.Image, region=None) -> float:
    if a.size != b.size:
        return 1.0
    if region:
        box = region_box(region, a.size)
        a, b = a.crop(box), b.crop(box)
    hist = ImageChops.difference(a, b).histogram()
    total = sum(hist)
    return sum(hist[PIXEL_DELTA:]) / total if total else 0.0


def rotated(im: Image.Image, degrees: int) -> Image.Image:
    return im.rotate(-degrees, expand=True) if degrees else im


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


def fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts % 1 * 10)} (t={ts:.2f})"


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def pick_evenly(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    if n == 1:
        return [items[-1]]
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


# ---------------------------------------------------------------------------------------------
# Hub: the connected phone, the frame buffer and request/response plumbing
# ---------------------------------------------------------------------------------------------


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

    # --- connection ---------------------------------------------------------------------------

    def not_connected_text(self) -> str:
        text = (
            "No phone is connected to Claude Cam. Ask the user to open the Claude Cam app on their "
            f"phone (on the same Wi-Fi as this PC; it finds {self.lan_url} by itself) and point the "
            "camera at what you need to see, then try again."
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
                fut.set_exception(CamError("The phone disconnected before it answered."))

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
            raise CamError(f"The phone did not answer within {timeout:g} s.") from None
        finally:
            self.pending.pop(rid, None)

    def resolve(self, rid: str | None, value=None, error: str | None = None) -> None:
        fut = self.pending.get(rid or "")
        if fut and not fut.done():
            if error:
                fut.set_exception(CamError(f"The phone reported an error: {error}"))
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

    def on_ack(self, mid: str) -> None:
        if self.message and self.message.get("id") == mid:
            self.message = None
        fut = self.acks.get(mid)
        if fut and not fut.done():
            fut.set_result(True)


# ---------------------------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------------------------


def text_block(s: str) -> dict:
    return {"type": "text", "text": s}


def image_block(jpeg: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(jpeg).decode(), "mimeType": "image/jpeg"}


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

- Start with camera_status. If no phone is connected, ask the user to open the Claude Cam app \
and point it at the device; it finds this PC on the same Wi-Fi by itself.
- camera_frames is instant (frames from the live stream, also the last ~90 s of history). \
camera_snapshot takes a full-resolution photo and can crop, which is best for reading small text.
- To catch the effect of a command: get a timestamp first (`date +%s.%N`), run the command, then \
call camera_wait_for_change with baseline_at set to that timestamp. It returns before/after \
frames as soon as the picture changes and settles.
- camera_message puts text on the phone screen (e.g. "Move closer to the LCD") and can wait for \
the user to tap Done. camera_control sets torch, zoom, exposure and focus; lower exposure helps \
with glowing screens.
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
                "The app may be in the background or the screen off."
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
    public = request.path in ("/", "/claude-cam.apk", "/ws/device", "/favicon.ico")
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
    p = argparse.ArgumentParser(prog="claude-cam", description="Let Claude see through your phone's camera.")
    p.add_argument(
        "mode",
        nargs="?",
        choices=["serve", "stdio"],
        default="serve",
        help="serve: run as a service (Claude connects to http://127.0.0.1:PORT/mcp). "
        "stdio: speak MCP on stdin/stdout, for Claude Code to start (default: serve)",
    )
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="port for the phone and the API (default 8777, or $CLAUDE_CAM_PORT)")
    p.add_argument("--advertise-ip", help="LAN address to advertise over mDNS (default: the default-route address)")
    p.add_argument("--no-mdns", action="store_true", help="don't advertise over mDNS")
    p.add_argument("--buffer-seconds", type=float, default=90, help="how much stream history to keep")
    p.add_argument("--fps", type=float, default=3, help="idle stream frame rate")
    p.add_argument("--boost-fps", type=float, default=10, help="frame rate while Claude is watching")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    # stdout carries the MCP protocol in stdio mode, so logs always go to stderr.
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
