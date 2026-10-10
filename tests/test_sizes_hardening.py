"""sizes.size_of on hostile headers: bounded walks, and the OS/2 BMP header."""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sizes

MB = 1024 * 1024


def run(argv):
    subprocess.run(argv, check=True, capture_output=True, timeout=60)


class Hardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def timed(self, path):
        t = time.perf_counter()
        found = sizes.size_of(path)
        return found, time.perf_counter() - t

    def test_mkv_of_tiny_voids_gives_up_fast(self):
        # EBML header, then a Segment of unknown size full of 2-byte Void elements
        path = self.dir / "voids.mkv"
        with open(path, "wb") as f:
            f.write(b"\x1a\x45\xdf\xa3\x80")
            f.write(b"\x18\x53\x80\x67\x01\xff\xff\xff\xff\xff\xff\xff")
            f.write(b"\xec\x80" * (25 * MB))
        found, took = self.timed(path)
        self.assertIsNone(found)
        self.assertLess(took, 0.2)

    def test_mp4_of_tiny_boxes_gives_up_fast(self):
        path = self.dir / "boxes.mp4"
        with open(path, "wb") as f:
            f.write(struct.pack(">I4s4sI", 16, b"ftyp", b"isom", 0))
            f.write(b"\x00\x00\x00\x08free" * (50 * MB // 8))
        found, took = self.timed(path)
        self.assertIsNone(found)
        self.assertLess(took, 0.2)

    def test_real_videos_still_read(self):
        for name, extra in (("a.mkv", []), ("a.webm", ["-c:v", "libvpx-vp9"]),
                            ("tail.mp4", []), ("fast.mp4", ["-movflags", "+faststart"])):
            with self.subTest(name):
                path = self.dir / name
                run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1",
                     *extra, str(path)])
                self.assertEqual(sizes.size_of(path), (320, 240))

    def test_mp4_moov_position(self):
        # The two layouts above must really differ, or the moov-at-end case isn't covered
        for name, extra, moov_last in (("tail.mp4", [], True),
                                       ("fast.mp4", ["-movflags", "+faststart"], False)):
            path = self.dir / name
            run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1",
                 *extra, str(path)])
            data = path.read_bytes()
            self.assertEqual(data.find(b"moov") > data.find(b"mdat"), moov_last, name)

    def test_os2_bmp(self):
        path = self.dir / "os2.bmp"
        core = struct.pack("<IHHHH", 12, 300, 200, 1, 24)
        pixels = b"\x00" * (((300 * 3 + 3) & ~3) * 200)
        path.write_bytes(b"BM" + struct.pack("<IHHI", 26 + len(pixels), 0, 0, 26) + core + pixels)
        self.assertEqual(sizes.size_of(path), (300, 200))

    def test_windows_bmp(self):
        path = self.dir / "x.bmp"
        run(["magick", "-size", "300x200", "xc:red", str(path)])
        self.assertEqual(sizes.size_of(path), (300, 200))


if __name__ == "__main__":
    unittest.main()
