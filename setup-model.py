#!/usr/bin/python3
"""Sets up REMOVE BG's GPU model (BiRefNet_HR-matting), run by the installers.

1. Checks the machine can run it: an NVIDIA GPU of the Turing generation
   (RTX 20) or newer with 8 GB of video memory, a driver new enough for current
   PyTorch, and enough RAM. If not, it says why and stops; flat graphics
   (logos, screenshots) still work without the model.
2. Finds PyTorch with CUDA: reuses a working one from /usr/bin/python3, or
   installs the build matching the driver into .model-venv next to Prism.
3. Downloads the weights (444 MB) and checks their SHA-256.

The outcome goes to ~/.local/share/prism-models/status.json, which Prism reads
to pick the interpreter and to explain a refusal. Re-running is cheap: what is
already in place is checked, not downloaded again.

    setup-model.py           check, ask (on a terminal), then set up
    setup-model.py --check   only check the machine and print the verdict
Environment: PRISM_SKIP_MODEL=1 skips everything; PRISM_MODEL_YES=1 doesn't ask.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import matting  # noqa: E402  (constants only: WEIGHTS, SHA256, NEEDS; no torch import)

URL = os.environ.get("PRISM_MODEL_URL",
                     "https://huggingface.co/ZhengPeng7/BiRefNet_HR-matting/resolve/main/model.safetensors")
WEIGHTS_MB = 444
VENV = HERE / ".model-venv"
STATUS = matting.STATUS

MIN_VRAM_MIB = 7680        # "8 GB" cards report ~8 GiB; a pass peaks at 5.5 GB plus what the desktop holds
MIN_RAM = 12 << 30         # the worker's 6 GB cap plus 3 GB always left for the desktop, plus the rest
MIN_CC = (7, 5)            # Turing: fp16 tensor cores; older cards are too slow (and CUDA 13 drops them)
# The newest PyTorch CUDA build the driver can run: (driver CUDA version, wheel index, GPUs it covers).
BUILDS = (((13, 0), "cu130"), ((12, 8), "cu128"), ((12, 6), "cu126"))
EXTRAS = ("kornia", "einops", "safetensors")


def say(msg=""):
    print(msg, flush=True)


def write_status(ok, reason="", python=None):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps({"ok": ok, "reason": reason, "python": python}, indent=1) + "\n")


def _run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 30), **kw)
    except (OSError, subprocess.SubprocessError):
        return None


def other_gpus():
    r = _run(["lspci"])
    if not r or r.returncode:
        return []
    return [line.split(": ", 1)[-1] for line in r.stdout.splitlines()
            if re.search(r"VGA compatible|3D controller|Display controller", line)]


def gpu():
    """The NVIDIA GPU with the most memory: dict(name, mib, cc, cuda), or None."""
    r = _run(["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader,nounits"])
    if not r or r.returncode:
        return None
    cards = []
    for line in r.stdout.strip().splitlines():
        try:
            name, mib, cc = (p.strip() for p in line.split(","))
            major, minor = cc.split(".")
            cards.append({"name": name, "mib": int(float(mib)), "cc": (int(major), int(minor))})
        except ValueError:
            continue
    if not cards:
        return None
    best = max(cards, key=lambda c: c["mib"])
    # The newest CUDA the driver supports, from the header: "CUDA Version: 12.8",
    # or "CUDA UMD Version: 13.4" on newer drivers.
    head = _run(["nvidia-smi"])
    m = re.search(r"CUDA (?:UMD )?Version:\s*(\d+)\.(\d+)", head.stdout if head else "")
    best["cuda"] = (int(m[1]), int(m[2])) if m else None
    return best


def ram_total():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return 0


def assess():
    """(ok, reason, wheel index or None)."""
    card = gpu()
    if card is None:
        found = other_gpus()
        if found:
            return False, ("needs an NVIDIA GPU (CUDA); this machine has " + "; ".join(found)), None
        return False, "needs an NVIDIA GPU with its driver installed (nvidia-smi wasn't found or sees no GPU)", None
    name, gb = card["name"], card["mib"] / 1024
    if card["mib"] < MIN_VRAM_MIB:
        return False, f"needs an NVIDIA GPU with 8 GB of video memory; the {name} has {gb:.0f} GB", None
    if card["cc"] < MIN_CC:
        return False, (f"needs an NVIDIA GPU from the RTX 20 series (Turing) or newer; the {name} "
                       f"(compute {card['cc'][0]}.{card['cc'][1]}) is too old to run it in time"), None
    if ram_total() < MIN_RAM:
        return False, f"needs 12 GB of RAM or more; this machine has {ram_total() / (1 << 30):.0f} GB", None
    cuda = card["cuda"]
    for need, index in BUILDS:
        if cuda and cuda >= need and not (index == "cu126" and card["cc"] >= (12, 0)):
            return True, f"{name}, {gb:.0f} GB, driver CUDA {cuda[0]}.{cuda[1]}", index
    have = f"{cuda[0]}.{cuda[1]}" if cuda else "unknown"
    return False, (f"needs a newer NVIDIA driver (one supporting CUDA 12.6 or later; this one: {have}). "
                   "Update the driver and run the installer again"), None


def runtime_works(python):
    """True if this interpreter has every package and torch can use the GPU."""
    code = ("import importlib.util, sys\n"
            f"missing = [m for m in {matting.NEEDS!r} if not importlib.util.find_spec(m)]\n"
            "if missing: sys.exit('missing: ' + ' '.join(missing))\n"
            "import torch\n"
            "assert torch.cuda.is_available(), 'torch has no CUDA'\n"
            "x = torch.ones(8, device='cuda', dtype=torch.float16); assert float((x * 2).sum()) == 16\n")
    r = _run([python, "-c", code], timeout=120)
    return bool(r and r.returncode == 0)


def install_runtime(index):
    """PyTorch for this driver in .model-venv. It sees the system packages (numpy,
    scipy, Pillow) but not ~/.local, so a user's other Pythons stay out of it."""
    py = VENV / "bin" / "python"
    if not py.exists():
        say(f"Creating {VENV}...")
        subprocess.run(["/usr/bin/python3", "-m", "venv", "--system-site-packages", str(VENV)], check=True)
    pip = [str(py), "-m", "pip", "install", "--upgrade", "--disable-pip-version-check"]
    builds = [i for _, i in BUILDS]
    for idx in builds[builds.index(index):]:  # the matching build, then older ones if it isn't published
        say(f"Installing PyTorch ({idx}, about 3 GB)...")
        if subprocess.run([*pip, "torch", "torchvision", "--index-url", f"https://download.pytorch.org/whl/{idx}"]).returncode == 0:
            break
    else:
        raise RuntimeError("PyTorch couldn't be installed (see pip's message above)")
    if subprocess.run([*pip, *EXTRAS]).returncode:
        raise RuntimeError("kornia, einops or safetensors couldn't be installed (see pip's message above)")
    return str(py)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch_weights():
    dest = matting.WEIGHTS
    if dest.is_file() and sha256(dest) == matting.SHA256:
        say("The model weights are in place.")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    say(f"Downloading the model weights ({WEIGHTS_MB} MB) from {URL}...")
    try:
        with urllib.request.urlopen(URL, timeout=60) as r, open(part, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            done, shown = 0, -10
            while block := r.read(1 << 20):
                out.write(block)
                done += len(block)
                if total and done * 100 // total >= shown + 10:
                    shown = done * 100 // total
                    say(f"  {shown}%")
        if sha256(part) != matting.SHA256:
            raise RuntimeError("the downloaded weights don't match their checksum (a damaged or changed file)")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    say("Downloaded and verified.")


def consent(index, need_runtime, need_weights):
    """Ask on the terminal (/dev/tty: under curl | bash, stdin is the script).
    No terminal at all (a script, CI): go ahead; PRISM_SKIP_MODEL=1 opts out."""
    if os.environ.get("PRISM_MODEL_YES"):
        return True
    parts = []
    if need_runtime:
        parts.append(f"PyTorch for this GPU ({index}, about 3 GB)")
    if need_weights:
        parts.append(f"the model weights ({WEIGHTS_MB} MB)")
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(f"REMOVE BG on photos needs {' and '.join(parts)}. Download now? [Y/n] ")
            tty.flush()
            answer = tty.readline().strip().lower()
    except OSError:
        return True
    return answer in ("", "y", "yes")


def main():
    if os.environ.get("PRISM_SKIP_MODEL"):
        return 0
    ok, reason, index = assess()
    if "--check" in sys.argv:
        say(("Can run REMOVE BG's GPU model: " if ok else "REMOVE BG's GPU model ") + reason)
        return 0 if ok else 1
    if not ok:
        write_status(False, f"Remove background on photos {reason}")
        say(f"REMOVE BG on photos {reason}. It still works on flat graphics (logos, screenshots).")
        return 0
    say(f"REMOVE BG's GPU model: {reason}.")
    python = "/usr/bin/python3"
    need_runtime = not runtime_works(python)
    if need_runtime and (VENV / "bin" / "python").exists() and runtime_works(str(VENV / "bin" / "python")):
        python, need_runtime = str(VENV / "bin" / "python"), False
    need_weights = not (matting.WEIGHTS.is_file() and sha256(matting.WEIGHTS) == matting.SHA256)
    if (need_runtime or need_weights) and not consent(index, need_runtime, need_weights):
        say("Skipped. Run the installer again to set it up later; flat graphics work without it.")
        return 0
    try:
        if need_runtime:
            python = install_runtime(index)
            if not runtime_works(python):
                raise RuntimeError("PyTorch was installed but can't use the GPU")
        else:
            say(f"Using PyTorch from {python}.")
        fetch_weights()
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        write_status(False, f"Remove background on photos isn't set up ({e}). Run the installer again")
        say(f"REMOVE BG's GPU model wasn't set up: {e}. Flat graphics still work; run the installer again to retry.")
        return 0
    write_status(True, reason, python)
    say("REMOVE BG's GPU model is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
