"""Conversion batches with live progress (shared by the ring and menu.py).

Progress shows in Prism's own window (progress.py): a small card in the centre
of the screen with "Converting 3 files to JPG", the current file, a bar, "1 of
3" and the time left. The result ("Converted 3 to JPG") and any failures then
arrive as notifications, so they stay in the notification history.

If the window can't open (no Wayland, no gtk4-layer-shell), the progress goes
to one notification instead: the freedesktop "value" hint (0-100) and
replaces-id, which swaync, dunst and KDE draw. The bar moves per finished file:
the converters report no progress inside a file.
"""
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

import convert

APP = ["notify-send", "-a", "Prism", "-i", "document-save"]
HERE = os.path.dirname(os.path.abspath(__file__))
LOAD_UNITS = 2.0  # Remove background: loading the model takes as long as ~2 photos
LAYER_SHELL = "/usr/lib64/libgtk4-layer-shell.so.0"


class ProgressWindow:
    """progress.py, fed one JSON line per update. .ok is False when it couldn't
    start (the caller falls back to a notification). Once the user hides it
    (the x), updates are quietly dropped: the work goes on."""

    def __init__(self):
        self.proc = None
        if not (os.environ.get("WAYLAND_DISPLAY") and os.path.exists(LAYER_SHELL)):
            return
        env = dict(os.environ, LD_PRELOAD=LAYER_SHELL)  # must load before libwayland-client
        try:
            self.proc = subprocess.Popen(["/usr/bin/python3", os.path.join(HERE, "progress.py")],
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, text=True, env=env, start_new_session=True)
        except OSError:
            self.proc = None

    @property
    def ok(self):
        return self.proc is not None

    def update(self, **msg):
        if self.proc is None or self.proc.stdin.closed:
            return
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):  # hidden by the user: it exited
            pass

    def close(self):
        if self.proc is None:
            return
        self.update(finish=True)
        try:
            self.proc.stdin.close()
        except OSError:
            pass


def send(title, body="", value=None, replace=None, sticky=False):
    """Show or update a notification; returns its id (None if it couldn't be shown).
    Blocks for the notify-send round trip (~10 ms): call it off the GTK thread."""
    argv = APP + ["--print-id"]
    if replace:
        argv += ["-r", str(replace)]
    if value is not None:
        argv += ["-h", f"int:value:{max(0, min(100, int(value)))}"]
    if sticky:
        argv += ["-t", "0"]  # stays while the batch runs (a long video would outlive the default)
    try:
        r = subprocess.run(argv + [title, body[:400]], capture_output=True, text=True, timeout=5)
        return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.split() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def files_label(n):
    return f"{n} file" + ("s" if n != 1 else "")


def doing(ext, n):
    """Title while the batch runs."""
    if ext == "clean":
        return f"Cleaning {files_label(n)}"
    if ext == "nobg":
        return f"Removing the background from {files_label(n)}"
    if ext in convert.RESIZE:
        return f"Resizing {files_label(n)} to {convert.PERCENT[ext]}%"
    if ext in convert.COMPRESS:
        return f"Compressing {files_label(n)} ({convert.LEVEL_NAMES[convert.COMPRESS[ext]].lower()})"
    return f"Converting {files_label(n)} to {ext.upper()}"


def done_title(ext, n):
    """Title once it's finished (the ring's wording, unchanged)."""
    if ext == "clean":
        return f"Cleaned {files_label(n)} (metadata removed)"
    if ext == "nobg":
        return f"Removed the background from {files_label(n)}"
    if ext in convert.RESIZE:
        return f"Resized {files_label(n)} to {convert.PERCENT[ext]}%"
    if ext in convert.COMPRESS:
        return f"Compressed {files_label(n)} ({convert.LEVEL_NAMES[convert.COMPRESS[ext]].lower()})"
    return f"Converted {n} to {ext.upper()}"


def _pooled(files, ext, work):
    """(index, result or exception) as each finishes, at most 4 at once."""
    with ThreadPoolExecutor(max_workers=min(4, len(files))) as pool:
        jobs = {pool.submit(work, f, ext): i for i, f in enumerate(files)}
        for job in as_completed(jobs):
            try:
                yield jobs[job], job.result()
            except Exception as e:  # report every failure, don't stop the batch
                yield jobs[job], e


def icon_for(ext):
    """The window's badge: the same icon as the Dolphin menu entry."""
    import menu  # (imports this module; loaded only once a batch starts)
    if ext in convert.COMPRESS:
        return "archive-insert"
    return menu.ICONS.get(ext, "emblem-synchronizing")


def convert_all(files, ext, work=None, window=None):
    """Convert files with live progress. work(src, ext) -> ConversionResult,
    run at most 4 at once; by default convert.convert_isolated, except Remove
    background, which runs the whole batch in one GPU worker (the model loads once)."""
    total = len(files)
    title = doing(ext, total)
    window = window or ProgressWindow()
    nid = None
    staged = work is None and ext == "nobg"  # the batch worker reports its stages
    # With stages, the last bit of the bar is releasing the GPU after the last file.
    scale = 0.98 if staged else 1.0
    # The bar is shared by real time spent: loading the model (~5 s) counts as
    # two photos (~2.4 s each, measured). If the first file never needed it
    # (a flat logo), that share is dropped.
    load_units, load_done = (LOAD_UNITS if staged else 0.0), 0.0

    def overall(current=0.0):
        return scale * (load_units * load_done + len(outcomes) + current) / (load_units + total)

    if window.ok:
        window.update(title=title, icon=icon_for(ext), file=os.path.basename(files[0]) if total == 1 else "",
                      stage="Starting", done=0, total=total, fraction=0.0)
    else:
        nid = send(title, f"0 of {total}", value=0, sticky=True)
    stream = (convert.cutout_isolated(files) if staged
              else _pooled(files, ext, work or convert.convert_isolated))
    outcomes = {}
    try:
        for i, outcome in stream:
            if isinstance(outcome, convert.Progress):
                if outcome.kind == "load":
                    load_done = outcome.fraction
                if not window.ok:
                    continue
                if outcome.kind == "release":
                    window.update(file="", stage=outcome.stage, fraction=scale)
                elif outcome.kind == "load":
                    window.update(stage=outcome.stage, fraction=overall())
                else:
                    window.update(file=os.path.basename(files[i]), stage=outcome.stage,
                                  fraction=overall(outcome.fraction))
                continue
            outcomes[i] = outcome
            if staged and not load_done:
                load_units = 0.0  # didn't need the model (so far)
            n = len(outcomes)
            if window.ok:
                mark = "\u2717 Failed" if isinstance(outcome, BaseException) else "\u2713 Done"
                window.update(done=n, file=os.path.basename(files[i]), stage=mark, fraction=overall())
            elif n < total:
                nid = send(title, f"{n} of {total}", value=n * 100 / total, replace=nid, sticky=True) or nid
    finally:
        window.close()
    done, failed = [], []
    for i, f in enumerate(files):  # in the order they were selected
        outcome = outcomes.get(i, RuntimeError("no result"))
        if isinstance(outcome, BaseException):
            failed.append(f"{os.path.basename(f)}: {outcome}")
        else:
            done.append(outcome)
    if done:
        body = os.path.basename(done[0]) if len(done) == 1 else ""
        notices = dict.fromkeys(note for result in done for note in result.warnings)
        send(done_title(ext, len(done)), "\n".join(filter(None, [body, *notices])), replace=nid)
        nid = None  # the next one is separate
    if failed:
        send(f"{len(failed)} failed", "\n".join(failed), replace=nid)
    return done, failed
