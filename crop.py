#!/usr/bin/python3
"""Prism's edit window: one image, cropped or rotated, then Replace / Save / Copy.

    crop.py [--crop | --rotate] IMAGE

An ordinary window (Hyprland tiles it; no title bar, Esc closes), run by the
ring daemon as its own process, so a huge image or a crash here can't take the
ring down. The pixel work is convert.crop(); this file only collects the box
(crop) or the orientation (rotate: quarter turns and flips)."""
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["GSK_RENDERER"] = "cairo"
os.environ["GDK_BACKEND"] = "wayland"
# Save goes through xdg-desktop-portal's file chooser, never GTK's built-in one.
os.environ["GDK_DEBUG"] = ",".join(filter(None, [os.environ.get("GDK_DEBUG"), "portals"]))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
import cairo  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert  # noqa: E402

# Prism's look: tangerine and white, on a deep warm grey.
ACCENT = (1.0, 0.55, 0.20)
BACKDROP = (0.085, 0.08, 0.075)
FONT = "Adwaita Sans"
HANDLE = 16  # px from an edge or corner that still grabs it
INSETS = (48, 64, 48, 112)  # left, top, right, bottom: room for the chip and toolbar
EASE = 0.22  # s, box and fade animations
ZOOM_STEP = 1.5  # Ctrl + / Ctrl -
ZOOM_PIXEL = 24  # max zoom: one image pixel this many screen pixels wide
CSS = b"""
window.crop { background-color: #161412; font-family: "Adwaita Sans", "Inter", sans-serif; }
.chip { background-color: rgba(22, 20, 18, 0.78); color: rgba(255, 255, 255, 0.9);
        border-radius: 10px; padding: 6px 12px; font-size: 13px; }
.chip .dim { color: rgba(255, 255, 255, 0.5); }
.toolbar { background-color: rgba(255, 255, 255, 0.96); border-radius: 18px; padding: 6px;
           box-shadow: 0 10px 36px rgba(0, 0, 0, 0.45), 0 1px 0 rgba(255, 255, 255, 0.6) inset; }
.toolbar label { color: #2a2520; font-size: 14px; font-weight: 600; }
.toolbar .size { color: #8a7f74; font-weight: 500; font-feature-settings: "tnum"; padding: 0 10px 0 8px; }
.toolbar .question { padding: 0 12px 0 10px; }
.toolbar .sep { background-color: rgba(0, 0, 0, 0.09); min-width: 1px; margin: 8px 4px; }
.toolbar button { background: none; border: none; box-shadow: none; text-shadow: none; color: #2a2520;
                  border-radius: 12px; padding: 8px 14px; min-height: 0;
                  transition: background-color 120ms ease, color 120ms ease, opacity 120ms ease; }
.toolbar button image { color: #2a2520; -gtk-icon-size: 16px; }
.toolbar button:hover { background-color: rgba(0, 0, 0, 0.06); }
.toolbar button:active { background-color: rgba(0, 0, 0, 0.11); }
.toolbar button.primary { background-image: linear-gradient(to bottom, #ff9b4a, #ff7f24); color: #1f1206;
                          box-shadow: 0 2px 8px rgba(255, 127, 36, 0.35); }
.toolbar button.primary label, .toolbar button.primary image { color: #1f1206; }
.toolbar button.primary:hover { background-image: linear-gradient(to bottom, #ffab63, #ff8c33); }
.toolbar button.primary:active { background-image: linear-gradient(to bottom, #f5832a, #f07215); }
.toolbar button:disabled { opacity: 0.35; box-shadow: none; }
.toast { background-color: rgba(28, 26, 24, 0.94); color: #ffffff; border-radius: 12px; padding: 9px 16px;
         font-size: 13px; font-weight: 500; box-shadow: 0 8px 28px rgba(0, 0, 0, 0.4); }
.toast.error { background-color: #c8401f; }
"""


def ease(t):
    return 1 - (1 - min(1.0, max(0.0, t))) ** 3


class Box:
    """The crop box, in the image's upright pixels, and the view mapping."""
    def __init__(self, width, height):
        self.w, self.h = width, height
        self.box = [0.0, 0.0, float(width), float(height)]
        self.s, self.ox, self.oy = 1.0, 0.0, 0.0
        # View zoom (the window only, never the crop): 1 = whole image fits; the
        # view is centred on image point (vx, vy).
        self.zoom, self.vx, self.vy = 1.0, width / 2, height / 2
        self.view = (1, 1)

    def layout(self, width, height):
        left, top, right, bottom = INSETS
        aw, ah = max(1, width - left - right), max(1, height - top - bottom)
        self.view = (width, height)
        self.fit = max(1e-6, min(aw / self.w, ah / self.h))
        self.s = self.fit * self.zoom
        # Keep the image covering the view when it's bigger than it, centred when smaller.
        for axis, size, room in (("vx", self.w, aw), ("vy", self.h, ah)):
            half = room / (2 * self.s)
            v = size / 2 if size * self.s <= room else max(half, min(size - half, getattr(self, axis)))
            setattr(self, axis, v)
        self.ox = left + aw / 2 - self.vx * self.s
        self.oy = top + ah / 2 - self.vy * self.s

    def max_zoom(self):
        return max(1.0, ZOOM_PIXEL / self.fit)

    def zoom_at(self, zoom, px, py):
        """Zoom the view to `zoom`, keeping the image point under (px, py) there."""
        ix, iy = self.to_image(px, py)
        left, top, right, bottom = INSETS
        cx = left + (self.view[0] - left - right) / 2
        cy = top + (self.view[1] - top - bottom) / 2
        self.zoom = max(1.0, min(self.max_zoom(), zoom))
        s = self.fit * self.zoom
        self.vx, self.vy = ix - (px - cx) / s, iy - (py - cy) / s
        self.layout(*self.view)

    def pan(self, dx, dy):
        """Move the view by (dx, dy) screen pixels."""
        self.vx -= dx / self.s
        self.vy -= dy / self.s
        self.layout(*self.view)

    def to_screen(self, x, y):
        return self.ox + x * self.s, self.oy + y * self.s

    def to_image(self, x, y):
        return (x - self.ox) / self.s, (y - self.oy) / self.s

    def grab(self, px, py):
        """What a press here drags: an edge/corner ('n', 'se', ...), 'move' or 'new'."""
        (x0, y0), (x1, y1) = self.to_screen(*self.box[:2]), self.to_screen(*self.box[2:])
        across_x, across_y = x0 - HANDLE <= px <= x1 + HANDLE, y0 - HANDLE <= py <= y1 + HANDLE
        v = h = ""
        if across_x and min(abs(py - y0), abs(py - y1)) <= HANDLE:
            v = "n" if abs(py - y0) < abs(py - y1) else "s"
        if across_y and min(abs(px - x0), abs(px - x1)) <= HANDLE:
            h = "w" if abs(px - x0) < abs(px - x1) else "e"
        if v or h:
            return v + h
        return "move" if x0 < px < x1 and y0 < py < y1 else "new"

    def drag(self, mode, start, a, b):
        """Box after dragging `mode` from image point a to b, starting from box `start`."""
        clamp = lambda v, lo, hi: max(lo, min(hi, v))
        x0, y0, x1, y1 = start
        dx, dy = b[0] - a[0], b[1] - a[1]
        if mode == "move":
            dx, dy = clamp(dx, -x0, self.w - x1), clamp(dy, -y0, self.h - y1)
            return [x0 + dx, y0 + dy, x1 + dx, y1 + dy]
        if mode == "new":
            ax, ay = clamp(a[0], 0, self.w), clamp(a[1], 0, self.h)
            bx, by = clamp(b[0], 0, self.w), clamp(b[1], 0, self.h)
            return [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]
        if "w" in mode:
            x0 = clamp(x0 + dx, 0, x1 - 1)
        if "e" in mode:
            x1 = clamp(x1 + dx, x0 + 1, self.w)
        if "n" in mode:
            y0 = clamp(y0 + dy, 0, y1 - 1)
        if "s" in mode:
            y1 = clamp(y1 + dy, y0 + 1, self.h)
        return [x0, y0, x1, y1]

    def rect(self):
        """(x, y, width, height) in whole pixels, inside the image."""
        x, y = (max(0, min(round(v), n - 1)) for v, n in zip(self.box[:2], (self.w, self.h)))
        right, bottom = min(self.w, round(self.box[2])), min(self.h, round(self.box[3]))
        return x, y, max(1, right - x), max(1, bottom - y)

    def whole(self):
        return self.rect() == (0, 0, self.w, self.h)


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


_checker = None


def checker():
    """Transparency shows as a quiet checkerboard, as in every image editor."""
    global _checker
    if _checker is None:
        tile = cairo.ImageSurface(cairo.FORMAT_RGB24, 16, 16)
        c = cairo.Context(tile)
        c.set_source_rgb(0.22, 0.21, 0.2)
        c.paint()
        c.set_source_rgb(0.28, 0.27, 0.26)
        c.rectangle(0, 0, 8, 8)
        c.rectangle(8, 8, 8, 8)
        c.fill()
        _checker = cairo.SurfacePattern(tile)
        _checker.set_extend(cairo.EXTEND_REPEAT)
    return _checker


def draw(cr, width, height, box, preview, dragging=False, fade=1.0):
    """The image, everything outside the box dimmed, and the box with its handles."""
    backdrop(cr, width, height)
    box.layout(width, height)
    ix, iy, iw, ih = box.ox, box.oy, box.w * box.s, box.h * box.s
    for k in range(1, 7):  # soft shadow under the image
        rounded(cr, ix - k * 2, iy - k * 2 + 6, iw + k * 4, ih + k * 4, 6 + k * 2)
        cr.set_source_rgba(0, 0, 0, 0.06 * fade)
        cr.fill()
    if preview.get_format() == cairo.FORMAT_ARGB32:
        cr.rectangle(ix, iy, iw, ih)
        cr.set_source(checker())
        cr.fill()
    cr.save()
    cr.rectangle(ix, iy, iw, ih)
    cr.clip()
    cr.translate(ix, iy)
    cr.scale(iw / preview.get_width(), ih / preview.get_height())
    cr.set_source_surface(preview, 0, 0)
    # Zoomed past the preview's own pixels: show them crisp, for exact edges.
    deep = box.s * box.w / preview.get_width() > 2
    cr.get_source().set_filter(cairo.FILTER_NEAREST if deep else cairo.FILTER_GOOD)
    cr.paint_with_alpha(fade)
    cr.restore()
    if fade < 1:
        return
    (x0, y0), (x1, y1) = box.to_screen(*box.box[:2]), box.to_screen(*box.box[2:])
    cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
    cr.rectangle(ix, iy, iw, ih)
    cr.rectangle(x0, y0, x1 - x0, y1 - y0)
    cr.set_source_rgba(0.04, 0.035, 0.03, 0.66)
    cr.fill()
    cr.set_fill_rule(cairo.FILL_RULE_WINDING)
    if dragging:  # rule of thirds while adjusting
        cr.set_source_rgba(1, 1, 1, 0.28)
        cr.set_line_width(1)
        for k in (1, 2):
            gx, gy = round(x0 + (x1 - x0) * k / 3) + 0.5, round(y0 + (y1 - y0) * k / 3) + 0.5
            cr.move_to(gx, y0); cr.line_to(gx, y1)
            cr.move_to(x0, gy); cr.line_to(x1, gy)
        cr.stroke()
    cr.set_source_rgba(1, 1, 1, 0.9)
    cr.set_line_width(1.5)
    cr.rectangle(x0, y0, x1 - x0, y1 - y0)
    cr.stroke()
    # Tangerine corner brackets, just outside the box, and small edge grips.
    arm = max(6.0, min(22.0, (x1 - x0) / 3, (y1 - y0) / 3))
    cr.set_source_rgb(*ACCENT)
    cr.set_line_width(3.5)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    o = 1.5
    for cx, cy, sx, sy in ((x0 - o, y0 - o, 1, 1), (x1 + o, y0 - o, -1, 1), (x1 + o, y1 + o, -1, -1),
                           (x0 - o, y1 + o, 1, -1)):
        cr.move_to(cx + sx * arm, cy); cr.line_to(cx, cy); cr.line_to(cx, cy + sy * arm)
    cr.stroke()
    cr.set_source_rgba(1, 1, 1, 0.95)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    if x1 - x0 > 4 * arm:
        for yy in (y0, y1):
            rounded(cr, mx - 10, yy - 2.5, 20, 5, 2.5)
    if y1 - y0 > 4 * arm:
        for xx in (x0, x1):
            rounded(cr, xx - 2.5, my - 10, 5, 20, 2.5)
    cr.fill()
    if dragging:  # the live size, just above the box (inside it when there's no room)
        x, y, w, h = box.rect()
        text = f"{w} × {h}"
        cr.select_font_face(FONT, 0, 1)
        cr.set_font_size(12)
        ext = cr.text_extents(text)
        bw, bh = ext.x_advance + 18, 24
        bx = min(max(x0, ix), ix + iw - bw)
        by = y0 - bh - 10 if y0 - bh - 10 > 8 else y0 + 10
        rounded(cr, bx, by, bw, bh, 8)
        cr.set_source_rgba(0.08, 0.075, 0.07, 0.86)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(bx + 9, by + bh / 2 + 4.5)
        cr.show_text(text)


# Orientations as 2x2 matrices from image to screen (y down): what rotate shows.
IDENTITY = ((1, 0), (0, 1))
TURN_RIGHT = ((0, -1), (1, 0))  # clockwise on screen
TURN_LEFT = ((0, 1), (-1, 0))
FLIP_H = ((-1, 0), (0, 1))  # mirror left-right
FLIP_V = ((1, 0), (0, -1))  # mirror top-bottom


def mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(2)) for j in range(2)) for i in range(2))


def turns_mirror(m):
    """m as convert.crop's (turns, mirror): mirror left-right first, then turn clockwise."""
    mirror = m[0][0] * m[1][1] - m[0][1] * m[1][0] < 0
    unmirrored = mul(m, FLIP_H) if mirror else m
    r = IDENTITY
    for turns in range(4):
        if r == unmirrored:
            return turns, mirror
        r = mul(TURN_RIGHT, r)
    raise ValueError(m)


def draw_rotate(cr, width, height, size, preview, m, motion=None, fade=1.0):
    """The whole image in orientation m, fitted to the window; motion = (step, t) glides
    from m to step * m: a quarter turn swings round, a flip folds over its axis."""
    backdrop(cr, width, height)
    left, top, right, bottom = INSETS
    aw, ah = max(1, width - left - right), max(1, height - top - bottom)
    w, h = size
    anim = ((1, 0), (0, 1))
    fit = m
    if motion:
        step, t = motion
        e = ease(t)
        if step in (TURN_RIGHT, TURN_LEFT):
            a = (math.pi / 2 if step == TURN_RIGHT else -math.pi / 2) * e
            anim = ((math.cos(a), -math.sin(a)), (math.sin(a), math.cos(a)))
            fit = anim  # applied to m below; sizes the swing so it never clips
        else:  # a fold: squash through zero along the flip's axis
            k = 1 - 2 * e
            k = math.copysign(max(abs(k), 0.002), k)  # edge-on is a singular matrix: cairo refuses it
            anim = ((k, 0), (0, 1)) if step == FLIP_H else ((1, 0), (0, k))
            fit = ((1, 0), (0, 1))
        fit = tuple(tuple(sum(fit[i][k] * m[k][j] for k in range(2)) for j in range(2)) for i in range(2))
    bw = abs(fit[0][0]) * w + abs(fit[0][1]) * h
    bh = abs(fit[1][0]) * w + abs(fit[1][1]) * h
    s = min(aw / bw, ah / bh)
    full = tuple(tuple(sum(anim[i][k] * m[k][j] for k in range(2)) for j in range(2)) for i in range(2))
    cx, cy = left + aw / 2, top + ah / 2
    cr.save()
    cr.translate(cx, cy)
    cr.transform(cairo.Matrix(full[0][0], full[1][0], full[0][1], full[1][1], 0, 0))
    cr.scale(s, s)
    for k in range(1, 7):  # soft shadow under the image
        rounded(cr, -w / 2 - k * 2 / s, -h / 2 - k * 2 / s, w + k * 4 / s, h + k * 4 / s, (6 + k * 2) / s)
        cr.set_source_rgba(0, 0, 0, 0.06 * fade)
        cr.fill()
    if preview.get_format() == cairo.FORMAT_ARGB32:
        cr.rectangle(-w / 2, -h / 2, w, h)
        cr.save()
        cr.identity_matrix()
        cr.set_source(checker())
        cr.restore()
        cr.fill()
    cr.translate(-w / 2, -h / 2)
    cr.scale(w / preview.get_width(), h / preview.get_height())
    cr.set_source_surface(preview, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_GOOD)
    cr.rectangle(0, 0, preview.get_width(), preview.get_height())
    cr.clip()
    cr.paint_with_alpha(fade)
    cr.restore()


def backdrop(cr, width, height):
    glow = cairo.RadialGradient(width / 2, height * 0.45, 0, width / 2, height * 0.45, max(width, height) * 0.7)
    glow.add_color_stop_rgb(0, 0.13, 0.12, 0.112)
    glow.add_color_stop_rgb(1, *BACKDROP)
    cr.set_source(glow)
    cr.paint()


def notify(title, body=""):
    try:
        subprocess.Popen(["notify-send", "-a", "Prism", "-i", "document-save", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


CURSORS = {"n": "n-resize", "s": "s-resize", "e": "e-resize", "w": "w-resize", "ne": "ne-resize",
           "nw": "nw-resize", "se": "se-resize", "sw": "sw-resize", "move": "move", "new": "crosshair"}


def button(text, icon, handler, primary=False, tip=None):
    b = Gtk.Button(tooltip_text=tip)
    row = Gtk.Box(spacing=7)
    if icon:
        row.append(Gtk.Image.new_from_icon_name(icon))
    if text:
        row.append(Gtk.Label(label=text))
    b.set_child(row)
    if primary:
        b.add_css_class("primary")
    b.connect("clicked", handler)
    return b


class CropWindow(Gtk.ApplicationWindow):
    def __init__(self, app, path, tool="crop"):
        super().__init__(application=app, title=f"{tool.title()} — {os.path.basename(path)}")
        self.path = Path(path).absolute()
        self.tool = tool
        self.m = IDENTITY  # rotate: the orientation shown and saved
        self.motion = None  # rotate: (step, start time) of the turn or flip gliding in
        self.box = self.preview = None
        self.mode, self.start_box, self.start_pt = None, None, None
        self.busy = False
        self.fade = 0.0  # the image fading in once loaded
        self.glide = None  # (from box, to box, start time): the box animating to a new place
        self.tick_id = None
        self.toast_id = None
        self.tmp = tempfile.TemporaryDirectory(prefix="prism-crop-")
        self.add_css_class("crop")
        self.set_decorated(False)  # tiled: no floating title bar
        self.set_default_size(1100, 760)

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True, focusable=True)
        self.area.set_draw_func(self.on_draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self.on_drag_begin)
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        self.area.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        self.area.add_controller(motion)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self.on_scroll)
        self.area.add_controller(scroll)
        pan = Gtk.GestureDrag(button=2)  # middle-drag pans a zoomed view
        pan.connect("drag-begin", lambda _g, x, y: setattr(self, "pan_from", (0.0, 0.0)))
        pan.connect("drag-update", self.on_pan)
        self.area.add_controller(pan)
        self.pointer = None  # last pointer position over the canvas
        self.zoom_goal = None  # (zoom, screen x, screen y): Ctrl +/- glides there
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        # Top left: what is being cropped.
        chip = Gtk.Box(spacing=8, halign=Gtk.Align.START, valign=Gtk.Align.START,
                       margin_start=16, margin_top=16, can_target=False)
        chip.add_css_class("chip")
        chip.append(Gtk.Label(label=convert.ext_of(self.path.name).upper() or "IMAGE", css_classes=["dim"]))
        chip.append(Gtk.Label(label=self.path.name, ellipsize=3, max_width_chars=48))
        self.full_size = Gtk.Label(css_classes=["dim"])
        chip.append(self.full_size)
        self.zoom_label = Gtk.Label(visible=False)
        chip.append(self.zoom_label)

        # Bottom centre: a floating toolbar, which turns into the Replace confirmation.
        self.size_label = Gtk.Label(label="—", width_chars=11, xalign=0.5, css_classes=["size"])
        self.copy_btn = button("Copy", "edit-copy-symbolic", self.on_copy)
        self.save_btn = button("Save", "document-save-symbolic", self.on_save)
        self.replace_btn = button("Replace", "view-refresh-symbolic", self.on_replace, primary=True)
        main = Gtk.Box(spacing=2)
        self.orient_btns = []
        if tool == "rotate":
            for icon, step, tip in (("object-rotate-left-symbolic", TURN_LEFT, "Rotate left  ["),
                                    ("object-rotate-right-symbolic", TURN_RIGHT, "Rotate right  ]"),
                                    ("object-flip-horizontal-symbolic", FLIP_H, "Flip horizontally  H"),
                                    ("object-flip-vertical-symbolic", FLIP_V, "Flip vertically  V")):
                b = button(None, icon, lambda _b, step=step: self.orient(step), tip=tip)
                self.orient_btns.append(b)
                main.append(b)
            main.append(self.separator())
        for w in (self.size_label, self.separator(), self.copy_btn, self.save_btn, self.replace_btn):
            main.append(w)
        confirm = Gtk.Box(spacing=2)
        confirm.append(Gtk.Label(label="Replace the original? Its metadata goes too.", css_classes=["question"]))
        confirm.append(button("Cancel", None, lambda *_: self.bar.set_visible_child_name("main")))
        confirm.append(button("Replace", "object-select-symbolic", self.on_confirm, primary=True))
        self.bar = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=160,
                             interpolate_size=True, hhomogeneous=False, halign=Gtk.Align.CENTER,
                             valign=Gtk.Align.END, margin_bottom=28)
        self.bar.add_css_class("toolbar")
        self.bar.add_named(main, "main")
        self.bar.add_named(confirm, "confirm")

        # Above the toolbar: short confirmations, and errors (which stay until the next action).
        self.toast = Gtk.Label(wrap=True, max_width_chars=70, justify=Gtk.Justification.CENTER)
        self.toast.add_css_class("toast")
        self.toast_box = Gtk.Revealer(child=self.toast, halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
                                      margin_bottom=96, can_target=False, transition_duration=180,
                                      transition_type=Gtk.RevealerTransitionType.SLIDE_UP)

        overlay = Gtk.Overlay(child=self.area)
        for w in (chip, self.bar, self.toast_box):
            overlay.add_overlay(w)
        self.set_child(overlay)
        self.set_ready(False)
        self.connect("close-request", self.on_close)
        threading.Thread(target=self.load, daemon=True).start()

    def separator(self):
        sep = Gtk.Box()
        sep.add_css_class("sep")
        return sep

    # Loading

    def load(self):
        try:
            self.stamp = self.path.stat()
            preview, size = convert.crop_preview(self.path, Path(self.tmp.name))
            surface = cairo.ImageSurface.create_from_png(str(preview))
        except Exception as e:
            GLib.idle_add(self.say, f"Can't open this image: {e}", True)
            return
        GLib.idle_add(self.loaded, surface, size)

    def loaded(self, surface, size):
        self.preview, self.box = surface, Box(*size)
        self.full_size.set_text(f"{size[0]} × {size[1]}")
        self.set_ready(True)
        self.loaded_at = time.monotonic()
        self.animate()
        self.area.grab_focus()

    def say(self, message, error=False, stay=False):
        """A toast above the toolbar: confirmations fade by themselves; errors and
        progress ("Saving…") stay until replaced."""
        if self.toast_id:
            GLib.source_remove(self.toast_id)
            self.toast_id = None
        self.toast.set_text(message)
        (self.toast.add_css_class if error else self.toast.remove_css_class)("error")
        self.toast_box.set_reveal_child(True)
        if not (error or stay):
            self.toast_id = GLib.timeout_add(1800, self.unsay)

    def unsay(self):
        self.toast_id = None
        self.toast_box.set_reveal_child(False)
        return False

    def set_ready(self, ready):
        ready = ready and self.box is not None and not self.busy
        self.copy_btn.set_sensitive(ready)
        for b in self.orient_btns:
            b.set_sensitive(ready)
        # Replacing or saving the image unchanged would be pointless.
        unchanged = self.box is not None and self.box.whole() and self.m == IDENTITY
        for b in (self.save_btn, self.replace_btn):
            b.set_sensitive(ready and not unchanged)
        if self.box:
            w, h = self.result_size(self.box.rect())
            self.size_label.set_text(f"{w} × {h}")

    def result_size(self, rect, m=None):
        """What the edit writes: the box, turned on its side by an odd number of quarter turns."""
        turns, _ = turns_mirror(self.m if m is None else m)
        return (rect[3], rect[2]) if turns % 2 else (rect[2], rect[3])

    def orient(self, step):
        """Rotate: one quarter turn or flip, gliding in."""
        if not self.box or self.busy:
            return
        self.bar.set_visible_child_name("main")  # a Replace question was about the old orientation
        self.m = mul(step, self.m)
        self.motion = (step, time.monotonic())
        self.set_ready(True)
        self.animate()

    # Drawing, animation and dragging

    def on_draw(self, _area, cr, width, height):
        if self.box and self.tool == "rotate":
            motion = None
            if self.motion:
                step, t0 = self.motion
                before = mul(((step[0][0], step[1][0]), (step[0][1], step[1][1])), self.m)  # step is orthogonal
                motion = (step, (time.monotonic() - t0) / EASE)
                draw_rotate(cr, width, height, (self.box.w, self.box.h), self.preview, before, motion, self.fade)
            else:
                draw_rotate(cr, width, height, (self.box.w, self.box.h), self.preview, self.m, fade=self.fade)
        elif self.box:
            draw(cr, width, height, self.box, self.preview, dragging=self.mode is not None, fade=self.fade)
        else:
            cr.set_source_rgb(*BACKDROP)
            cr.paint()

    def animate(self):
        if self.tick_id is None:
            self.tick_id = self.area.add_tick_callback(self.on_tick)

    def on_tick(self, _area, _clock):
        now = time.monotonic()
        dt = min(0.05, now - getattr(self, "last_tick", now - 0.016))
        self.last_tick = now
        self.fade = ease((now - self.loaded_at) / EASE)
        if self.zoom_goal:
            goal, x, y = self.zoom_goal
            z = self.box.zoom + (goal - self.box.zoom) * (1 - math.exp(-dt * 20))
            if abs(goal - z) < 0.002 * goal:
                z, self.zoom_goal = goal, None
            self.box.zoom_at(z, x, y)
            self.zoomed()
        if self.glide:
            start, end, t0 = self.glide
            t = ease((now - t0) / EASE)
            self.box.box = [a + (b - a) * t for a, b in zip(start, end)]
            if t >= 1:
                self.glide = None
                self.set_ready(True)
        if self.motion and now - self.motion[1] >= EASE:
            self.motion = None
        self.area.queue_draw()
        if self.fade >= 1 and not self.glide and not self.zoom_goal and not self.motion:
            self.tick_id = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def on_scroll(self, controller, dx, dy):
        if not self.box or self.tool != "crop":
            return False
        wheel = controller.get_unit() == Gdk.ScrollUnit.WHEEL
        if controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            self.zoom_goal = None
            x, y = self.pointer or self.centre()
            self.box.zoom_at(self.box.zoom * (1.25 if wheel else 1.01) ** -dy, x, y)
        else:
            step = 60 if wheel else 1
            self.box.pan(-dx * step, -dy * step)
        self.zoomed()
        return True

    def on_pan(self, _g, dx, dy):
        if self.box and self.tool == "crop":
            self.box.pan(dx - self.pan_from[0], dy - self.pan_from[1])
            self.pan_from = (dx, dy)
            self.zoomed()

    def centre(self):
        return self.area.get_width() / 2, self.area.get_height() / 2

    def zoom_by(self, factor):
        """Ctrl + / Ctrl - / Ctrl 0: glide the zoom, around the pointer if it's over the image."""
        if not self.box or self.tool != "crop":
            return
        current = self.zoom_goal[0] if self.zoom_goal else self.box.zoom
        goal = 1.0 if factor is None else max(1.0, min(self.box.max_zoom(), current * factor))
        x, y = self.pointer or self.centre()
        self.zoom_goal = (goal, x, y)
        self.last_tick = time.monotonic()
        self.animate()

    def zoomed(self):
        z = self.box.zoom
        self.zoom_label.set_visible(z > 1.001)
        self.zoom_label.set_text(f"{round(z * 100)}%")
        self.area.queue_draw()

    def on_motion(self, _c, x, y):
        self.pointer = (x, y)
        if self.box and self.mode is None and self.tool == "crop":
            self.area.set_cursor_from_name(CURSORS[self.box.grab(x, y)])

    def on_drag_begin(self, _g, x, y):
        if not self.box or self.busy or self.tool != "crop":
            return
        self.glide = None
        self.mode = self.box.grab(x, y)
        self.start_box, self.start_pt = list(self.box.box), (x, y)
        self.start_img = self.box.to_image(x, y)

    def on_drag_update(self, _g, dx, dy):
        if self.mode is None:
            return
        a = self.start_img
        b = self.box.to_image(self.start_pt[0] + dx, self.start_pt[1] + dy)
        new = self.box.drag(self.mode, self.start_box, a, b)
        if self.mode != "new" or (new[2] - new[0] >= 1 and new[3] - new[1] >= 1):
            self.box.box = new
        self.set_ready(True)
        self.area.queue_draw()

    def on_drag_end(self, _g, dx, dy):
        if self.mode is None:
            return
        if self.mode == "new" and abs(dx) < 3 and abs(dy) < 3:
            # A click outside: glide back to the whole image.
            self.glide = (list(self.start_box), [0.0, 0.0, float(self.box.w), float(self.box.h)],
                          time.monotonic())
            self.animate()
        self.mode = None
        self.set_ready(True)
        self.area.queue_draw()

    def on_key(self, _c, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            if self.bar.get_visible_child_name() == "confirm":
                self.bar.set_visible_child_name("main")
            else:
                self.close()
            return True
        if state & Gdk.ModifierType.CONTROL_MASK:
            factor = {Gdk.KEY_plus: ZOOM_STEP, Gdk.KEY_equal: ZOOM_STEP, Gdk.KEY_KP_Add: ZOOM_STEP,
                      Gdk.KEY_minus: 1 / ZOOM_STEP, Gdk.KEY_underscore: 1 / ZOOM_STEP,
                      Gdk.KEY_KP_Subtract: 1 / ZOOM_STEP, Gdk.KEY_0: None, Gdk.KEY_KP_0: None}
            if keyval in factor:
                self.zoom_by(factor[keyval])
                return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C) and state & Gdk.ModifierType.CONTROL_MASK:
            if self.copy_btn.get_sensitive():
                self.on_copy()
            return True
        steps = {Gdk.KEY_bracketleft: TURN_LEFT, Gdk.KEY_bracketright: TURN_RIGHT,
                 Gdk.KEY_h: FLIP_H, Gdk.KEY_H: FLIP_H, Gdk.KEY_v: FLIP_V, Gdk.KEY_V: FLIP_V}
        if self.tool == "rotate" and keyval in steps and not state & Gdk.ModifierType.CONTROL_MASK:
            self.orient(steps[keyval])
            return True
        return False

    # Replace / Save / Copy

    def run(self, label, job, done):
        """Run job() off the GTK thread; then done(result) on it, or show the error.
        One at a time: the portal's Save dialog doesn't block the window, so a Copy
        can still be running when a destination comes back."""
        if self.busy:
            self.say("Still working on the last one; try again in a moment", True)
            return
        self.busy = True
        self.set_ready(False)
        self.say(label, stay=True)
        rect = self.box.rect()
        m = self.m
        self.edit = turns_mirror(m)  # what this job writes, whatever is clicked meanwhile

        def work():
            try:
                result = job(rect)
            except Exception as e:
                GLib.idle_add(self.finished, None, str(e))
            else:
                GLib.idle_add(self.finished, lambda: done(result, self.result_size(rect, m)), None)

        threading.Thread(target=work, daemon=True).start()

    def finished(self, then, error):
        self.busy = False
        self.set_ready(True)
        if error:
            self.say(error, True)
        else:
            self.unsay()
            then()

    def on_copy(self, *_):
        def job(rect):
            with tempfile.TemporaryDirectory(prefix="prism-crop-") as tmp:
                png = self.write(rect, Path(tmp) / "edit.png")
                env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
                with open(png, "rb") as data:
                    # wl-copy forks and keeps serving the image after this window closes.
                    code = subprocess.run(["wl-copy", "--type", "image/png"], stdin=data, env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          timeout=10).returncode
            if code:
                raise RuntimeError("wl-copy could not reach the clipboard")

        self.run("Copying…", job, lambda _r, size: self.say(f"Copied {size[0]} × {size[1]} to the clipboard"))

    def write(self, rect, dest):
        return convert.crop(self.path, rect, dest, *self.edit)

    def done_word(self):
        return "cropped" if self.tool == "crop" else "rotated"

    def on_save(self, *_):
        dialog = Gtk.FileDialog(title=f"Save {self.done_word()} image", modal=True,
                                initial_name=f"{self.path.stem} ({self.done_word()}){self.path.suffix}",
                                initial_folder=Gio.File.new_for_path(str(self.path.parent)))
        dialog.save(self, None, self.on_save_chosen)

    def on_save_chosen(self, dialog, result):
        try:
            chosen = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        dest = chosen.get_path() if chosen else None
        if not dest:
            self.say("Choose a folder on this computer", True)
            return
        self.run("Saving…", lambda rect: self.write(rect, dest),
                 lambda saved, size: self.done(f"Saved {self.done_word()} image ({size[0]} × {size[1]})",
                                               os.path.basename(saved)))

    def on_replace(self, *_):
        self.bar.set_visible_child_name("confirm")

    def on_confirm(self, *_):
        self.bar.set_visible_child_name("main")

        def job(rect):
            now = self.path.stat()
            if (now.st_mtime_ns, now.st_size, now.st_ino) != (self.stamp.st_mtime_ns, self.stamp.st_size,
                                                             self.stamp.st_ino):
                raise RuntimeError(f"The file changed since it was opened; reopen it to {self.tool}")
            return self.write(rect, self.path)

        self.run("Replacing…", job, lambda _r, size: self.done(
            f"{self.done_word().capitalize()} {self.path.name} ({size[0]} × {size[1]})", "The original was replaced."))

    def done(self, title, body):
        notify(title, body)
        self.close()

    def on_close(self, *_):
        if self.busy:
            return True  # never leave a half-finished save behind; it closes when done
        self.tmp.cleanup()
        return False


def main():
    args = sys.argv[1:]
    tool = args.pop(0)[2:] if args and args[0] in ("--crop", "--rotate") else "crop"
    if len(args) != 1:
        sys.exit(__doc__)
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        notify(f"Can't open the {tool} tool", "No Wayland display.")
        sys.exit(1)
    css = Gtk.CssProvider()
    css.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    app = Gtk.Application(application_id="dev.prism.Crop", flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", lambda a: CropWindow(a, args[0], tool).present())
    sys.exit(app.run([sys.argv[0]]))


if __name__ == "__main__":
    main()
