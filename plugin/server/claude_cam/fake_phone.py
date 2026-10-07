#!/usr/bin/env python3
"""Pretend to be the Claude Cam app, for testing the server without a phone.

Streams a synthetic "device display" whose text comes from --display-file, so writing to that
file simulates the device reacting to a command. Put GIF in the text for a 24 fps animation, and
FLICKER as well to blank every 12th animation frame. Recordings are encoded with PyAV and uploaded
like the real app does.
"""

import argparse
import asyncio
import io
import json
import math
import struct
import tempfile
import time
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont


def font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def scene(text: str, w: int, h: int, torch: bool, t: float | None = None) -> Image.Image:
    im = Image.new("RGB", (w, h), (60, 58, 54) if torch else (38, 36, 33))
    d = ImageDraw.Draw(im)
    # a "device" with a display in the middle
    d.rounded_rectangle((w * 0.15, h * 0.2, w * 0.85, h * 0.8), radius=w // 40, fill=(20, 20, 22))
    d.rectangle((w * 0.22, h * 0.3, w * 0.78, h * 0.62), fill=(10, 40, 30))
    d.text((w * 0.25, h * 0.36), text, font=font(h // 12), fill=(120, 255, 170))
    d.ellipse((w * 0.25, h * 0.68, w * 0.25 + h * 0.05, h * 0.73), fill=(255, 60, 40) if "LED" in text else (60, 20, 20))
    if "GIF" in text:
        n = math.floor((time.time() if t is None else t) * 24)  # a 24 fps animation
        if "FLICKER" in text and n % 12 == 0:
            d.rectangle((w * 0.22, h * 0.3, w * 0.78, h * 0.62), fill=(0, 0, 0))
        else:
            x = w * 0.25 + (n % 20) * w * 0.022
            d.rectangle((x, h * 0.48, x + w * 0.05, h * 0.58), fill=(255, 220, 60))
    return im


def jpeg(im: Image.Image, quality: int, exif_orientation: int | None = None) -> bytes:
    out = io.BytesIO()
    kwargs = {}
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif
    im.save(out, "JPEG", quality=quality, **kwargs)
    return out.getvalue()


def packet(header: dict, data: bytes) -> bytes:
    h = json.dumps(header).encode()
    return struct.pack(">I", len(h)) + h + data


async def run(url: str, display_file: Path) -> None:
    state = {"torch": False, "zoom": 1.0, "exposure": 0, "focus": "auto"}
    cfg = {"fps": 3, "quality": 70, "size": 1280}
    async with aiohttp.ClientSession() as session, session.ws_connect(url, max_msg_size=0) as ws:
        await ws.send_str(json.dumps({"type": "hello", "manufacturer": "Fake", "model": "Phone", "android": "16", "app_version": "test"}))

        def status() -> dict:
            return {"type": "status", "battery": 77, "charging": True, "camera_ready": True, "zoom_min": 0.6, "zoom_max": 10.0,
                    "exposure_min": -8, "exposure_max": 8, "exposure_step": 0.25, "has_flash": True, "orientation": "portrait",
                    "video_fps": [30, 60], "video_qualities": ["720p", "1080p"], "high_speed_fps": [120, 240],
                    "high_speed_qualities": ["720p"], "captures_waiting": len(captures), **state}

        rec = {}
        base = url.replace("ws://", "http://").split("/ws/")[0]

        # Captures the "user" took with the app's own buttons, held until the server fetches them.
        def make_clip() -> bytes:
            import av

            path = Path(tempfile.mkdtemp()) / "capture.mp4"
            with av.open(str(path), "w") as out:
                stream = out.add_stream("libx264", rate=30)
                stream.width, stream.height, stream.pix_fmt = 640, 480, "yuv420p"
                for k in range(60):
                    out.mux(stream.encode(av.VideoFrame.from_image(scene("GIF PHONE VIDEO", 640, 480, False, k / 30))))
                out.mux(stream.encode(None))
            return path.read_bytes()

        now_ms = int(time.time() * 1000)
        captures = {
            "photo-1": {"kind": "photo", "taken_at": now_ms - 60_000, "data": jpeg(scene("USER PHOTO", 2000, 1500, False), 90)},
            "video-1": {"kind": "video", "taken_at": now_ms - 30_000, "data": await asyncio.to_thread(make_clip)},
        }

        async def send_capture(cid: str, token: str) -> None:
            cap = captures.get(cid)
            if cap is None:
                await ws.send_str(json.dumps({"type": "upload_failed", "token": token, "error": "no such capture"}))
                return
            async with session.post(f"{base}/upload/{token}", data=cap["data"]) as r:
                print("capture upload ->", r.status)
                if r.status == 200:
                    captures.pop(cid, None)  # the app deletes it once Claude has it

        async def finish_recording(token: str, fps: int, size: tuple[int, int], t0: float, t1: float) -> None:
            import av  # only needed for recordings

            text = display_file.read_text().strip() if display_file.exists() else "READY"
            path = Path(tempfile.mkdtemp()) / "rec.mp4"

            def encode():
                with av.open(str(path), "w") as out:
                    stream = out.add_stream("libx264", rate=fps)
                    stream.width, stream.height = size
                    stream.pix_fmt = "yuv420p"
                    stream.options = {"preset": "ultrafast"}
                    for k in range(max(1, round((t1 - t0) * fps))):
                        frame = av.VideoFrame.from_image(scene(text, *size, state["torch"], t0 + k / fps))
                        out.mux(stream.encode(frame))
                    out.mux(stream.encode(None))

            await asyncio.to_thread(encode)
            async with session.post(f"{base}/upload/{token}", data=path.read_bytes()) as r:
                print("upload ->", r.status, await r.text())

        await ws.send_str(json.dumps(status()))

        async def stream():
            while True:
                text = display_file.read_text().strip() if display_file.exists() else "READY"
                w = cfg["size"]
                im = scene(text, w, w * 3 // 4, state["torch"])
                await ws.send_bytes(packet({"kind": "stream", "age_ms": 30}, jpeg(im, cfg["quality"])))
                await asyncio.sleep(1 / cfg["fps"])

        task = asyncio.create_task(stream())
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                t = m.get("type")
                print("<-", m)
                if t == "config":
                    cfg.update({k: m[k] for k in ("fps", "quality", "size") if k in m})
                elif t == "photo":
                    text = display_file.read_text().strip() if display_file.exists() else "READY"
                    upright = scene(text, 4000, 3000, state["torch"])
                    # store it sideways with an EXIF orientation, like real phones do
                    data = jpeg(upright.rotate(90, expand=True), 92, exif_orientation=6)
                    await ws.send_bytes(packet({"kind": "photo", "req": m["req"], "age_ms": 200}, data))
                elif t == "control":
                    state.update({k: m[k] for k in ("torch", "zoom", "exposure", "focus") if k in m})
                    if isinstance(state["focus"], list):
                        state["focus"] = f"locked at {state['focus']}"
                    await ws.send_str(json.dumps({"type": "result", "req": m["req"], "ok": True, "state": status()}))
                elif t == "captures_list":
                    items = [{"id": cid, "kind": c["kind"], "taken_at": c["taken_at"], "size": len(c["data"])}
                             for cid, c in sorted(captures.items(), key=lambda kv: -kv[1]["taken_at"])]
                    await ws.send_str(json.dumps({"type": "result", "req": m["req"], "ok": True, "captures": items}))
                elif t == "capture_send":
                    asyncio.create_task(send_capture(m.get("id"), m.get("token")))
                elif t == "record_start":
                    fps = 240 if m["fps"] > 120 else (120 if m["fps"] > 60 else min(60, m["fps"]))
                    size = (1280, 720) if fps > 60 else ((1920, 1080) if m.get("quality") == "1080p" else (1280, 720))
                    rec.update(token=m["token"], fps=fps, size=size, t0=time.time())
                    await ws.send_str(json.dumps({"type": "result", "req": m["req"], "ok": True, "fps": fps, "width": size[0],
                                                  "height": size[1], "high_speed": fps > 60, "notes": []}))
                elif t == "record_stop":
                    await ws.send_str(json.dumps({"type": "result", "req": m["req"], "ok": True}))
                    if rec.get("token") == m.get("token"):
                        asyncio.create_task(finish_recording(rec["token"], rec["fps"], rec["size"], rec["t0"], time.time()))
                        rec.clear()
                elif t == "message" and m.get("ack"):
                    async def ack(mid=m["id"]):
                        await asyncio.sleep(1.5)
                        await ws.send_str(json.dumps({"type": "ack", "id": mid}))
                    asyncio.create_task(ack())
        finally:
            task.cancel()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="ws://127.0.0.1:8777/ws/device")
    p.add_argument("--display-file", type=Path, default=Path("fake-display.txt"))
    args = p.parse_args()
    asyncio.run(run(args.url, args.display_file))


if __name__ == "__main__":
    main()
