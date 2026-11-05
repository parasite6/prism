#!/usr/bin/python3
"""Prism without the ring, for Dolphin's right-click menu.

    menu.py <target> <file>...      run one target (png, compress3, half, clean, crop, ...)
    menu.py --dolphin-menus <dir>   write Dolphin service menus for every target into <dir>

Dolphin service menus nest one level only, so the ring's groups become top-level
submenus: Prism Convert, Prism Compress, Prism Rescale, Prism Edit, plus Prism Clean.
A menu only lists file types, not the exact selection, so every run first checks the
target against convert.targets_for() (the same list the ring offers) and says so in a
notification when it doesn't apply.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import convert  # noqa: E402
import notices  # noqa: E402

PREFIX = "dev.prism.menu-"  # every generated file starts with this (install-dolphin.sh replaces them all)
GLOBS = "/usr/share/mime/globs2"
SUBCLASSES = "/usr/share/mime/subclasses"


def notify(title, body=""):
    subprocess.run(["notify-send", "-a", "Prism", "-i", "document-save", title, body[:400]],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def describe(target):
    if target in convert.COMPRESS:
        return f"Compress ({convert.LEVEL_NAMES[convert.COMPRESS[target]].lower()})"
    if target in convert.RESIZE:
        return f"Resize to {convert.PERCENT[target]}%"
    if target in ("clean", "crop", "rotate"):
        return target.capitalize()
    if target == "nobg":
        return "Remove background"
    return target.upper()


def run(target, paths):
    files = [os.path.abspath(p) for p in paths if os.path.isfile(p)]
    if not files:
        notify(f"Prism: {describe(target)}", "Nothing to convert: select files, not folders.")
        return 1
    if target not in convert.targets_for(files):
        what = os.path.basename(files[0]) if len(files) == 1 else f"these {len(files)} files"
        notify(f"Prism: {describe(target)} isn't available", f"Not offered for {what}.")
        return 1
    if target in ("crop", "rotate"):  # the edit window, one image (as from the ring)
        env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
        subprocess.Popen(["/usr/bin/python3", os.path.join(HERE, "crop.py"), f"--{target}", files[0]],
                         env=env, stdout=subprocess.DEVNULL, start_new_session=True)
        return 0
    _done, failed = notices.convert_all(files, target)
    return 1 if failed else 0


# --- Dolphin service menus -------------------------------------------------

FAMILY = {}  # extension -> "image/", "audio/" or "video/" for the media kinds
for _family, _exts in (("image/", convert.IMAGE), ("audio/", convert.AUDIO), ("video/", convert.VIDEO)):
    FAMILY.update({e: _family for e in _exts})


def mime_types(exts):
    """MIME types for these extensions, from the system's shared-mime-info globs.
    A media extension only maps into its own family (.ogg is audio here, not
    video/ogg; .png not some application/ type)."""
    want, found = {e.lower() for e in exts}, set()
    with open(GLOBS) as f:
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split(":")
            ext = parts[2][2:].lower() if len(parts) >= 3 and parts[2].startswith("*.") else None
            if ext in want and parts[1].startswith(FAMILY.get(ext, "")):
                found.add(parts[1])
    return sorted(found)


def subclasses():
    """{parent MIME type: direct children}, from shared-mime-info."""
    children = {}
    try:
        with open(SUBCLASSES) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2:
                    children.setdefault(parts[1], set()).add(parts[0])
    except OSError:
        pass
    return children


def exclusions(mimes, children):
    """Subtypes KIO would also match (it matches by inheritance: text/plain for .txt
    would otherwise put Convert on every CSV, script and config file) that aren't
    ours. A type that is itself an ancestor of one of ours stays out of the list,
    since excluding it would exclude ours too."""
    ours = set(mimes)
    parents = {}  # a type can have several parents
    for p, cs in children.items():
        for c in cs:
            parents.setdefault(c, set()).add(p)
    ancestors, todo = set(), list(ours)
    while todo:
        for p in parents.get(todo.pop(), ()):
            if p not in ancestors:
                ancestors.add(p)
                todo.append(p)
    out, todo = set(), list(ours)
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in out and child not in ours:
                out.add(child)
                todo.append(child)
    return sorted(out - ancestors)


def service_menu(submenu, exts, targets, icon, single=False, children=None):
    """One .desktop service menu: a top-level submenu (or a lone action) for these types."""
    mimes = mime_types(exts)
    if not mimes or not targets:
        return None
    exe = f'/usr/bin/python3 "{os.path.join(HERE, "menu.py")}"'
    lines = ["[Desktop Entry]", "Type=Service", f"MimeType={';'.join(mimes)};",
             f"Actions={';'.join(targets)};", "X-KDE-Priority=TopLevel",
             # Local files only: for sftp/smb KIO would hand over a temporary download,
             # and the converted file would land in that temp folder, never seen.
             "X-KDE-Protocol=file"]
    excluded = exclusions(mimes, children if children is not None else subclasses())
    if excluded:
        lines.append(f"ExcludeServiceTypes={','.join(excluded)}")  # KConfig list: commas
    if submenu:
        lines.append(f"X-KDE-Submenu={submenu}")
    if single:
        lines.append("X-KDE-MaxNumberOfUrls=1")
    for t in targets:
        name = {"clean": "Prism Clean (remove metadata)", "nobg": "Prism Remove Background"}.get(t, describe(t))
        if t in convert.RESIZE:
            name = f"{convert.PERCENT[t]}%  ({convert.LABELS[t]})"
        elif t in convert.COMPRESS:
            name = convert.LEVEL_NAMES[convert.COMPRESS[t]]
        lines += ["", f"[Desktop Action {t}]", f"Name={name}", f"Icon={ICONS.get(t, icon)}", f"Exec={exe} {t} %F"]
    return "\n".join(lines) + "\n"


# Per-action icons: freedesktop MIME/action names, so any icon theme has them (checked
# against kora/breeze). Formats a theme has no icon of their own for (webp, avif, heic,
# opus) fall back to the menu's generic icon. A submenu shows its first action's icon.
ICONS = {
    "png": "image-png", "jpg": "image-jpeg", "gif": "image-gif", "ico": "image-x-ico",
    "mp4": "video-mp4", "webm": "video-webm", "mkv": "video-x-matroska", "mov": "video-quicktime",
    "mp3": "audio-mpeg", "flac": "audio-flac", "ogg": "audio-x-vorbis+ogg", "wav": "audio-x-wav",
    "m4a": "audio-mp4",
    "pdf": "application-pdf", "md": "text-markdown", "html": "text-html", "txt": "text-plain", "csv": "text-csv",
    "docx": "application-vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application-vnd.oasis.opendocument.text",
    "xlsx": "application-vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ods": "application-vnd.oasis.opendocument.spreadsheet",
    "pptx": "application-vnd.openxmlformats-officedocument.presentationml.presentation",
    "odp": "application-vnd.oasis.opendocument.presentation",
    "half": "zoom-out", "third": "zoom-out", "double": "zoom-in",
    # kora's transform-crop is an abstract shape and object-rotate-right a bare arrow:
    # crop marks and a picture-with-arrow read as crop and rotate at menu size.
    "crop": "image-crop", "rotate": "osd-rotate-cw", "clean": "edit-clear-all", "nobg": "draw-eraser",
}


def dolphin_menus():
    """{file name: contents}. Convert is one file per kind (a menu can't know the
    exact selection; files that share a submenu name merge into one submenu)."""
    kinds = {"image": convert.IMAGE, "video": convert.VIDEO, "audio": convert.AUDIO, "doc": convert.DOC,
             "sheet": convert.SHEET, "slides": convert.SLIDES, "pdf": convert.PDF}
    icons = {"image": "image-x-generic", "video": "video-x-generic", "audio": "audio-x-generic",
             "doc": "x-office-document", "sheet": "x-office-spreadsheet", "slides": "x-office-presentation",
             "pdf": "application-pdf"}
    heic = bool(convert._heic_encoder())
    children = subclasses()
    menus = {}
    for kind, exts in kinds.items():
        targets = [t for t in convert.TARGETS[kind] if t != "heic" or heic]
        menus[f"convert-{kind}"] = service_menu("Prism Convert", exts, targets, icons[kind], children=children)
    menus["compress"] = service_menu("Prism Compress", convert.COMPRESSIBLE_AUDIO | convert.COMPRESSIBLE_IMAGE,
                                     sorted(convert.COMPRESS), "archive-insert", children=children)
    menus["rescale"] = service_menu("Prism Rescale", convert.RESIZABLE_IMAGE | convert.RESIZABLE_VIDEO,
                                    ["half", "third", "double"], "transform-scale", children=children)
    edit = {e for e in convert.RESIZABLE_IMAGE if e not in ("heic", "heif") or heic}
    menus["edit"] = service_menu("Prism Edit", edit, ["crop", "rotate"], "transform-crop", single=True, children=children)
    menus["nobg"] = service_menu(None, convert.CUTOUT_FORMATS, ["nobg"], "draw-eraser", children=children)
    menus["clean"] = service_menu(None, convert.CLEANABLE, ["clean"], "edit-clear", children=children)
    return {f"{PREFIX}{name}.desktop": text for name, text in menus.items() if text}


def write_dolphin_menus(folder):
    os.makedirs(folder, exist_ok=True)
    for name, text in dolphin_menus().items():
        path = os.path.join(folder, name)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o755)  # KIO only runs user service menus that are executable
        os.replace(tmp, path)
        print(path)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--dolphin-menus":
        write_dolphin_menus(sys.argv[2])
    elif len(sys.argv) >= 3 and not sys.argv[1].startswith("-"):
        # Exit 0 even when nothing was done: Dolphin starts each action as a
        # systemd unit (app-dev.prism.menu-*@….service), and a non-zero exit marks
        # it failed, which failed-unit watchers report as a system fault. The
        # notification is the report; tracebacks still go to the journal.
        try:
            run(sys.argv[1], sys.argv[2:])
        except Exception as e:  # started from a menu: no terminal, so say it out loud
            notify(f"Prism: {describe(sys.argv[1])} failed", f"{type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    else:
        sys.exit(__doc__)
