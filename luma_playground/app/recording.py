# SPDX-License-Identifier: Apache-2.0
"""Turning the Stage's recorded frames into one video file (WebM).

While it records, the Stage saves PNG frames (0001.png, 0002.png …) into a folder under
`.playground/local/recordings/`. When it stops, they become `<folder>.webm`, made by
GStreamer (`gst-launch-1.0` with vp8enc and webmmux, from gstreamer1-plugins-good) or by
ffmpeg. The frames are removed once the video is safely written. With neither tool, the
frames stay as they are and the caller says so.

No GTK here: this runs on a worker thread and is unit-tested without a display.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

FRAME = "%04d.png"
MAX_FPS = 5


@dataclass
class Result:
    path: Path            # the .webm, or the frames folder when no video could be made
    video: bool
    words: str            # plain words for a person: what happened


def _has_gst_elements(gst_inspect: str | None, *elements: str) -> bool:
    if gst_inspect is None:
        return False
    for element in elements:
        try:
            done = subprocess.run([gst_inspect, "--exists", element], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode != 0:
            return False
    return True


def encoder() -> tuple[str, str] | None:
    """("gstreamer" | "ffmpeg", program path), or None when this computer has neither."""
    launch = shutil.which("gst-launch-1.0")
    if launch and _has_gst_elements(shutil.which("gst-inspect-1.0"), "pngdec", "vp8enc", "webmmux",
                                    "multifilesrc", "videoconvert", "videoscale"):
        return "gstreamer", launch
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return "ffmpeg", ffmpeg
    return None


def png_size(path: Path) -> tuple[int, int] | None:
    """A PNG's width and height from its header."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def frames_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("[0-9][0-9][0-9][0-9].png") if p.is_file())


def frame_rate(frames: int, seconds: float) -> int:
    """Frames per second as recorded (the Stage asks for five a second; a busy page gives fewer)."""
    if frames <= 0 or seconds <= 0:
        return 1
    return max(1, min(MAX_FPS, round(frames / seconds)))


def command(tool: str, program: str, folder: Path, output: Path, fps: int, size: tuple[int, int]) -> list[str]:
    """The argv that turns `folder/0001.png …` into `output` (WebM, VP8)."""
    width, height = (max(2, size[0] - size[0] % 2), max(2, size[1] - size[1] % 2))
    pattern = str(folder / FRAME)
    if tool == "gstreamer":
        return [program, "-q", "multifilesrc", f"location={pattern}", "index=1",
                f"caps=image/png,framerate={fps}/1", "!", "pngdec", "!", "videoconvert", "!", "videoscale", "!",
                f"video/x-raw,width={width},height={height}", "!", "videoconvert", "!",
                "vp8enc", "deadline=1", "cpu-used=8", "target-bitrate=1500000", "!", "webmmux", "!",
                "filesink", f"location={output}"]
    return [program, "-y", "-loglevel", "error", "-framerate", str(fps), "-start_number", "1", "-i", pattern,
            "-vf", f"scale={width}:{height}", "-c:v", "libvpx", "-b:v", "1500k", "-pix_fmt", "yuv420p", str(output)]


def make_video(folder: Path | str, seconds: float, *, tool: tuple[str, str] | None = None,
               timeout: float = 180) -> Result:
    """Turn a recording's frames into `<folder>.webm`. Never raises; the Result says what happened."""
    folder = Path(folder)
    frames = frames_in(folder)
    if not frames:
        return Result(folder, False, "Nothing was recorded.")
    size = png_size(frames[0])
    tool = tool if tool is not None else encoder()
    if tool is None or size is None:
        return Result(folder, False, "Saved as still frames. Making a video needs GStreamer’s good plugins "
                                     "or ffmpeg on this computer.")
    output = folder.with_suffix(".webm")
    partial = folder.with_suffix(".webm.part")
    argv = command(tool[0], tool[1], folder, partial, frame_rate(len(frames), seconds), size)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        ok = done.returncode == 0 and partial.is_file() and partial.stat().st_size > 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        partial.unlink(missing_ok=True)
        return Result(folder, False, "The video couldn’t be made, so the recording is kept as still frames.")
    partial.replace(output)
    shutil.rmtree(folder, ignore_errors=True)
    return Result(output, True, "Saved as a video.")
