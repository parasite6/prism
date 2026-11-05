"""Remove a plain background: no AI model, just colour and connectivity.

The background colour comes from the image's border. The background is
everything connected to that border that's close to it, like a magic wand
started from every edge. Then:
  - Holes that are that colour almost exactly also go: the inside of an "O" in
    a logo, the gap under a mug handle.
  - The subject's outline is softened. Pixels that blend subject and background
    get partial transparency, and the background tint is unmixed from their
    colour, so no white or green fringe is left.

Works for logos, icons, screenshots, scans and product shots on a plain or
studio backdrop. A busy background (most photos) is refused, so nothing
half-cut is saved.
"""
import numpy as np
from PIL import Image
from scipy import ndimage

BORDER = 2           # px of each edge sampled for the background colour
MIN_COVERAGE = 0.7   # this share of the border must be background colours
MIN_REMOVED = 0.005  # less than this removed: there was no background
MAX_REMOVED = 0.995  # more: the whole picture matched, nothing to keep


class NoPlainBackground(RuntimeError):
    pass


class AlreadyTransparent(NoPlainBackground):
    pass


def _border(rgb):
    b = BORDER
    return np.concatenate([rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
                           rgb[b:-b, :b].reshape(-1, 3), rgb[b:-b, -b:].reshape(-1, 3)])


def background_colours(border):
    """Up to three dominant border colours (a backdrop with a gradient or a
    vignette has more than one), or NoPlainBackground."""
    bins = (border >> 4).astype(np.int32)  # 16 levels per channel
    keys = bins[:, 0] * 256 + bins[:, 1] * 16 + bins[:, 2]
    values, counts = np.unique(keys, return_counts=True)
    order = np.argsort(counts)[::-1]
    colours, covered = [], 0
    for i in order[:3]:
        if counts[i] < 0.08 * len(keys):
            break
        colours.append(border[keys == values[i]].astype(np.float32).mean(axis=0))
        covered += counts[i]
    # Neighbouring bins of the same colour (a value right on a bin edge) count too.
    near = _distance(border.astype(np.float32), colours) < 24 if colours else np.zeros(len(keys), bool)
    if not colours or near.mean() < MIN_COVERAGE:
        raise NoPlainBackground("The background isn't a plain colour, so nothing was removed")
    return colours


def is_flat(rgba):
    """A digitally flat backdrop (a logo, icon or screenshot, not a photographed
    one): nearly the whole opaque border is within a hair of one colour. Here the
    colour method is exact. Anything else goes to the GPU model (matting.py)."""
    h, w = rgba.shape[:2]
    if min(h, w) < 4 * BORDER:
        return False
    border = _border(rgba[..., :3])[_border(rgba[..., 3:].repeat(3, -1))[:, 0] >= 16]
    if len(border) < 0.3 * len(_border(rgba[..., :3])):
        return True  # already transparent around the edge: the colour method's refusal says so
    centre = np.median(border, axis=0).astype(np.float32)
    return float((_distance(border.astype(np.float32), [centre]) <= 6).mean()) >= 0.98


def _distance(rgb, colours):
    """Per pixel: RGB distance (0..441) to the nearest background colour. float32."""
    best = None
    for c in colours:
        d = np.sqrt(((rgb - np.asarray(c, np.float32)) ** 2).sum(axis=-1, dtype=np.float32))
        best = d if best is None else np.minimum(best, d)
    return best


def _nearest(rgb, colours):
    """Per pixel: the nearest background colour itself (for unmixing the edge)."""
    if len(colours) == 1:
        return np.broadcast_to(np.asarray(colours[0], np.float32), rgb.shape)
    ds = np.stack([((rgb - np.asarray(c, np.float32)) ** 2).sum(axis=-1) for c in colours])
    return np.asarray(colours, np.float32)[ds.argmin(axis=0)]


def remove_background(rgba):
    """uint8 HxWx4 array -> (uint8 HxWx4 array with the background transparent,
    share of pixels removed). Raises NoPlainBackground."""
    h, w = rgba.shape[:2]
    if min(h, w) < 4 * BORDER:
        raise NoPlainBackground("Too small to find a background in")
    rgb = rgba[..., :3].astype(np.float32)
    already = rgba[..., 3] < 16  # already transparent: background whatever its colour
    opaque_border = _border(rgba[..., :3])[_border(rgba[..., 3:].repeat(3, -1))[:, 0] >= 16]
    if len(opaque_border) < 0.3 * len(_border(rgba[..., :3])):
        raise AlreadyTransparent("The background is already transparent")
    colours = background_colours(opaque_border)
    d = _distance(rgb, colours)

    # Tolerances from the backdrop's own noise (JPEG artefacts, sensor grain).
    border_d = _border(d[..., None].repeat(3, -1))[:, 0]
    noise = float(np.percentile(border_d[border_d < 24], 95)) if (border_d < 24).any() else 0.0
    lo = max(10.0, 1.5 * noise)  # at most this far: certainly background
    hi = lo + 28.0               # up to this far: background if connected to it

    # Background: the near-colour region connected to the border (8-connected).
    near = (d < hi) | already
    labels, _ = ndimage.label(near, structure=np.ones((3, 3), bool))
    edge_labels = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    bg = np.isin(labels, edge_labels[edge_labels > 0])
    # Enclosed holes: mostly the exact background colour, and not a speck. The
    # whole near-colour patch goes, its blurred edge included.
    holes, n = ndimage.label(near & ~bg, structure=np.ones((3, 3), bool))
    if n:
        sizes = np.bincount(holes.ravel())
        exact = np.bincount(holes.ravel(), weights=(d < lo).ravel())
        keep = np.flatnonzero((sizes >= max(64, h * w // 20000)) & (exact >= 0.7 * sizes))
        keep = keep[keep > 0]
        if len(keep):
            bg |= np.isin(holes, keep)
    del labels, holes, near

    removed = float(bg.mean())
    if removed < MIN_REMOVED:
        raise NoPlainBackground("Found no plain background around the subject, so nothing was removed")
    if removed > MAX_REMOVED:
        raise NoPlainBackground("The whole picture matched the background, so nothing was saved")

    # The edge: the outer part of the background (lo..hi, a blurred outline or a
    # soft shadow) and the subject's rim 2 px in. Those pixels blend subject and
    # backdrop, so their opacity is how far they are from the backdrop, measured
    # against the solid subject just inside.
    alpha = np.where(bg, 0.0, 1.0).astype(np.float32)
    grown = ndimage.binary_dilation(bg, iterations=2)
    inner = ndimage.binary_dilation(bg, iterations=5) & ~grown
    ref = max(float(np.median(d[inner])) if inner.any() else 2 * hi, hi + 1.0)
    edge = ((grown & ~bg) | (bg & (d > lo))) & ~already
    alpha[edge] = np.clip((d[edge] - lo) / (ref - lo), 0.0, 1.0)
    alpha = np.minimum(alpha, rgba[..., 3].astype(np.float32) / 255)  # never more opaque than before

    # Unmix the backdrop tint from partially transparent pixels:
    # pixel = a*subject + (1-a)*backdrop  ->  subject = backdrop + (pixel - backdrop)/a
    out = rgba.copy()
    part = (alpha > 0.02) & (alpha < 0.98)
    if part.any():
        b = _nearest(rgb[part], colours)
        a = alpha[part][:, None]
        out[..., :3][part] = np.clip(b + (rgb[part] - b) / a, 0, 255).round().astype(np.uint8)
    out[..., 3] = np.clip(alpha * 255, 0, 255).round().astype(np.uint8)
    out[..., :3][out[..., 3] == 0] = 0  # fully transparent: no hidden backdrop colour left in the file
    return out, removed


def remove_background_file(src_png, out_png):
    """An 8-bit RGBA PNG in (already upright, sRGB, stripped), a PNG out."""
    with Image.open(src_png) as im:
        rgba = np.asarray(im.convert("RGBA"))
    out, removed = remove_background(rgba)
    Image.fromarray(out, "RGBA").save(out_png, compress_level=6)
    return removed
