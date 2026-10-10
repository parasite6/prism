"""Parallel FFmpeg GIF encoding with the original palette, pixels, and timing."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

FPS = 15
SCALE = "fps=15,scale=640:-1:flags=lanczos"
GRAPH = SCALE + ",split[a][b];[a]palettegen[p];[b][p]paletteuse"
MAX_CACHE = 1024 ** 3


class IncompatibleChunks(RuntimeError):
    pass


def _subblocks_end(data, pos):
    while pos < len(data):
        size = data[pos]
        pos += 1
        if not size:
            return pos
        pos += size
    raise IncompatibleChunks("Incomplete GIF data block")


def _header(data):
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a") or not data[10] & 128:
        raise IncompatibleChunks("Missing shared GIF palette")
    end = 13 + 3 * 2 ** ((data[10] & 7) + 1)
    if end >= len(data):
        raise IncompatibleChunks("Incomplete GIF header")
    return end


def _merge(chunks, out):
    """Copy compressed image blocks; keep one header, loop extension and trailer.

    FFmpeg's concat/copy route retains embedded GIF headers/trailers, which makes
    ordinary readers stop at the first batch. These FFmpeg-generated chunks all
    use the same palette, so their image blocks can be joined without decoding.
    """
    first = chunks[0].read_bytes()
    header_end = _header(first)
    palette = first[13:header_end]
    with out.open("wb") as output:
        output.write(first[:header_end])
        for index, chunk in enumerate(chunks):
            data = first if index == 0 else chunk.read_bytes()
            pos = _header(data)
            if data[6:10] != first[6:10] or data[13:pos] != palette:
                raise IncompatibleChunks("GIF chunks have different canvases or palettes")
            while pos < len(data):
                start, kind = pos, data[pos]
                pos += 1
                if kind == 0x3B:
                    break
                if kind == 0x21:
                    if pos >= len(data):
                        raise IncompatibleChunks("Incomplete GIF extension")
                    label = data[pos]
                    pos = _subblocks_end(data, pos + 1)
                    # Retain the first NETSCAPE loop extension only.
                    if index == 0 or label != 0xFF:
                        output.write(data[start:pos])
                elif kind == 0x2C:
                    if pos + 9 >= len(data):
                        raise IncompatibleChunks("Incomplete GIF image descriptor")
                    packed = data[pos + 8]
                    pos += 9
                    if packed & 128:
                        pos += 3 * 2 ** ((packed & 7) + 1)
                    # Skip the LZW minimum code-size byte, then image subblocks.
                    pos = _subblocks_end(data, pos + 1)
                    output.write(data[start:pos])
                else:
                    raise IncompatibleChunks("Unexpected GIF block")
            else:
                raise IncompatibleChunks("Missing GIF trailer")
        output.write(b";")


def _worker_count(stream, out):
    cpus = len(os.sched_getaffinity(0))
    if cpus < 2:
        return 1
    try:
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(stream.get("duration", 0))
        if min(width, height) <= 0 or duration <= 0 or not math.isfinite(duration):
            return 1
        for side_data in stream.get("side_data_list", []):
            if abs(float(side_data.get("rotation", 0))) % 180 == 90:
                width, height = height, width
        frames = math.ceil(duration * FPS) + 3
        pixels = 640 * max(1, round(640 * height / width)) * frames
        cache_bytes = pixels * 4
        # Preserve the original route for small clips, uncertain durations,
        # transparency, and clips that would need excessive temporary storage.
        fmt = stream.get("pix_fmt", "")
        alpha = fmt.startswith(("yuva", "gbrap")) or fmt in {"rgba", "bgra", "argb", "abgr"}
        if alpha or pixels < 24_000_000 or cache_bytes > MAX_CACHE:
            return 1
        if shutil.disk_usage(out.parent).free < cache_bytes * 2 + 64 * 1024 ** 2:
            return 1
    except (KeyError, ValueError, OverflowError, OSError):
        return 1
    return min(4, cpus)


def convert_gif(src, out, stream, run):
    workers = _worker_count(stream, out)
    if workers == 1:
        run(["ffmpeg", "-n", "-loglevel", "error", "-i", str(src), "-map", f"0:{stream['index']}",
             "-vf", GRAPH, str(out)], timeout=6 * 3600)
        return
    with tempfile.TemporaryDirectory(prefix="gif-work-", dir=out.parent.parent) as tmp:
        work = Path(tmp)
        cache, palette = work / "frames.nut", work / "palette.png"
        # Decode and scale once; use lossless raw frames and the same full-video
        # palette as the existing filter graph. No dithering/quality tradeoff.
        run(["ffmpeg", "-n", "-loglevel", "error", "-filter_complex_threads", "2", "-i", str(src),
             "-filter_complex", f"[0:{stream['index']}]" + SCALE + ",format=bgra,split[v][p];[p]palettegen[pal]",
             "-map", "[v]", "-c:v", "rawvideo", "-threads:v", "1", str(cache),
             "-map", "[pal]", "-frames:v", "1", "-threads:v", "1", str(palette)], timeout=6 * 3600)
        info = json.loads(run(["ffprobe", "-v", "error", "-count_packets", "-show_entries",
                               "stream=nb_read_packets", "-of", "json", str(cache)]))
        count = int(info["streams"][0]["nb_read_packets"])
        if count < 1:
            raise RuntimeError("The video produced no GIF frames")
        # Fifteen FPS uses a three-frame delay cycle in GIF's centisecond clock.
        # Align batch boundaries to that cycle so frame delays stay identical.
        span = math.ceil(math.ceil(count / workers) / 3) * 3
        pieces = [(start, min(span, count - start)) for start in range(0, count, span)]

        def encode(piece):
            start, length = piece
            target = work / f"part-{start:08d}.gif"
            run(["ffmpeg", "-n", "-loglevel", "error", "-filter_complex_threads", "1",
                 "-ss", str(start / FPS), "-i", str(cache), "-i", str(palette),
                 "-filter_complex", "[0:v][1:v]paletteuse", "-frames:v", str(length), str(target)], timeout=6 * 3600)
            return target

        with ThreadPoolExecutor(max_workers=workers) as pool:
            chunks = list(pool.map(encode, pieces))
        try:
            _merge(chunks, out)
        except IncompatibleChunks:
            out.unlink(missing_ok=True)
            # Some palette/transparency combinations cannot be joined safely.
            # Use the cached frames in one pass rather than changing the image.
            run(["ffmpeg", "-n", "-loglevel", "error", "-filter_complex_threads", "1",
                 "-i", str(cache), "-i", str(palette), "-filter_complex",
                 "[0:v][1:v]paletteuse", str(out)], timeout=6 * 3600)
