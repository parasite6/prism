"""GIF parallel cache stays within the size checked from the header duration."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import convert
import gif_convert


def frames(path):
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout)
    return int(info["streams"][0]["nb_read_frames"])


class CacheCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="prism-gif-")
        cls.root = Path(cls.tmp.name)
        cls.src = cls.root / "clip.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=640x360:rate=15:duration=20", "-c:v", "libx264",
                        "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(cls.src)],
                       check=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def convert(self, name, duration):
        folder = self.root / name / "out"
        folder.mkdir(parents=True)
        out = folder / "clip.gif"
        stream = {"index": 0, "width": 640, "height": 360, "pix_fmt": "yuv420p",
                  "duration": str(duration)}
        calls, peak = [], [0]
        def run(cmd, **kwargs):
            calls.append(cmd)
            # Like convert.convert's job folder, in whichever thread runs this.
            convert._job.work = self.root / name / "work"
            result = convert._run(cmd, **kwargs)
            for cache in (self.root / name).glob("gif-work-*/frames.nut"):
                peak[0] = max(peak[0], cache.stat().st_size)
            return result
        with patch.object(gif_convert.os, "sched_getaffinity", return_value={0, 1, 2, 3}):
            workers, cap, cache_bytes = gif_convert._worker_count(stream, out)
            self.assertGreater(workers, 1)
            try:
                gif_convert.convert_gif(self.src, out, stream, run)
            finally:
                convert._job.work = None
        self.assertLessEqual(peak[0], cache_bytes + 1024 ** 2)
        return out, calls, cap

    def test_understated_duration_falls_back_to_full_single_pass(self):
        out, calls, cap = self.convert("lying", 8)
        self.assertLess(cap, 300)
        self.assertTrue(any("frames.nut" in arg for cmd in calls for arg in cmd))
        self.assertFalse(any(arg.startswith("part-") or "/part-" in arg for cmd in calls for arg in cmd))
        self.assertEqual(frames(out), 300)
        self.assertEqual(list((self.root / "lying").glob("gif-work-*")), [])

    def test_honest_duration_still_goes_parallel(self):
        out, calls, _ = self.convert("honest", 20)
        self.assertTrue(any("/part-" in arg for cmd in calls for arg in cmd))
        self.assertEqual(frames(out), 300)


if __name__ == "__main__":
    unittest.main()
