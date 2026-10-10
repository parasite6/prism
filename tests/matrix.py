"""Every input type x every target the ring offers for it; each output is opened
for real (decoded/probed), resizes are checked for their new pixel size.

    TMPDIR=/some/scratch /usr/bin/python3 tests/matrix.py

Fixtures and outputs go in a fresh folder under TMPDIR (kept for inspection)."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import convert as c  # noqa: E402

ROOT = Path(tempfile.mkdtemp(prefix="prism-matrix-"))
F, OUT = ROOT / "fixtures", ROOT / "out"


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def make_fixtures():
    F.mkdir()
    os.chdir(F)
    sh("magick", "-size", "320x200", "gradient:orange-purple", "-depth", "8", "img.png")
    for e in ("jpg", "webp", "bmp", "tiff", "ico"):
        sh("magick", "img.png", f"img.{e}")
    sh("magick", "-delay", "20", "img.png", "-negate", "img.png", "-flop", "img.png", "img.gif")
    sh("heif-enc", "-A", "-q", "70", "-o", "img.avif", "img.png")
    Path("img.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
                               '<rect width="200" height="100" fill="orange"/><circle cx="100" cy="50" r="40" fill="purple"/></svg>')
    base = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2",
            "-f", "lavfi", "-i", "sine=f=440:d=2", "-shortest"]
    for e in ("mp4", "mkv", "mov"):
        sh(*base, "-c:v", "libx264", "-c:a", "aac", f"vid.{e}")
    sh(*base, "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", "vid.webm")
    sh(*base, "-c:v", "mpeg4", "-c:a", "mp3", "vid.avi")
    a = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=2"]
    for e, codec in (("mp3", "libmp3lame"), ("flac", "flac"), ("ogg", "libvorbis"), ("opus", "libopus"),
                     ("wav", "pcm_s16le"), ("m4a", "aac"), ("aac", "aac")):
        sh(*a, "-c:a", codec, f"aud.{e}")
    Path("doc.md").write_text("# Title\n\nSome **bold** text and a list:\n\n- one\n- two\n")
    Path("doc.txt").write_text("Plain text document.\nSecond line.\n")
    sh("pandoc", "doc.md", "-s", "-o", "doc.html")
    for e in ("docx", "odt", "rtf", "epub"):
        sh("pandoc", "doc.md", "-s", "-o", f"doc.{e}")
    profile = (ROOT / "lo-profile").as_uri()
    office = lambda src, to: sh("soffice", f"-env:UserInstallation={profile}", "--headless", "--convert-to", to, src)
    office("doc.docx", "doc")
    Path("sheet.csv").write_text("name,value\nalpha,1\nbeta,2\n")
    for e in ("xlsx", "ods", "xls"):
        office("sheet.csv", e)
    sh("pandoc", "doc.md", "-o", "slides.pptx")
    for e in ("odp", "ppt"):
        office("slides.pptx", e)
    office("doc.docx", "pdf")
    os.rename("doc.pdf", "paper.pdf")
    return sorted(p for p in F.iterdir() if p.is_file() and c.kind_of(str(p)))


def pixels(path):
    if str(path).endswith((".avif", ".heic")):
        m = re.search(r"(\d+)x(\d+)", sh("heif-info", str(path)).stdout)
        return tuple(map(int, m.groups())) if m else None
    if str(path).endswith((".mp4", ".mkv", ".mov", ".webm", ".m4v")):
        r = sh("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
               "-of", "csv=p=0", str(path))
        return tuple(map(int, r.stdout.strip().split(","))) if r.stdout.strip() else None
    r = sh("magick", "identify", "-format", "%w %h\n", str(path))
    return tuple(map(int, r.stdout.split("\n")[0].split())) if r.returncode == 0 else None


def verify(path, ext, src):
    """Open the output for real; return '' if valid, else the reason."""
    p = Path(path)
    if ext in c.RESIZE:
        want = pixels(src)
        got = pixels(p)
        if not got or not want:
            return f"unreadable size ({got})"
        factor = c.RESIZE[ext]
        if any(abs(g - w * factor) > 2 for g, w in zip(got, want)):
            return f"size {got}, wanted ~{tuple(round(w * factor) for w in want)}"
        ext = c.ext_of(p.name)
    elif ext == "clean" or ext in c.COMPRESS:
        if ext in c.COMPRESS and p.stat().st_size >= Path(src).stat().st_size:
            return "compressed output is not smaller"
        ext = c.ext_of(p.name)
    if p.is_dir():
        if ext in ("png", "jpg"):  # one image per page
            pages = sorted(p.iterdir())
            bad = [x.name for x in pages if sh("magick", "identify", str(x)).returncode]
            return "" if pages and not bad else f"bad pages: {bad or 'none'}"
        md = list(p.glob("*.md"))  # pdf->md bundle with images
        return "" if md and md[0].read_text().strip() else "bundle has no markdown"
    if p.stat().st_size == 0:
        return "empty file"
    mime = sh("file", "-b", "--mime-type", str(p)).stdout.strip()
    if ext in ("png", "jpg", "jpeg", "webp", "gif", "ico", "bmp", "tif", "tiff", "svg"):
        return "" if sh("magick", "identify", str(p)).returncode == 0 else f"not decodable ({mime})"
    if ext in ("avif", "heic", "heif"):
        return "" if sh("heif-info", str(p)).returncode == 0 else f"not a valid {ext} ({mime})"
    if ext in ("mp4", "webm", "mkv", "mov", "avi", "m4v", "mp3", "flac", "ogg", "opus", "wav", "m4a", "aac"):
        r = sh("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p))
        if r.returncode or not r.stdout.strip():
            return f"ffprobe failed ({mime})"
        d = sh("ffmpeg", "-v", "error", "-i", str(p), "-f", "null", "-")
        return "" if d.returncode == 0 and not d.stderr.strip() else f"decode errors: {d.stderr.strip()[:80]}"
    expect = {"pdf": "application/pdf", "docx": "openxmlformats-officedocument.wordprocessingml",
              "xlsx": "openxmlformats-officedocument.spreadsheetml", "pptx": "openxmlformats-officedocument.presentationml",
              "odt": "opendocument.text", "ods": "opendocument.spreadsheet", "odp": "opendocument.presentation",
              "html": "text/html", "md": "text/", "txt": "text/", "csv": "text/", "rtf": "text/rtf"}
    want = expect.get(ext)
    if want and want not in mime:
        return f"wrong type: {mime}"
    if ext in ("md", "txt", "csv", "html") and not p.read_text(errors="replace").strip():
        return "no text"
    return ""


def run_one(job):
    src, ext = job
    dest = OUT / f"{src.stem}-{src.suffix[1:]}-to-{ext}"  # own folder per job: no shared input
    dest.mkdir(parents=True)
    copy = dest / src.name
    shutil.copy(src, copy)
    t = time.time()
    try:
        out = c.convert(str(copy), ext)
        why = verify(out, ext, src)
    except Exception as e:
        # Refusing to "compress" into a bigger file is the correct outcome.
        why = "" if ext in c.COMPRESS and "Already smaller" in str(e) else f"convert failed: {str(e)[:120]}"
    return src.name, ext, time.time() - t, why


if __name__ == "__main__":
    fixtures = make_fixtures()
    jobs = [(f, t) for f in fixtures for t in c.targets_for([str(f)]) if t != "crop"]  # crop: a window
    print(f"{ROOT}\n{len(fixtures)} input files, {len(jobs)} conversions", flush=True)
    fails = []
    with ThreadPoolExecutor(4) as pool:
        for name, ext, secs, why in pool.map(run_one, jobs):
            print(f"{'OK  ' if not why else 'FAIL'} {name:14} -> {ext:6} {secs:5.1f}s {why}", flush=True)
            if why:
                fails.append((name, ext, why))
    # A level that never compresses would hide behind the "Already smaller"
    # refusals: every compressible fixture must shrink at some level.
    compressed = {Path(o.name).name.split(" (compressed")[0] for o in OUT.rglob("* (compressed*")}
    for f in fixtures:
        if any(t in c.COMPRESS for t in c.targets_for([str(f)])) and f.stem not in compressed:
            fails.append((f.name, "compress", "no level made it smaller"))
            print(f"FAIL {f.name:14} -> compress: no level made it smaller", flush=True)
    print(f"\nDONE: {len(jobs) - len(fails)}/{len(jobs)} passed", flush=True)
    for f in fails:
        print("  FAIL", *f)
    sys.exit(1 if fails else 0)
