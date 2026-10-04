#!/usr/bin/env python3
"""Pretend to be the Claude Cam app, for testing the server without a phone.

Streams a synthetic "device display" whose text comes from --display-file, so writing to that
file simulates the device reacting to a command.
"""

import argparse
import asyncio
import io
import json
import struct
import time
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont


def font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def scene(text: str, w: int, h: int, torch: bool) -> Image.Image:
    im = Image.new("RGB", (w, h), (60, 58, 54) if torch else (38, 36, 33))
    d = ImageDraw.Draw(im)
    # a "device" with a display in the middle
    d.rounded_rectangle((w * 0.15, h * 0.2, w * 0.85, h * 0.8), radius=w // 40, fill=(20, 20, 22))
    d.rectangle((w * 0.22, h * 0.3, w * 0.78, h * 0.62), fill=(10, 40, 30))
    d.text((w * 0.25, h * 0.36), text, font=font(h // 12), fill=(120, 255, 170))
    d.ellipse((w * 0.25, h * 0.68, w * 0.25 + h * 0.05, h * 0.73), fill=(255, 60, 40) if "LED" in text else (60, 20, 20))
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
                    "exposure_min": -8, "exposure_max": 8, "exposure_step": 0.25, "has_flash": True, "orientation": 0, **state}

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
