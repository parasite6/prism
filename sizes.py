"""Pixel size of an image or video from its header, in pure Python.

The ring decides which resize options to show while it opens, on the UI
thread, so this must be instant: no subprocesses (ffprobe takes ~200 ms).
Returns (width, height); (0, 0) for a video file with no picture; or None
when the size can't be read cheaply, and callers then offer every option and
let the conversion itself decide."""
import struct

_HEAD = 256 * 1024


def size_of(path):
    try:
        with open(path, "rb") as f:
            head = f.read(_HEAD)
            for probe in (_png, _gif, _jpeg, _webp, _bmp, _heif, _avi):
                found = probe(head)
                if found:
                    return found
            for probe in (_tiff, _mp4, _matroska):
                found = probe(f)
                if found:
                    return found
    except (OSError, struct.error, IndexError, ValueError):
        pass
    return None


def _png(h):
    if h[:8] == b"\x89PNG\r\n\x1a\n" and h[12:16] == b"IHDR":
        return struct.unpack(">II", h[16:24])


def _gif(h):
    if h[:4] == b"GIF8":
        return struct.unpack("<HH", h[6:10])


def _jpeg(h):
    if h[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(h):
        if h[i] != 0xFF:
            return None
        marker = h[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame
            height, width = struct.unpack(">HH", h[i + 5:i + 9])
            return width, height
        i += 2 + struct.unpack(">H", h[i + 2:i + 4])[0]


def _webp(h):
    if h[:4] != b"RIFF" or h[8:12] != b"WEBP":
        return None
    kind = h[12:16]
    if kind == b"VP8X":
        return 1 + int.from_bytes(h[24:27], "little"), 1 + int.from_bytes(h[27:30], "little")
    if kind == b"VP8 ":
        width, height = struct.unpack("<HH", h[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if kind == b"VP8L":
        bits = int.from_bytes(h[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1


def _bmp(h):
    if h[:2] == b"BM":
        width, height = struct.unpack("<ii", h[18:26])
        return abs(width), abs(height)


def _tiff(f):
    """The first IFD can sit anywhere (ImageMagick writes it after the pixels)."""
    f.seek(0)
    h = f.read(8)
    order = {b"II*\x00": "<", b"MM\x00*": ">"}.get(h[:4])
    if not order:
        return None
    f.seek(struct.unpack(order + "I", h[4:8])[0])
    count = struct.unpack(order + "H", f.read(2))[0]
    entries = f.read(12 * min(count, 512))
    found = {}
    for k in range(len(entries) // 12):
        tag, kind, _, value = struct.unpack(order + "HHI4s", entries[12 * k:12 * k + 12])
        if tag in (256, 257):  # ImageWidth, ImageLength: SHORT or LONG
            found[tag] = struct.unpack(order + ("H" if kind == 3 else "I"), value[:2 if kind == 3 else 4])[0]
    if 256 in found and 257 in found:
        return found[256], found[257]


def _heif(h):
    """AVIF/HEIC: the largest 'ispe' (image spatial extent) property."""
    if h[4:8] != b"ftyp":
        return None
    data = h.find(b"mdat")  # properties live before the image data; bytes inside it could mimic one
    h = h[:data] if data != -1 else h
    best, i = None, h.find(b"ispe")
    while i != -1 and i + 16 <= len(h):
        width, height = struct.unpack(">II", h[i + 8:i + 16])  # after version/flags
        if not best or width * height > best[0] * best[1]:
            best = (width, height)
        i = h.find(b"ispe", i + 4)
    return best


def _avi(h):
    if h[:4] == b"RIFF" and h[8:12] == b"AVI ":
        i = h.find(b"avih")
        if i != -1:
            return struct.unpack("<II", h[i + 40:i + 48])  # dwWidth, dwHeight


def _boxes(f, start, stop):
    """(type, payload start, payload end) for each ISO-BMFF box in [start, stop)."""
    pos = start
    while pos + 8 <= stop:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head)
        header = 8
        if size == 1:
            size, header = struct.unpack(">Q", f.read(8))[0], 16
        elif size == 0:
            size = stop - pos
        if size < header:
            return
        yield kind, pos + header, min(pos + size, stop)
        pos += size


def _mp4(f):
    """MP4/MOV: moov > trak > tkhd, by seeking; only box headers are read, so a
    long recording's large index is never loaded."""
    f.seek(4)
    if f.read(4) not in (b"ftyp", b"moov", b"wide", b"free", b"mdat", b"skip"):
        return None
    end = f.seek(0, 2)
    best = None
    for kind, start, stop in _boxes(f, 0, end):
        if kind != b"moov":
            continue
        for kind, start, stop in _boxes(f, start, stop):
            if kind != b"trak":
                continue
            for kind, start, stop in _boxes(f, start, stop):
                if kind != b"tkhd":
                    continue
                f.seek(start)
                body = f.read(min(stop - start, 96))
                offset = 88 if body[0] == 1 else 76  # version 1 has 64-bit times
                width, height = (v >> 16 for v in struct.unpack(">II", body[offset:offset + 8]))
                if width and height and (not best or width * height > best[0] * best[1]):
                    best = (width, height)
        return best or (0, 0)  # parsed fine, but no video track (audio-only .mp4/.m4v)
    return None


def _vint(f, keep_marker):
    first = f.read(1)
    if not first:
        raise ValueError("end of file")
    b = first[0]
    length = 1
    while length <= 8 and not b & (0x80 >> (length - 1)):
        length += 1
    if length > 8:
        raise ValueError("bad EBML length")
    value = b if keep_marker else b & (0xFF >> length)
    for byte in f.read(length - 1):
        value = (value << 8) | byte
    return value


def _matroska(f):
    """MKV/WebM: Segment > Tracks > TrackEntry > Video > PixelWidth/PixelHeight."""
    f.seek(0)
    if f.read(4) != b"\x1a\x45\xdf\xa3":
        return None
    f.seek(0)
    end = f.seek(0, 2)
    f.seek(0)

    def children(stop):
        while f.tell() < stop:
            ident = _vint(f, True)
            size = _vint(f, False)
            start = f.tell()
            yield ident, start, min(start + size, stop)
            f.seek(start + size)

    best = None
    for ident, start, stop in children(end):
        if ident == 0x1A45DFA3:  # EBML header
            continue
        if ident != 0x18538067:
            return None
        for ident, start, stop in children(stop):
            if ident == 0x1F43B675:  # Cluster: tracks always come first
                break
            if ident != 0x1654AE6B:
                continue
            for ident, start, stop in children(stop):
                if ident != 0xAE:
                    continue
                for ident, start, stop in children(stop):
                    if ident != 0xE0:
                        continue
                    dims = {}
                    for ident, start, stop in children(stop):
                        if ident in (0xB0, 0xBA) and stop - start <= 8:  # a corrupt size can't make us read GBs
                            dims[ident] = int.from_bytes(f.read(stop - start), "big")
                    if 0xB0 in dims and 0xBA in dims:
                        size = (dims[0xB0], dims[0xBA])
                        if not best or size[0] * size[1] > best[0] * best[1]:
                            best = size
            return best
        return best
    return best


def is_apng(path):
    """An animated PNG: an acTL chunk before the first image data."""
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
            for _ in range(64):  # acTL must precede IDAT, which comes early
                head = f.read(8)
                if len(head) < 8:
                    return False
                length, kind = struct.unpack(">I4s", head)
                if kind == b"acTL":
                    return True
                if kind == b"IDAT":
                    return False
                f.seek(length + 4, 1)
    except (OSError, struct.error):
        pass
    return False


def is_animated_avif(path):
    """An AVIF image sequence ('avis' brand), which heif-dec flattens to one frame."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    if head[4:8] != b"ftyp":
        return False
    size = struct.unpack(">I", head[:4])[0]
    return b"avis" in head[8:min(size, 64)]
