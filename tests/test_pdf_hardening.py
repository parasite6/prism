"""PDF scrub/convert hardening: EXIF in filtered JPEG streams, dropped pdf2docx pages."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import convert

CANARY = b"PRISM-EXIF-CANARY-31337"

try:
    PY = str(convert._pdf_python())
except RuntimeError:
    PY = None

# argv: out.pdf jpeg mode. Draws the JPEG over the page, then rewrites its stream/filter.
BUILD = r"""
import sys, zlib, pymupdf
out, jpeg, mode = sys.argv[1], open(sys.argv[2], "rb").read(), sys.argv[3]
doc = pymupdf.open()
doc.new_page().insert_image(pymupdf.Rect(0, 0, 100, 100), stream=jpeg)
xref = next(x for x in range(1, doc.xref_length()) if doc.xref_get_key(x, "Subtype") == ("name", "/Image"))
data, flt = {"name": (jpeg, "/DCTDecode"), "array": (jpeg, "[/DCTDecode]"),
             "chain": (zlib.compress(jpeg), "[/FlateDecode /DCTDecode]"),
             "garbage": (b"not a jpeg at all", "/DCTDecode"),
             "garbage-chain": (zlib.compress(b"not a jpeg at all"), "[/FlateDecode /DCTDecode]")}[mode]
doc.update_stream(xref, data, compress=0)
doc.xref_set_key(xref, "Filter", flt)
doc.save(out)
"""

# argv: file.pdf canary. Looks in the file, every raw/inflated/decoded stream; renders the page.
INSPECT = r"""
import json, sys, zlib, pymupdf
path, canary = sys.argv[1], sys.argv[2].encode()
blobs = [open(path, "rb").read()]
doc = pymupdf.open(path)
for x in range(1, doc.xref_length()):
    if not doc.is_stream(x):
        continue
    raw = doc.xref_stream_raw(x)
    blobs.append(raw)
    try:
        blobs.append(zlib.decompress(raw))
    except zlib.error:
        pass
    try:
        blobs.append(doc.xref_stream(x))
    except Exception:
        pass
print(json.dumps({"canary": any(canary in b for b in blobs if b),
                  "pixel": doc[0].get_pixmap().pixel(50, 50),
                  "filters": [doc.xref_get_key(x, "Filter")[1] for x in range(1, doc.xref_length())
                              if doc.xref_get_key(x, "Subtype") == ("name", "/Image")]}))
"""

# argv: src.pdf out.docx. Makes pdf2docx fail on page 2, runs pdf_convert.py as a script.
PAGE_ERROR = r"""
import os, runpy, sys
sys.path.insert(0, sys.argv[1])
from pdf2docx.page.Page import Page
parse = Page.parse
def broken(self, **kwargs):
    if self.id == 1:
        raise ValueError("boom")
    return parse(self, **kwargs)
Page.parse = broken
script = os.path.join(sys.argv[1], "pdf_convert.py")
sys.argv = [script] + sys.argv[2:]
runpy.run_path(script, run_name="__main__")
"""


@unittest.skipUnless(PY, "PDF venv not installed")
class PdfHardeningTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="prism-pdf-test-")
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def py(self, script, *args, check=True):
        return subprocess.run([PY, "-c", script, *map(str, args)], capture_output=True, text=True,
                              timeout=120, check=check, cwd=self.folder)

    def jpeg(self):
        magick = shutil.which("magick") or shutil.which("convert")
        if not magick:
            self.skipTest("ImageMagick not installed")
        plain = self.folder / "plain.jpg"
        subprocess.run([magick, "-size", "32x32", "xc:red", str(plain)], check=True, capture_output=True)
        data = plain.read_bytes()
        app1 = b"Exif\0\0" + CANARY
        path = self.folder / "exif.jpg"
        path.write_bytes(data[:2] + b"\xff\xe1" + (len(app1) + 2).to_bytes(2, "big") + app1 + data[2:])
        return path

    def build(self, mode):
        pdf = self.folder / f"{mode}.pdf"
        self.py(BUILD, pdf, self.jpeg(), mode)
        return pdf

    def inspect(self, pdf):
        return json.loads(self.py(INSPECT, pdf, CANARY.decode()).stdout.strip().splitlines()[-1])

    def scrub(self, pdf):
        return subprocess.run([PY, str(ROOT / "pdf_convert.py"), "--scrub", str(pdf)],
                              capture_output=True, text=True, timeout=120)

    def assert_red(self, pixel):
        r, g, b = pixel
        self.assertTrue(r > 200 and g < 60 and b < 60, pixel)

    def test_scrub_strips_exif_under_every_dct_filter_form(self):
        for mode in ("name", "array", "chain"):
            with self.subTest(mode):
                pdf = self.build(mode)
                before = self.inspect(pdf)
                self.assertTrue(before["canary"], "fixture lost its EXIF canary")
                self.assert_red(before["pixel"])
                result = self.scrub(pdf)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(CANARY, pdf.read_bytes())
                after = self.inspect(pdf)
                self.assertFalse(after["canary"])
                self.assert_red(after["pixel"])
                self.assertEqual(after["filters"], ["/DCTDecode"])

    def test_scrub_fails_closed_on_unreadable_jpeg(self):
        for mode in ("garbage", "garbage-chain"):
            with self.subTest(mode):
                pdf = self.build(mode)
                original = pdf.read_bytes()
                result = self.scrub(pdf)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.strip().splitlines()[-1],
                                 "An image in this PDF could not be cleaned; output was not saved")
                self.assertEqual(pdf.read_bytes(), original)
                self.assertFalse((self.folder / f"{mode}.pdf.scrub").exists())

    def test_docx_fails_when_a_page_fails(self):
        src = self.folder / "pages.pdf"
        self.py("import sys, pymupdf\ndoc = pymupdf.open()\n"
                "for n in range(3): doc.new_page().insert_text((72, 72), f'page {n + 1}')\n"
                "doc.save(sys.argv[1])", src)
        out = self.folder / "out.docx"
        result = self.py(PAGE_ERROR, ROOT, src, out, check=False)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.strip().splitlines()[-1],
                         "Page 2 could not be converted; nothing was saved")
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
