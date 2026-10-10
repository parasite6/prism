#!/usr/bin/python3
"""Prism: choose what opens the ring. Press the keys or mouse button to hold,
then OK. Esc or Cancel keeps the current binding.

    bind.py

An ordinary window (Hyprland tiles it), like the crop tool."""
import os
import subprocess
import sys
from pathlib import Path

os.environ["GSK_RENDERER"] = "cairo"
os.environ["GDK_BACKEND"] = "wayland"

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import binding  # noqa: E402

CSS = b"""
window.bind { background-color: #161412; font-family: "Adwaita Sans", "Inter", sans-serif; }
.page { background-image: radial-gradient(circle at 50% 40%, #221f1c, #161412 70%); }
.title { color: #ffffff; font-size: 22px; font-weight: 700; }
.hint { color: rgba(255, 255, 255, 0.55); font-size: 14px; }
.error { color: #ff8c5a; font-size: 14px; }
.keys { min-height: 64px; }
.key { background-color: #ffffff; color: #2a2520; border-radius: 12px; padding: 10px 18px;
       font-size: 17px; font-weight: 700; box-shadow: 0 3px 0 rgba(0, 0, 0, 0.35), 0 8px 24px rgba(0, 0, 0, 0.3); }
.key.trigger { background-image: linear-gradient(to bottom, #ff9b4a, #ff7f24); color: #1f1206; }
.plus { color: rgba(255, 255, 255, 0.45); font-size: 20px; font-weight: 600; }
.waiting { color: rgba(255, 255, 255, 0.35); font-size: 17px; font-weight: 600; }
.toolbar { background-color: rgba(255, 255, 255, 0.96); border-radius: 18px; padding: 6px;
           box-shadow: 0 10px 36px rgba(0, 0, 0, 0.45); }
.toolbar button { background: none; border: none; box-shadow: none; text-shadow: none; color: #2a2520;
                  border-radius: 12px; padding: 8px 22px; font-weight: 600; font-size: 14px;
                  transition: background-color 120ms ease, opacity 120ms ease; }
.toolbar button:hover { background-color: rgba(0, 0, 0, 0.06); }
.toolbar button.primary { background-image: linear-gradient(to bottom, #ff9b4a, #ff7f24); color: #1f1206;
                          box-shadow: 0 2px 8px rgba(255, 127, 36, 0.35); }
.toolbar button.primary:hover { background-image: linear-gradient(to bottom, #ffab63, #ff8c33); }
.toolbar button:disabled { opacity: 0.35; box-shadow: none; }
"""


def gdk_button_to_evdev(button):
    """GTK numbers mouse buttons 1-3, then (after the old scroll 4-7) 8 Back, 9 Forward..."""
    return {1: 272, 2: 274, 3: 273}.get(button, button + 267 if button >= 8 else None)


def key_label(display, keycode, keyval):
    """The key's unshifted name: "A", "F13", "Space", "Page Up"."""
    ok, _keys, keyvals = display.map_keycode(keycode)
    name = Gdk.keyval_name(keyvals[0] if ok and keyvals else keyval) or f"Key {keycode - 8}"
    if len(name) == 1:
        return name.upper()
    return {"space": "Space", "Return": "Enter", "Prior": "Page Up", "Next": "Page Down",
            "grave": "`", "minus": "-", "equal": "=", "comma": ",", "period": ".", "slash": "/",
            "semicolon": ";", "apostrophe": "'", "bracketleft": "[", "bracketright": "]",
            "backslash": "\\"}.get(name, name.replace("_", " "))


def notify(title, body=""):
    try:
        subprocess.Popen(["notify-send", "-a", "Prism", "-i", "input-mouse", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


class BindWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Prism — binding")
        self.add_css_class("bind")
        self.set_decorated(False)  # tiled: no floating title bar
        self.set_default_size(640, 380)
        self.held = set()  # modifier names held right now
        self.chosen = binding.load()  # what OK saves; starts as the current binding

        title = Gtk.Label(label="Press the keys or button you want to hold", css_classes=["title"], wrap=True,
                          justify=Gtk.Justification.CENTER)
        hint = Gtk.Label(label="Modifiers (Ctrl, Shift, Alt, Super) plus one key or mouse button. "
                               "Hold it in Nautilus to open the ring, release to choose.",
                         css_classes=["hint"], wrap=True, max_width_chars=56, justify=Gtk.Justification.CENTER)
        self.keys = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, css_classes=["keys"])
        self.message = Gtk.Label(label="", css_classes=["hint"])
        self.ok = Gtk.Button(label="OK", css_classes=["primary"])
        self.ok.connect("clicked", self.on_ok)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        bar = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER, css_classes=["toolbar"])
        bar.append(cancel)
        bar.append(self.ok)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER,
                         halign=Gtk.Align.CENTER, margin_start=32, margin_end=32)
        for w in (title, hint, self.keys, self.message, bar):
            column.append(w)
        page = Gtk.Box(css_classes=["page"], hexpand=True, vexpand=True)
        column.set_hexpand(True)
        page.append(column)
        self.set_child(page)

        # Capture phase: every key and button reaches us first, so Enter/Space
        # never activate a button and a mouse button over OK is still recorded.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        keys.connect("key-released", self.on_key_up)
        self.add_controller(keys)
        clicks = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        clicks.connect("pressed", self.on_button)
        self.add_controller(clicks)
        self.show_binding(self.chosen, current=True)

    def show_binding(self, bound, current=False):
        while (child := self.keys.get_first_child()) is not None:
            self.keys.remove(child)
        parts = [binding.MOD_LABELS[m] for m in bound["mods"]] if bound else []
        for i, text in enumerate(parts + ([bound["label"]] if bound else [])):
            if i:
                self.keys.append(Gtk.Label(label="+", css_classes=["plus"]))
            cap = Gtk.Label(label=text, css_classes=["key"])
            if i == len(parts):
                cap.add_css_class("trigger")
            self.keys.append(cap)
        self.message.set_css_classes(["hint"])
        self.message.set_text("Current binding" if current else "")

    def show_mods(self):
        """Only modifiers held so far: show them, waiting for the key."""
        while (child := self.keys.get_first_child()) is not None:
            self.keys.remove(child)
        for name in binding.MODS:
            if name in self.held:
                self.keys.append(Gtk.Label(label=binding.MOD_LABELS[name], css_classes=["key"]))
                self.keys.append(Gtk.Label(label="+", css_classes=["plus"]))
        self.keys.append(Gtk.Label(label="…", css_classes=["waiting"]))
        self.message.set_text("")

    def choose(self, code, label):
        try:
            self.chosen = binding.make(self.held, code, label)
        except ValueError as e:
            self.message.set_css_classes(["error"])
            self.message.set_text(str(e))
            return
        self.show_binding(self.chosen)
        self.ok.set_sensitive(True)

    def on_key(self, controller, keyval, keycode, _state):
        if keyval == Gdk.KEY_Escape and not self.held:
            self.close()
            return True
        code = keycode - 8  # X11/Wayland keycodes are evdev codes + 8
        if code in binding.MOD_CODES:
            self.held.add(binding.MOD_CODES[code])
            self.show_mods()
        else:
            self.choose(code, key_label(self.get_display(), keycode, keyval))
        return True

    def on_key_up(self, _controller, _keyval, keycode, _state):
        name = binding.MOD_CODES.get(keycode - 8)
        if name:
            self.held.discard(name)
            if not self.held and not self.keys_show_trigger():
                self.show_binding(self.chosen)

    def keys_show_trigger(self):
        last = self.keys.get_last_child()
        return last is not None and last.has_css_class("trigger")

    def on_button(self, gesture, _n, _x, _y):
        code = gdk_button_to_evdev(gesture.get_current_button())
        if code is None or code == binding.BTN_LEFT:
            return  # left click stays a click (OK, Cancel)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.choose(code, binding.MOUSE.get(code, f"Mouse {code - 271}"))

    def on_ok(self, *_):
        try:
            binding.save(self.chosen)
        except OSError as e:
            self.message.set_css_classes(["error"])
            self.message.set_text(f"Couldn't save: {e}")
            return
        env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
        # The running ring watches the new binding at once; Hyprland's reload
        # re-reads it for the Nautilus bind (hypr-binds.lua).
        subprocess.run(["bash", "-c", '. "$1/ring-ctl" && ring_alive && ring_send rebind', "_", str(BASE)],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        subprocess.run(["hyprctl", "reload"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5)
        notify(f"Prism: hold {binding.describe(self.chosen)}", "In Nautilus, to open the ring.")
        self.close()


def main():
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        notify("Can't open Prism's binding window", "No Wayland display.")
        sys.exit(1)
    css = Gtk.CssProvider()
    css.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    app = Gtk.Application(application_id="dev.prism.Bind", flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", lambda a: BindWindow(a).present())
    sys.exit(app.run([sys.argv[0]]))


if __name__ == "__main__":
    main()
