"""Selection-specific formats and conversions that never overwrite existing files."""
import html
import contextlib
import ctypes
import errno
import fcntl
import functools
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
from urllib.parse import unquote, urlsplit
from gif_convert import convert_gif
import scrub
from sizes import is_animated_avif, is_apng, size_of

IMAGE = {"png", "jpg", "jpeg", "webp", "avif", "heic", "heif", "gif", "bmp", "tiff", "tif", "ico", "svg"}
VIDEO = {"mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "wmv"}
AUDIO = {"mp3", "flac", "ogg", "opus", "wav", "m4a", "aac", "wma"}
DOC = {"md", "markdown", "txt", "docx", "doc", "odt", "rtf", "html", "htm", "epub"}
SHEET = {"xlsx", "xls", "ods", "csv"}
SLIDES = {"pptx", "ppt", "odp"}
PDF = {"pdf"}

TARGETS = {
    "image": ["png", "jpg", "webp", "avif", "heic", "gif", "ico"],
    "video": ["mp4", "webm", "mkv", "mov", "gif", "mp3"],
    "audio": ["mp3", "flac", "ogg", "opus", "wav", "m4a"],
    "doc": ["pdf", "docx", "odt", "md", "html", "txt", "png", "jpg"],
    "sheet": ["xlsx", "ods", "csv", "pdf", "png", "jpg"],
    "slides": ["pdf", "pptx", "odp", "png", "jpg"],
    "pdf": ["md", "docx", "txt", "png", "jpg"],
}
# "clean": same format, all metadata stripped. Offered where there is
# metadata to strip and a way to strip it (not legacy .doc/.xls/.ppt, epub,
# or plain txt/csv, which have none).
CLEANABLE = IMAGE | VIDEO | AUDIO | {"docx", "odt", "rtf", "html", "htm", "md", "markdown", "xlsx", "ods",
                                     "pptx", "odp", "pdf"}
# Resize: same format, scaled. Which are offered depends on the pixel size
# (read from the header, so the ring stays instant): no shrinking tiny files,
# no doubling what is already huge. 2x video stays <= 4K so NVENC can encode it.
RESIZE = {"half": 1 / 2, "third": 1 / 3, "double": 2}
PERCENT = {"half": 50, "third": 33, "double": 200}
RESIZABLE_IMAGE = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif", "heic", "heif"}
RESIZABLE_VIDEO = {"mp4", "mov", "m4v", "mkv", "webm"}
# (min short side for 1/2, min short side for 1/3, max long side for 2x)
_RESIZE_LIMITS = {"image": (32, 48, 3000), "video": (240, 360, 1920)}
_RESIZE_PROBES = 64  # read at most this many headers while the ring opens
# ImageMagick here may take 31 GiB of RAM and unlimited disk; a 2x of a long
# animation must fail cleanly instead of swamping the desktop.
# Its cache spills to MAGICK_TEMPORARY_PATH: the job's work folder, on the
# source's disk (the default, /tmp, is tmpfs here: that "disk" is RAM too).
# Mapped cache counts as resident memory too, so both stay small; beyond them
# pixels go to plain disk files (pread/pwrite), not RAM.
_MAGICK_LIMITS = ["-limit", "memory", "768MiB", "-limit", "map", "768MiB", "-limit", "disk", "4GiB"]
_job = threading.local()  # .work: the running conversion's work folder
# Compress: levels 1 (light) .. 5 (smallest), same dimensions. Audio and images only.
COMPRESS = {f"compress{n}": n for n in range(1, 6)}
# What the ring, notifications and file names call each level: plain words, not numbers.
LEVEL_NAMES = {1: "Light", 2: "Medium", 3: "Strong", 4: "Heavy", 5: "Max"}
COMPRESSIBLE_AUDIO = {"mp3", "ogg", "opus", "m4a", "aac", "wav", "flac", "wma"}
COMPRESSIBLE_IMAGE = {"jpg", "jpeg", "png", "webp", "avif", "gif"}
# Audio per level: (bitrate, mono, sample-rate cap). Level 4 is the usual
# speech-to-text setting (mono, 16 kHz, 48k); 5 still transcribes fine.
_AUDIO_LEVELS = {1: ("160k", False, 48000), 2: ("112k", False, 44100), 3: ("64k", True, 24000),
                 4: ("48k", True, 16000), 5: ("32k", True, 16000)}
_OPUS_LEVELS = {1: ("128k", False, 48000), 2: ("96k", False, 48000), 3: ("48k", True, 24000),
                4: ("24k", True, 16000), 5: ("16k", True, 16000)}
# Images per level: lossy quality (JPEG/WebP/AVIF) or palette colours (PNG/GIF;
# level 1 there is lossless re-compression only).
_JPEG_Q = {1: 85, 2: 75, 3: 65, 4: 50, 5: 35}
_WEBP_Q = {1: 80, 2: 70, 3: 60, 4: 45, 5: 30}
_AVIF_Q = {1: 60, 2: 50, 3: 40, 4: 30, 5: 20}
_COLOURS = {1: None, 2: 256, 3: 128, 4: 64, 5: 32}
# With pngquant/oxipng/gifsicle installed (much better than ImageMagick here):
# PNG level 1 is lossless oxipng; 2-5 pngquant up to this quality (0-100).
_PNGQUANT_MAX = {2: 95, 3: 85, 4: 70, 5: 50}
# GIF level 1 is lossless frame optimisation; 2-5 add lossy LZW and fewer colours.
_GIFSICLE = {1: [], 2: ["--lossy=80", "--colors", "192"], 3: ["--lossy=120", "--colors", "128"],
             4: ["--lossy=160", "--colors", "64"], 5: ["--lossy=200", "--colors", "32"]}
SUBMENUS = {"compress": set(COMPRESS), "rescale": set(RESIZE), "edit": {"crop", "rotate"}}
LABELS = {"half": "\u00bd", "third": "\u2153", "double": "2\u00d7", "clean": "CLEAN", "convert": "CONVERT", "crop": "CROP",
          "edit": "EDIT", "rotate": "ROTATE",
          "rescale": "RESCALE", "compress": "COMPRESS", "back": "BACK",
          **{t: LEVEL_NAMES[n].upper() for t, n in COMPRESS.items()}}
BASE = Path(__file__).resolve().parent
_renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
_renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
_renameat2.restype = ctypes.c_int
_SRGB = Path("/usr/share/color/icc/colord/sRGB.icc")
# Appended to every ImageMagick write: bake EXIF rotation into the pixels and
# embedded colour profiles into sRGB first, then strip ALL metadata.
CLEAN = ["-auto-orient", *(["-profile", str(_SRGB)] if _SRGB.is_file() else []), "-strip"]


class ConversionResult(str):
    """An output path, with notices for content a target cannot represent."""
    def __new__(cls, path, warnings=()):
        result = super().__new__(cls, str(path))
        result.warnings = tuple(warnings)
        return result


def ext_of(path):
    return os.path.splitext(path)[1].lower().lstrip(".")


def kind_of(path):
    e = ext_of(path)
    for kind, group in (("image", IMAGE), ("video", VIDEO), ("audio", AUDIO), ("doc", DOC),
                        ("sheet", SHEET), ("slides", SLIDES), ("pdf", PDF)):
        if e in group:
            return kind
    return None


@functools.lru_cache(maxsize=None)
def _heic_encoder():
    """libheif lists encoders per format; Fedora's build has no HEVC one."""
    try:
        listing = subprocess.run(["heif-enc", "--list-encoders"], capture_output=True,
                                 text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    section = listing.split("HEIC encoders:", 1)[-1].split("encoders:", 1)[0]
    return "HEIC encoders:" in listing and "- " in section


def _cleanable(path):
    e = ext_of(path)
    return e in CLEANABLE and (e not in ("heic", "heif") or bool(shutil.which("exiftool")) or _heic_encoder())


def label(target):
    """What the ring shows for a target."""
    return LABELS.get(target, target.upper())


def _resizes(path, probe=True):
    e = ext_of(path)
    kind = "image" if e in RESIZABLE_IMAGE else "video" if e in RESIZABLE_VIDEO else None
    if kind is None or (e in ("heic", "heif") and not _heic_encoder()) or (probe and _animated_still_format(path)):
        return []  # (an animated PNG/AVIF would be flattened to one frame)
    size = size_of(path) if probe else None
    if size == (0, 0):
        return []  # no picture to resize (audio-only)
    if not size or not all(size):
        return list(RESIZE)  # unreadable header: offer all, the conversion decides
    half, third, double = _RESIZE_LIMITS[kind]
    short, long = min(size), max(size)
    return [t for t, ok in (("half", short >= half), ("third", short >= third), ("double", long <= double)) if ok]


def _animated_still_format(path):
    """Animated PNG/AVIF: our compress/resize tools would keep only the first frame."""
    e = ext_of(path)
    return (e == "png" and is_apng(path)) or (e == "avif" and is_animated_avif(path))


def _compressible(path, probe=True):
    e = ext_of(path)
    # Without probing (huge selections) convert() re-checks each file anyway.
    return e in COMPRESSIBLE_AUDIO or (e in COMPRESSIBLE_IMAGE and not (probe and _animated_still_format(path)))


def _croppable(path):
    e = ext_of(path)
    return (e in RESIZABLE_IMAGE and (e not in ("heic", "heif") or _heic_encoder())
            and not _animated_still_format(path))


def _compressed_ext(src):
    """Lossless audio (and WMA) compresses to MP3; everything else keeps its format."""
    e = ext_of(src)
    return "mp3" if e in ("wav", "flac", "wma") else e


def targets_for(paths):
    """Only formats common to the selected files, in display order."""
    sets = []
    for n, p in enumerate(paths):
        kind = kind_of(p)
        if kind is None:
            return []
        own = {"jpeg": "jpg", "markdown": "md", "heif": "heic"}.get(ext_of(p), ext_of(p))
        sets.append([t for t in TARGETS[kind] if t != own and (t != "heic" or _heic_encoder())]
                    + _resizes(p, probe=n < _RESIZE_PROBES)
                    + (list(COMPRESS) if _compressible(p, probe=n < _RESIZE_PROBES) else [])
                    + (["clean"] if _cleanable(p) else []))
    common = [t for t in sets[0] if all(t in s for s in sets[1:])] if sets else []
    if len(paths) == 1 and _croppable(paths[0]):
        common += ["crop", "rotate"]  # the edit window: one image at a time
    return common


# File signatures, so a converter that silently writes another format
# (ImageMagick writes PNG data when it has no AVIF/HEIC encoder) is caught.
def _is_format(path, ext):
    with open(path, "rb") as f:
        head = f.read(32)
    brand = head[8:12]
    return {
        "png": head.startswith(b"\x89PNG"),
        "jpg": head.startswith(b"\xff\xd8\xff"),
        "gif": head.startswith(b"GIF8"),
        "webp": head[:4] == b"RIFF" and head[8:12] == b"WEBP",
        "ico": head.startswith(b"\x00\x00\x01\x00"),
        "avif": head[4:8] == b"ftyp" and brand in (b"avif", b"avis"),
        "heic": head[4:8] == b"ftyp" and brand in (b"heic", b"heix", b"mif1", b"heim", b"heis"),
    }.get(ext, True)


def _heif(src, ext, out, work):
    """AVIF/HEIC via libheif's encoder, ~3x faster than ImageMagick and actually that format."""
    # Always via a stripped PNG: heif-enc copies the input's EXIF/XMP, and
    # can't read palette TIFFs etc.
    readable = work / "frame.png"
    _run(["magick", str(src) + "[0]", *CLEAN, "-define", "png:compression-level=1", str(readable)], timeout=600)
    cmd = ["heif-enc", "-q", "70", "-o", str(out), str(readable)]
    if ext == "avif":
        cmd[1:1] = ["-A", "-p", "speed=8"]
    _run(cmd, timeout=600)  # big photos take a while: a 24 MP AVIF ~15 s


def _names(folder, stem, suffix):
    yield folder / f"{stem}{suffix}"
    n = 1
    while True:
        yield folder / f"{stem} ({n}){suffix}"
        n += 1


def output_path(src, ext):
    """Suggest a name; final publication still reserves it atomically."""
    src = Path(src).absolute()
    return str(next(p for p in _names(src.parent, src.stem, f".{ext}") if not os.path.lexists(p)))


# Tools whose input formats can reference other files or URLs (LibreOffice's
# linked images, ffmpeg's DASH/concat manifests) run in a bubblewrap sandbox:
# no network, no home folder, and of the user's files only the one being
# converted (read-only) and the job's own temp folder (writable) exist.
SANDBOXED = {"soffice", "ffmpeg", "ffprobe"}


def _sandbox(cmd, cwd):
    """bwrap argv that runs cmd with only its input files and the job folder visible."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("Prism needs bubblewrap to convert files safely: sudo dnf install bubblewrap")
    job = getattr(_job, "work", None)
    writable = [str(job.parent)] if job else []
    # Outputs (absolute paths that don't exist yet) go to folders Prism made;
    # also covers threads without _job (gif_convert's parallel chunk encodes).
    outputs = [os.path.dirname(a) for a in map(str, cmd) if a.startswith("/") and not os.path.lexists(a)]
    for folder in [*outputs, *([str(cwd)] if cwd else [])]:
        if os.path.isdir(folder) and not any(folder == w or folder.startswith(w + os.sep) for w in writable):
            writable.append(folder)
    # Absolute paths in the command that already exist outside the writable
    # folders are its inputs: each is bound alone, read-only, never its folder.
    inputs = [a for a in map(str, cmd) if a.startswith("/") and os.path.isfile(a)
              and not a.startswith(("/usr/", "/etc/"))  # already visible, read-only
              and not any(a.startswith(w + os.sep) for w in writable)]
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--ro-bind-try", "/sys", "/sys",
            "--tmpfs", "/tmp", "--dir", "/tmp/home",
            "--setenv", "HOME", "/tmp/home", "--setenv", "TMPDIR", "/tmp", "--unsetenv", "XDG_RUNTIME_DIR",
            "--unsetenv", "DBUS_SESSION_BUS_ADDRESS", "--unsetenv", "WAYLAND_DISPLAY", "--unsetenv", "DISPLAY"]
    # The GPU (NVENC/CUDA) and render nodes, but no other devices (no /dev/input).
    for dev in ["/dev/dri", *(f"/dev/{d}" for d in os.listdir("/dev") if d.startswith("nvidia"))]:
        args += ["--dev-bind-try", dev, dev]
    # Fonts for LibreOffice, and their cache so it isn't rebuilt every time.
    home = Path.home()
    for fonts in (home / ".local/share/fonts", home / ".fonts", home / ".cache/fontconfig"):
        args += ["--ro-bind-try", str(fonts), str(fonts)]
    for path in inputs:
        args += ["--ro-bind", path, path]
    for path in writable:
        args += ["--bind", path, path]
    if cwd:
        args += ["--chdir", str(cwd)]
    return [*args, "--", *cmd]


def _run(cmd, *, cwd=None, timeout=120, isolated_python=False, extra_env=None):
    env = {**os.environ, **(extra_env or {})}
    # The launcher preloads layer-shell for GTK, not conversion subprocesses.
    env.pop("LD_PRELOAD", None)
    tool = Path(cmd[0]).name
    if cmd[0] == "magick":
        if "-limit" not in cmd:  # after a subcommand ("magick identify ..."), if any
            at = 2 if len(cmd) > 1 and cmd[1] in ("identify", "mogrify", "compare", "composite",
                                                   "montage", "stream", "conjure") else 1
            cmd = [*cmd[:at], *_MAGICK_LIMITS, *cmd[at:]]
        if getattr(_job, "work", None):
            env["MAGICK_TEMPORARY_PATH"] = str(_job.work)
        # Only the formats Prism handles: no sniffing a renamed file into
        # PostScript/MSL/URL coders or delegates (magick/policy.xml).
        env["MAGICK_CONFIGURE_PATH"] = str(BASE / "magick")
    if isolated_python:
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
    if tool in SANDBOXED:
        cmd = _sandbox(cmd, cwd)
    try:
        # Background priority, so encodes don't make the desktop lag. Via `nice`,
        # not preexec_fn, which isn't safe in this multi-threaded process.
        proc = subprocess.Popen(["nice", "-n", "10", *cmd], cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors="replace",
                                start_new_session=True)
    except FileNotFoundError:
        raise RuntimeError("Required tool is not installed: nice") from None
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise RuntimeError(f"{tool} timed out after {timeout:g} seconds") from None
    if proc.returncode == 127 and not stdout:
        raise RuntimeError(f"Required tool is not installed: {tool}")
    if proc.returncode:
        message = (stderr or stdout).strip()
        if "cache resources exhausted" in message:
            raise RuntimeError("Too large to process without hogging memory; output was not saved")
        raise RuntimeError(message.splitlines()[-1] if message else f"{cmd[0]} failed")
    if tool == "pandoc" and "Could not fetch resource" in stderr:
        raise RuntimeError("A document image or linked resource could not be read; output was not saved")
    return stdout


def _soffice(src, ext, out, filter=None):
    # Inside the job's folder: the one place the sandboxed soffice may write.
    job = getattr(_job, "work", None)
    with tempfile.TemporaryDirectory(prefix="office-", dir=job) as tmp:
        folder = Path(tmp)
        profile = (folder / "profile").as_uri()
        exported = folder / "export"
        exported.mkdir()
        _run(["soffice", f"-env:UserInstallation={profile}", "--headless", "--convert-to", filter or ext,
              "--outdir", str(exported), str(src)], timeout=600)
        if filter:  # may write several files (one per sheet)
            made = sorted(exported.iterdir())
            if not made:
                raise RuntimeError(f"LibreOffice could not convert this document to {ext.upper()}")
            for f in made:
                shutil.move(f, out / f.name)
            return len(made)
        expected = exported / f"{Path(src).stem}.{ext}"
        if not expected.is_file():
            raise RuntimeError(f"LibreOffice could not convert this document to {ext.upper()}")
        shutil.move(expected, out)


def _publish(payload, src, ext, *, bundle=False, folder=None, stem=None, suffix=None):
    made = list(payload.iterdir())
    if not made:
        raise RuntimeError("The converter produced no output")
    if len(made) == 1 and made[0].is_file() and not bundle:
        generated = made[0]
        names = _names(src.parent, stem or src.stem, suffix or f".{ext}")
    else:
        generated = payload
        names = _names(src.parent, folder or f"{src.stem}-{ext}", "")
    for dest in names:
        # Linux RENAME_NOREPLACE publishes a complete file or folder atomically,
        # without replacing existing files, directories, or dangling symlinks.
        if _renameat2(-100, os.fsencode(generated), -100, os.fsencode(dest), 1) == 0:
            return dest
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            continue
        if error not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise OSError(error, os.strerror(error), str(dest))
        # No atomic no-replace rename here: reserve the name, then move onto it.
        try:
            if generated.is_dir():
                os.mkdir(dest)  # an empty dir may be replaced by rename
            else:
                os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            continue
        try:
            os.rename(generated, dest)
        except OSError:
            (os.rmdir if generated.is_dir() else os.unlink)(dest)
            raise
        return dest


def _local_images(src, resource_dir):
    """The image paths in a document that are files inside its own folder
    (symlinks resolved). Only these, the document's own media and data: URIs
    may be embedded: a downloaded document must not pull ~/.ssh/id_rsa, ../..,
    or a URL into its output (safe-resources.lua drops everything else)."""
    tree = json.loads(_run(["pandoc", str(src), "-t", "json"]))
    targets = set()
    stack = [tree.get("blocks", [])]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("t") == "Image":
                targets.add(node["c"][2][0])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    root = os.path.realpath(resource_dir)
    safe = []
    for target in targets:
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or not parts.path or target.startswith(("/", "~", "\\")) or "\n" in target:
            continue
        path = os.path.realpath(os.path.join(root, unquote(parts.path)))
        if path.startswith(root + os.sep) and os.path.isfile(path):
            safe.append(target)
    return safe


def _pandoc(src, ext, out, *, cwd, resource_dir):
    target = {"md": "gfm", "txt": "plain"}.get(ext, ext)
    cmd = ["pandoc", str(src), "-s", "-t", target, "-o", str(out),
           f"--resource-path={os.path.relpath(resource_dir, cwd)}",  # ':' in a path would split it
           f"--lua-filter={BASE / 'safe-resources.lua'}",
           f"--lua-filter={BASE / 'strip-meta.lua'}", f"--variable=pagetitle:{Path(out).stem}"]
    if ext == "html":
        cmd.append("--embed-resources")
    elif ext == "md":
        cmd.append("--extract-media=media")
    _run(cmd, cwd=cwd, extra_env={"PRISM_IMAGES": "\n".join(_local_images(src, resource_dir))})


def _pages_to_images(src, kind, ext, payload, work):
    """Document -> one image per page (laid out as it would print, via PDF).
    Returns True when there are several pages (published as a folder)."""
    if kind == "pdf":
        pdf = src
    else:
        pdf = work / "render.pdf"
        if kind == "doc":
            _document(src, "pdf", pdf, work, work)
        else:
            _soffice(src, "pdf", pdf)
    pages = work / "pages"
    pages.mkdir()
    fmt = ["-png"] if ext == "png" else ["-jpeg", "-jpegopt", "quality=90"]
    _run(["pdftoppm", "-r", "150", *fmt, str(pdf), str(pages / "p")], timeout=600)
    made = sorted(pages.iterdir(), key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
    if not made:
        raise RuntimeError("The document has no pages to render")
    if len(made) == 1:
        shutil.move(made[0], payload / f"{src.stem}.{ext}")
        return False
    width = max(2, len(str(len(made))))
    for n, page in enumerate(made, 1):
        shutil.move(page, payload / f"{src.stem}-{n:0{width}d}.{ext}")
    return True


def _document(src, ext, out, payload, work):
    source_ext = ext_of(src)
    resource_dir = src.parent
    if source_ext == "txt":
        raw = src.read_bytes()
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16")
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1252", errors="replace")  # old Windows/Latin-1 text
        literal = work / "literal.html"
        literal.write_text('<!doctype html><html><head><meta charset="utf-8"></head><body>'
                           '<pre style="white-space: pre-wrap">' + html.escape(text) +
                           '</pre></body></html>', encoding="utf-8")
        src = literal
    elif source_ext == "doc" and ext != "pdf":
        readable = work / "source.docx"
        _soffice(src, "docx", readable)
        src = readable
    if ext == "pdf":
        if source_ext in {"md", "markdown", "txt", "epub", "html", "htm"}:
            rendered = work / "document.html"
            _pandoc(src, "html", rendered, cwd=work, resource_dir=resource_dir)
            _soffice(rendered, ext, out)
        else:
            _soffice(src, ext, out)
    else:
        _pandoc(src, ext, out, cwd=payload, resource_dir=resource_dir)


def _pdf_python():
    # A repo checkout has none of its own: it uses the installed copy's (install.sh).
    installed = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "prism"
    for python in (BASE / ".pdf-venv/bin/python", installed / ".pdf-venv/bin/python"):
        if python.is_file():
            return python
    raise RuntimeError("PDF dependencies are missing. Run install.sh (or setup-pdf.sh) first.")


def _scrub_pdf(path):
    _run([str(_pdf_python()), str(BASE / "pdf_convert.py"), "--scrub", str(path)],
         timeout=600, isolated_python=True)


def _pdf(src, ext, out, payload):
    if ext == "txt":
        _run(["pdftotext", "-layout", str(src), str(out)])
        if not out.read_text(encoding="utf-8").strip():
            raise RuntimeError("This PDF has no extractable text; scanned PDFs need OCR")
        return
    _run([str(_pdf_python()), str(BASE / "pdf_convert.py"), str(src), str(out)],
         cwd=payload, timeout=600, isolated_python=True)


@functools.lru_cache(maxsize=None)
def _nvenc(encoder):
    """Listed encoders can still fail (no driver, GPU busy); probe one tiny frame."""
    try:
        return subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.1",
                               "-c:v", encoder, "-f", "null", "-"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _video_encoder(ext, index, gpu=True):
    if ext == "webm":
        if gpu and _nvenc("av1_nvenc"):
            return [f"-c:v:{index}", "av1_nvenc", f"-preset:v:{index}", "p5", f"-cq:v:{index}", "28"]
        return [f"-c:v:{index}", "libsvtav1", f"-preset:v:{index}", "10", f"-crf:v:{index}", "30"]
    if gpu and _nvenc("h264_nvenc"):
        return [f"-c:v:{index}", "h264_nvenc", f"-preset:v:{index}", "p5", f"-cq:v:{index}", "19"]
    return [f"-c:v:{index}", "libx264", f"-preset:v:{index}", "veryfast", f"-crf:v:{index}", "18"]


def _cpu_encoders(args, ext):
    """Swap each NVENC video encoder (with its preset/cq) for the CPU equivalent."""
    result, i = [], 0
    while i < len(args):
        if str(args[i]).startswith("-c:v:") and str(args[i + 1]).endswith("_nvenc"):
            index = args[i].rsplit(":", 1)[1]
            result += _video_encoder(ext, index, gpu=False)
            i += 6  # -c:v:N enc -preset:v:N p5 -cq:v:N q
        else:
            result.append(args[i])
            i += 1
    return result


def _media(src, ext, out, copy=False, scale=None):
    streams = json.loads(_run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(src)]))["streams"]
    cmd = ["ffmpeg", "-n", "-loglevel", "error", "-i", str(src)]
    warnings = []
    if ext == "gif":
        videos = [s for s in streams if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")]
        if not videos:
            raise RuntimeError("This file has no video track")
        video = max(videos, key=lambda s: s.get("width", 0) * s.get("height", 0))
        convert_gif(src, out, video, _run)
        return warnings
    audio_target = ext in TARGETS["audio"]
    audio_count = sum(s["codec_type"] == "audio" for s in streams)
    if audio_target and not audio_count:
        raise RuntimeError("This file has no audio track")
    counters = {"video": 0, "audio": 0, "subtitle": 0, "attachment": 0}
    codec_args = []
    text_subtitles = {"subrip", "ass", "ssa", "text", "mov_text", "webvtt"}
    for stream in streams:
        kind = stream["codec_type"]
        codec = stream.get("codec_name", "")
        cover = kind == "video" and stream.get("disposition", {}).get("attached_pic", 0)
        if cover:  # cover art is tag data too
            continue
        if audio_target:
            if kind != "audio" or (ext in {"mp3", "flac", "wav"} and counters["audio"]):
                continue
        elif kind not in {"video", "audio", "subtitle"} and not (kind == "attachment" and ext == "mkv"):
            warnings.append(f"Removed a {kind} track (can hold GPS or sensor data)" if copy
                            else f"{ext.upper()} cannot retain the {kind} stream")
            continue
        if kind == "subtitle" and ext != "mkv" and codec not in text_subtitles:
            warnings.append(f"{ext.upper()} cannot retain this image-based subtitle track")
            continue
        cmd += ["-map", f"0:{stream['index']}"]
        index = counters[kind]
        counters[kind] += 1
        # Track language isn't personal, and players need it to pick audio/subtitles.
        language = stream.get("tags", {}).get("language", "und")
        if kind != "attachment" and language != "und":
            codec_args += [f"-metadata:s:{kind[0]}:{index}", f"language={language}"]
        if scale and kind == "video":  # resize: re-encode the picture, copy the rest
            factor = f"{scale:.6f}"
            codec_args += _video_encoder(ext, index)
            codec_args += [f"-filter:v:{index}",
                           f"scale=trunc(iw*{factor}/2)*2:trunc(ih*{factor}/2)*2:flags=lanczos",
                           f"-pix_fmt:v:{index}", "yuv420p"]
        elif copy:  # clean: streams untouched, only metadata goes
            codec_args += [f"-c:{ {'attachment': 't'}.get(kind, kind[0])}:{index}", "copy"]
            if kind == "attachment":
                mimetype = stream.get("tags", {}).get("mimetype", "application/octet-stream")
                codec_args += [f"-metadata:s:t:{index}", f"mimetype={mimetype}",
                               f"-metadata:s:t:{index}", f"filename=attachment{index}"]
        elif kind == "attachment":
            # The muxer needs a filename and type; the original name is not kept.
            mimetype = stream.get("tags", {}).get("mimetype", "application/octet-stream")
            codec_args += [f"-c:t:{index}", "copy", f"-metadata:s:t:{index}", f"mimetype={mimetype}",
                           f"-metadata:s:t:{index}", f"filename=attachment{index}"]
        elif kind == "video":
            allowed = {"webm": {"vp8", "vp9", "av1"},
                       "mp4": {"h264", "hevc", "av1", "mpeg4", "mjpeg"},
                       "mov": {"h264", "hevc", "prores", "mpeg4", "mjpeg", "dvvideo", "png"}}.get(ext)
            if allowed is None or codec in allowed:
                codec_args += [f"-c:v:{index}", "copy"]
            else:
                # GPU first: NVENC is ~10x faster than x264 and ~80x faster than
                # libvpx-vp9, which took minutes for a short 1080p clip.
                codec_args += _video_encoder(ext, index)
                codec_args += [f"-filter:v:{index}", "pad=ceil(iw/2)*2:ceil(ih/2)*2", f"-pix_fmt:v:{index}", "yuv420p"]
        elif kind == "audio":
            wanted = {"mp3": "mp3", "flac": "flac", "ogg": "vorbis", "opus": "opus", "wav": "pcm_s16le", "m4a": "aac"}.get(ext)
            if ext == "wav":
                sample_format = stream.get("sample_fmt", "")
                bits = int(stream.get("bits_per_raw_sample", 0) or 0)
                if sample_format.startswith("flt"):
                    wanted = "pcm_f32le"
                elif sample_format.startswith("dbl"):
                    wanted = "pcm_f64le"
                elif bits > 24 or (sample_format.startswith("s32") and not bits):
                    wanted = "pcm_s32le"
                elif bits > 16:
                    wanted = "pcm_s24le"
                elif sample_format.startswith("u8"):
                    wanted = "pcm_u8"
            elif ext == "m4a" and (codec in {"flac", "alac"} or codec.startswith("pcm_")):
                wanted = "alac"
            if wanted:
                encoder = "copy" if codec == wanted else {"mp3": "libmp3lame", "vorbis": "libvorbis", "opus": "libopus"}.get(wanted, wanted)
            elif ext == "webm":
                encoder = "copy" if codec in {"opus", "vorbis"} else "libopus"
            elif ext in {"mp4", "mov"}:
                encoder = "copy" if codec in {"aac", "alac", "mp3"} else "aac"
            else:
                encoder = "copy"
            codec_args += [f"-c:a:{index}", encoder]
            if encoder in {"aac", "libmp3lame", "libopus", "libvorbis"}:
                codec_args += [f"-b:a:{index}", "192k"]
        elif kind == "subtitle":
            wanted = {"mp4": "mov_text", "mov": "mov_text", "webm": "webvtt"}.get(ext)
            encoder = wanted or ("srt" if codec == "mov_text" else "copy")
            codec_args += [f"-c:s:{index}", encoder]
    if audio_target and ext in {"mp3", "flac", "wav"} and audio_count > 1:
        warnings.append(f"{ext.upper()} holds one audio track; kept the first of {audio_count}")
    # No tags (global or per stream), chapters, or encoder stamps. Empty
    # "encoder=" stops ffmpeg adding its own; Matroska's required muxing-app
    # header still says just "Lavf".
    tail = ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-metadata", "encoder=", "-metadata:s", "encoder="]
    if any(str(a).endswith("_nvenc") for a in codec_args):
        # Decode on the GPU too (halves the time); ffmpeg falls back to CPU itself.
        try:
            _run(cmd[:1] + ["-hwaccel", "cuda"] + cmd[1:] + tail + codec_args + [str(out)], timeout=6 * 3600)
            return warnings
        except RuntimeError:
            # NVENC limits (h264 max 4096 px, session caps, CUDA lost after
            # suspend): redo the encode on the CPU rather than fail.
            out.unlink(missing_ok=True)
            codec_args = _cpu_encoders(codec_args, ext)
    _run(cmd + tail + codec_args + [str(out)], timeout=6 * 3600)
    return warnings


def _clean(src, kind, payload, work):
    """Same format, metadata stripped; lossless wherever the format allows."""
    e = ext_of(src)
    out = payload / f"clean.{e}"  # fixed name: ImageMagick expands %d etc.
    if kind == "image":
        if e in ("jpg", "jpeg", "png", "webp", "gif") and (not scrub.needs_pixels(src.read_bytes())
                                                           or (e == "png" and is_apng(src))):
            # (An animated PNG is never re-encoded: that would keep one frame.)
            shutil.copyfile(src, out)  # metadata-only: pixels untouched (scrub_tree strips it)
        elif e in ("avif", "heic", "heif"):
            if not _heif_clean_lossless(src, out):
                decoded = work / "decoded.png"
                _run(["heif-dec", str(src), str(decoded)], timeout=600)
                _heif(decoded, "avif" if e == "avif" else "heic", out, work)
        elif e == "svg":
            out.write_text(scrub.strip_svg(src.read_text(encoding="utf-8", errors="surrogateescape")),
                           encoding="utf-8", errors="surrogateescape")
        else:
            # Rotation or a colour profile is part of how it looks: bake it into
            # the pixels (re-encode), then strip. All frames/sizes/pages kept.
            quality = []
            if e == "webp":
                lossless = b"VP8L" in src.read_bytes()[12:4096]
                quality = ["-define", "webp:lossless=true"] if lossless else ["-quality", "92"]
            _run(["magick", str(src), *CLEAN, *quality, str(out)])
        if not _is_format(out, {"jpeg": "jpg", "heif": "heic"}.get(e, e)):
            raise RuntimeError(f"Could not rewrite this {e.upper()}; output was not saved")
        return []
    if kind in ("video", "audio"):
        return _media(src, e, out, copy=True)
    if e in ("md", "markdown"):
        out.write_text(scrub.strip_markdown(src.read_text(encoding="utf-8", errors="surrogateescape")),
                       encoding="utf-8", errors="surrogateescape")
    elif e == "rtf":
        out.write_text(scrub.strip_rtf(src.read_text(encoding="latin-1")), encoding="latin-1")
    else:  # office packages, HTML, PDF: scrub_tree does the work
        shutil.copyfile(src, out)
    return []


def _heif_clean_lossless(src, out):
    """Drop EXIF/XMP boxes without re-encoding (the fallback re-encodes at q70).
    Rotation (irot) and the colour profile are image structure and stay."""
    if not shutil.which("exiftool"):
        return False
    try:
        _run(["exiftool", "-q", "-q", "-all=", "--icc_profile:all", "-o", str(out), str(src)])
    except RuntimeError:
        out.unlink(missing_ok=True)
        return False
    return out.is_file()


def _compress(src, kind, level, payload, work):
    """Smaller file, same dimensions; fails rather than saving a bigger one."""
    e = ext_of(src)
    out_ext = _compressed_ext(src)
    out = payload / f"compressed.{out_ext}"  # fixed name: ImageMagick expands %d etc.
    if kind == "audio":
        streams = json.loads(_run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                                   "stream=codec_name,channels,sample_rate", "-of", "json", str(src)]))["streams"]
        if not streams:
            raise RuntimeError("This file has no audio track")
        audio = streams[0]
        codec = {"mp3": "libmp3lame", "opus": "libopus", "m4a": "aac", "aac": "aac",
                 "ogg": "libopus" if audio.get("codec_name") == "opus" else "libvorbis"}[out_ext]
        bitrate, mono, rate_cap = (_OPUS_LEVELS if codec == "libopus" else _AUDIO_LEVELS)[level]
        rate = int(audio.get("sample_rate") or 48000)
        if codec == "libopus":  # Opus only runs at 8/12/16/24/48 kHz
            rate = min((r for r in (8000, 12000, 16000, 24000, 48000) if r >= min(rate, rate_cap)), default=48000)
        else:
            rate = min(rate, rate_cap)
        channels = 1 if mono else min(int(audio.get("channels") or 2), 2)
        vbr = ["-vbr", "constrained"] if codec == "libopus" else []  # Opus VBR can overshoot a lot
        _run(["ffmpeg", "-n", "-loglevel", "error", "-i", str(src), "-map", "0:a:0", "-vn",
              "-c:a", codec, "-b:a", bitrate, *vbr, "-ac", str(channels), "-ar", str(rate),
              "-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
              "-metadata", "encoder=", "-metadata:s", "encoder=", str(out)], timeout=6 * 3600)
    elif e == "avif":
        decoded = work / "decoded.png"
        _run(["heif-dec", str(src), str(decoded)], timeout=600)
        _run(["magick", str(decoded), *CLEAN, "-define", "png:compression-level=1", str(work / "frame.png")],
             timeout=600)
        _run(["heif-enc", "-A", "-p", "speed=6", "-q", str(_AVIF_Q[level]), "-o", str(out), str(work / "frame.png")],
             timeout=600)
    elif e == "png" and shutil.which("oxipng") and (level == 1 or shutil.which("pngquant")):
        clean = work / "clean.png"  # rotation/colour baked in, metadata gone
        _run(["magick", str(src), *CLEAN, "-define", "png:compression-level=1", str(clean)], timeout=600)
        if level == 1:
            shutil.copyfile(clean, out)
        else:
            _run(["pngquant", f"--quality=0-{_PNGQUANT_MAX[level]}", "--speed", "3", "--strip",
                  "--force", "--output", str(out), str(_COLOURS[level]), str(clean)], timeout=600)
        _run(["oxipng", "-o", "3" if level == 1 else "2", "--strip", "all", "-t", "2", str(out)], timeout=600)
    elif e == "gif" and shutil.which("gifsicle"):
        # Straight from the source: an ImageMagick pass first would re-quantize
        # frames with local palettes, so level 1 would no longer be lossless.
        # Comments/XMP go with these flags (and scrub_tree's GIF pass).
        _run(["gifsicle", "-O3", "--no-comments", "--no-names", "--no-extensions", *_GIFSICLE[level],
              "-o", str(out), str(src)], timeout=600)
    else:
        frames = ["-coalesce"] if e in ("gif", "webp") else []
        if e in ("jpg", "jpeg"):
            # Progressive + 4:2:0 chroma: smaller at the same visible quality.
            opts = ["-quality", str(_JPEG_Q[level]), "-sampling-factor", "4:2:0", "-interlace", "Plane"]
        elif e == "webp":
            opts = ["-quality", str(_WEBP_Q[level]), "-define", "webp:method=6"]
        else:  # png, gif: fewer colours (dithered) from level 2; best zlib/LZW effort always
            colours = _COLOURS[level]
            opts = ["-dither", "FloydSteinberg", "-colors", str(colours)] if colours else []
            if e == "png":
                # No forced PNG8: that cuts soft (partial) transparency to on/off.
                opts += ["-define", "png:compression-level=9", "-define", "png:compression-filter=5"]
            else:
                opts += ["-layers", "optimize"]
        target = str(out)
        if e == "png" and _COLOURS[level]:
            opaque = _run(["magick", "identify", "-format", "%[opaque]", str(src) + "[0]"]).strip() == "True"
            # Palette output only when opaque: PNG8 cuts soft transparency to on/off,
            # and with alpha ImageMagick picks a palette by itself.
            target = f"PNG8:{out}" if opaque else target
        _run(["magick", str(src), *frames, *CLEAN, *opts, target], timeout=600)
    if kind == "image" and not _is_format(out, {"jpeg": "jpg"}.get(out_ext, out_ext)):
        raise RuntimeError(f"Could not compress this {e.upper()}; output was not saved")
    before, after = src.stat().st_size, out.stat().st_size
    if after >= before:
        raise RuntimeError(f"Already smaller than {LEVEL_NAMES[level].lower()} compression would make it; nothing saved")
    return [f"{_human_size(before)} \u2192 {_human_size(after)} (\u2212{round(100 * (1 - after / before))}%)"]


def _human_size(n):
    """Human file size, like Nautilus (decimal units)."""
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def _resize(src, kind, target, payload, work):
    """Same format at 1/2, 1/3 or 2x the pixel size; metadata stripped as always."""
    e = ext_of(src)
    out = payload / f"resized.{e}"  # fixed name: ImageMagick expands %d etc.
    if kind == "video":
        streams = json.loads(_run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:stream_disposition=attached_pic",
                                   "-of", "json", str(src)]))["streams"]
        if not any(s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams):
            raise RuntimeError("This file has no video track to resize")
        return _media(src, e, out, copy=True, scale=RESIZE[target])
    geometry = {"half": "50%", "third": "33.3333%", "double": "200%"}[target]
    resize = ["-filter", "Lanczos", "-resize", geometry]
    if e in ("avif", "heic", "heif"):
        decoded, scaled = work / "decoded.png", work / "scaled.png"
        _run(["heif-dec", str(src), str(decoded)], timeout=600)
        _run(["magick", str(decoded), *CLEAN, *resize, str(scaled)], timeout=600)
        _heif(scaled, "avif" if e == "avif" else "heic", out, work)
    else:
        frames = ["-coalesce"] if e in ("gif", "webp") else []  # animations: every frame
        quality = []
        if e == "webp":
            lossless = b"VP8L" in src.read_bytes()[12:4096]
            quality = ["-define", "webp:lossless=true"] if lossless else ["-quality", "92"]
        elif e in ("jpg", "jpeg"):
            quality = ["-quality", "92"]
        optimize = ["-layers", "optimize"] if e == "gif" else []
        _run(["magick", str(src), *frames, *CLEAN, *resize, *quality, *optimize, str(out)], timeout=600)
    if not _is_format(out, {"jpeg": "jpg", "heif": "heic"}.get(e, e)):
        raise RuntimeError(f"Could not resize this {e.upper()}; output was not saved")
    return []


# Crop (crop.py's window): the box is drawn on an upright preview, so the full
# image is EXIF-rotated before it is cut, exactly like the preview.
CROP_FORMATS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif", "heic", "heif"}
_PREVIEW_SIDE = 4096  # enough detail for the crop window's zoom; ~64 MB at most


def _decoded(src, work):
    """What ImageMagick can read: this build has no AVIF/HEIC decoder; libheif does."""
    if ext_of(src) in ("avif", "heic", "heif"):
        image = work / "decoded.png"
        _run(["heif-dec", str(src), str(image)], timeout=600)
        return image
    return src


def crop_preview(src, work):
    """An upright, sRGB PNG of the first frame, at most _PREVIEW_SIDE px, for drawing
    the crop box on; and the upright full size, (width, height), the box is in."""
    image = _decoded(Path(src), Path(work))
    preview = Path(work) / "preview.png"
    size = _run(["magick", f"{image}[0]", *CLEAN, "-print", "%w %h\\n",
                 "-resize", f"{_PREVIEW_SIDE}x{_PREVIEW_SIDE}>", "-define", "png:compression-level=1",
                 str(preview)], timeout=600)
    width, height = map(int, size.split()[:2])
    return preview, (width, height)


def crop(src, box, dest, turns=0, mirror=False):
    """Cut box = (x, y, width, height), in upright pixels, out of src, then mirror it
    left-right (if mirror) and turn it clockwise by turns quarter turns, and write it
    to dest in dest's format, metadata stripped as always. dest may be src itself
    (the crop tool's Replace) or any existing file: it is swapped in atomically,
    keeping that file's permissions. Animated GIF/WebP keep every frame."""
    src = Path(src).absolute()
    dest = Path(os.path.realpath(dest))  # replace a symlink's target, not the link
    e = ext_of(dest)
    if e not in CROP_FORMATS:
        raise RuntimeError(f"Can't save an image as .{e}" if e else "Add a file extension, e.g. .png")
    if e in ("heic", "heif") and not _heic_encoder():
        raise RuntimeError("No HEIC encoder is installed; save as another format")
    x, y, width, height = (int(v) for v in box)
    if width < 1 or height < 1 or x < 0 or y < 0:
        raise RuntimeError("Nothing selected to crop")
    if not src.is_file():
        raise RuntimeError("The image no longer exists")
    with tempfile.TemporaryDirectory(prefix=".prism-", dir=dest.parent) as tmp, _registered(tmp):
        payload, work = Path(tmp) / "output", Path(tmp) / "work"
        payload.mkdir()
        work.mkdir()
        image = _decoded(src, work)
        animated = ext_of(src) in ("gif", "webp") and e in ("gif", "webp")
        read = str(image) if animated else f"{image}[0]"  # still targets: the first frame/page
        frames = ["-coalesce"] if animated else []
        cut = ["-auto-orient", "-crop", f"{width}x{height}+{x}+{y}", "+repage",
               *(["-flop"] if mirror else []), *(["-rotate", str(90 * (turns % 4))] if turns % 4 else [])]
        out = payload / f"cropped.{e}"  # fixed name: ImageMagick expands %d etc.
        if e in ("avif", "heic", "heif"):
            flat = work / "cropped.png"
            _run(["magick", read, *cut, *CLEAN, "-define", "png:compression-level=1", str(flat)], timeout=600)
            _heif(flat, "avif" if e == "avif" else "heic", out, work)
        else:
            # JPEG to JPEG keeps the source's quality (ImageMagick's default).
            quality = {"webp": ["-define", "webp:lossless=true"] if ext_of(src) == "webp"
                       and b"VP8L" in src.read_bytes()[12:4096] else ["-quality", "92"],
                       "jpg": [] if ext_of(src) in ("jpg", "jpeg") else ["-quality", "92"]}.get(
                           {"jpeg": "jpg"}.get(e, e), [])
            optimize = ["-layers", "optimize"] if animated and e == "gif" else []
            _run(["magick", read, *frames, *cut, *CLEAN, *quality, *optimize, str(out)], timeout=600)
        if not _is_format(out, {"jpeg": "jpg", "heif": "heic"}.get(e, e)):
            raise RuntimeError(f"Could not write a cropped {e.upper()}; nothing was saved")
        scrub.scrub_tree(payload, _scrub_pdf)
        if dest.exists():
            shutil.copymode(dest, out)
        os.replace(out, dest)
    return ConversionResult(dest)


REGISTRY = Path(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")) / "prism.jobs"
_registry_lock = threading.Lock()


@contextlib.contextmanager
def _registry():
    """The job list, locked across processes: the daemon, conversion workers
    and crop windows all update it. Lines are "pid<TAB>temp folder"."""
    with _registry_lock, open(REGISTRY.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lines = REGISTRY.read_text().splitlines()
        except OSError:
            lines = []
        entries = [l.split("\t", 1) if "\t" in l else ["0", l] for l in lines if l]  # old lines: no pid
        yield entries
        REGISTRY.write_text("".join(f"{pid}\t{path}\n" for pid, path in entries))


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        pass
    return int(pid) > 0


@contextlib.contextmanager
def _registered(tmp):
    """Record in-progress temp folders, so one whose process was killed
    mid-conversion can be removed from beside the user's files later."""
    try:
        with _registry() as entries:
            entries.append([str(os.getpid()), tmp])
    except OSError:
        pass
    _job.work = Path(tmp) / "work"  # this thread's magick calls spill here
    try:
        yield
    finally:
        _job.work = None
        try:
            with _registry() as entries:
                entries[:] = [e for e in entries if e[1] != tmp]
        except OSError:
            pass


def sweep_leftovers():
    """Remove temp folders of conversions whose process died (daemon start, and
    after a worker is killed). Live jobs, e.g. a crop window saving, are kept."""
    try:
        with _registry() as entries:
            dead = [e for e in entries if not _alive(e[0])]
            for _pid, path in dead:
                p = Path(path)
                # (".convert-ring-": folders left by this tool under its old name)
                if p.name.startswith((".prism-", ".convert-ring-")) and p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p, ignore_errors=True)
            entries[:] = [e for e in entries if e not in dead]
    except OSError:
        pass


def convert(src, ext):
    """Return the saved file/folder path. Failed conversions publish nothing."""
    src = Path(src).absolute()
    kind = kind_of(src)
    resizes = _resizes(src) if ext in RESIZE else []
    if resizes and ext not in resizes:  # resizable, but not by this much
        raise RuntimeError(f"Too {'large' if ext == 'double' else 'small'} to resize to {PERCENT[ext]}%")
    if kind is None or not (ext in TARGETS[kind] or (ext == "clean" and _cleanable(src)) or ext in resizes
                            or (ext in COMPRESS and _compressible(src))):
        raise RuntimeError("unsupported conversion for this file type")
    if not src.is_file():
        raise RuntimeError("The input file no longer exists")
    warnings = []
    with tempfile.TemporaryDirectory(prefix=".prism-", dir=src.parent) as tmp, _registered(tmp):
        payload, work = Path(tmp) / "output", Path(tmp) / "work"
        payload.mkdir()
        work.mkdir()
        out = payload / f"{src.stem}.{ext}"
        folder = None
        if ext == "clean":
            warnings = _clean(src, kind, payload, work)
        elif ext in RESIZE:
            warnings = _resize(src, kind, ext, payload, work)
        elif ext in COMPRESS:
            warnings = _compress(src, kind, COMPRESS[ext], payload, work)
        elif ext in ("png", "jpg") and kind in ("doc", "sheet", "slides", "pdf"):
            # Several pages: a folder named after the file, one image per page.
            if _pages_to_images(src, kind, ext, payload, work):
                folder = src.stem
        elif kind == "image":
            # Fixed name: ImageMagick expands %d etc. in output names. Publishing
            # names the result after src anyway.
            out = payload / f"converted.{ext}"
            image = src  # what the encoders read; output is still named after src
            if ext_of(src) == "svg":
                image = f"rsvg:{src}"  # the built-in librsvg; never an external delegate (Inkscape)
            frames = ["-coalesce"] if ext_of(src) in {"gif", "webp"} else []
            if ext_of(src) == "avif" and is_animated_avif(src):
                warnings.append("Animated AVIF: kept the first frame")
            if ext_of(src) == "png" and is_apng(src):
                if ext in ("gif", "webp"):  # keep the animation: read every frame
                    image, frames = f"apng:{src}", ["-coalesce"]
                else:  # the plain PNG reader gives the first frame only
                    warnings.append(f"{ext.upper()} stores a still image; kept the first frame")
            if ext_of(src) in ("avif", "heic", "heif"):
                # This ImageMagick has no AVIF/HEIC decoder; libheif does.
                image = work / "decoded.png"
                _run(["heif-dec", str(src), str(image)], timeout=600)
            if ext in ("avif", "heic"):
                _heif(image, ext, out, work)
            elif ext == "ico":
                _run(["magick", str(image) + "[0]", *frames, *CLEAN, "-resize", "256x256>", str(out)])
                if ext_of(src) == "gif":
                    warnings.append("ICO stores a still image; kept the first frame")
            elif ext not in ("gif", "webp") and ext_of(src) in ("gif", "webp", "ico", "tif", "tiff"):
                # A still format from a multi-frame source (animation, icon sizes,
                # TIFF pages): ImageMagick would write one file per frame.
                sizes = _run(["magick", "identify", "-format", "%w %h\\n", str(image)]).split("\n")
                sizes = [tuple(map(int, l.split())) for l in sizes if l.strip()]
                pick = max(range(len(sizes)), key=lambda i: sizes[i][0] * sizes[i][1]) \
                    if ext_of(src) == "ico" else 0  # an icon: its largest size
                _run(["magick", f"{image}[{pick}]", *CLEAN, str(out)])
                if len(sizes) > 1 and ext_of(src) != "ico":
                    what = "page" if ext_of(src) in ("tif", "tiff") else "frame"
                    warnings.append(f"{ext.upper()} stores a still image; kept the first {what}")
            else:
                # ImageMagick's default effort is slow for little gain on photos:
                # PNG level 1 is ~2x faster for ~10% more size; WebP method 2 same size.
                speed = {"png": ["-define", "png:compression-level=1"],
                         "webp": ["-define", "webp:method=2"]}.get(ext, [])
                _run(["magick", str(image), *frames, *CLEAN, *speed, str(out)])
            if not _is_format(out, ext):
                raise RuntimeError(f"No working {ext.upper()} encoder; output was not saved")
        elif kind in {"video", "audio"}:
            warnings = _media(src, ext, out)
        elif kind == "doc":
            _document(src, ext, out, payload, work)
        elif kind == "sheet" and ext == "csv":
            # One CSV per sheet (LibreOffice otherwise silently exports only the
            # first). Several sheets: a folder named after the file.
            n = _soffice(src, ext, payload, filter="csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,false,false,false,-1")
            if n == 1:
                only = next(payload.iterdir())
                only.rename(payload / f"{src.stem}.csv")
            else:
                folder = src.stem
        elif kind in {"sheet", "slides"}:
            _soffice(src, ext, out)
        elif kind == "pdf":
            _pdf(src, ext, out, payload)
        # Nothing is published unscrubbed: unknown output types fail here.
        scrub.scrub_tree(payload, _scrub_pdf)
        saved = _publish(payload, src, ext, bundle=(kind == "pdf" and ext == "md") or folder is not None,
                         folder=folder, **({"stem": f"{src.stem} (clean)", "suffix": src.suffix}
                                           if ext == "clean" else
                                           {"stem": f"{src.stem} ({PERCENT[ext]}%)", "suffix": src.suffix}
                                           if ext in RESIZE else
                                           {"stem": f"{src.stem} (compressed, {LEVEL_NAMES[COMPRESS[ext]].lower()})",
                                            "suffix": src.suffix if _compressed_ext(src) == ext_of(src)
                                            else f".{_compressed_ext(src)}"}
                                           if ext in COMPRESS else {}))
    return ConversionResult(saved, warnings)


# Each conversion runs in its own process (convert_isolated), so whatever a
# hostile or huge file does to memory hits a worker, never the resident ring;
# under systemd the worker is also in its own scope with a hard memory cap.
WORKER_MEMORY = "3G"


def convert_isolated(src, ext):
    """convert(), in a memory-capped worker process. Same result or RuntimeError."""
    env = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", "PYTHONHOME", "PYTHONPATH")}
    worker = ["/usr/bin/python3", str(BASE / "convert.py"), "--job", str(src), ext]
    if shutil.which("systemd-run") and env.get("DBUS_SESSION_BUS_ADDRESS"):
        worker = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                  "-p", f"MemoryMax={WORKER_MEMORY}", "-p", "MemorySwapMax=0", "--", *worker]
    proc = subprocess.run(worker, env=env, capture_output=True, text=True, errors="replace")
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        result = None
    if proc.returncode == 0 and result and "path" in result:
        return ConversionResult(result["path"], result.get("warnings", ()))
    sweep_leftovers()  # a killed worker leaves its temp folder behind
    if result and "error" in result:
        raise RuntimeError(result["error"])
    if proc.returncode in (-9, 137):
        raise RuntimeError(f"Ran out of memory (a conversion may use {WORKER_MEMORY}B); nothing was saved")
    detail = proc.stderr.strip().splitlines()
    raise RuntimeError(detail[-1] if detail else f"The conversion stopped unexpectedly ({proc.returncode})")


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] != ["--job"] or len(sys.argv) != 4:
        sys.exit("convert.py --job SRC TARGET  (used by the ring; prints a JSON result)")
    try:
        saved = convert(sys.argv[2], sys.argv[3])
        print(json.dumps({"path": str(saved), "warnings": list(saved.warnings)}))
    except Exception as e:  # every failure is reported, as the ring's notification
        print(json.dumps({"error": str(e) or type(e).__name__}))
        sys.exit(1)
