#!/usr/bin/python3
"""Prism's progress window: a small solid card in the centre of the focused screen.

notices.convert_all starts it (with gtk4-layer-shell preloaded) and writes
one JSON object per line to its stdin. All fields are optional:
    {"title": "Removing the background from 3 files", "icon": "draw-eraser",
     "file": "cat.jpg", "stage": "Refining edges", "done": 1, "total": 3, "fraction": 0.46}
    {"finish": true}    or end of input: closes.

"fraction" is the real progress, from the work's own stages (model loading, the
model's passes, edge refinement, saving...). Without it, the bar is files
done / total. The bar eases toward the real value and never runs ahead of it.
The window never takes keyboard focus. The x hides it, and the work goes on;
the result still arrives as a notification.
"""
import json
import os
import subprocess
import sys
import threading
import time

import cairo
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell as LayerShell, Pango  # noqa: E402

BG = "#1c1a18"
CSS = f"""
window.prism-progress {{ background: none; }}  /* only the rounded corners outside the card */
.card {{ background-color: {BG}; border-radius: 16px; border: 1px solid #34302b;
         padding: 16px 16px 14px 16px; font-family: "Adwaita Sans", "Inter", sans-serif; }}
.badge {{ background-image: linear-gradient(to bottom, #ff9b4a, #ff7f24); border-radius: 11px;
          min-width: 38px; min-height: 38px; }}
.badge image {{ color: #1f1206; -gtk-icon-size: 20px; }}
.title {{ color: #f4efe9; font-size: 14px; font-weight: 600; }}
.detail {{ color: #a39a90; font-size: 12px; }}
.count {{ color: #d6cfc7; font-size: 12px; font-weight: 600; font-feature-settings: "tnum"; }}
.percent {{ color: #ff9b4a; font-size: 12px; font-weight: 700; font-feature-settings: "tnum"; }}
.time {{ color: #8d847a; font-size: 12px; font-feature-settings: "tnum"; }}
button.close {{ background: none; border: none; box-shadow: none; min-height: 0; min-width: 0;
                padding: 3px; border-radius: 8px; color: #8d847a; }}
button.close:hover {{ background-color: #2c2925; color: #f4efe9; }}
""".encode()
WIDTH, BAR_H = 400, 8
ACCENT = ((1.0, 0.608, 0.29), (1.0, 0.498, 0.141))  # #ff9b4a -> #ff7f24, Prism's orange
TRACK = (0.204, 0.188, 0.169)  # #34302b


def focused_monitor():
    try:
        out = subprocess.run(["hyprctl", "-j", "monitors"], capture_output=True, text=True, timeout=2).stdout
        mon = next((m for m in json.loads(out) if m.get("focused")), None)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not mon:
        return None
    monitors = Gdk.Display.get_default().get_monitors()
    for i in range(monitors.get_n_items()):
        if monitors.get_item(i).get_connector() == mon.get("name"):
            return monitors.get_item(i)
    return None


def clock(seconds):
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s} s"


class Bar(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_content_height(BAR_H)
        self.set_hexpand(True)
        self.target = self.shown = 0.0
        self.finished = False
        self.start = time.monotonic()
        self.set_draw_func(self.draw)
        self.add_tick_callback(self.tick)

    def tick(self, _widget, _clock):
        self.shown += (self.target - self.shown) * 0.12  # ease toward the real value, never past it
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def draw(self, _area, cr, w, h):
        r = h / 2

        def pill(x, width):
            width = max(width, h)
            cr.new_sub_path()
            cr.arc(x + width - r, r, r, -1.5708, 1.5708)
            cr.arc(x + r, r, r, 1.5708, 4.7124)
            cr.close_path()

        cr.set_source_rgb(*TRACK)
        pill(0, w)
        cr.fill()
        fill = w * min(1.0, self.shown)
        if fill < 0.5:
            return
        g = cairo.LinearGradient(0, 0, fill, 0)
        g.add_color_stop_rgb(0, *ACCENT[0])
        g.add_color_stop_rgb(1, *ACCENT[1])
        cr.set_source(g)
        pill(0, fill)
        cr.fill()
        if not self.finished:  # still working: a soft highlight sweeps the filled part
            t = (time.monotonic() - self.start) % 1.4 / 1.4
            x = -0.4 * fill + t * 1.4 * fill
            g = cairo.LinearGradient(x, 0, x + 0.4 * fill, 0)
            g.add_color_stop_rgba(0, 1, 1, 1, 0)
            g.add_color_stop_rgba(0.5, 1, 1, 1, 0.28)
            g.add_color_stop_rgba(1, 1, 1, 1, 0)
            cr.set_source(g)
            pill(0, fill)
            cr.fill()


def label(css, **kw):
    lab = Gtk.Label(xalign=kw.pop("xalign", 0), **kw)
    lab.add_css_class(css)
    return lab


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Prism progress")
        self.add_css_class("prism-progress")
        LayerShell.init_for_window(self)
        LayerShell.set_namespace(self, "prism-progress")
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)  # never takes focus
        LayerShell.set_exclusive_zone(self, 0)  # no anchors: the compositor centres it
        monitor = focused_monitor()
        if monitor:
            LayerShell.set_monitor(self, monitor)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        card = Gtk.Box(spacing=14)
        card.add_css_class("card")
        card.set_size_request(WIDTH, -1)

        badge = Gtk.Box(valign=Gtk.Align.START)
        badge.add_css_class("badge")
        self.icon = Gtk.Image(icon_name="emblem-synchronizing-symbolic", hexpand=True, vexpand=True)
        badge.append(self.icon)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        top = Gtk.Box(spacing=8)
        self.title = label("title", hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Hide (keeps working)",
                           valign=Gtk.Align.START)
        close.add_css_class("close")
        close.connect("clicked", lambda *_: app.quit())
        top.append(self.title)
        top.append(close)
        self.detail = label("detail", ellipsize=Pango.EllipsizeMode.MIDDLE, use_markup=True)
        self.bar = Bar()
        self.bar.set_margin_top(4)
        bottom = Gtk.Box(spacing=8)
        self.count = label("count")
        self.percent = label("percent")
        self.time = label("time", xalign=1, hexpand=True)
        bottom.append(self.count)
        bottom.append(self.percent)
        bottom.append(self.time)
        for w in (top, self.detail, self.bar, bottom):
            body.append(w)
        card.append(badge)
        card.append(body)
        self.set_child(card)

        self.started = time.monotonic()
        self.done, self.total, self.fraction = 0, 1, 0.0
        self.file = self.stage = ""
        GLib.timeout_add(250, self.refresh)

    def update(self, msg):
        if "title" in msg:
            self.title.set_text(str(msg["title"]))
        if msg.get("icon"):
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            name = str(msg["icon"])
            for candidate in (f"{name}-symbolic", name, "emblem-synchronizing-symbolic"):
                if theme.has_icon(candidate):
                    self.icon.set_from_icon_name(candidate)
                    break
        self.file = str(msg.get("file", self.file))
        self.stage = str(msg.get("stage", self.stage))
        if "total" in msg:
            self.total = max(1, int(msg["total"]))
        if "done" in msg:
            self.done = max(0, min(self.total, int(msg["done"])))
        if "fraction" in msg:
            self.fraction = max(self.fraction, min(1.0, float(msg["fraction"])))  # never goes back
        else:
            self.fraction = max(self.fraction, self.done / self.total)
        parts = [f'<span foreground="#d6cfc7">{GLib.markup_escape_text(self.file)}</span>'] if self.file else []
        if self.stage:
            parts.append(GLib.markup_escape_text(self.stage))
        self.detail.set_markup(" · ".join(parts))
        self.detail.set_visible(bool(parts))
        self.bar.target = self.fraction
        self.count.set_text(f"{self.done} of {self.total}" if self.total > 1 else "")
        self.count.set_visible(self.total > 1)
        self.refresh()

    def refresh(self):
        elapsed = time.monotonic() - self.started
        self.percent.set_text(f"{int(self.bar.target * 100)}%")
        text = clock(elapsed)
        f = self.fraction
        if 0.1 < f < 1 and elapsed > 1.5:
            text = f"~{clock(elapsed / f * (1 - f))} left · {text}"
        self.time.set_text(text)
        return GLib.SOURCE_CONTINUE

    def finish(self):
        self.bar.finished = True
        self.fraction = self.bar.target = 1.0
        self.refresh()
        GLib.timeout_add(500, lambda: self.get_application().quit())  # let the bar land, then go


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=None)
        self.window = None

    def do_activate(self):
        self.window = Window(self)
        self.window.update({"title": "Prism"})
        self.window.present()
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("finish"):
                break
            GLib.idle_add(self.window.update, msg)
        GLib.idle_add(self.window.finish)


if __name__ == "__main__":
    if not os.environ.get("WAYLAND_DISPLAY"):
        sys.exit(2)
    sys.exit(App().run([sys.argv[0]]))
