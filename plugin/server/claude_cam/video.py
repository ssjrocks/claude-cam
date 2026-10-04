"""Recorded videos: where they're stored, and decoding them frame by frame for Claude (PyAV).

Everything here is blocking CPU work; the server runs it in a worker thread.
"""

from __future__ import annotations

import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import av
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat

from .common import PIXEL_DELTA, CamError, changed_fraction, region_box

# Decode whole clips up to this long frame by frame; longer videos are sampled by seeking.
FULL_SCAN_SECONDS = 30
MAX_SCAN_FRAMES = 4000


def recordings_dir() -> Path:
    if env := os.environ.get("CLAUDE_CAM_RECORDINGS"):
        return Path(env).expanduser()
    base = Path.home() / ("Movies" if sys.platform == "darwin" else "Videos")
    return base / "Claude Cam"


def new_recording_path(name: str, fps: int | None = None, suffix: str = ".mp4") -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:60] or "recording"
    folder = recordings_dir()
    folder.mkdir(parents=True, exist_ok=True)
    rate = f"_{fps}fps" if fps else ""
    return folder / f"{time.strftime('%Y-%m-%d_%H-%M-%S')}_{slug}{rate}{suffix}"


def list_recordings(limit: int = 50) -> list[Path]:
    folder = recordings_dir()
    if not folder.is_dir():
        return []
    files = sorted((p for p in folder.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".3gp", ".mkv", ".webm")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def resolve_video(value: str | None) -> Path:
    """'latest' (default), a file name in the recordings folder, or a path."""
    if not value or value == "latest":
        files = list_recordings(1)
        if not files:
            raise CamError("There are no recordings yet. Record one with camera_record_video.")
        return files[0]
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = recordings_dir() / value
    if not p.is_file():
        raise CamError(f"No video at {p}.")
    return p


@dataclass
class VideoInfo:
    path: Path
    frames: int | None
    fps: float
    duration: float
    width: int  # as displayed, after rotation
    height: int
    rotation: int  # degrees counter-clockwise to display upright
    start_time: float  # timestamp of the first frame

    def describe(self) -> str:
        frames = f"{self.frames} frames, " if self.frames else ""
        return f"{self.path.name}: {self.duration:.2f} s, {frames}{self.fps:.1f} fps, {self.width}x{self.height}"


def probe(path: Path) -> VideoInfo:
    try:
        with av.open(str(path)) as c:
            s = c.streams.video[0]
            first = next(c.decode(s), None)
            if first is None:
                raise CamError(f"{path.name} has no video frames.")
            rotation = int(first.rotation or 0) % 360
            w, h = first.width, first.height
            if rotation in (90, 270):
                w, h = h, w
            if s.duration:
                duration = float(s.duration * s.time_base)
            elif c.duration:
                duration = c.duration / 1_000_000
            else:
                duration = 0.0
            fps = float(s.average_rate) if s.average_rate else (s.frames / duration if s.frames and duration else 0.0)
            return VideoInfo(path, s.frames or None, fps, duration, w, h, rotation, first.time or 0.0)
    except av.FFmpegError as e:
        raise CamError(f"Couldn't read {path.name}: {e}") from None


@dataclass
class FrameStat:
    index: int  # frame number from the start of the video (estimated after a seek)
    t: float  # seconds from the first frame
    brightness: float  # mean grey level 0-255 (of the crop, if any)
    change: float | None  # fraction of the area that changed since the previous frame


@dataclass
class Scan:
    info: VideoInfo
    stats: list[FrameStat]
    exact_index: bool  # False when the scan started with a seek, so frame numbers are estimates
    picked: list[tuple[FrameStat, Image.Image]] = field(default_factory=list)
    minis: list[Image.Image] = field(default_factory=list)  # small blurred grey whole frames, for active_area()


def _upright(frame: av.VideoFrame, rotation: int, *, width: int | None = None, gray: bool = False) -> Image.Image:
    if width:
        height = max(2, round(frame.height * width / frame.width) // 2 * 2)
        frame = frame.reformat(width=width, height=height, format="gray" if gray else "rgb24")
    elif gray:
        frame = frame.reformat(format="gray")
    im = frame.to_image()  # always RGB, even from a grey frame
    if gray:
        im = im.convert("L")
    return im.rotate(rotation, expand=True) if rotation else im


def _frames(path: Path, info: VideoInfo, start: float | None, frame_mode: bool, slowdown: float = 1.0):
    """Yield (index, t, frame, exact) with t in real seconds, seeking first when starting deep into a long video."""
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        file_start = None if start is None else start * slowdown
        seek = (not frame_mode) and file_start is not None and file_start > 5
        if seek:
            c.seek(int((info.start_time + file_start - 1) / s.time_base), stream=s, backward=True)
        for i, f in enumerate(c.decode(s)):
            file_t = (f.time or 0.0) - info.start_time
            index = round(file_t * info.fps) if seek else i
            yield index, file_t / slowdown, f, not seek


def scan(
    path: Path,
    *,
    start: float | None = None,
    end: float | None = None,
    frame_mode: bool = False,
    crop=None,
    pick=None,
    pick_width: int | None = None,
    keep=None,
    slowdown: float = 1.0,
) -> Scan:
    """Decode a range of frames: per-frame brightness and change, plus full images for picked frames.

    The range is in seconds, or in frame numbers when frame_mode is set. `pick` is a function
    from the list of frame stats in the range to the frame numbers to return images for; it runs
    after a first pass, then a second pass grabs those frames. `keep(stat, image)` can save or
    shrink each picked image and returns what to hold on to.
    """
    info = probe(path)

    def in_range(index: int, t: float) -> int:
        """-1 before the range, 0 inside, 1 after it."""
        value = index if frame_mode else t
        if start is not None and value < start - (0 if frame_mode else 1e-6):
            return -1
        if end is not None and value > end + (0 if frame_mode else 1e-6):
            return 1
        return 0

    stats: list[FrameStat] = []
    minis: list[Image.Image] = []
    exact = True
    prev = None
    for index, t, f, exact_index in _frames(path, info, start, frame_mode, slowdown):
        pos = in_range(index, t)
        if pos < 0:
            continue
        if pos > 0 or len(stats) >= MAX_SCAN_FRAMES:
            break
        exact = exact and exact_index
        full = _upright(f, info.rotation, width=max(64, min(320, f.width // 4)), gray=True).filter(ImageFilter.GaussianBlur(1))
        thumb = full.crop(region_box(crop, full.size)) if crop else full
        change = changed_fraction(prev, thumb) if prev is not None else None
        stats.append(FrameStat(index, t, ImageStat.Stat(thumb).mean[0], change))
        minis.append(full.resize((160, max(1, round(160 * full.height / full.width)))) if full.width > 160 else full)
        prev = thumb
    if not stats:
        raise CamError("No frames in that range of the video.")
    result = Scan(info, stats, exact, minis=minis)
    if pick is None:
        return result

    wanted = set(pick(stats))
    by_index = {s.index: s for s in stats}
    for index, t, f, _ in _frames(path, info, start, frame_mode, slowdown):
        if index > stats[-1].index:
            break
        if index in wanted and index in by_index:
            im = _upright(f, info.rotation, width=pick_width if not crop else None)
            if crop:
                im = im.crop(region_box(crop, im.size))
            stat = by_index[index]
            result.picked.append((stat, keep(stat, im) if keep else im))
            wanted.discard(index)
            if not wanted:
                break
    return result


def sample(path: Path, times: list[float], width: int) -> tuple[VideoInfo, list[tuple[float, Image.Image]]]:
    """Grab frames near the given times by seeking (for long videos)."""
    info = probe(path)
    out = []
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        for target in times:
            c.seek(int((info.start_time + max(0.0, target)) / s.time_base), stream=s, backward=True)
            for f in c.decode(s):
                t = (f.time or 0.0) - info.start_time
                if t >= target - 1 / max(info.fps, 1):
                    out.append((t, _upright(f, info.rotation, width=width)))
                    break
    return info, out


def active_area(scan_: Scan, threshold: float):
    """Find where the picture changes when that's a small part of the frame (a screen, an LED).

    Returns (region, fraction_of_frame, scan_of_that_region) or None. Whole-frame numbers dilute a
    small animated screen below every threshold, which is how a GIF can look "black" or "frozen".
    """
    minis = scan_.minis
    if len(minis) < 3:
        return None
    acc = Image.new("L", minis[0].size, 0)
    for a, b in zip(minis, minis[1:]):
        if a.size == b.size:
            acc = ImageChops.add(acc, ImageChops.difference(a, b).point(lambda v: 1 if v > PIXEL_DELTA else 0))
    peak = acc.getextrema()[1]
    if peak < 1:
        return None
    floor = max(1, round(peak * 0.2))
    mask = acc.point(lambda v: 255 if v >= floor else 0)
    box = mask.getbbox()
    if not box or mask.histogram()[255] < 4:  # a few stray pixels aren't an "area"
        return None
    w, h = acc.size
    left, top, right, bottom = max(0, box[0] - 2), max(0, box[1] - 2), min(w, box[2] + 2), min(h, box[3] + 2)
    fraction = (right - left) * (bottom - top) / (w * h)
    if fraction > 0.5:
        return None
    region = (round(left / w, 3), round(top / h, 3), round((right - left) / w, 3), round((bottom - top) / h, 3))
    stats, prev = [], None
    for st, m in zip(scan_.stats, minis):
        part = m.crop(region_box(region, m.size))
        stats.append(FrameStat(st.index, st.t, ImageStat.Stat(part).mean[0], changed_fraction(prev, part) if prev else None))
        prev = part
    return region, fraction, Scan(scan_.info, stats, scan_.exact_index)


def widen(region, by: float = 0.08):
    """Pad a region a little on every side for context, staying inside the frame."""
    x, y, w, h = region
    x2, y2 = min(1.0, x + w + by), min(1.0, y + h + by)
    x, y = max(0.0, x - by), max(0.0, y - by)
    return (x, y, x2 - x, y2 - y)


def active_area_lines(scan_: Scan, threshold: float) -> tuple[list[str], tuple | None]:
    found = active_area(scan_, threshold)
    if not found:
        return [], None
    region, fraction, sub = found
    r = ", ".join(f"{v:g}" for v in region)
    lines = [f"Most of the change happens in one area, [{r}] ({fraction * 100:.{0 if fraction >= 0.1 else 1}f}% of the frame; probably the screen or "
             "light you care about). Measured on that area alone:"]
    lines += ["  " + line for line in summarize(sub, threshold, crop=region)[1:]]
    lines.append(f"  (Pass crop=[{r}] to camera_video_frames for full-resolution frames of it.)")
    return lines, region


# --- reporting -----------------------------------------------------------------------------------


def _ranges(indices: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for i in indices:
        if out and i == out[-1][1] + 1:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


def summarize(scan_: Scan, threshold: float, crop=None) -> list[str]:
    stats = scan_.stats
    area = "crop" if crop else "frame"
    hash_ = "#" if scan_.exact_index else "~#"
    lines = []
    span = stats[-1].t - stats[0].t
    gaps = [b.t - a.t for a, b in zip(stats, stats[1:]) if b.t > a.t]
    if gaps:
        med = statistics.median(gaps)
        line = f"Measured frame rate {1 / med:.1f} fps (frame interval median {med * 1000:.1f} ms"
        if max(gaps) > med * 1.8:
            worst = max(range(len(gaps)), key=gaps.__getitem__)
            line += f", longest {max(gaps) * 1000:.1f} ms after {hash_}{stats[worst].index}: the phone dropped frames there"
        lines.append(line + ").")

    bright = [s.brightness for s in stats]
    med_b = statistics.median(bright)
    lo = min(stats, key=lambda s: s.brightness)
    hi = max(stats, key=lambda s: s.brightness)
    lines.append(
        f"Brightness of the {area} (0-255): min {lo.brightness:.0f} at {hash_}{lo.index}, "
        f"median {med_b:.0f}, max {hi.brightness:.0f} at {hash_}{hi.index}."
    )
    dark_limit = max(8.0, med_b * 0.5)
    dark = [s.index for s in stats if s.brightness < dark_limit]
    if not dark:
        lines.append(f"No dark frames: none is below half the median brightness ({dark_limit:.0f}).")
    else:
        t_of = {s.index: s.t for s in stats}
        parts = []
        for a, b in _ranges(dark)[:12]:
            n = b - a + 1
            parts.append(f"{hash_}{a}" + (f"-{b}" if b != a else "") + f" ({n} frame{'s' if n > 1 else ''}, from {t_of[a]:.3f} s)")
        more = len(_ranges(dark)) - 12
        lines.append(
            f"Dark frames (below {dark_limit:.0f}, half the median): {len(dark)} in {len(_ranges(dark))} run(s): "
            + ", ".join(parts) + (f", and {more} more runs" if more > 0 else "") + "."
        )

    # A content update can straddle two frames at high frame rates (rolling shutter), so a pair
    # of changed frames counts as one update. Longer runs are continuous change: every frame counts.
    changed = [s.index for s in stats if s.change is not None and s.change >= threshold]
    t_of = {s.index: s.t for s in stats}
    events = []
    for a, b in _ranges(changed):
        events += [t_of[a]] if b - a <= 1 else [t_of[i] for i in range(a, b + 1) if i in t_of]
    transitions = len(stats) - 1
    if transitions >= 4 and len(changed) >= 0.8 * transitions:
        fps = 1 / statistics.median(gaps) if gaps else 0
        lines.append(
            f"WARNING: the {area} changes in nearly every frame ({len(changed)} of {transitions}), so it updates at least "
            f"as fast as this recording ({fps:.0f} fps). Updates between frames are being missed and the real update rate "
            "can't be measured from this video; frames can also blend two screen updates. Record at a higher frame rate "
            "if the phone allows it, and don't read missing or odd-looking frames as a fault of the device."
        )
    if len(stats) < 2:
        pass
    elif not events:
        biggest = max((s.change or 0) for s in stats)
        lines.append(
            f"The picture never changed between consecutive frames (largest change {biggest * 100:.2f}% of the {area}, "
            f"threshold {threshold * 100:.1f}%)."
        )
    elif len(events) <= 5:
        firsts = [a for a, _ in _ranges(changed)]
        when = ", ".join(f"{hash_}{i} ({t_of[i]:.3f} s)" for i in firsts)
        lines.append(
            f"The picture changed {'once' if len(events) == 1 else f'{len(events)} times'} "
            f"(at least {threshold * 100:.1f}% of the {area} between frames): at {when}."
        )
    else:
        line = f"Picture updates (at least {threshold * 100:.1f}% of the {area} changing between frames): {len(events)} in {span:.2f} s"
        intervals = [b - a for a, b in zip(events, events[1:])]
        if intervals:
            mi = statistics.median(intervals)
            line += (
                f", median interval {mi * 1000:.1f} ms (about {1 / mi:.1f} updates/s), "
                f"shortest {min(intervals) * 1000:.1f} ms, longest {max(intervals) * 1000:.1f} ms"
            )
        lines.append(line + ".")
    return lines


def table(scan_: Scan, limit: int = 400) -> str:
    stats = scan_.stats
    hash_ = "#" if scan_.exact_index else "~#"
    rows = ["frame    t (ms)   bright  change%"]
    for s in stats[:limit]:
        change = "-" if s.change is None else f"{s.change * 100:.1f}"
        rows.append(f"{hash_}{s.index:<6} {s.t * 1000:8.1f} {s.brightness:7.0f} {change:>8}")
    if len(stats) > limit:
        rows.append(f"... {len(stats) - limit} more frames (narrow the range to see them)")
    return "\n".join(rows)


def contact_sheet(items: list[tuple[str, Image.Image]], tile_width: int = 320, max_cols: int = 4) -> Image.Image:
    """Lay frames out in a labelled grid."""
    cols = min(max_cols, len(items))
    rows = (len(items) + cols - 1) // cols
    first = items[0][1]
    tile_h = max(1, round(first.height * tile_width / first.width))
    label_h = 20
    gap = 4
    sheet = Image.new("RGB", (cols * tile_width + (cols - 1) * gap, rows * (tile_h + label_h) + (rows - 1) * gap), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    for n, (label, im) in enumerate(items):
        x = (n % cols) * (tile_width + gap)
        y = (n // cols) * (tile_h + label_h + gap)
        tile = im.copy()
        tile.thumbnail((tile_width, tile_h), Image.Resampling.LANCZOS)
        sheet.paste(tile, (x, y + label_h))
        draw.text((x + 4, y + 2), label, fill=(240, 238, 230), font=font)
    return sheet


def even_picks(stats: list[FrameStat], count: int, step: int | None) -> list[int]:
    indices = [s.index for s in stats]
    if step:
        return indices[::step][:count]
    if len(indices) <= count:
        return indices
    if count == 1:
        return [indices[0]]
    return [indices[round(i * (len(indices) - 1) / (count - 1))] for i in range(count)]
