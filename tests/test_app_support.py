# SPDX-License-Identifier: Apache-2.0
"""The app's display-free helpers: the dark switch, recordings to video, the installed icon,
the code page's file lists and the sample's history. No GTK display is needed."""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def tiny_png(path: Path, width: int = 6, height: int = 4, shade: int = 0) -> None:
    """A real, small RGB PNG, written without any imaging library."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes([shade, 120, 200]) * width for _ in range(height))
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    path.write_bytes(data)


class DevModeTests(unittest.TestCase):
    def test_scheme_follows_the_switch(self) -> None:
        from luma_playground.app import devmode
        self.assertIsNone(devmode.requested_scheme({}))
        self.assertIsNone(devmode.requested_scheme({"PLAYGROUND_DARK": ""}))
        self.assertEqual(devmode.requested_scheme({"PLAYGROUND_DARK": "1"}), "prefer-dark")
        self.assertEqual(devmode.requested_scheme({"PLAYGROUND_DARK": "yes"}), "prefer-dark")
        self.assertEqual(devmode.requested_scheme({"PLAYGROUND_DARK": "0"}), "prefer-light")

    def test_a_private_memory_backend_is_chosen(self) -> None:
        from luma_playground.app import devmode
        env: dict[str, str] = {"PLAYGROUND_DARK": "1"}
        self.assertEqual(devmode.apply_colour_scheme(env), "prefer-dark")
        self.assertEqual(env["GSETTINGS_BACKEND"], "memory")
        untouched: dict[str, str] = {}
        self.assertIsNone(devmode.apply_colour_scheme(untouched))
        self.assertNotIn("GSETTINGS_BACKEND", untouched)


class RecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "20261009-101500"
        self.folder.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_frame_rate_and_size(self) -> None:
        from luma_playground.app import recording
        self.assertEqual(recording.frame_rate(50, 10), 5)
        self.assertEqual(recording.frame_rate(100, 10), recording.MAX_FPS)
        self.assertEqual(recording.frame_rate(3, 10), 1)
        self.assertEqual(recording.frame_rate(0, 0), 1)
        tiny_png(self.folder / "0001.png", 7, 5)
        self.assertEqual(recording.png_size(self.folder / "0001.png"), (7, 5))
        (self.folder / "0002.png").write_bytes(b"not a png")
        self.assertIsNone(recording.png_size(self.folder / "0002.png"))

    def test_commands_make_webm_with_even_sizes(self) -> None:
        from luma_playground.app import recording
        gst = recording.command("gstreamer", "gst-launch-1.0", self.folder, self.folder.with_suffix(".webm"), 5, (7, 5))
        self.assertIn("vp8enc", gst)
        self.assertIn("webmmux", gst)
        self.assertIn("video/x-raw,width=6,height=4", gst)
        self.assertIn("index=1", gst)
        ffmpeg = recording.command("ffmpeg", "ffmpeg", self.folder, self.folder.with_suffix(".webm"), 4, (8, 6))
        self.assertEqual(ffmpeg[ffmpeg.index("-framerate") + 1], "4")
        self.assertIn("libvpx", ffmpeg)

    def test_without_an_encoder_the_frames_stay(self) -> None:
        from luma_playground.app import recording
        tiny_png(self.folder / "0001.png")
        with mock.patch.object(recording, "encoder", return_value=None):
            result = recording.make_video(self.folder, 1.0)
        self.assertFalse(result.video)
        self.assertEqual(result.path, self.folder)
        self.assertTrue((self.folder / "0001.png").exists())
        self.assertIn("still frames", result.words)

    def test_nothing_recorded(self) -> None:
        from luma_playground.app import recording
        result = recording.make_video(self.folder, 1.0)
        self.assertFalse(result.video)
        self.assertEqual(result.words, "Nothing was recorded.")

    def test_a_failed_encode_keeps_the_frames(self) -> None:
        from luma_playground.app import recording
        tiny_png(self.folder / "0001.png")
        result = recording.make_video(self.folder, 1.0, tool=("ffmpeg", "/nonexistent/ffmpeg"))
        self.assertFalse(result.video)
        self.assertTrue((self.folder / "0001.png").exists())
        self.assertFalse(self.folder.with_suffix(".webm").exists())
        self.assertFalse(self.folder.with_suffix(".webm.part").exists())

    @unittest.skipUnless(shutil.which("gst-launch-1.0") or shutil.which("ffmpeg"), "no video encoder here")
    def test_a_real_video_is_made(self) -> None:
        from luma_playground.app import recording
        tool = recording.encoder()
        if tool is None:
            self.skipTest("GStreamer is here, but not its VP8 and WebM plugins")
        for index in range(1, 7):
            tiny_png(self.folder / f"{index:04d}.png", 32, 24, shade=index * 30)
        result = recording.make_video(self.folder, 2.0, tool=tool)
        self.assertTrue(result.video, result.words)
        self.assertEqual(result.path.suffix, ".webm")
        self.assertEqual(result.path.read_bytes()[:4], b"\x1a\x45\xdf\xa3")   # EBML: a Matroska/WebM file
        self.assertFalse(self.folder.exists())


class IconTests(unittest.TestCase):
    def test_the_app_icon_is_on_the_icon_path(self) -> None:
        source = ROOT / "data" / "org.projectluma.Playground.svg"
        themed = ROOT / "data" / "icons" / "hicolor" / "scalable" / "apps" / "org.projectluma.Playground.svg"
        self.assertTrue(themed.is_file(), "the title row's identity icon is looked up here")
        self.assertEqual(source.read_bytes(), themed.read_bytes(), "keep the two copies of the app icon the same")


class CodePageListTests(unittest.TestCase):
    def test_playground_records_are_not_product_changes(self) -> None:
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from luma_playground.app.sheets import code
        except (ImportError, ValueError, RuntimeError) as error:
            self.skipTest(f"GTK isn't here: {error}")
        self.assertTrue(code._is_record(".playground/threads/a.jsonl"))
        self.assertFalse(code._is_record("src/index.html"))
        self.assertIn("IBM Plex Mono", code.MONO_FAMILIES.split(",")[0])


class MoneyFromDailyReadingsTests(unittest.TestCase):
    def test_daily_snapshots_add_up_to_the_month(self) -> None:
        from luma_playground.core import events, samples, services
        with tempfile.TemporaryDirectory() as tmp:
            project = samples.create_sample_kiln(tmp, git=False, bus=events.EventBus())
            month = services.period_of(services._now())
            latest: dict[tuple[str, str], dict] = {}
            for row in services.snapshots(project.path):
                period = str(row.get("period"))
                if period.startswith(month + "-") and len(period) == 10:
                    key = (row["service_id"], period)
                    if key not in latest or str(row["ts"]) >= str(latest[key]["ts"]):
                        latest[key] = row
            expected = round(sum(float(r.get("cost") or 0) for r in latest.values()), 2)
            summary = services.money_summary(project.path)
            self.assertAlmostEqual(summary["total"]["cost"], expected, places=2)
            self.assertGreater(summary["total"]["cost"], 0)


@unittest.skipUnless(shutil.which("git"), "git isn't here")
class SampleHistoryTests(unittest.TestCase):
    def test_the_waitlist_has_its_own_commit_and_base(self) -> None:
        from luma_playground.core import events, samples
        from luma_playground.core.store import ProjectStore
        with tempfile.TemporaryDirectory() as tmp:
            project = samples.create_sample_kiln(tmp, bus=events.EventBus())
            run = lambda *a: subprocess.run(["git", *a], cwd=project.path, capture_output=True, text=True)  # noqa: E731
            self.assertEqual(len(run("log", "--oneline").stdout.strip().splitlines()), 2)
            self.assertEqual(run("status", "--porcelain").stdout.strip(), "")
            base = ProjectStore(project.path).read_goal(samples.WAITLIST).extra.get("base_commit")
            self.assertTrue(base)
            changed = run("diff", "--name-only", base).stdout.split()
            self.assertIn("src/waitlist.js", changed)
            self.assertIn("src/kiln.js", changed)
            thread = (Path(project.path) / ".playground" / "threads" / f"{samples.TEXTS}.jsonl").read_text()
            self.assertIn("#13", thread)


if __name__ == "__main__":
    unittest.main()
