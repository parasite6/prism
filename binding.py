"""Which key or mouse button opens the ring when held: $XDG_CONFIG_HOME/prism/binding.json.

A binding is modifiers (Ctrl, Shift, Alt, Super) plus one key or mouse button,
as evdev codes, so the daemon can watch it straight from /dev/input. The
default is the mouse Forward button on its own. bind.py writes it; ring.py and
hypr-binds.lua read it (the Lua side only needs the "hypr" string)."""
import json
import os
from pathlib import Path

# Modifier name -> its evdev codes (left, right).
MODS = {"CTRL": (29, 97), "SHIFT": (42, 54), "ALT": (56, 100), "SUPER": (125, 126)}
MOD_CODES = {code: name for name, codes in MODS.items() for code in codes}
MOD_LABELS = {"CTRL": "Ctrl", "SHIFT": "Shift", "ALT": "Alt", "SUPER": "Super"}
BTN_LEFT = 272
MOUSE = {273: "Right click", 274: "Middle click", 275: "Back", 276: "Forward"}
DEFAULT = {"mods": [], "code": 276, "label": "Forward"}
# Prism itself sends Ctrl+Alt+C to Nautilus to read the selection.
RESERVED = ({"CTRL", "ALT"}, 46)


def path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "prism" / "binding.json"


def is_mouse(code):
    return 0x110 <= code < 0x118  # BTN_MOUSE .. BTN_TASK


def hypr(binding):
    """Hyprland's name for it, e.g. "CTRL + ALT + code:38" or "mouse:276"."""
    key = f"mouse:{binding['code']}" if is_mouse(binding["code"]) else f"code:{binding['code'] + 8}"
    return " + ".join([*binding["mods"], key])


def describe(binding):
    return " + ".join([*(MOD_LABELS[m] for m in binding["mods"]), binding["label"]])


def make(mods, code, label):
    """A checked binding, or ValueError with a reason to show."""
    mods = [m for m in MODS if m in set(mods)]  # canonical order
    if code in MOD_CODES:
        raise ValueError("Add a key or button to the modifiers")
    if code == BTN_LEFT:
        raise ValueError("Left click can't be used")
    if not 0 < code < 0x300:
        raise ValueError("That key can't be read")
    if (set(mods), code) == RESERVED:
        raise ValueError("Ctrl + Alt + C is what Prism uses to read the selection")
    return {"mods": mods, "code": code, "label": str(label)[:40] or f"Key {code}"}


def load():
    """The saved binding, or the default if there is none (or it is unusable)."""
    try:
        data = json.loads(path().read_text())
        return make(data["mods"], int(data["code"]), data["label"])
    except (OSError, ValueError, KeyError, TypeError):
        return dict(DEFAULT)


def save(binding):
    """Atomically, so a crash never leaves half a file for the ring to read."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({**binding, "hypr": hypr(binding)}, indent=2) + "\n")
    os.replace(tmp, target)
