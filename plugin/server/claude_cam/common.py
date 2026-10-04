"""Helpers shared by the live-stream server and the video analysis."""

from __future__ import annotations

import base64
import io
import time

from PIL import Image, ImageChops

# A pixel of the blurred greyscale thumbnail must move this many grey levels to count as
# changed. Sensor noise and small auto-exposure drift stay below it.
PIXEL_DELTA = 22
# Fraction of the watched area that must change for camera_wait_for_change to fire.
SENSITIVITY = {"low": 0.05, "medium": 0.012, "high": 0.003}


class CamError(Exception):
    """An error that is shown to Claude as a tool error."""


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
    # One histogram band: a multi-band histogram would count every other band as changed.
    a, b = a.convert("L"), b.convert("L")
    if region:
        box = region_box(region, a.size)
        a, b = a.crop(box), b.crop(box)
    hist = ImageChops.difference(a, b).histogram()
    total = sum(hist)
    return sum(hist[PIXEL_DELTA:]) / total if total else 0.0


def rotated(im: Image.Image, degrees: int) -> Image.Image:
    return im.rotate(-degrees, expand=True) if degrees else im


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


def text_block(s: str) -> dict:
    return {"type": "text", "text": s}


def image_block(jpeg: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(jpeg).decode(), "mimeType": "image/jpeg"}


def jpeg_bytes(im: Image.Image, max_size: int, quality: int = 80) -> bytes:
    if max(im.size) > max_size:
        im = im.copy()
        im.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=quality)
    return out.getvalue()
