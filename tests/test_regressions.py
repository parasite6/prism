"""Integration regressions, using disposable inputs and system conversion tools."""
import ast
import concurrent.futures
import importlib.util
import json
import hashlib
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import convert


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          timeout=45, **kwargs).stdout


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="prism-test-")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        # Old code shares the desktop profile. Keep even pre-fix tests isolated.
        original = convert._run
        def isolated(cmd, **kwargs):
            if cmd[0] == "soffice":
                cmd = ["/usr/bin/soffice",
                       f"-env:UserInstallation={(self.folder / 'profile').as_uri()}",
                       *cmd[1:]]
            return original(cmd, **kwargs)
        self.isolation = patch.object(convert, "_run", isolated)
        self.isolation.start()
        self.addCleanup(self.isolation.stop)

    def fixture(self, name, text):
        p = self.folder / name
        p.write_text(text)
        return p

    def picture(self, name="picture.png"):
        p = self.folder / name
        run(["magick", "-size", "32x32", "xc:red", str(p)])
        return p

    def pdf(self):
        text = self.fixture("source.txt", "An editable review document.\n")
        run(["/usr/bin/soffice", f"-env:UserInstallation={(self.folder / 'pdf-profile').as_uri()}",
             "--headless", "--convert-to", "pdf", "--outdir", str(self.folder), str(text)])
        return self.folder / "source.pdf"

    def test_pdf_page_rendering_keeps_existing_files(self):
        src = self.pdf()
        old = self.folder / "source.png"
        old.write_bytes(b"keep this existing file")
        self.assertIn("png", convert.targets_for([str(src)]))
        out = Path(convert.convert(str(src), "png"))
        self.assertEqual(old.read_bytes(), b"keep this existing file")
        self.assertNotEqual(out, old)
        self.assertTrue(out.read_bytes().startswith(b"\x89PNG"))

    def test_pdf_markdown_bundle_preserves_text_and_image_links(self):
        image = self.picture()
        src = self.folder / "pictured.pdf"
        code = ("import pymupdf,sys; d=pymupdf.open(); p=d.new_page(); "
                "p.insert_text((72,72),'Preserved document text.'); "
                "p.insert_image(pymupdf.Rect(72,100,250,278),filename=sys.argv[1]); "
                "d.save(sys.argv[2])")
        run([str(convert._pdf_python()), "-c", code, str(image), str(src)])
        out = Path(convert.convert(str(src), "md"))
        self.assertTrue(out.is_dir())
        markdown = next(out.glob("*.md")).read_text()
        self.assertIn("Preserved document text.", markdown)
        import re
        refs = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', markdown)
        self.assertTrue(refs)
        self.assertTrue(all((out / r).is_file() for r in refs))

    def test_tiff_pages_do_not_overwrite_and_return_real_output(self):
        src = self.folder / "pages.tiff"
        run(["magick", "-size", "16x16", "xc:red", "xc:blue", str(src)])
        old = self.folder / "pages-0.png"
        old.write_bytes(b"old page")
        out = Path(convert.convert(str(src), "png"))
        self.assertEqual(old.read_bytes(), b"old page")
        self.assertTrue(out.exists())

    def test_gif_webp_preserves_both_frames(self):
        src = self.folder / "animation.gif"
        run(["magick", "-delay", "20", "-size", "16x16", "xc:red", "xc:blue",
             "-loop", "0", str(src)])
        out = convert.convert(str(src), "webp")
        self.assertEqual(run(["magick", "identify", "-format", "%n ", out]).split(), ["2", "2"])

    def test_animated_gif_to_png_keeps_full_first_frame(self):
        first = self.folder / "first.png"; second = self.folder / "second.png"
        run(["magick", "-size", "32x32", "xc:white", "-fill", "red", "-draw", "rectangle 0,0 10,10", str(first)])
        run(["magick", "-size", "32x32", "xc:white", "-fill", "blue", "-draw", "rectangle 20,20 30,30", str(second)])
        src = self.folder / "optimized.gif"
        run(["magick", "-delay", "20", str(first), str(second), "-layers", "Optimize", str(src)])
        out = convert.convert(str(src), "png")
        self.assertTrue(Path(out).is_file())
        self.assertEqual(run(["magick", "identify", "-format", "%wx%h", out]), "32x32")
        self.assertTrue(any("first frame" in w for w in out.warnings))

    def test_docx_html_has_working_embedded_image(self):
        image = self.picture()
        md = self.fixture("document.md", f"# Heading\n\n![picture]({image})\n")
        docx = self.folder / "document.docx"
        run(["pandoc", str(md), "-o", str(docx)])
        out = Path(convert.convert(str(docx), "html"))
        if out.is_dir():
            out = next(out.glob("*.html"))
        import re
        refs = re.findall(r'<img[^>]*src="([^"]+)"', out.read_text())
        self.assertTrue(refs)
        self.assertTrue(all(r.startswith("data:image/") or (out.parent / r).is_file() for r in refs))

    def test_docx_markdown_bundle_has_working_extracted_images(self):
        image = self.picture()
        md = self.fixture("pictured.md", f"# Heading\n\n![picture]({image})\n")
        docx = self.folder / "pictured.docx"
        run(["pandoc", str(md), "-o", str(docx)])
        out = Path(convert.convert(str(docx), "md"))
        self.assertTrue(out.is_dir())
        text = next(out.glob("*.md")).read_text()
        import re
        refs = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', text) + re.findall(r'<img[^>]*src="([^"]+)"', text)
        self.assertTrue(refs)
        self.assertTrue(all((out / r).is_file() for r in refs))

    def test_plain_text_content_is_not_parsed_as_markup(self):
        src = self.fixture("literal.txt", "Keep *literal stars* and <angle brackets>\n")
        out = convert.convert(str(src), "docx")
        with zipfile.ZipFile(out) as z:
            xml = ET.fromstring(z.read("word/document.xml"))
        text = "".join(n.text or "" for n in xml.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        self.assertIn("Keep *literal stars* and <angle brackets>", text)

    def test_epub_pdf_has_readable_text(self):
        md = self.fixture("book.md", "# Book\n\nPreserved EPUB text.\n")
        epub = self.folder / "book.epub"
        run(["pandoc", str(md), "-o", str(epub)])
        try:
            out = convert.convert(str(epub), "pdf")
        except RuntimeError as exc:
            self.fail(f"Supported EPUB-to-PDF route failed: {exc}")
        self.assertIn("Preserved EPUB text.", run(["/usr/bin/pdftotext", out, "-"]))

    def test_pdf_docx_contains_editable_text(self):
        src = self.pdf()
        try:
            out = convert.convert(str(src), "docx")
        except RuntimeError as exc:
            self.fail(f"Supported PDF-to-DOCX route failed: {exc}")
        with zipfile.ZipFile(out) as z:
            xml = ET.fromstring(z.read("word/document.xml"))
        text = "".join(n.text or "" for n in xml.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        self.assertIn("An editable review document.", text)

    def test_video_keeps_both_audio_tracks(self):
        src = self.folder / "tracks.mov"
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32:r=5",
             "-f", "lavfi", "-i", "sine=frequency=400", "-f", "lavfi", "-i", "sine=frequency=800",
             "-t", "0.4", "-map", "0:v", "-map", "1:a", "-map", "2:a",
             "-c:v", "mpeg4", "-c:a", "aac", str(src)])
        out = convert.convert(str(src), "mkv")
        streams = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", out]))["streams"]
        self.assertEqual(sum(s["codec_type"] == "audio" for s in streams), 2)

    @unittest.skipUnless(len(os.sched_getaffinity(0)) >= 2, "parallel conversion needs multiple CPUs")
    def test_video_gif_parallel_work_preserves_pixels_timing_and_loop(self):
        src = self.folder / "motion.mp4"
        run(["ffmpeg", "-n", "-loglevel", "error", "-f", "lavfi", "-i",
             "testsrc2=size=480x640:rate=30", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", str(src)])
        reference = self.folder / "reference.gif"
        run(["ffmpeg", "-n", "-loglevel", "error", "-i", str(src), "-vf",
             "fps=15,scale=640:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse", str(reference)])
        active = peak = 0
        lock = threading.Lock()
        original = convert._run
        def tracked(cmd, **kwargs):
            nonlocal active, peak
            is_ffmpeg = Path(cmd[0]).name == "ffmpeg"
            if is_ffmpeg:
                with lock:
                    active += 1
                    peak = max(peak, active)
            try:
                return original(cmd, **kwargs)
            finally:
                if is_ffmpeg:
                    with lock:
                        active -= 1
        with patch.object(convert, "_run", tracked):
            out = convert.convert(str(src), "gif")
        def decoded_frames(path):
            text = run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-pix_fmt", "rgba", "-f", "framemd5", "-"])
            return [line for line in text.splitlines() if not line.startswith("#")]
        self.assertEqual(decoded_frames(out), decoded_frames(reference))
        from PIL import Image
        with Image.open(out) as output, Image.open(reference) as expected:
            self.assertEqual(output.n_frames, expected.n_frames)
            self.assertEqual(output.info.get("loop"), expected.info.get("loop"))
            durations = []
            for image in (output, expected):
                total = 0
                for frame in range(image.n_frames):
                    image.seek(frame)
                    total += image.info.get("duration", 0)
                durations.append(total)
            self.assertEqual(durations[0], durations[1])
        self.assertGreater(peak, 1, "Large GIF conversion still runs frame processing serially")

    def test_concurrent_conversions_keep_source_and_distinct_outputs(self):
        src = self.picture()
        before = hashlib.sha256(src.read_bytes()).digest()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(lambda _: convert.convert(str(src), "jpg"), range(8)))
        self.assertEqual(len(set(outputs)), 8)
        self.assertTrue(all(Path(p).is_file() for p in outputs))
        self.assertEqual(hashlib.sha256(src.read_bytes()).digest(), before)

    def test_failed_converter_leaves_no_final_or_staging_files(self):
        src = self.picture()
        before = src.read_bytes()
        bin_dir = self.folder / "fake-bin"; bin_dir.mkdir()
        executable = bin_dir / "magick"
        executable.write_text("#!/usr/bin/python3\nimport sys\nopen(sys.argv[-1], 'wb').write(b'partial output')\nsys.exit(1)\n")
        executable.chmod(0o755)
        with patch.dict(os.environ, {"PATH": f"{bin_dir}:/usr/bin:/bin"}):
            with self.assertRaises(RuntimeError):
                convert.convert(str(src), "jpg")
        self.assertFalse((self.folder / "picture.jpg").exists())
        self.assertEqual(list(self.folder.glob(".prism-*")), [])
        self.assertEqual(src.read_bytes(), before)

    def test_dangling_output_symlink_is_not_followed_or_replaced(self):
        src = self.picture()
        link = self.folder / "picture.jpg"
        elsewhere = self.folder / "missing-target"
        link.symlink_to(elsewhere)
        out = Path(convert.convert(str(src), "jpg"))
        self.assertTrue(link.is_symlink())
        self.assertFalse(elsewhere.exists())
        self.assertNotEqual(out, link)
        self.assertTrue(out.is_file())

    def test_scan_is_flagged_without_publishing_docx(self):
        src = self.folder / "scan.pdf"
        run(["magick", "-size", "32x32", "xc:white", str(src)])
        with self.assertRaisesRegex(RuntimeError, "OCR"):
            convert.convert(str(src), "docx")
        self.assertFalse((self.folder / "scan.docx").exists())

    def test_markdown_relative_image_survives_pdf_conversion(self):
        image = self.picture()
        src = self.fixture("relative.md", f"# Picture\n\n![image]({image.name})\n")
        out = convert.convert(str(src), "pdf")
        listing = run(["/usr/bin/pdfimages", "-list", out])
        self.assertTrue(any(line.split()[:1] == ["1"] for line in listing.splitlines()))

    def test_flac_to_wav_preserves_24_bit_depth(self):
        src = self.folder / "depth.flac"
        run(["ffmpeg", "-n", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=400",
             "-t", "0.2", "-c:a", "flac", "-sample_fmt", "s32", str(src)])
        out = convert.convert(str(src), "wav")
        info = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", out]))
        self.assertEqual(info["streams"][0]["bits_per_sample"], 24)


def ring_nodes(namespace, *names, prefix=None):
    """Load top-level definitions from ring.py without importing GTK."""
    tree = ast.parse((ROOT / "ring.py").read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))
             and (n.name in names or (prefix and n.name.startswith(prefix)))]
    for n in nodes:
        if isinstance(n, ast.ClassDef):
            n.bases = []
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "<ring.py>", "exec"), namespace)
    return namespace


class RingGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exercise the application's actual geometry without opening GTK windows.
        import cairo
        tree = ast.parse((ROOT / "ring.py").read_text())
        start = next(i for i, n in enumerate(tree.body) if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", "") == "ACCENT" for t in n.targets))
        look = {}
        for n in tree.body[start:]:  # the "# Look" constants block
            if not isinstance(n, ast.Assign):
                break
            exec(compile(ast.Module(body=[n], type_ignores=[]), "<look>", "exec"), look)
        stub = type("Stub", (), {"__getattr__": lambda self, name: self, "__call__": lambda self, *a, **k: self})()
        # Synchronous stand-ins: set_files probes in a thread and hands back via idle_add.
        glib = type("GLib", (), {"idle_add": staticmethod(lambda fn, *a: fn(*a)),
                                 "format_size": staticmethod(lambda n: f"{n} B"), "__getattr__": lambda *a: stub})()
        sync = type("Threading", (), {"Thread": staticmethod(lambda target, daemon=None: type(
            "T", (), {"start": staticmethod(target)})())})()
        namespace = {"math": math, "cairo": cairo, "convert": convert, "os": os, "time": __import__("time"),
                     "GLib": glib, "threading": sync, "thumbnail": lambda f: None, "notify": lambda *a: None,
                     **look}
        cls.Logic = ring_nodes(namespace, "guarded", "Ring", "mix", "centred_text", "pill", "paint_cover",
                               "rounded_rect", "short_name", prefix="ease_")["Ring"]

    def logic(self):
        obj = self.Logic.__new__(self.Logic)
        obj.cx = obj.cy = obj.anchor_x = obj.anchor_y = 5
        obj.px = obj.py = obj.gx = obj.gy = 5
        obj.active = True
        obj.targets = ["png", "jpg", "webp"]
        obj.pop = [0., 0., 0.]
        obj.t_open = obj.t_slices = 1.; obj.t_out = 0.; obj.phase = "idle"
        obj.files = ["sample.png"]
        obj.thumb = None
        obj.size_text, obj.name_text = "2.2 MB", "sample.png"
        obj.hover = -1
        return obj

    def test_outside_ring_is_not_a_target(self):
        obj = self.logic()
        self.assertEqual(obj.slice_at(10000, 5), -1)

    def test_drawing_moves_edge_ring_into_surface(self):
        import cairo
        obj = self.logic()
        obj.draw_ghost = lambda *_: None
        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 600))
        obj.draw(None, cr, 800, 600)
        for i in range(3):
            x, y = obj.slice_center(i)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x, 800)
            self.assertLessEqual(y, 600)

    def test_small_surface_keeps_target_centers_visible(self):
        obj = self.logic()
        obj.place_ring(200, 160)
        for i in range(3):
            x, y = obj.slice_center(i)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x, 200)
            self.assertLessEqual(y, 160)


class RingMenuTests(unittest.TestCase):
    """Main ring: CONVERT / COMPRESS / RESCALE sub rings, then CLEAN."""
    setUpClass = classmethod(RingGeometryTests.setUpClass.__func__)
    logic = RingGeometryTests.logic

    def opened(self, files):
        obj = self.logic()
        obj.targets, obj.phase, obj.from_hold, obj.session, obj.probing = [], "open", True, 1, False
        obj.parent, obj.rot, obj.armed = None, 0.0, True
        obj.set_files([str(f) for f in files])
        return obj

    def make(self, tmp, name, data=b"x"):
        path = Path(tmp) / name
        path.write_bytes(data)
        return path

    def test_image_main_ring_is_groups_then_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = self.make(tmp, "a.png", b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR\0\0\x04\0\0\0\x03\0\x08\x02\0\0\0")
            obj = self.opened([png])
            self.assertEqual(obj.targets, ["convert", "compress", "rescale", "edit", "clean"])
            self.assertEqual(obj.menus["edit"], ["back", "crop", "rotate"])
            self.assertEqual(obj.menus["convert"][0], "back")
            self.assertIn("jpg", obj.menus["convert"])
            self.assertNotIn("png", obj.menus["convert"])
            self.assertNotIn("clean", obj.menus["convert"])

    def test_formats_only_skip_the_extra_ring(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = self.opened([self.make(tmp, "notes.txt")])  # plain text: nothing to clean
            self.assertNotIn("convert", obj.targets)
            self.assertNotIn("back", obj.targets)
            self.assertIn("pdf", obj.targets)
            self.assertEqual(obj.menus, {"main": obj.targets})

    def test_convert_submenu_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = self.opened([self.make(tmp, "doc.docx")])
            self.assertEqual(obj.targets, ["convert", "clean"])
            obj.switch_menu(obj.targets.index("convert"))
            self.assertEqual(obj.targets[0], "back")
            self.assertIn("pdf", obj.targets)
            self.assertFalse(obj.armed)
            obj.switch_menu(0)
            self.assertEqual(obj.targets, ["convert", "clean"])

    def test_late_probe_result_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = self.make(tmp, "a.png")
            obj = self.logic()
            obj.targets, obj.phase, obj.active = [], "open", True
            obj.session = 2  # the ring that asked (session 1) is gone; a new one is open
            obj.probing = True
            obj.apply_files(1, [str(png)], ["jpg", "clean"], 10)
            self.assertEqual(obj.targets, [])
            self.assertTrue(obj.probing)  # still waiting for its own selection

    def test_every_target_reachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.mp4", "a.mp3", "a.docx", "a.xlsx", "a.pptx", "a.pdf", "a.gif", "a.txt"):
                src = self.make(tmp, name)
                obj = self.opened([src])
                reach = {t for menu in obj.menus.values() for t in menu} - {"back"} - set(obj.menus)
                self.assertEqual(reach, set(convert.targets_for([str(src)])), name)


class CropTests(unittest.TestCase):
    """convert.crop (the pixel work) and crop.Box (the window's box maths)."""
    def run_(self, *cmd):
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

    def test_box_is_cut_from_the_upright_image(self):
        if not shutil.which("exiftool"):
            self.skipTest("exiftool needed to tag orientation")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "phone.jpg"
            self.run_("magick", "-size", "300x200", "xc:white", "-fill", "red", "-draw", "rectangle 0,0 49,49", str(src))
            self.run_("exiftool", "-Orientation=6", "-n", "-Artist=CANARY", "-overwrite_original", str(src))
            _, size = convert.crop_preview(src, Path(tmp))
            self.assertEqual(size, (200, 300))  # what the window shows
            out = convert.crop(src, (150, 0, 50, 50), Path(tmp) / "corner.png")  # red is top-right upright
            w, h, red, green = self.run_("magick", out, "-format", "%w %h %[fx:mean.r] %[fx:mean.g]", "info:").split()
            self.assertEqual((w, h), ("50", "50"))
            self.assertGreater(float(red), 0.9)
            self.assertLess(float(green), 0.1)

    def test_replace_keeps_mode_and_strips_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.jpg"
            self.run_("magick", "-size", "120x90", "gradient:red-blue", "-set", "comment", "CANARY", str(src))
            os.chmod(src, 0o640)
            convert.crop(src, (10, 10, 60, 40), src)
            self.assertEqual(oct(src.stat().st_mode & 0o777), "0o640")
            self.assertEqual(self.run_("magick", "identify", "-format", "%w %h", str(src)), "60 40")
            self.assertNotIn(b"CANARY", src.read_bytes())
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["a.jpg"])  # no temp left

    def test_animated_gif_keeps_every_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.gif"
            self.run_("magick", "-delay", "10", "-size", "120x80", "xc:red", "xc:blue", "-loop", "0", str(src))
            out = convert.crop(src, (10, 10, 50, 40), Path(tmp) / "b.gif")
            self.assertEqual(self.run_("magick", "identify", "-format", "%w %h\\n", out).split("\n")[:2],
                             ["50 40", "50 40"])

    def test_refuses_unknown_extension_and_empty_box(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.png"
            self.run_("magick", "-size", "20x20", "xc:red", str(src))
            for box, dest in (((0, 0, 0, 5), "z.png"), ((0, 0, 5, 5), "z.xyz"), ((0, 0, 5, 5), "z")):
                with self.assertRaises(RuntimeError):
                    convert.crop(src, box, Path(tmp) / dest)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["a.png"])

    def test_offered_for_one_still_image_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, doc = Path(tmp) / "a.png", Path(tmp) / "b.png", Path(tmp) / "c.docx"
            for p in (a, b, doc):
                p.write_bytes(b"x")
            self.assertIn("crop", convert.targets_for([str(a)]))
            self.assertNotIn("crop", convert.targets_for([str(a), str(b)]))
            self.assertNotIn("crop", convert.targets_for([str(doc)]))
            apng = Path(tmp) / "anim.png"
            apng.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + bytes(17) + b"\0\0\0\x08acTL" + bytes(12))
            self.assertNotIn("crop", convert.targets_for([str(apng)]))
            self.assertNotIn("rotate", convert.targets_for([str(apng)]))
            self.assertIn("rotate", convert.targets_for([str(a)]))
            self.assertNotIn("rotate", convert.targets_for([str(a), str(b)]))

    def test_rotate_and_flip_after_the_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.png"  # 300x200, red block top-left
            self.run_("magick", "-size", "300x200", "xc:white", "-fill", "red", "-draw", "rectangle 0,0 49,49", str(src))
            def red_corner(out):
                w, h = map(int, self.run_("magick", "identify", "-format", "%w %h", str(out)).split())
                corners = {name: float(self.run_("magick", str(out), "-crop", f"1x1+{x}+{y}", "-format",
                                                 "%[fx:r-g]", "info:"))
                           for name, (x, y) in {"tl": (0, 0), "tr": (w - 1, 0), "bl": (0, h - 1),
                                                "br": (w - 1, h - 1)}.items()}
                return (w, h), max(corners, key=corners.get)
            whole = (0, 0, 300, 200)
            self.assertEqual(red_corner(convert.crop(src, whole, Path(tmp) / "r.png", turns=1)), ((200, 300), "tr"))
            self.assertEqual(red_corner(convert.crop(src, whole, Path(tmp) / "l.png", turns=3)), ((200, 300), "bl"))
            self.assertEqual(red_corner(convert.crop(src, whole, Path(tmp) / "h.png", mirror=True)), ((300, 200), "tr"))
            # Mirror then a half turn is a vertical flip.
            self.assertEqual(red_corner(convert.crop(src, whole, Path(tmp) / "v.png", turns=2, mirror=True)),
                             ((300, 200), "bl"))

    def test_every_frame_of_a_flip_draws(self):
        # Halfway through, the fold is edge-on: a singular matrix cairo refuses.
        import cairo
        import crop
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 300)
        preview = cairo.ImageSurface(cairo.FORMAT_ARGB32, 40, 20)
        for step in (crop.FLIP_H, crop.FLIP_V, crop.TURN_LEFT, crop.TURN_RIGHT):
            for t in [n / 200 for n in range(201)]:
                crop.draw_rotate(cairo.Context(surface), 300, 300, (40, 20), preview, crop.TURN_RIGHT, (step, t))
        with unittest.mock.patch.object(crop, "ease", lambda t: 0.5):  # exactly edge-on
            for step in (crop.FLIP_H, crop.FLIP_V):
                crop.draw_rotate(cairo.Context(surface), 300, 300, (40, 20), preview, crop.IDENTITY, (step, 0.3))

    def test_orientation_maths(self):
        tree = ast.parse((ROOT / "crop.py").read_text())
        keep = {"IDENTITY", "TURN_RIGHT", "TURN_LEFT", "FLIP_H", "FLIP_V", "mul", "turns_mirror"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in keep
                 or isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) in keep]
        ns = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "crop.py", "exec"), ns)
        tm, mul = ns["turns_mirror"], ns["mul"]
        self.assertEqual(tm(ns["IDENTITY"]), (0, False))
        self.assertEqual(tm(ns["TURN_RIGHT"]), (1, False))
        self.assertEqual(tm(ns["TURN_LEFT"]), (3, False))
        self.assertEqual(tm(ns["FLIP_H"]), (0, True))
        self.assertEqual(tm(ns["FLIP_V"]), (2, True))
        # Turn right, then flip horizontally (as on screen): mirror first, then three quarter turns.
        self.assertEqual(tm(mul(ns["FLIP_H"], ns["TURN_RIGHT"])), (3, True))
        m = ns["IDENTITY"]
        for _ in range(4):
            m = mul(ns["TURN_LEFT"], m)
        self.assertEqual(m, ns["IDENTITY"])

    def box(self):
        namespace = {"INSETS": (0, 0, 0, 0), "HANDLE": 14, "ZOOM_PIXEL": 24}
        Box = ring_nodes_from("crop.py", namespace, "Box")["Box"]
        box = Box(400, 200)
        box.layout(400, 200)  # 1:1 view
        return box

    def test_box_grab_and_drag(self):
        box = self.box()
        box.box = [100.0, 50.0, 300.0, 150.0]
        self.assertEqual(box.grab(100, 50), "nw")
        self.assertEqual(box.grab(305, 100), "e")
        self.assertEqual(box.grab(200, 100), "move")
        self.assertEqual(box.grab(10, 10), "new")
        moved = box.drag("move", box.box, (200, 100), (900, 900))  # clamped inside the image
        self.assertEqual(moved, [200.0, 100.0, 400.0, 200.0])
        self.assertEqual(box.drag("w", box.box, (100, 0), (350, 0))[0], 299.0)  # can't pass the other edge
        self.assertEqual(box.drag("new", box.box, (300, 20), (-50, 500)), [0, 20, 300, 200])
        box.box = [0.4, 0.6, 399.7, 200.0]
        self.assertEqual(box.rect(), (0, 1, 400, 199))
        box.box = [0.0, 0.0, 400.0, 200.0]
        self.assertTrue(box.whole())

    def test_view_zoom_keeps_the_point_under_the_pointer(self):
        box = self.box()
        box.box = [100.0, 50.0, 300.0, 150.0]
        before = box.to_image(300, 50)
        box.zoom_at(4.0, 300, 50)
        self.assertEqual(box.s, 4.0)
        for got, want in zip(box.to_image(300, 50), before):
            self.assertAlmostEqual(got, want)
        self.assertEqual(box.box, [100.0, 50.0, 300.0, 150.0])  # the crop is untouched
        box.pan(10000, 10000)  # can't pan past the image's edge
        self.assertAlmostEqual(box.ox, 0)
        self.assertAlmostEqual(box.oy, 0)
        box.zoom_at(0.1, 0, 0)  # never smaller than "whole image fits"
        self.assertEqual((box.zoom, box.s, box.ox, box.oy), (1.0, 1.0, 0, 0))


def ring_nodes_from(filename, namespace, *names):
    tree = ast.parse((ROOT / filename).read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


class ApplicationTests(unittest.TestCase):
    def test_repeat_activation_retains_one_ring_and_original_selection(self):
        class Window:
            def __init__(self, app):
                self.files = list(app.files)
        stub = type("Stub", (), {"__getattr__": lambda self, name: self, "__call__": lambda self, *a, **k: self})()
        namespace = ring_nodes({"Ring": Window, "GLib": stub, "signal": signal, "shutdown": None,
                                "convert": stub, "threading": stub, "ButtonWatch": lambda cb: None},
                               "guarded", "App")
        app = namespace["App"].__new__(namespace["App"])
        app.hold = app.ensure_channel = lambda: None
        app.files = ["original.png"]; app.window = None
        app.do_activate()
        first = app.window
        app.files = ["other.png"]
        app.do_activate()
        self.assertIs(app.window, first)
        self.assertEqual(app.window.files, ["original.png"])

    def parse_selection(self):
        from urllib.parse import unquote_to_bytes, urlsplit
        namespace = {"os": os, "unquote_to_bytes": unquote_to_bytes, "urlsplit": urlsplit}
        return ring_nodes(namespace, "parse_selection")["parse_selection"]

    def test_nautilus_uri_handles_newline_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a\nb.png"; src.write_bytes(b"input")
            self.assertEqual(self.parse_selection()(src.as_uri() + "\n"), [str(src)])

    def test_nautilus_uri_handles_non_utf8_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / os.fsdecode(b"picture-\xff.png")
            src.write_bytes(b"input")
            self.assertEqual(self.parse_selection()(src.as_uri() + "\n"), [str(src)])


class CleanTests(unittest.TestCase):
    """The "clean" target: same format, metadata gone, content untouched."""
    SECRET = "ZZSECRETZZ"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="prism-clean-")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def tagged_jpeg(self, name="photo.jpg"):
        p = self.folder / name
        run(["magick", "-size", "64x48", "gradient:red-blue", str(p)])
        run(["exiftool", "-q", "-overwrite_original", f"-Artist={self.SECRET}", "-GPSLatitude=40.7",
             "-GPSLatitudeRef=N", f"-XMP-dc:Creator={self.SECRET}", f"-Comment={self.SECRET}", str(p)])
        return p

    @unittest.skipUnless(shutil.which("exiftool"), "needs exiftool to write fixtures")
    def test_jpeg_metadata_and_appended_data_removed_pixels_identical(self):
        src = self.tagged_jpeg()
        inner = self.tagged_jpeg("inner.jpg")
        with open(src, "ab") as f:  # what Motion Photos / MPF depth maps look like
            f.write(inner.read_bytes() + b"trailer " + self.SECRET.encode())
        out = convert.convert(str(src), "clean")
        self.assertEqual(Path(out).name, "photo (clean).jpg")
        self.assertNotIn(self.SECRET.encode(), Path(out).read_bytes())
        self.assertEqual(run(["magick", "compare", "-metric", "AE", str(src), out, "null:"]).strip() or "0", "0")

    def test_truncated_jpeg_is_not_published(self):
        src = self.folder / "broken.jpg"
        src.write_bytes(b"\xff\xd8\xff\xe1\x00\x40Exif\x00\x00" + self.SECRET.encode())
        with self.assertRaises(RuntimeError):
            convert.convert(str(src), "clean")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["broken.jpg"])

    def test_markdown_keeps_content_and_drops_front_matter(self):
        src = self.folder / "notes.md"
        src.write_text(f"\ufeff---\nauthor: {self.SECRET}\n---\n\nText [c]{{.comment-start id=\"0\" "
                       f"author=\"{self.SECRET}\"}}x\n\n```js\nconst o = {{date=\"keep\"}}\n```\n", encoding="utf-8")
        text = Path(convert.convert(str(src), "clean")).read_text(encoding="utf-8")
        self.assertNotIn(self.SECRET, text)
        self.assertIn('{date="keep"}', text)
        rule = self.folder / "rule.md"
        rule.write_text("---\nIntro paragraph.\n\n---\n\nMore.\n")
        self.assertIn("Intro paragraph.", Path(convert.convert(str(rule), "clean")).read_text())

    def test_docx_template_path_and_variables_removed(self):
        md = self.folder / "d.md"; md.write_text("Body text.\n")
        plain = self.folder / "plain.docx"
        run(["pandoc", str(md), "-o", str(plain)])
        src = self.folder / "d.docx"
        rel = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.'
               'openxmlformats.org/package/2006/relationships"><Relationship Id="rIdT" Type="http://schemas.openxml'
               'formats.org/officeDocument/2006/relationships/attachedTemplate" Target="file:///C:/Users/'
               f'{self.SECRET}/Normal.dotm" TargetMode="External"/></Relationships>')
        with zipfile.ZipFile(plain) as a, zipfile.ZipFile(src, "w") as b:
            for item in a.infolist():
                data = a.read(item.filename)
                if item.filename == "word/settings.xml":
                    data = data.replace(b"</w:settings>", b'<w:docVars><w:docVar w:name="k" w:val="'
                                        + self.SECRET.encode() + b'"/></w:docVars></w:settings>')
                b.writestr(item, data)
            b.writestr("word/_rels/settings.xml.rels", rel)
        out = convert.convert(str(src), "clean")
        with zipfile.ZipFile(out) as z:
            for name in z.namelist():
                self.assertNotIn(self.SECRET.encode(), z.read(name), name)
            self.assertIn(b"Body text.", z.read("word/document.xml"))

    def test_pdf_form_field_names_survive(self):
        src = self.folder / "form.pdf"
        code = ("import pymupdf,sys; d=pymupdf.open(); p=d.new_page(); w=pymupdf.Widget(); w.field_name='first';"
                "w.field_type=pymupdf.PDF_WIDGET_TYPE_TEXT; w.rect=pymupdf.Rect(72,72,200,90); p.add_widget(w);"
                "wx=[x for x in range(1,d.xref_length()) if d.xref_get_key(x,'Subtype')==('name','/Widget')][0];"
                "par=d.get_new_xref(); d.update_object(par,f'<< /T (person) /Kids [{wx} 0 R] >>');"
                "d.xref_set_key(wx,'Parent',f'{par} 0 R'); c=d.pdf_catalog(); a=d.xref_get_key(c,'AcroForm');"
                "(d.xref_set_key(int(a[1].split()[0]),'Fields',f'[{par} 0 R]') if a[0]=='xref' "
                "else d.xref_set_key(c,'AcroForm/Fields',f'[{par} 0 R]'));"
                "d.set_metadata({'author':'ZZSECRETZZ'}); d.save(sys.argv[1])")
        python = str(convert._pdf_python())
        run([python, "-c", code, str(src)])
        out = convert.convert(str(src), "clean")
        names = run([python, "-c", "import pymupdf,sys; print([w.field_name for p in pymupdf.open(sys.argv[1]) "
                     "for w in p.widgets()])", out])
        self.assertIn("person.first", names)
        self.assertNotIn(self.SECRET.encode(), Path(out).read_bytes())

    def test_video_clean_keeps_track_language(self):
        src = self.folder / "clip.mkv"
        run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32:r=5", "-f", "lavfi",
             "-i", "sine", "-t", "0.4", "-c:v", "mpeg4", "-c:a", "flac", "-metadata", f"title={self.SECRET}",
             "-metadata:s:a:0", "language=fra", str(src)])
        out = convert.convert(str(src), "clean")
        info = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", out]))
        self.assertNotIn(self.SECRET, json.dumps(info))
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual(audio["tags"]["language"], "fra")

    def test_scrubbers(self):
        import scrub
        svg = scrub.strip_svg(f"<svg sodipodi:docname='{self.SECRET}' inkscape:export-filename=\"{self.SECRET}\"/>")
        self.assertNotIn(self.SECRET, svg)
        html = self.folder / "p.html"
        html.write_bytes(f"<head><meta content='{self.SECRET}' name='author'><meta charset=\"utf-8\">"
                         f"<!-- saved from url=(0020){self.SECRET} --></head>".encode())
        scrub.strip_html(html)
        self.assertNotIn(self.SECRET, html.read_text())
        self.assertIn("charset", html.read_text())
        rtf = scrub.strip_rtf(r"{\rtf1{\*\template C:\\Users\\" + self.SECRET + r"\\N.dotm}"
                              r"{\*\atnauthor " + self.SECRET + r"}body}")
        self.assertNotIn(self.SECRET, rtf)
        self.assertIn("body", rtf)
        # EMF images (docx -> md extracts them) are scrubbed, not refused.
        media = self.folder / "media"; media.mkdir()
        (media / "chart.emf").write_bytes(b"\x01\x00\x00\x00" + bytes(36) + b" EMF" + bytes(48))
        scrub.scrub_tree(media, lambda p: None)


class HostileInputTests(unittest.TestCase):
    """Files to convert come from anywhere: they must not read other files or URLs."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="prism-hostile-")
        root = Path(self.tmp.name)
        self.outside = root / "private"; self.outside.mkdir()
        self.folder = root / "downloads"; self.folder.mkdir()
        subprocess.run(["magick", "-size", "13x7", "xc:#123456", "-comment", "SECRETPIXELS",
                        str(self.outside / "secret.png")], check=True)
        self.secret = (self.outside / "secret.png").read_bytes()
        subprocess.run(["magick", "-size", "8x8", "xc:#ff8800", str(self.folder / "ok.png")], check=True)
        (self.folder / "link.png").symlink_to(self.outside / "secret.png")

    def tearDown(self):
        self.tmp.cleanup()

    def leaked(self, out):
        """The secret (13x7 px) in the output: embedded as data: or copied as a file."""
        import base64
        data = Path(out).read_bytes()
        thirteen_by_seven = b"IHDR\x00\x00\x00\x0d\x00\x00\x00\x07"
        return any(x in data for x in (base64.b64encode(self.secret), base64.b64encode(thirteen_by_seven)[:16],
                                       thirteen_by_seven, b"SECRETPIXELS"))

    def test_markdown_cannot_embed_files_outside_its_folder(self):
        src = self.folder / "doc.md"
        src.write_text(f"# Hi\n\n![abs]({self.outside}/secret.png)\n\n![up](../private/secret.png)\n\n"
                       "![sym](link.png)\n\n"
                       f'<img src="{self.outside}/secret.png">\n\n'
                       f'<div style="background:url({self.outside}/secret.png)">styled</div>\n\n'
                       "![fine](ok.png)\n")
        out = convert.convert(str(src), "html")
        html_out = Path(out).read_text()
        self.assertFalse(self.leaked(out))
        self.assertIn("data:image/png;base64", html_out)  # ok.png, inside the folder, still embedded
        self.assertIn("abs", html_out)  # dropped images keep their alt text

    def test_html_cannot_embed_files_outside_its_folder(self):
        src = self.folder / "page.html"
        src.write_text(f'<html><body><p>Hello</p><img src="{self.outside}/secret.png" alt="a">'
                       '<img src="../private/secret.png" alt="b"><img src="link.png" alt="c">'
                       f'<p style="background:url(file://{self.outside}/secret.png)">x</p>'
                       '<img src="ok.png" alt="fine"></body></html>')
        for ext in ("md",):
            out = Path(convert.convert(str(src), ext))
            files = [out] if out.is_file() else [p for p in out.rglob("*") if p.is_file()]
            self.assertFalse(any(self.leaked(f) for f in files), ext)

    def test_markdown_does_not_fetch_urls(self):
        src = self.folder / "web.md"
        src.write_text("![web](http://127.0.0.1:9/x.png) and text\n")
        html_out = Path(convert.convert(str(src), "html")).read_text()  # no fetch, so no failure
        self.assertNotIn("127.0.0.1", html_out)
        self.assertIn("and text", html_out)

    # A .docx whose only image is an external link (LibreOffice resolves those).
    CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
    <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
    <Default Extension="xml" ContentType="application/xml"/>
    <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
    </Types>"""

    RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
    <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
    </Relationships>"""

    DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
    <Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="TARGET" TargetMode="External"/>
    </Relationships>"""

    DOC = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
     xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
     xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
     xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
     xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
    <w:body>
    <w:p><w:r><w:t>Hello, harmless document.</w:t></w:r></w:p>
    <w:p><w:r><w:drawing>
    <wp:inline distT="0" distB="0" distL="0" distR="0">
    <wp:extent cx="1828800" cy="914400"/>
    <wp:docPr id="1" name="Picture 1"/>
    <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
    <pic:pic>
    <pic:nvPicPr><pic:cNvPr id="1" name="secret.png"/><pic:cNvPicPr/></pic:nvPicPr>
    <pic:blipFill><a:blip r:link="rId9"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
    <pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1828800" cy="914400"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
    </pic:pic>
    </a:graphicData></a:graphic>
    </wp:inline>
    </w:drawing></w:r></w:p>
    <w:sectPr/>
    </w:body>
    </w:document>"""

    def linked_docx(self, name, target):
        path = self.folder / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", self.CT)
            z.writestr("_rels/.rels", self.RELS)
            z.writestr("word/_rels/document.xml.rels", self.DOC_RELS.replace("TARGET", target))
            z.writestr("word/document.xml", self.DOC)
        return path

    def test_office_documents_cannot_embed_linked_local_files(self):
        for name, target in (("abs.docx", (self.outside / "secret.png").as_uri()), ("rel.docx", "../private/secret.png")):
            out = convert.convert(str(self.linked_docx(name, target)), "pdf")
            listing = subprocess.run(["pdfimages", "-list", out], capture_output=True, text=True).stdout
            self.assertNotIn(" 13     7 ", listing, name)  # the 13x7 secret is not in the PDF

    def test_video_manifest_cannot_read_other_videos(self):
        secret = self.outside / "secret.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=64x48:d=1",
                        "-c:v", "libx264", str(secret)], check=True)
        for base in (secret.as_uri(), str(secret), "../private/secret.mp4"):
            clip = self.folder / "clip.mp4"
            clip.write_text('<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
                            'mediaPresentationDuration="PT1S" minBufferTime="PT1S" '
                            'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011"><Period><AdaptationSet '
                            'mimeType="video/mp4"><Representation id="1" bandwidth="1000">'
                            f'<BaseURL>{base}</BaseURL><SegmentBase/></Representation></AdaptationSet></Period></MPD>')
            with self.assertRaises(RuntimeError, msg=base):
                convert.convert(str(clip), "webm")

    def test_sandbox_has_no_network_home_or_input_devices(self):
        import socket
        server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(1)
        probe = ("import socket, os\n"
                 f"for host, port in (('127.0.0.1', {server.getsockname()[1]}), ('1.1.1.1', 443)):\n"
                 "    s = socket.socket(); s.settimeout(2)\n"
                 "    try: s.connect((host, port)); print('REACHED', host)\n"
                 "    except OSError: pass\n"
                 f"home = {str(Path.home())!r}\n"
                 "print(sorted(os.listdir(home)) if os.path.isdir(home) else [], os.path.exists('/dev/input'))\n")
        out = subprocess.run(convert._sandbox(["/usr/bin/python3", "-c", probe], None),
                             capture_output=True, text=True).stdout
        server.close()
        visible, devices = out.strip().rsplit(" ", 1)
        self.assertNotIn("REACHED", out)
        self.assertEqual(devices, "False")
        # Only the skeleton bubblewrap makes for the font folders it shares.
        self.assertTrue(set(eval(visible)) <= {".local", ".cache", ".fonts"}, visible)

    def test_renamed_postscript_is_not_run(self):
        src = self.folder / "photo.png"
        src.write_text("%!PS\n/Helvetica findfont 20 scalefont setfont 10 10 moveto (hi) show showpage\n")
        with self.assertRaises(RuntimeError):
            convert.convert(str(src), "jpg")


class BindingTests(unittest.TestCase):
    """binding.py (what opens the ring) and ButtonWatch's hold/release logic."""
    def setUp(self):
        import binding
        self.binding = binding
        self.tmp = tempfile.TemporaryDirectory(prefix="prism-bind-")
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_default_is_forward_and_round_trips(self):
        b = self.binding
        self.assertEqual(b.load(), b.DEFAULT)
        self.assertEqual(b.hypr(b.DEFAULT), "mouse:276")
        chosen = b.make(["ALT", "CTRL"], 30, "A")
        self.assertEqual(chosen["mods"], ["CTRL", "ALT"])  # canonical order
        self.assertEqual(b.hypr(chosen), "CTRL + ALT + code:38")
        self.assertEqual(b.describe(chosen), "Ctrl + Alt + A")
        b.save(chosen)
        self.assertEqual(b.load(), chosen)
        self.assertIn('"hypr": "CTRL + ALT + code:38"', b.path().read_text())  # what hypr-binds.lua reads
        self.assertEqual(b.hypr(b.make(["SHIFT"], 275, "Back")), "SHIFT + mouse:275")

    def test_refuses_unusable_bindings(self):
        b = self.binding
        for mods, code in ((["CTRL"], 29), ([], 272), (["CTRL", "ALT"], 46), ([], 0)):
            with self.assertRaises(ValueError):
                b.make(mods, code, "x")

    def test_broken_file_falls_back_to_forward(self):
        b = self.binding
        b.path().parent.mkdir(parents=True)
        for text in ("{not json", '{"mods": ["CTRL"], "code": 29, "label": "Ctrl"}', '{"code": "x"}'):
            b.path().write_text(text)
            self.assertEqual(b.load(), b.DEFAULT)

    def watch(self, bound):
        import struct as st
        glib = type("GLib", (), {"IO_ERR": 8, "IO_HUP": 16})
        namespace = ring_nodes({"binding": self.binding, "debug": lambda m: None, "os": os, "GLib": glib,
                                "Gio": None, "EV_KEY": 1, "EVENT": st.Struct("llHHi"), "BTN_EXTRA": 276},
                               "guarded", "ButtonWatch")
        Watch = namespace["ButtonWatch"]
        w = Watch.__new__(Watch)
        w.on_change, w.devices, w.held, w.down = [].append, {}, {}, False
        w.changes = []
        w.on_change = w.changes.append
        w.set_binding(bound, rescan=False)
        w.EVENT = namespace["EVENT"]
        return w

    def feed(self, w, path, *events):
        data = b"".join(w.EVENT.pack(0, 0, 1, code, value) for code, value in events)
        with patch("os.read", lambda fd, n: data):
            w.read(0, 0, path)

    def test_modifiers_on_keyboard_trigger_on_mouse(self):
        w = self.watch(self.binding.make(["CTRL"], 276, "Forward"))
        self.feed(w, "mouse", (276, 1), (276, 0))  # Forward alone: not the binding
        self.assertEqual(w.changes, [])
        self.feed(w, "kbd", (97, 1))  # right Ctrl counts as Ctrl
        self.feed(w, "mouse", (276, 1))
        self.feed(w, "kbd", (97, 0))  # letting go of Ctrl first doesn't end the hold
        self.assertEqual(w.changes, [True])
        self.feed(w, "mouse", (276, 0))
        self.assertEqual(w.changes, [True, False])

    def test_key_binding_ignores_repeat(self):
        w = self.watch(self.binding.make(["CTRL", "ALT"], 30, "A"))
        self.feed(w, "kbd", (29, 1), (56, 1), (30, 1), (30, 2), (30, 2), (30, 0))
        self.assertEqual(w.changes, [True, False])
        self.assertEqual(w.codes, {30, 29, 97, 56, 100})  # what the kernel mask lets through


class RegistryTests(unittest.TestCase):
    """The temp-folder registry is shared by the daemon, workers and crop windows."""
    def test_sweep_removes_only_dead_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            live, dead, old = (folder / f".prism-{n}" for n in ("live", "dead", "old"))
            for f in (live, dead, old):
                f.mkdir()
            gone = subprocess.Popen(["true"]); gone.wait()  # a pid that no longer exists
            registry = folder / "prism.jobs"
            registry.write_text(f"{os.getpid()}\t{live}\n{gone.pid}\t{dead}\n{old}\n")  # old format: no pid
            with patch.object(convert, "REGISTRY", registry):
                convert.sweep_leftovers()
            self.assertTrue(live.is_dir())  # e.g. a crop window still saving
            self.assertFalse(dead.exists())
            self.assertFalse(old.exists())
            self.assertEqual(registry.read_text(), f"{os.getpid()}\t{live}\n")

    def test_concurrent_processes_do_not_lose_entries(self):
        with tempfile.TemporaryDirectory() as d:
            registry = Path(d) / "prism.jobs"
            code = ("import sys, convert\nfrom pathlib import Path\nconvert.REGISTRY = Path(sys.argv[1])\n"
                    "for i in range(200):\n"
                    "    with convert._registered(sys.argv[2] + str(i)): pass\n"
                    "with convert._registry() as e: e.append(['1', sys.argv[2] + 'kept'])\n")
            procs = [subprocess.Popen([sys.executable, "-c", code, str(registry), f"/x/{n}-"], cwd=ROOT)
                     for n in range(4)]
            for proc in procs:
                self.assertEqual(proc.wait(), 0)
            self.assertEqual(sorted(registry.read_text().split()),
                             sorted(["1", "1", "1", "1", *(f"/x/{n}-kept" for n in range(4))]))


class InstallerTests(unittest.TestCase):
    def test_installer_uses_xdg_data_directory(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            bin_dir = folder / "bin"; bin_dir.mkdir()
            # Never touch the live session: no Hyprland reload, no killing the running ring.
            for name in ("nautilus", "hyprctl", "pgrep"):
                fake = bin_dir / name
                fake.write_text("#!/bin/sh\nexit 0\n"); fake.chmod(0o755)
            runtime = folder / "run"; runtime.mkdir()
            env = dict(os.environ, HOME=d, XDG_DATA_HOME=str(folder / "data"), XDG_RUNTIME_DIR=str(runtime), PRISM_SKIP_PDF="1",
                       XDG_CONFIG_HOME=str(folder / "config"), PATH=f"{bin_dir}:/usr/bin:/bin")
            run(["bash", str(ROOT / "install.sh")], env=env)
            launcher = folder / "data/nautilus/scripts/Prism"
            self.assertTrue(launcher.is_symlink())
            installed = folder / "data/prism"  # copied there, not run from the repo
            self.assertEqual(launcher.resolve(), installed / "prism")
            self.assertTrue((installed / "ring.py").is_file())
            self.assertFalse((installed / ".git").exists())
            self.assertTrue((folder / "config/nautilus/scripts-accels").is_file())
            desktop = (folder / "data/applications/dev.prism.Bind.desktop").read_text()
            self.assertIn(f'Exec="{installed}/prism-bind"', desktop)


    def test_installer_takes_over_from_the_old_name(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            bin_dir = folder / "bin"; bin_dir.mkdir()
            for name in ("nautilus", "hyprctl", "pgrep"):  # never touch the live session
                fake = bin_dir / name
                fake.write_text("#!/bin/sh\nexit 0\n"); fake.chmod(0o755)
            runtime = folder / "run"; runtime.mkdir()
            scripts = folder / "data/nautilus/scripts"; scripts.mkdir(parents=True)
            (scripts / "Convert").symlink_to("/home/x/.local/share/convert-ring/convert-ring")
            (scripts / "Mine").write_text("#!/bin/sh\n")  # someone else's script stays
            accels = folder / "config/nautilus/scripts-accels"; accels.parent.mkdir(parents=True)
            accels.write_text("<Control><Alt>c Convert\n<Control>F5 Mine\n")
            custom = folder / ".config/hypr/custom.lua"; custom.parent.mkdir(parents=True)
            custom.write_text('hl.config({})\ndofile("/home/x/.local/share/convert-ring/hypr-binds.lua")\n')
            env = dict(os.environ, HOME=d, XDG_DATA_HOME=str(folder / "data"), XDG_RUNTIME_DIR=str(runtime), PRISM_SKIP_PDF="1",
                       XDG_CONFIG_HOME=str(folder / "config"), PATH=f"{bin_dir}:/usr/bin:/bin")
            installed = folder / "data/prism"
            (installed / ".pdf-venv").mkdir(parents=True)
            (installed / ".pdf-venv/keep").write_text("x")
            (installed / "ring.py").write_text("# an older install")
            (installed / "removed-from-repo.py").write_text("x")
            run(["bash", str(ROOT / "install.sh")], env=env)
            run(["bash", str(ROOT / "install.sh")], env=env)  # re-running changes nothing more
            self.assertTrue((installed / ".pdf-venv/keep").is_file())  # never deleted
            self.assertFalse((installed / "removed-from-repo.py").exists())
            self.assertEqual((installed / "ring.py").read_bytes(), (ROOT / "ring.py").read_bytes())
            self.assertEqual(sorted(p.name for p in scripts.iterdir()), ["Mine", "Prism"])
            self.assertEqual(accels.read_text(), "<Control>F5 Mine\n<Control><Alt>c Prism\n")
            self.assertEqual(custom.read_text(), f'hl.config({{}})\ndofile("{folder}/data/prism/hypr-binds.lua")\n')


    def test_installer_refuses_a_folder_that_is_not_prism(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            target = folder / "data/prism"; target.mkdir(parents=True)
            (target / "notes.txt").write_text("someone else's")
            env = dict(os.environ, HOME=d, XDG_DATA_HOME=str(folder / "data"), PRISM_SKIP_PDF="1",
                       XDG_RUNTIME_DIR=d, XDG_CONFIG_HOME=str(folder / "config"))
            result = subprocess.run(["bash", str(ROOT / "install.sh")], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("isn't a Prism install", result.stderr)
            self.assertEqual([p.name for p in target.iterdir()], ["notes.txt"])

if __name__ == "__main__":
    unittest.main()
