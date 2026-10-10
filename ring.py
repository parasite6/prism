#!/usr/bin/python3
"""Prism: select files, hold the Forward button (or press the shortcut),
release on a format.

Runs as a resident daemon (`ring.py --daemon`) so the ring appears instantly:
starting Python + GTK per press took over a second. Clients (ring-ctl) send
NUL-terminated messages through a FIFO:

    show            open at the cursor in hold mode; formats follow with `files`
    files\\n<uris>   the selection (from the Nautilus script); opens if not shown
    release         the held button was released: pick the hovered slice
"""
import ctypes
import fcntl
import hashlib
import io
import json
import math
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote_to_bytes, urlsplit

# Software rendering only. The ring is simple 2D cairo drawing; GTK's default GL
# renderer opened the NVIDIA dGPU on every show (leaking device handles and
# keeping a laptop dGPU awake).
os.environ["GSK_RENDERER"] = "cairo"
os.environ["GDK_DISABLE"] = "gl,vulkan"
os.environ["GDK_BACKEND"] = "wayland"  # layer-shell is Wayland-only; never fall back to X11

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
import cairo  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Gtk, Gtk4LayerShell as LayerShell  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import binding  # noqa: E402
import convert  # noqa: E402

if not os.environ.get("XDG_RUNTIME_DIR"):
    sys.exit("prism: XDG_RUNTIME_DIR is not set (refusing to use a shared /tmp)")
RUN = os.path.join(os.environ["XDG_RUNTIME_DIR"], "prism")
TAP = 0.25  # a release sooner than this after the press is a tap: stay open for clicking
NO_FILES = 2.0  # shown but the selection never came (no Nautilus script, etc.): hide
IDLE = 60.0  # never leave a keyboard-grabbing overlay up unattended
DRY = bool(os.environ.get("PRISM_DRY"))
STOPPING = False  # a shutdown signal arrived; finishing running conversions first
BUSY = 0  # batches converting right now (guarded by BUSY_LOCK)
BUSY_LOCK = threading.Lock()  # tests: animate, never convert

# Look
ACCENT = (1.0, 0.55, 0.20)  # tangerine: the hovered petal
FROST = (1.0, 1.0, 1.0)  # petals; Hyprland's layer blur behind them makes the frost
FROST_ALPHA = 0.5
INK = (0.16, 0.145, 0.13)  # labels on frost
ON_ACCENT = (0.1, 0.06, 0.02)
PILL = (0.18, 0.44, 0.89)  # file name pills
R_OUT, R_IN = 150, 62  # ring radii
GROW = 12  # how far the hovered petal grows outward
PETAL_GAP = 0.075  # radians trimmed from each side of a petal
PETAL_ROUND = 14  # corner diameter
GHOST = 64  # thumbnail size
FONT = "Adwaita Sans"  # Inter-based; Inter itself is not installed here
GHOST_W, GHOST_H = 300, GHOST + 60
GHOST_TOP = GHOST / 2 + 14  # the ghost's centre, from the top of its layer
RING_REACH = R_OUT + GROW + PETAL_ROUND + 4  # everything the ring draws is within this of its centre  # the ghost layer: stacked cards and the name pill below
DWELL = 0.25  # resting this long on RESCALE / BACK switches rings


def debug(msg):
    """Off unless $XDG_RUNTIME_DIR/prism.debug exists."""
    if os.path.exists(RUN + ".debug"):
        with open(RUN + ".log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} ring {msg}\n")


def guarded(fallback=True, on_error=None):
    """GLib removes a callback for good when it raises, which silently left the
    ring deaf (FIFO), blind (mouse) or stuck on screen (animation). Log instead."""
    def wrap(fn):
        def run(*args):
            try:
                return fn(*args)
            except Exception:
                import traceback
                debug(f"{fn.__name__} failed:\n{traceback.format_exc()}")
                if on_error:
                    try:
                        on_error(args[0])
                    except Exception:
                        pass
                return fallback
        run.__name__ = fn.__name__
        return run
    return wrap


def ease_out_back(t):
    c = 1.70158
    t -= 1
    return 1 + (c + 1) * t ** 3 + c * t ** 2


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def hypr(cmd):
    """Query Hyprland directly over its socket: ~0.06 ms, versus ~8 ms for hyprctl."""
    try:
        path = f"{os.environ['XDG_RUNTIME_DIR']}/hypr/{os.environ['HYPRLAND_INSTANCE_SIGNATURE']}/.socket.sock"
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(0.05)
            s.connect(path)
            s.sendall(f"j/{cmd}".encode())
            data = b""
            while chunk := s.recv(65536):
                data += chunk
            return json.loads(data)
    except (KeyError, OSError, ValueError):
        return None


BTN_EXTRA = 276  # the mouse Forward button (Hyprland's mouse:276)
EV_KEY = 1
EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux


class ButtonWatch:
    """Reads the binding (Forward by default; see binding.py) straight from the
    mouse and keyboard: read-only, never grabbed, so apps still get the input.
    Hyprland's release bindings for mouse buttons were unreliable: sometimes no
    release, sometimes six."""

    def __init__(self, on_change, bound=None):
        self.on_change = on_change
        self.devices = {}  # path -> (fd, watch id)
        self.held = {}  # path -> modifier codes down on that device
        self.down = False
        self.set_binding(bound or binding.load(), rescan=False)
        self.rescan()
        # New/removed input devices (replugging the mouse): inotify, no polling.
        self.monitor = Gio.File.new_for_path("/dev/input").monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self.monitor.connect("changed", lambda *_: GLib.timeout_add(500, self.rescan))

    def set_binding(self, bound, rescan=True):
        """Watch a new binding: the trigger key/button and its modifiers' codes."""
        for path in list(self.devices):
            self.drop(path)
        self.trigger = bound["code"]
        self.mods = [binding.MODS[m] for m in bound["mods"]]
        self.codes = {self.trigger, *(c for pair in self.mods for c in pair)}
        debug(f"binding: {binding.describe(bound)}")
        if rescan:
            self.rescan()

    def has_codes(self, event):
        try:
            words = open(f"/sys/class/input/{event}/device/capabilities/key").read().split()
        except OSError:
            return False
        bits = int("".join(w.rjust(16, "0") for w in words), 16) if words else 0
        return any(bits >> code & 1 for code in self.codes)

    def rescan(self):
        for path in [p for p in self.devices if not os.path.exists(p)]:
            self.drop(path)
        for event in os.listdir("/sys/class/input"):
            path = f"/dev/input/{event}"
            if event.startswith("event") and path not in self.devices and self.has_codes(event):
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                except OSError:
                    continue  # not readable: the Ctrl+Alt+C shortcut still works
                try:
                    self.mask_to(fd, self.codes)
                except OSError:
                    os.close(fd)  # without the mask every mouse move (or keypress) would wake us
                    continue
                watch = GLib.io_add_watch(fd, GLib.PRIORITY_HIGH, GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                                          self.read, path)
                self.devices[path] = (fd, watch)
                debug(f"watching {path}")
        return GLib.SOURCE_REMOVE

    @staticmethod
    def mask_to(fd, codes=(BTN_EXTRA,)):
        """EVIOCSMASK: have the kernel deliver only the binding's key events to
        this fd, so mouse movement (~250 events/s) and typing never wake the daemon."""
        eviocsmask = (1 << 30) | (16 << 16) | (ord("E") << 8) | 0x93
        keys = ctypes.create_string_buffer(0x300 // 8)  # KEY_CNT bits, all off
        for code in codes:
            keys[code // 8] = keys[code // 8][0] | 1 << (code % 8)
        empty = ctypes.create_string_buffer(8)
        # EV_SYN is left unmasked: the kernel wakes readers on SYN_REPORT and
        # drops reports the mask left empty, so motion still never wakes us.
        # (Masking SYN, or EV_REP which has no mask, blocks or fails.)
        for ev_type in (EV_KEY, 2, 3, 4, 5, 0x11, 0x12, 0x15):  # KEY REL ABS MSC SW LED SND FF
            buf, size = (keys, len(keys)) if ev_type == EV_KEY else (empty, 0)
            fcntl.ioctl(fd, eviocsmask, struct.pack("IIQ", ev_type, size, ctypes.addressof(buf)))

    def drop(self, path):
        self.held.pop(path, None)
        fd, watch = self.devices.pop(path)
        GLib.source_remove(watch)
        os.close(fd)
        if self.down:  # unplugged mid-hold: treat it as a release
            self.down = False
            self.on_change(False)

    @guarded()
    def read(self, fd, cond, path):
        if cond & (GLib.IO_ERR | GLib.IO_HUP):
            self.drop(path)
            return False
        try:
            data = os.read(fd, EVENT.size * 64)
        except BlockingIOError:
            return True
        except OSError:
            self.drop(path)
            return False
        for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _, _, kind, code, value = EVENT.unpack_from(data, i)
            if kind != EV_KEY or value not in (0, 1):  # 2 is key repeat
                continue
            if code != self.trigger:  # a modifier
                held = self.held.setdefault(path, set())
                (held.add if value else held.discard)(code)
            elif value and not self.down and self.mods_held():
                self.down = True
                self.on_change(True)
            elif not value and self.down:  # released: the trigger alone decides
                self.down = False
                self.on_change(False)
        return True

    def mods_held(self):
        held = set().union(*self.held.values())
        return all(left in held or right in held for left, right in self.mods)


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def centred_text(cr, text, x, y, size, rgba, bold=False):
    cr.select_font_face(FONT, 0, 1 if bold else 0)
    cr.set_font_size(size)
    e = cr.text_extents(text)
    cr.move_to(x - e.width / 2 - e.x_bearing, y - e.height / 2 - e.y_bearing)
    cr.set_source_rgba(*rgba)
    cr.show_text(text)
    return e


def pill(cr, x, y, text, bg, bg_alpha, fg, alpha, size=11):
    cr.select_font_face(FONT, 0, 0)
    cr.set_font_size(size)
    w, h = cr.text_extents(text).x_advance + 16, size + 9
    rounded_rect(cr, x - w / 2, y - h / 2, w, h, h / 2 - 2)
    cr.set_source_rgba(*bg, bg_alpha * alpha)
    cr.fill()
    centred_text(cr, text, x, y, size, (*fg, alpha))


def paint_cover(cr, surface, x, y, side, radius, alpha):
    """The surface scaled to cover a rounded square, centre-cropped."""
    cr.save()
    rounded_rect(cr, x, y, side, side, radius)
    cr.clip()
    w, h = surface.get_width(), surface.get_height()
    sc = side / min(w, h)
    cr.translate(x + (side - w * sc) / 2, y + (side - h * sc) / 2)
    cr.scale(sc, sc)
    cr.set_source_surface(surface, 0, 0)
    cr.paint_with_alpha(alpha)
    cr.restore()


def short_name(name, limit=30):
    return name if len(name) <= limit else name[:limit - 13] + "…" + name[-12:]


def thumbnail(path):
    """A small cairo surface, without GdkPixbuf: its sandboxed loaders (glycin)
    leave threads and processes behind in a long-running daemon."""
    if convert.kind_of(path) not in ("image", "video"):
        return None
    uri = Gio.File.new_for_path(path).get_uri()
    digest = hashlib.md5(uri.encode()).hexdigest()
    cache = os.path.join(GLib.get_user_cache_dir(), "thumbnails")
    for size in ("normal", "large"):  # Nautilus has usually made one already
        try:
            return cairo.ImageSurface.create_from_png(os.path.join(cache, size, digest + ".png"))
        except (cairo.Error, OSError, MemoryError):
            pass
    if convert.kind_of(path) != "image":
        return None
    try:
        # Force the reader from the extension: magick otherwise sniffs content and
        # would hand a renamed PostScript/MSL file to its delegates.
        coder = {"jpg": "jpeg", "tif": "tiff"}.get(convert.ext_of(path), convert.ext_of(path))
        png = subprocess.run(["nice", "-n", "10", "magick", f"{coder}:{path}[0]", "-thumbnail",
                              f"{GHOST * 2}x{GHOST * 2}", "png:-"], capture_output=True, timeout=5).stdout
        return cairo.ImageSurface.create_from_png(io.BytesIO(png)) if png else None
    except (OSError, subprocess.TimeoutExpired, cairo.Error, MemoryError):
        return None


class Ring(Gtk.ApplicationWindow):
    """Created once and kept hidden between uses; begin() starts a session."""

    # Ring-swap state (begin() resets these per session; defaults for safety).
    rot = 0.0
    t_menu = 1.0
    armed = True
    hover_since = 0.0
    polled_at = 0.0
    ring_origin = (0, 0)  # where the ring layer sits (update_layers)

    def __init__(self, app):
        super().__init__(application=app)
        LayerShell.init_for_window(self)
        LayerShell.set_namespace(self, "prism")
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.EXCLUSIVE)
        LayerShell.set_exclusive_zone(self, -1)
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self, edge, True)

        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)

        # Three layers, so a frame only repaints what changed: a full-screen dim
        # (redrawn only while fading), the ring (only while its petals animate)
        # and the thumbnail ghost, which is just moved to follow the pointer.
        # One full-screen layer meant ~1920x1080 repainted and uploaded every frame.
        area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        area.set_draw_func(self.draw_dim)
        area.set_cursor(Gdk.Cursor.new_from_name("grabbing"))
        self.ring_area = Gtk.DrawingArea(can_target=False)
        self.ring_area.set_draw_func(self.draw_ring_layer)
        self.ghost_area = Gtk.DrawingArea(can_target=False)
        self.ghost_area.set_size_request(GHOST_W, GHOST_H)
        self.ghost_area.set_draw_func(self.draw_ghost_layer)
        self.stage = Gtk.Fixed(can_target=False)
        self.stage.put(self.ring_area, 0, 0)
        self.stage.put(self.ghost_area, 0, 0)
        overlay = Gtk.Overlay()
        overlay.set_child(area)
        overlay.add_overlay(self.stage)
        self.set_child(overlay)
        self.area = area
        self.drawn = {}  # layer -> what it last drew; redraw only when that changes

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        area.add_controller(motion)
        click = Gtk.GestureClick(button=0)
        click.connect("released", self.on_release)
        area.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        self.tick_id = None  # the frame callback exists only while shown (see end())

        self.active = False
        self.from_hold = False
        self.ignore_files_until = 0.0
        self.session = 0
        self.files, self.targets, self.pop = [], [], []
        self.thumb = None
        self.size_text = self.name_text = ""

    # --- sessions ------------------------------------------------------
    def begin(self, holding):
        """Show at the cursor now; the formats arrive with set_files()."""
        cur = hypr("cursorpos") or {"x": 0, "y": 0}
        mon = next((m for m in hypr("monitors") or [] if m.get("focused")), None)
        self.ox, self.oy = (mon["x"], mon["y"]) if mon else (0, 0)
        if mon:
            monitors = Gdk.Display.get_default().get_monitors()
            for i in range(monitors.get_n_items()):
                if monitors.get_item(i).get_connector() == mon["name"]:
                    LayerShell.set_monitor(self, monitors.get_item(i))

        self.session += 1
        self.active = True
        self.holding = self.from_hold = holding
        self.pressed_at = self.touched_at = time.monotonic()
        self.files, self.targets, self.pop = [], [], []
        self.thumb = None
        self.size_text = self.name_text = ""
        self.result = None
        self.cx, self.cy = cur["x"] - self.ox, cur["y"] - self.oy
        self.anchor_x, self.anchor_y = self.cx, self.cy
        self.ui_scale = 1.0
        self.px, self.py = self.cx, self.cy  # pointer
        self.gx, self.gy = self.cx, self.cy  # ghost (springs toward pointer)
        self.hover = -1
        self.hover_since = time.monotonic()
        self.armed = True  # dwell counts only after moving onto a petal
        self.menus = {}  # "main" plus one list per submenu: the rings it swaps between
        self.parent = None  # the submenu petal that the current sub ring came from
        self.rot = 0.0  # angle offset of petal 0, so BACK lands where its petal was
        self.t_menu = 1.0  # 0..1 petals fading in after a ring swap
        self.t_open = 0.0  # 0..1 open animation
        self.t_slices = 0.0  # 0..1 formats fading in once the selection arrives
        self.phase = "open"  # open -> idle -> drop | cancel
        self.t_out = 0.0
        self.last = None
        self.drawn = {}
        if self.tick_id is None:
            self.tick_id = self.area.add_tick_callback(self.tick)
        # Frame callbacks stop when the output is off, so the safety timeouts
        # run on a plain timer that hides the ring directly.
        GLib.timeout_add(500, self.watchdog, self.session)
        # While holding, the press belongs to Nautilus, so Wayland sends it the
        # pointer until release: tick() reads the cursor from Hyprland's socket,
        # once per frame, so the position is never older than the frame showing it.
        # A plain timer backs that up, should the frame clock ever stall.
        self.polled_at = 0.0
        if holding:
            GLib.timeout_add(12, self.poll_backstop, self.session)
        self.present()

    def set_files(self, files):
        if not self.active:
            if time.monotonic() < self.ignore_files_until:
                return  # late selection for a hold that was already released
            self.begin(holding=False)
        elif self.targets or self.phase not in ("open", "idle"):
            return  # one selection per session
        files = [f for f in files if os.path.isfile(f)]
        targets = convert.targets_for(files) if files else []
        if not targets:
            self.end()
            # Forward in Nautilus with nothing convertible selected is ordinary
            # navigation: stay silent. Only the explicit shortcut explains itself.
            if not self.from_hold:
                notify("Can't convert that selection" if files else "Nothing to convert",
                       "Mixed or unsupported file types." if files else "Select one or more files first.")
            return
        # Formats, sizes and compression levels each live in their own ring,
        # behind one petal: CONVERT, COMPRESS, RESCALE; then the direct actions
        # CROP (opens the crop window) and CLEAN.
        actions = [t for t in ("crop", "clean") if t in targets]
        groups = {"convert": [t for t in targets if t not in actions
                              and not any(t in group for group in convert.SUBMENUS.values())],
                  **{name: [t for t in targets if t in group] for name, group in convert.SUBMENUS.items()}}
        main = [name for name, items in groups.items() if items] + actions
        self.menus = {"main": main, **{name: ["back", *items] for name, items in groups.items() if items}}
        if len(main) == 1 and main[0] in self.menus:
            main = self.menus[main[0]][1:]  # a lone CONVERT petal is just an extra step
            self.menus = {"main": main}
        self.files, self.targets = files, main
        self.pop = [0.0] * len(main)
        try:
            self.size_text = GLib.format_size(sum(os.path.getsize(f) for f in files))
        except OSError:
            self.size_text = ""
        name = os.path.basename(files[0])
        self.name_text = short_name(name) + (f" +{len(files) - 1}" if len(files) > 1 else "")
        self.set_hover(self.slice_at(self.px, self.py))
        session = self.session

        def load():
            thumb = thumbnail(files[0])
            GLib.idle_add(lambda: setattr(self, "thumb", thumb) if self.session == session else None)

        threading.Thread(target=load, daemon=True).start()

    def release(self):
        """The held button came up: pick, cancel, or (a tap) stay open for clicks."""
        if not self.active or self.phase not in ("open", "idle") or not self.holding:
            return
        if not self.targets:
            # Released before Nautilus sent anything convertible: just a Forward
            # click. Hide, and drop the selection if it turns up a moment later.
            self.end()  # also ignores the selection if it arrives late
            return
        if time.monotonic() - self.pressed_at < TAP:
            self.holding = False  # a tap: stay open for clicking
            return
        self.holding = False
        if 0 <= self.hover < len(self.targets) and self.is_switch(self.targets[self.hover]) and not self.armed:
            return  # released right after a dwell swap, still on the landing petal: keep this ring
        self.pick(self.hover)

    def pick(self, idx):
        if idx >= 0 and self.targets and self.is_switch(self.targets[idx]):
            # Released/clicked on a ring switch: switch now and stay open for clicks.
            self.holding = False
            self.switch_menu(idx)
            return
        if idx >= 0 and self.targets:
            self.result = self.targets[idx]
            self.drop_to = self.slice_center(idx)
            self.phase = "drop"
            # Start converting now, while the drop animation plays.
            if DRY:
                pass
            elif self.result == "crop":
                open_crop(self.files[0])
            else:
                self.get_application().start(self.files, self.result)
        else:
            self.phase = "cancel"
        self.t_out = 0.0

    def is_switch(self, target):
        return target == "back" or target in self.menus and target != "main"

    def switch_menu(self, idx):
        """Swap the whole ring: main <-> a submenu. The new ring is rotated so
        its BACK petal (or, going back, the submenu's petal) sits under the pointer."""
        here = self.rot + idx * self.slice_span()  # angle of the petal being left
        if self.targets[idx] == "back":
            targets = self.menus["main"]
            landing = targets.index(self.parent)
        else:
            self.parent = self.targets[idx]
            targets = self.menus[self.parent]
            landing = targets.index("back")
        self.targets = targets
        self.rot = here - landing * self.slice_span()
        self.pop = [0.0] * len(targets)
        # The pointer may be in the grown band of the petal it dwelt on; start the
        # landing petal grown too, or it would fall outside and read as "nothing".
        self.pop[landing] = 1.0
        self.t_menu = 0.0  # petals and labels fade/stagger in again, like a fresh ring
        self.hover = self.slice_at(self.px, self.py)
        self.hover_since = time.monotonic()
        self.armed = False  # the pointer starts on BACK: don't bounce straight back

    def set_hover(self, idx):
        if idx != self.hover:
            self.hover = idx
            self.hover_since = time.monotonic()
            self.armed = True

    def end(self):
        if self.active and self.from_hold and not self.targets:
            # A hold that ended before Nautilus answered (released, cancelled or
            # timed out): drop that selection if it turns up afterwards.
            self.ignore_files_until = time.monotonic() + 3.0
        self.active = False
        self.holding = False
        self.set_visible(False)
        # A hidden layer surface's frame clock keeps ticking (~65 wakeups/s),
        # so detach the animation callback entirely while idle.
        if self.tick_id is not None:
            self.area.remove_tick_callback(self.tick_id)
            self.tick_id = None

    @guarded(fallback=False)
    def watchdog(self, session):
        if session != self.session or not self.active:
            return GLib.SOURCE_REMOVE
        now = time.monotonic()
        if self.phase in ("open", "idle") and (
                (not self.targets and now - self.pressed_at > NO_FILES + 0.5)
                or now - self.touched_at > IDLE + 0.5):
            self.end()
            return GLib.SOURCE_REMOVE
        if self.phase in ("drop", "cancel") and now - self.pressed_at > IDLE + 5:
            self.end()  # animation never finished (frames stopped)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    @guarded(fallback=False)
    @guarded(fallback=False)
    def poll_backstop(self, session):
        if session != self.session or not self.holding or self.phase not in ("open", "idle"):
            return GLib.SOURCE_REMOVE
        if time.monotonic() - self.polled_at > 0.012:  # tick() hasn't polled lately
            self.poll_cursor()
            self.update_layers()  # moving the ghost requests a frame, waking the clock
        return GLib.SOURCE_CONTINUE

    def poll_cursor(self):
        self.polled_at = time.monotonic()
        pos = hypr("cursorpos")
        if pos:
            self.on_motion(None, pos["x"] - self.ox, pos["y"] - self.oy)

    # --- input ---------------------------------------------------------
    def on_motion(self, _c, x, y):
        if (x, y) != (self.px, self.py):
            self.touched_at = time.monotonic()
        self.px, self.py = x, y
        self.set_hover(self.slice_at(x, y))

    def on_release(self, gesture, _n, x, y):
        if self.phase not in ("open", "idle"):
            return
        self.pick(self.slice_at(x, y) if gesture.get_current_button() == 1 else -1)

    def on_key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape and self.phase in ("open", "idle"):
            self.pick(-1)
        return True

    # --- geometry ------------------------------------------------------
    def slice_span(self):
        return 2 * math.pi / max(1, len(self.targets))

    def slice_center(self, i, r=None, *, drawing=False):
        a = -math.pi / 2 + self.rot + i * self.slice_span()
        r = r or (R_IN + R_OUT) / 2
        if not drawing:
            r *= getattr(self, "ui_scale", 1.0)
        return self.cx + r * math.cos(a), self.cy + r * math.sin(a)

    def slice_at(self, x, y):
        if not self.targets:
            return -1
        dx, dy = x - self.cx, y - self.cy
        d = math.hypot(dx, dy)
        scale = getattr(self, "ui_scale", 1.0)
        if self.phase == "open":
            scale *= 0.6 + 0.4 * ease_out_back(self.t_open)
        d /= scale
        if d < R_IN - 2:
            return -1
        a = (math.atan2(dy, dx) + math.pi / 2 - self.rot + self.slice_span() / 2) % (2 * math.pi)
        idx = int(a / self.slice_span()) % len(self.targets)
        return idx if d <= R_OUT + GROW * self.pop[idx] + 2 else -1

    def place_ring(self, width, height):
        # Use the configured layer-surface size: already logical and rotated.
        margin = R_OUT + 34
        self.ui_scale = min(1.0, width / (2 * margin), height / (2 * margin))
        inset = margin * self.ui_scale
        self.cx = max(inset, min(width - inset, self.anchor_x))
        self.cy = max(inset, min(height - inset, self.anchor_y))
        self.set_hover(self.slice_at(self.px, self.py))

    # --- animation -----------------------------------------------------
    @guarded(fallback=False, on_error=lambda self: (setattr(self, "tick_id", None), self.end()))
    def tick(self, widget, clock):
        if not self.active:
            self.tick_id = None
            return GLib.SOURCE_REMOVE
        now = clock.get_frame_time() / 1e6
        dt = min(0.05, now - self.last) if self.last else 0.016
        self.last = now
        if self.holding and self.phase in ("open", "idle"):
            self.poll_cursor()

        clock_now = time.monotonic()
        if self.phase in ("open", "idle") and (
                (not self.targets and clock_now - self.pressed_at > NO_FILES)
                or clock_now - self.touched_at > IDLE):
            self.pick(-1)  # watchdogs: never a stuck overlay
        if self.phase == "open":
            self.t_open = min(1.0, self.t_open + dt / 0.18)
            if self.t_open >= 1.0:
                self.phase = "idle"
        if self.targets:
            self.t_slices = min(1.0, self.t_slices + dt / 0.12)
        self.t_menu = min(1.0, self.t_menu + dt / 0.14)
        for i in range(len(self.pop)):
            goal = 1.0 if i == self.hover and self.phase in ("open", "idle") else 0.0
            self.pop[i] += (goal - self.pop[i]) * (1 - math.exp(-dt * 18))  # same feel at any refresh rate
        if (self.armed and self.phase in ("open", "idle") and 0 <= self.hover < len(self.targets)
                and self.is_switch(self.targets[self.hover])
                and clock_now - self.hover_since >= DWELL):
            self.switch_menu(self.hover)

        if self.phase == "drop":
            tx, ty = self.drop_to
        elif self.phase == "cancel":
            tx, ty = self.cx, self.cy
        else:
            tx, ty = self.px, self.py
        k = 1 - math.exp(-dt * (26 if self.phase in ("open", "idle") else 24))  # ghost trails the pointer ~40 ms
        self.gx += (tx - self.gx) * k
        self.gy += (ty - self.gy) * k

        if self.phase in ("drop", "cancel"):
            self.t_out = min(1.0, self.t_out + dt / (0.28 if self.phase == "drop" else 0.18))
            if self.t_out >= 1.0:
                self.end()  # removes this callback
                return GLib.SOURCE_CONTINUE
        self.update_layers()
        return GLib.SOURCE_CONTINUE

    def update_layers(self):
        """Redraw a layer only when what it shows changed; move the ghost."""
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        self.place_ring(width, height)
        out, _show, alpha = self.anim()
        side = 2 * math.ceil(RING_REACH * 1.05 * self.ui_scale)  # 1.05: the open bounce
        rx, ry = round(self.cx - side / 2), round(self.cy - side / 2)
        if self.ring_area.get_size_request() != (side, side):
            self.ring_area.set_size_request(side, side)
        self.ring_origin = (rx, ry)
        self.stage.move(self.ring_area, rx, ry)
        self.stage.move(self.ghost_area, self.gx - GHOST_W / 2, self.gy - GHOST_TOP)
        keys = {
            self.area: round(alpha, 3),
            self.ring_area: (rx, ry, side, round(alpha, 3), round(self.t_open, 3), round(self.t_slices, 3),
                             round(self.t_menu, 3), round(self.rot, 4), tuple(round(p, 3) for p in self.pop),
                             tuple(self.targets), self.thumb is not None, self.size_text, self.name_text),
            self.ghost_area: (round(alpha * self.t_slices, 3), round(out, 3), self.phase == "drop",
                              self.thumb is not None, self.name_text, len(self.files)),
        }
        for layer, key in keys.items():
            if self.drawn.get(layer) != key:
                self.drawn[layer] = key
                layer.queue_draw()

    # --- drawing -------------------------------------------------------
    def draw(self, _area, cr, _w, _h):
        """Everything in one surface (the three layers, composited; used by tests)."""
        if _w <= 0 or _h <= 0 or not self.active:
            return
        self.place_ring(_w, _h)
        self.draw_dim(None, cr, _w, _h)
        self.draw_ring(cr)
        if self.files:
            self.draw_ghost(cr, self.gx, self.gy)

    def anim(self):
        """(out, show, alpha): the close progress, ring scale and overall opacity."""
        out = ease_out_cubic(self.t_out) if self.phase in ("drop", "cancel") else 0.0
        show = ease_out_back(self.t_open) * (1 - out * 0.15)
        alpha = min(1.0, self.t_open * 2) * (1 - out)
        return out, show, alpha

    @guarded(fallback=False)
    def draw_dim(self, _area, cr, _w, _h):
        if self.active:  # dim the screen slightly so the ring reads as modal
            cr.set_source_rgba(0, 0, 0, 0.18 * self.anim()[2])
            cr.paint()

    @guarded(fallback=False)
    def draw_ring_layer(self, _area, cr, _w, _h):
        if self.active:
            cr.translate(-self.ring_origin[0], -self.ring_origin[1])
            self.draw_ring(cr)

    @guarded(fallback=False)
    def draw_ghost_layer(self, _area, cr, _w, _h):
        if self.active and self.files:
            self.draw_ghost(cr, GHOST_W / 2, GHOST_TOP)

    def draw_ring(self, cr):
        out, show, alpha = self.anim()
        fade = self.t_slices
        cr.save()
        cr.translate(self.cx, self.cy)
        scale = (0.6 + 0.4 * show) * self.ui_scale
        cr.scale(scale, scale)
        cr.translate(-self.cx, -self.cy)
        # Everything below fits in the ring's square. Clipping to it keeps each
        # push_group() a ring-sized buffer, not a full-screen one (~5x faster).
        reach = RING_REACH
        cr.rectangle(self.cx - reach, self.cy - reach, 2 * reach, 2 * reach)
        cr.clip()

        # soft shadow; kept under ignore_alpha (with the dim) so only petals get blurred
        cr.arc(self.cx, self.cy + 8, R_OUT + 10, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.12 * alpha)
        cr.fill()

        # frosted petals: rounded by a stroke the petal's own colour, composited
        # once as a group so the stroke doesn't double the alpha at the edges
        span = self.slice_span()
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_width(PETAL_ROUND)
        for i, ext in enumerate(self.targets):
            p = self.pop[i]
            a0 = -math.pi / 2 + self.rot + i * span - span / 2 + PETAL_GAP
            a1 = a0 + span - 2 * PETAL_GAP
            bloom = 0.75 + 0.25 * ease_out_back(self.t_menu)  # a swapped-in ring grows out
            r_in = R_IN + PETAL_ROUND / 2
            r_out = r_in + (R_OUT + GROW * p - PETAL_ROUND / 2 - r_in) * bloom
            cr.push_group()
            cr.new_path()  # show_text leaves a current point; arc() would draw a line from it
            cr.arc(self.cx, self.cy, r_out, a0, a1)
            cr.arc_negative(self.cx, self.cy, r_in, a1, a0)
            cr.close_path()
            cr.set_source_rgb(*mix(FROST, ACCENT, p))
            cr.fill_preserve()
            cr.stroke()
            cr.pop_group_to_source()
            swap = ease_out_cubic(self.t_menu)
            cr.paint_with_alpha((FROST_ALPHA + (0.92 - FROST_ALPHA) * p) * alpha * fade * swap)

            # stagger the labels in as the formats arrive (and after a ring swap)
            la = alpha * max(0.0, min(1.0, min(fade, self.t_menu) * 2.2 - i * 0.08))
            lx, ly = self.slice_center(i, (R_IN + R_OUT + GROW * p) / 2, drawing=True)
            text = convert.label(ext)
            # Fraction glyphs (½ ⅓) are drawn small by fonts; match them to the rest.
            size = (15 + 2 * p) * {"\u00bd": 1.35, "\u2153": 1.35, "2\u00d7": 1.15}.get(text, 1.0)
            # Long labels (RESCALE in a crowded ring) shrink to fit their petal.
            room = 2 * ((R_IN + R_OUT) / 2) * math.sin(max(0.1, span / 2 - PETAL_GAP)) - 14
            cr.select_font_face(FONT, 0, 1)
            cr.set_font_size(size)
            wide = cr.text_extents(text).width
            if wide > room:
                size *= room / wide
            centred_text(cr, text, lx, ly, size, (*mix(INK, ON_ACCENT, p), la), bold=True)

        # centre: frosted hub with the thumbnail and total size, name below
        hub = R_IN - 6
        cr.push_group()
        cr.arc(self.cx, self.cy, hub, 0, 2 * math.pi)
        cr.set_source_rgb(*FROST)
        cr.fill()
        cr.pop_group_to_source()
        cr.paint_with_alpha(FROST_ALPHA * alpha)
        if self.files:
            ha = alpha * fade
            side = 2 * (hub - 12)  # rounded square that stays inside the hub circle
            if self.thumb:
                paint_cover(cr, self.thumb, self.cx - side / 2, self.cy - side / 2, side, 18, ha)
            else:
                centred_text(cr, convert.ext_of(self.files[0]).upper()[:4] or "FILE", self.cx, self.cy - 14,
                             16, (*INK, ha), bold=True)
            if self.size_text:
                pill(cr, self.cx, self.cy + (0 if self.thumb else 10), self.size_text, (1, 1, 1), 0.85, INK, ha)
            pill(cr, self.cx, self.cy + hub + 12, self.name_text, PILL, 1.0, (1, 1, 1), ha)
        cr.restore()

    def draw_ghost(self, cr, gx, gy):
        """The dragged thumbnail, centred on (gx, gy)."""
        out, _show, alpha = self.anim()
        alpha *= self.t_slices
        s = GHOST * (1 - 0.85 * out if self.phase == "drop" else 1)
        ga = alpha if self.phase != "drop" else 1 - out
        x, y = gx - s / 2, gy - s / 2
        # stacked cards for multiple files
        for k in range(min(len(self.files), 3) - 1, -1, -1):
            off = k * 5
            rounded_rect(cr, x + off, y - off, s, s, 12)
            cr.set_source_rgba(1, 1, 1, 0.9 * ga)
            cr.fill()
        if self.thumb:
            paint_cover(cr, self.thumb, x + 3, y + 3, s - 6, 10, ga)
        else:
            label = convert.ext_of(self.files[0]).upper()[:4] or "FILE"
            centred_text(cr, label, gx, gy, 14 * s / GHOST, (*INK, ga), bold=True)
        if self.phase != "drop":
            pill(cr, gx, y + s + 14, self.name_text, PILL, 1.0, (1, 1, 1), ga)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.prism.Prism")
        self.window = None
        self.buffer = b""

    def do_activate(self):
        # Only the primary instance gets here (GApplication is single-instance);
        # a second start forwards activate, which re-checks the channel below.
        if not self.window:
            self.hold()  # stay resident with the window hidden
            self.window = Ring(self)
            self.fifo_fd = self.fifo_watch = None
            # Python-level signal handlers don't run while GTK's main loop is in C.
            for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, shutdown)
            convert.targets_for(["warm.png"])  # cache the encoder probe before first use
            # Remove half-written temp folders left by a previous ring that was killed.
            threading.Thread(target=convert.sweep_leftovers, daemon=True).start()
            self.buttons = ButtonWatch(self.on_forward)
        self.ensure_channel()

    def ensure_channel(self):
        """(Re)create the FIFO if it is missing or isn't our FIFO, and the pid
        file: a deleted FIFO used to leave the ring deaf until killed by hand."""
        path = RUN + ".fifo"
        try:
            st = os.lstat(path)
            ok = stat.S_ISFIFO(st.st_mode) and st.st_uid == os.getuid() and self.fifo_fd is not None \
                and os.fstat(self.fifo_fd).st_ino == st.st_ino
        except FileNotFoundError:
            ok = False
        if not ok:
            if self.fifo_watch:
                GLib.source_remove(self.fifo_watch)
                os.close(self.fifo_fd)
            if os.path.lexists(path):
                os.unlink(path)
            os.mkfifo(path, 0o600)
            self.fifo_fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)  # RDWR: never sees EOF
            self.fifo_watch = GLib.io_add_watch(self.fifo_fd, GLib.PRIORITY_HIGH, GLib.IO_IN, self.on_fifo)
            self.buffer = b""
        tmp = f"{RUN}.pid.{os.getpid()}"
        with open(tmp, "w") as f:
            f.write(str(os.getpid()))
        os.replace(tmp, RUN + ".pid")  # never write through a planted symlink

    @guarded(fallback=None)
    def on_forward(self, down):
        """Hold Forward in Nautilus: show now, ask Nautilus for the selection."""
        w = self.window
        debug(f"forward {'down' if down else 'up'}: active={w.active} holding={getattr(w, 'holding', None)} "
              f"hover={getattr(w, 'hover', None)} pointer=({getattr(w, 'px', 0):.0f},{getattr(w, 'py', 0):.0f}) "
              f"ring=({getattr(w, 'cx', 0):.0f},{getattr(w, 'cy', 0):.0f}) targets={len(w.targets)}")
        if not down:
            w.release()
            return
        if w.active and not w.holding:
            w.pick(-1)  # Forward again while open for clicking: dismiss it
            return
        if STOPPING:
            return
        win = hypr("activewindow") or {}
        if win.get("class") != "org.gnome.Nautilus":
            return  # Forward everywhere else stays plain Forward
        w.begin(holding=True)
        # Nautilus runs its script shortcut, which sends the selection.
        # GLib reaps the child itself (no zombie left in a long-running daemon).
        GLib.spawn_async(["hyprctl", "dispatch",
                          f'hl.dsp.send_shortcut({{ mods = "CTRL ALT", key = "c", window = "address:{win["address"]}" }})'],
                         flags=GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_DEV_NULL
                         | GLib.SpawnFlags.STDERR_TO_DEV_NULL)

    @guarded()
    def on_fifo(self, fd, _cond):
        try:
            self.buffer += os.read(fd, 65536)
        except BlockingIOError:
            return True
        *messages, self.buffer = self.buffer.split(b"\0")
        if len(self.buffer) > 1 << 20:
            self.buffer = b""  # a writer died mid-message long ago: don't grow forever
        for raw in messages:
            if STOPPING:
                break
            command, _, payload = os.fsdecode(raw).partition("\n")
            w = self.window
            debug(f"got {command!r}: active={w.active} holding={getattr(w, 'holding', None)} "
                  f"phase={getattr(w, 'phase', None)} targets={len(w.targets)} hover={getattr(w, 'hover', None)} "
                  f"held_for={time.monotonic() - getattr(w, 'pressed_at', 0):.3f}s")
            if command == "show":
                self.window.begin(holding=True)
            elif command == "files":
                self.window.set_files(parse_selection(payload))
            elif command == "release":
                self.window.release()
            elif command == "rebind":  # bind.py saved a new binding
                self.buttons.set_binding(binding.load())
        return True

    def start(self, files, ext):
        """Convert in parallel off the GTK thread, then report."""
        global BUSY

        def work():
            global BUSY
            try:
                with ThreadPoolExecutor(max_workers=min(4, len(files))) as pool:
                    jobs = [(f, pool.submit(convert.convert, f, ext)) for f in files]
                report(jobs, ext)
            finally:
                with BUSY_LOCK:
                    BUSY -= 1

        with BUSY_LOCK:
            BUSY += 1
        threading.Thread(target=work, daemon=False).start()


def notify(title, body=""):
    """Fire and forget: never block the ring (or a worker) on the notification daemon.
    Without DO_NOT_REAP_CHILD, GLib double-forks, so no zombie is left behind."""
    try:
        GLib.spawn_async(["notify-send", "-a", "Prism", "-i", "document-save", title, body],
                         flags=GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_DEV_NULL
                         | GLib.SpawnFlags.STDERR_TO_DEV_NULL)
    except GLib.Error:
        debug(f"notify failed: {title}: {body}")


def open_crop(path):
    """The crop tool is its own process and an ordinary (tiled) window."""
    env = [f"{k}={v}" for k, v in os.environ.items() if k != "LD_PRELOAD"]
    try:
        GLib.spawn_async(["/usr/bin/python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "crop.py"),
                          path], envp=env, flags=GLib.SpawnFlags.STDOUT_TO_DEV_NULL)
    except GLib.Error as e:
        notify("Can't open the crop tool", e.message)


def report(jobs, ext):
    done, failed = [], []
    for f, job in jobs:
        try:
            done.append(job.result())
        except Exception as e:  # report every failure, don't stop the batch
            failed.append(f"{os.path.basename(f)}: {e}")
    if done:
        body = os.path.basename(done[0]) if len(done) == 1 else ""
        notices = dict.fromkeys(note for result in done for note in result.warnings)
        body = "\n".join(filter(None, [body, *notices]))
        files = f"{len(done)} file" + ("s" if len(done) > 1 else "")
        if ext == "clean":
            title = f"Cleaned {files} (metadata removed)"
        elif ext in convert.RESIZE:
            title = f"Resized {files} to {convert.PERCENT[ext]}%"
        elif ext in convert.COMPRESS:
            title = f"Compressed {files} ({convert.LEVEL_NAMES[convert.COMPRESS[ext]].lower()})"
        else:
            title = f"Converted {len(done)} to {ext.upper()}"
        notify(title, body[:400])
    if failed:
        notify(f"{len(failed)} failed", "\n".join(failed)[:400])


def parse_selection(payload):
    """Nautilus sends file:// URIs (or paths, from the command line)."""
    paths = []
    for line in filter(None, payload.split("\n")):  # not splitlines(): U+2028 is a valid path char
        parts = urlsplit(line)
        if parts.scheme == "file" and parts.netloc in ("", "localhost"):
            paths.append(os.fsdecode(unquote_to_bytes(parts.path)))
        elif not parts.scheme:
            paths.append(line)
    return [os.path.abspath(p) for p in paths]


def shutdown(*_):
    """Exit once running conversions finish (at most 2 minutes), so a restart or
    logout never leaves half-written temp folders next to the user's files.
    Exits with os._exit: Gtk.Application.quit() doesn't end a held app."""
    global STOPPING
    if not STOPPING:
        STOPPING = True
        deadline = time.monotonic() + 120

        def wait():
            with BUSY_LOCK:
                busy = BUSY
            if busy and time.monotonic() < deadline:
                return GLib.SOURCE_CONTINUE
            really_exit()

        if BUSY:
            GLib.timeout_add(250, wait)
            return GLib.SOURCE_CONTINUE
    really_exit()


def really_exit():
    try:
        if open(RUN + ".pid").read() == str(os.getpid()):
            os.unlink(RUN + ".pid")
    except OSError:
        pass
    os._exit(0)


def daemon():
    # No display (started before the compositor was ready): exit instead of
    # lingering, since a stuck primary instance would swallow every later start.
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        sys.exit("prism: cannot open the Wayland display")
    try:
        App().run([])
    finally:
        shutdown()


if __name__ == "__main__":
    if sys.argv[1:] != ["--daemon"]:
        sys.exit("Run via ring-ctl / the Nautilus script; `ring.py --daemon` starts the resident ring.")
    daemon()
