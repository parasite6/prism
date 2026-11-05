"""Remove background for photos: BiRefNet_HR-matting on the NVIDIA GPU.

Accuracy first. The model runs at its full training size (2048 x 2048) and
returns a true alpha matte: partial transparency for hair, fur, glass and
motion blur, not a cut-out mask. The matte is scaled back to the original
size. Edge colours then get the backdrop's colour unmixed from them, using
BiRefNet's own refinement step (blur-fusion foreground estimation, Forte &
Pitie 2021). That step runs on the GPU too, with the same reflected-border
box blur as the reference's cv2.blur.

There's no CPU model fallback: without the GPU or the weights it says so and
nothing is saved.

Memory: nothing here may push the desktop into swap or the GPU into OOM.
  - VRAM: before the model loads and before every image, the free memory
    (all processes, from the driver) must cover what that step needs plus a
    reserve. This process is then hard-capped to what was checked, so a
    misjudged step fails here with a clear message and never squeezes anyone
    else. Batches take turns on the GPU (a lock).
  - RAM: free memory (MemAvailable) must cover the step plus a reserve left
    for the desktop, checked from the image's size before it's decoded. The
    worker also runs in a systemd scope with a hard cap (convert.CUTOUT_MEMORY).
The budgets below are peaks measured on this model, with margin added.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np

DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
WEIGHTS = DATA / "prism-models" / "ZhengPeng7" / "model.safetensors"
STATUS = DATA / "prism-models" / "status.json"  # written by setup-model.py
SHA256 = "a5a4de698739ea5e0e8bbab28e1b293dde95092b87a442d566cbc585c53cef55"  # BiRefNet_HR-matting
SIZE = 2048  # the HR model's training resolution
MEAN, STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
NEEDS = ("torch", "torchvision", "kornia", "einops", "safetensors")
_STAMP = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "prism" / "weights.ok"

MIB, GIB = 1 << 20, 1 << 30
# VRAM (measured peaks, plus margin)
# Measured on an RTX 4060: weights 0.41 GB; one pass peaks at 5.0 GB including
# them; edge refinement peaks at 96 B/px.
VRAM_WEIGHTS = 512 * MIB           # the fp16 weights, resident while the batch runs
VRAM_INFER = int(5.0 * GIB)        # one 2048 x 2048 pass, on top of the weights
VRAM_PER_PX = 32                   # bytes per image pixel outside edge refinement (image, matte, resizes)
FUSION_PER_PX = 160                # bytes per pixel of an edge-refinement band
BAND_MARGIN = 100                  # rows of overlap per band: the blurs reach 90 + 6
VRAM_RESERVE = 768 * MIB           # always left free for the desktop and other apps
# RAM (measured: torch + CUDA 2.1 GB resident)
WORKER_RAM = 6 * GIB               # the batch worker's hard cgroup cap (convert.cutout_isolated)
RAM_BASE = int(2.5 * GIB)          # torch + CUDA in this process
RAM_PER_PX = 48                    # bytes per image pixel: decoded image, matte, result, PNG encode
RAM_RESERVE = 3 * GIB              # always left available for the desktop
# Past this, refuse outright: it must fit under the worker's cap with 10% to spare (~68 MP).
MAX_PIXELS = int((WORKER_RAM * 0.9 - RAM_BASE) // RAM_PER_PX)

# Progress inside one model pass. The network runs its backbone at two scales
# (4 stages each), then a squeeze block and the decoder: 10 hooked steps. This
# is the share of the pass done after each one, from a warm run on an RTX 4060.
PASS_STEPS = (0.135, 0.206, 0.534, 0.563, 0.593, 0.607, 0.693, 0.700, 0.712, 1.0)


def _quiet(*_args):
    pass


def _status():
    """What setup-model.py (run by the installers) found: ok, reason, python."""
    try:
        s = json.loads(STATUS.read_text())
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def python():
    """The interpreter that has PyTorch with CUDA: setup-model.py's choice (its
    own .model-venv when the system had none), else the system one."""
    s = _status()
    py = s.get("python")
    return py if s.get("ok") and isinstance(py, str) and os.access(py, os.X_OK) else "/usr/bin/python3"


def available():
    """Cheap check for the menus: no torch import (that alone takes seconds)."""
    if not WEIGHTS.is_file():
        return False
    if python() != "/usr/bin/python3":
        return True  # setup-model.py checked that interpreter runs the model on this GPU
    return all(importlib.util.find_spec(m) for m in NEEDS)


def unavailable_reason():
    """Why the model can't run here, for the refusal: the installer's verdict
    when it made one (no NVIDIA GPU, too little VRAM...)."""
    s = _status()
    if s and not s.get("ok") and s.get("reason"):
        return f"{s['reason']}; nothing was saved"
    return ("This picture needs the GPU model (BiRefNet_HR-matting), which isn't set up: "
            "run install-nautilus.sh or install-dolphin.sh; nothing was saved")


def _gb(n):
    return f"{n / GIB:.1f} GB"


def ram_available():
    """MemAvailable: what can be used without swapping (page cache counts)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0  # unknown: treat as none, refuse rather than guess


def gpu_free_quick():
    """Free VRAM in bytes from nvidia-smi (~50 ms, no torch import), or None."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits", "-i", "0"],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.split()[0]) * MIB
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _need_ram(step, have=None):
    have = ram_available() if have is None else have
    if have - step < RAM_RESERVE:
        raise RuntimeError(f"Not enough free RAM: this needs {_gb(step)} and {_gb(RAM_RESERVE)} stays free "
                           f"for the desktop, but only {_gb(have)} is available; nothing was saved")


def precheck():
    """Before the batch worker starts (in the caller, cheaply): enough RAM for
    torch and enough VRAM for the model, or a clear refusal."""
    if not available():
        raise RuntimeError(unavailable_reason())
    _need_ram(RAM_BASE)
    free = gpu_free_quick()
    if free is None:
        raise RuntimeError("Remove background needs the NVIDIA GPU, and nvidia-smi can't see it; nothing was saved")
    need = VRAM_WEIGHTS + VRAM_INFER + VRAM_RESERVE
    if free < need:
        raise RuntimeError(f"The GPU is busy: this needs {_gb(need)} of video memory free, "
                           f"only {_gb(free)} is; nothing was saved")


def check_image(w, h):
    """Before an image is decoded (from its header size): refuse what wouldn't fit."""
    px = w * h
    if px > MAX_PIXELS:
        raise RuntimeError(f"Too large to remove the background from ({w} x {h}, over {MAX_PIXELS // 1_000_000} MP); "
                           "nothing was saved")
    _need_ram(px * RAM_PER_PX)


def _verify():
    """The weights must be exactly BiRefNet_HR-matting. Hashing 444 MB takes
    ~0.5 s, so a good result is remembered per file size and mtime."""
    st = WEIGHTS.stat()
    key = f"{st.st_size} {st.st_mtime_ns} {SHA256}"
    try:
        if _STAMP.read_text() == key:
            return
    except OSError:
        pass
    h = hashlib.sha256()
    with open(WEIGHTS, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    if h.hexdigest() != SHA256:
        raise RuntimeError(f"{WEIGHTS} isn't the BiRefNet_HR-matting weights (checksum differs); nothing was saved")
    try:
        _STAMP.parent.mkdir(parents=True, exist_ok=True)
        _STAMP.write_text(key)
    except OSError:
        pass


class Model:
    def __init__(self, progress=_quiet):
        """progress(fraction 0..1) as loading goes."""
        if not WEIGHTS.is_file():
            raise RuntimeError(unavailable_reason())
        progress(0.0)
        _verify()
        progress(0.05)
        _need_ram(RAM_BASE)
        # Fewer gaps between what's in use and what's held: measured 5.1 GB used,
        # 6.0 GB held without it. Must be set before torch starts CUDA.
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
        import torch
        torch.set_num_threads(4)  # the work is on the GPU; don't spin up all 20 CPU cores
        if not torch.cuda.is_available():
            raise RuntimeError("Remove background needs the NVIDIA GPU, and CUDA isn't available; nothing was saved")
        free, total = torch.cuda.mem_get_info()
        need = VRAM_WEIGHTS + VRAM_INFER + VRAM_RESERVE
        if free < need:
            raise RuntimeError(f"The GPU is busy: this needs {_gb(need)} of video memory free, "
                               f"only {_gb(free)} is; nothing was saved")
        # Hard cap: this process may never take more than was free minus the
        # reserve. Past it, PyTorch raises OutOfMemoryError here instead of the
        # driver squeezing other apps.
        self.cap = free - VRAM_RESERVE
        torch.cuda.set_per_process_memory_fraction(self.cap / total)
        progress(0.45)  # torch and CUDA up (measured 2.2 s of ~5 s)
        from safetensors.torch import load_file
        from birefnet.birefnet import BiRefNet
        from birefnet.BiRefNet_config import BiRefNetConfig
        self.torch = torch
        progress(0.94)  # the network's code: torchvision, kornia (2.5 s); then the weights (0.3 s)
        # Built empty and filled straight from the file on the GPU: no time spent
        # on random initial weights that would be overwritten anyway.
        with torch.device("meta"):
            model = BiRefNet(BiRefNetConfig(bb_pretrained=False))
        weights = {k: v.half() if v.is_floating_point() else v
                   for k, v in load_file(str(WEIGHTS), device="cuda").items()}
        missing, unexpected = model.load_state_dict(weights, strict=False, assign=True)
        if missing or unexpected or any(t.is_meta for t in (*model.parameters(), *model.buffers())):
            raise RuntimeError("The model weights don't match the BiRefNet code; nothing was saved")
        self.model = model.eval()
        self.mean = torch.tensor(MEAN, device="cuda").view(3, 1, 1)
        self.std = torch.tensor(STD, device="cuda").view(3, 1, 1)
        # Real progress inside a pass: each hooked step waits for the GPU to
        # finish it (10 syncs a pass, negligible) and reports.
        self._on_step, self._steps = None, 0
        for layer in model.bb.layers:
            layer.register_forward_hook(self._step)
        model.squeeze_module.register_forward_hook(self._step)
        model.decoder.register_forward_hook(self._step)
        progress(1.0)

    def _step(self, *_args):
        if self._on_step is None:
            return
        self.torch.cuda.synchronize()
        self._steps += 1
        self._on_step(PASS_STEPS[min(self._steps, len(PASS_STEPS)) - 1])

    def _check_vram(self, h, w):
        """Before each image: what's free now (other apps change it) must cover
        this image's pass plus the reserve, and stay inside this process's cap."""
        torch = self.torch
        torch.cuda.empty_cache()  # hand back the last image's memory first
        need = VRAM_INFER + h * w * VRAM_PER_PX
        free, _total = torch.cuda.mem_get_info()
        mine = torch.cuda.memory_allocated()
        if free - need < VRAM_RESERVE or mine + need > self.cap:
            raise RuntimeError(f"The GPU is busy: this image needs {_gb(need)} of video memory, "
                               f"only {_gb(max(0, free - VRAM_RESERVE))} can be spared; nothing was saved")

    def spare(self):
        """VRAM this process may still use right now: what the driver says is
        free (minus the reserve), and what's left under this process's cap."""
        torch = self.torch
        torch.cuda.empty_cache()
        free, _total = torch.cuda.mem_get_info()
        return max(0, min(free - VRAM_RESERVE, self.cap - torch.cuda.memory_allocated()))

    def cut(self, rgb, progress=_quiet):
        """uint8 HxWx3 -> (uint8 HxWx3 foreground colours, float32 HxW alpha 0..1).
        progress(stage, fraction 0..1 of this image's work)."""
        torch, F = self.torch, self.torch.nn.functional
        h, w = rgb.shape[:2]
        self._check_vram(h, w)
        progress("Finding the subject", 0.0)
        self._steps = 0
        self._on_step = lambda f: progress("Finding the subject", 0.75 * f)
        try:
            with torch.inference_mode():
                image = torch.from_numpy(np.ascontiguousarray(rgb)).cuda().permute(2, 0, 1)  # uint8, 3 B/px
                # Bicubic with antialiasing, as PIL's resize in the reference code.
                x = F.interpolate(image[None].float().div_(255), size=(SIZE, SIZE), mode="bicubic",
                                  align_corners=False, antialias=True)
                x = ((x[0].clamp_(0, 1) - self.mean) / self.std)[None].half()
                pred = self.model(x)[-1].float().sigmoid()
                self._on_step = None
                del x
                alpha = F.interpolate(pred, size=(h, w), mode="bilinear", align_corners=False)[0].clamp_(0, 1)
                del pred
                progress("Refining edges", 0.78)
                fg = self._foreground(image, alpha, lambda f: progress("Refining edges", 0.78 + 0.2 * f))
                alpha = alpha[0].cpu().numpy()
                progress("Refining edges", 1.0)
                return fg, alpha
        except torch.cuda.OutOfMemoryError:
            self._on_step = None
            torch.cuda.empty_cache()
            raise RuntimeError("The GPU ran out of video memory (another app may have taken some); "
                               "nothing was saved")

    def _foreground(self, image, alpha, progress=_quiet):
        """Edge colours for the whole image, in horizontal bands sized to the VRAM
        free right now. Each band carries BAND_MARGIN rows of real neighbours on
        both sides (the two blurs reach 90 + 6 rows), so the kept rows are exactly
        what a single full-image pass would give. Done bands go straight to RAM."""
        torch = self.torch
        h, w = alpha.shape[-2:]
        out = np.empty((h, w, 3), np.uint8)
        rows = self.spare() // (w * FUSION_PER_PX) - 2 * BAND_MARGIN
        if rows < 64:
            raise RuntimeError("The GPU is busy: not enough video memory left to refine the edges; nothing was saved")
        for top in range(0, h, rows):
            bottom = min(h, top + rows)
            lo, hi = max(0, top - BAND_MARGIN), min(h, bottom + BAND_MARGIN)
            band = image[:, lo:hi].float().div_(255)
            fg = _foreground(torch, band, alpha[:, lo:hi])
            fg = fg[:, top - lo:bottom - lo].mul_(255).round_().to(torch.uint8)
            out[top:bottom] = fg.permute(1, 2, 0).cpu().numpy()
            del band, fg
            progress(bottom / h)
        return out


def _blur(torch, x, r):
    """Box blur, radius r, of a CxHxW tensor: separable, reflected border
    (cv2.blur's default BORDER_REFLECT_101, as in the reference)."""
    F = torch.nn.functional
    k = 2 * r + 1
    h, w = x.shape[-2:]
    mode = "reflect" if r < min(h, w) else "replicate"  # reflect needs the image wider than the radius
    x = F.avg_pool2d(F.pad(x[None], (r, r, 0, 0), mode=mode), (1, k), stride=1)
    return F.avg_pool2d(F.pad(x, (0, 0, r, r), mode=mode), (k, 1), stride=1)[0]


def _blur_fusion(torch, image, fg, bg, alpha, r):
    blurred_a = _blur(torch, alpha, r)
    blurred_fa = _blur(torch, fg * alpha, r).div_(blurred_a + 1e-5)
    blurred_b = _blur(torch, bg * (1 - alpha), r).div_((1 - blurred_a) + 1e-5)
    del blurred_a
    fg = blurred_fa + alpha * (image - alpha * blurred_fa - (1 - alpha) * blurred_b)
    return fg.clamp_(0, 1), blurred_b


def _foreground(torch, image, alpha):
    """The subject's own colours where it blends into the backdrop (BiRefNet's
    refine_foreground: blur-fusion, a coarse pass then a fine one)."""
    fg, bg = _blur_fusion(torch, image, image, image, alpha, 90)
    fg, _ = _blur_fusion(torch, image, fg, bg, alpha, 6)
    return fg


_model = None
_failed = None
_lock = None


def shared(progress=_quiet):
    """One model per process (a batch loads it once). Batches take turns on the
    GPU: two copies at once wouldn't fit in 8 GB. If it can't load, every file
    in the batch gets the same reason, without trying again."""
    global _model, _failed, _lock
    if _failed:
        raise RuntimeError(_failed)
    if _model is None:
        if _lock is None:
            run = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
            _lock = open(os.path.join(run, "prism-gpu.lock"), "w")
            fcntl.flock(_lock, fcntl.LOCK_EX)  # released when this process exits
        try:
            _model = Model(progress)
        except Exception as e:
            _failed = str(e) or type(e).__name__
            raise
    return _model


def release():
    """Hand the GPU memory back now (the batch is done), not at process exit."""
    global _model
    if _model is not None:
        torch = _model.torch
        _model = None
        torch.cuda.synchronize()
        torch.cuda.empty_cache()


def remove_background(rgba, progress=_quiet):
    """uint8 HxWx4 (upright, sRGB) -> uint8 HxWx4 with the background transparent.
    progress(stage, fraction 0..1). The model must be loaded (shared()) first."""
    h, w = rgba.shape[:2]
    check_image(w, h)
    fg, alpha = shared().cut(rgba[..., :3], progress)
    alpha = np.minimum(alpha, rgba[..., 3].astype(np.float32) / 255)  # never more opaque than before
    if not alpha.size or alpha.max() < 0.5:
        raise RuntimeError("Found no subject to keep; nothing was saved")
    out = np.empty_like(rgba)
    out[..., :3] = fg
    out[..., 3] = (alpha * 255).round().astype(np.uint8)
    out[..., :3][out[..., 3] == 0] = 0  # no hidden backdrop colour left in clear pixels
    return out
