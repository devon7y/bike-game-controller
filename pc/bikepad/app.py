"""The Bike Controller window: live view, game profiles, control mapping,
tuning and calibration. All the work happens in the Engine thread; the window
reads its Snapshot ~30 times a second."""
import ctypes
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import sv_ttk

from . import game as games
from .calibration import PATH as CALIBRATION_PATH
from .engine import Engine
from .keyboard import KEYS
from .profiles import (AXES, KEYBOARD, MODES, PEDALLING_MEASURES, PRESETS, RESISTANCE_MODES, SOURCES,
                       TARGETS, TRIGGERS, Binding, Settings, copy_profile, make_preset, swap_triggers)

APP_NAME = "Bike Controller"
APP_ID = "BikeController.App"
ICON_PATH = Path(__file__).resolve().parent.parent / "icon.ico"
TICK_MS = 33
SAVE_DELAY_MS = 600

PALETTES = {
    "dark": dict(bg="#1c1c1c", panel="#2b2b2b", track="#3d3d3d", text="#ffffff", muted="#9d9d9d",
                 accent="#57c8ff", good="#6ccb5f", bad="#ff99a4", warn="#fce100",
                 a="#6ccb5f", b="#ff6b6b", x="#57a8ff", y="#fce100"),
    "light": dict(bg="#fafafa", panel="#ffffff", track="#dcdcdc", text="#1c1c1c", muted="#6b6b6b",
                  accent="#005fb8", good="#0f7b0f", bad="#c42b1c", warn="#9d5d00",
                  a="#107c10", b="#c42b1c", x="#0063b1", y="#b58b00"),
}
SOURCE_KEYS, TARGET_KEYS, KEY_IDS, MODE_KEYS = list(SOURCES), list(TARGETS), list(KEYS), list(MODES)
ZWIFT_STATUS = {  # Snapshot.zwift_state -> (status light, text)
    0: ("warn", "starting Bluetooth…"),
    1: ("warn", "waiting for Zwift: in Zwift's pairing screen, choose STERZO under Steer"),
    2: ("warn", "Zwift found it, connecting…"),
    3: ("good", "connected to Zwift"),
}


def angle_text(deg):
    if deg is None:
        return "-"
    return "straight" if abs(deg) < 0.5 else f"{abs(deg):.0f}° {'right' if deg > 0 else 'left'}"


def windows_dark_mode():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return True


# --- small drawing widgets ---

class Meter(tk.Canvas):
    """A horizontal bar: 0..1 from the left, or -1..1 from the centre."""

    def __init__(self, parent, app, width=300, height=22, centred=False):
        self.app, self.w, self.h = app, app.px(width), app.px(height)
        super().__init__(parent, width=self.w, height=self.h, highlightthickness=0, bg=app.color("bg"))
        self.centred = centred

    def _pill(self, x0, y0, x1, y1, color):
        r = (y1 - y0) / 2
        if x1 - x0 < 2 * r:
            self.create_oval(x0, y0, x0 + 2 * r, y1, fill=color, outline="")
            return
        self.create_oval(x0, y0, x0 + 2 * r, y1, fill=color, outline="")
        self.create_oval(x1 - 2 * r, y0, x1, y1, fill=color, outline="")
        self.create_rectangle(x0 + r, y0, x1 - r, y1, fill=color, outline="")

    def show(self, value, marks=(), color="accent"):
        c, w, h = self.app.color, self.w, self.h
        self.delete("all")
        self._pill(0, 0, w, h, c("track"))
        pad = max(2, h // 6)
        if self.centred:
            mid = w / 2
            x = mid + max(-1.0, min(value, 1.0)) * (mid - pad)
            x0, x1 = sorted((mid, x))
            if x1 - x0 >= 1:
                self.create_rectangle(x0, pad, x1, h - pad, fill=c(color), outline="")
            self.create_line(mid, 0, mid, h, fill=c("muted"))
        else:
            x = pad + max(0.0, min(value, 1.0)) * (w - 2 * pad)
            if x - pad >= 1:
                self._pill(pad, pad, max(x, pad + h - 2 * pad), h - pad, c(color))
        for m in marks:
            mx = (w / 2 + m * (w / 2 - pad)) if self.centred else pad + m * (w - 2 * pad)
            self.create_line(mx, 0, mx, h, fill=c("text"), width=2)


class StatusDot(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        d = app.px(12)
        self.canvas = tk.Canvas(self, width=d, height=d, highlightthickness=0, bg=app.color("bg"))
        self.dot = self.canvas.create_oval(1, 1, d - 1, d - 1, outline="")
        self.canvas.pack(side="left", padx=(0, app.px(6)))
        self.label = ttk.Label(self)
        self.label.pack(side="left")
        self._last = None

    def set(self, state, text):
        if (state, text) != self._last:
            self._last = (state, text)
            self.canvas.itemconfigure(self.dot, fill=self.app.color(state))
            self.label.configure(text=text)


class ControllerView(tk.Canvas):
    """An Xbox controller drawing that shows the virtual controller's state."""
    W, H = 440, 280
    ZOOM = 1.2

    def __init__(self, parent, app):
        self.app, self.k = app, app.scale * self.ZOOM
        super().__init__(parent, width=int(self.W * self.k), height=int(self.H * self.k),
                         highlightthickness=0, bg=app.color("bg"))

    def _p(self, *xy):
        return [v * self.k for v in xy]

    def show(self, out, on):
        c, p = self.app.color, self._p
        self.delete("all")
        live = c("accent") if on else c("muted")
        body = [120, 56, 320, 56, 375, 64, 410, 122, 430, 215, 410, 262, 370, 262, 330, 222,
                290, 200, 150, 200, 110, 222, 70, 262, 30, 262, 10, 215, 30, 122, 65, 64]
        self.create_polygon(p(*body), smooth=True, fill=c("panel"), outline=c("track"), width=2)
        t, a, btn = out.triggers, out.axes, out.buttons
        for name, x0, side in (("left_trigger", 40, "LT"), ("right_trigger", 290, "RT")):
            v = t.get(name, 0.0)
            self.create_rectangle(p(x0, 8, x0 + 110, 22), fill=c("track"), outline="")
            if v > 0.005:
                self.create_rectangle(p(x0, 8, x0 + 110 * v, 22), fill=live, outline="")
            lx = x0 - 18 if side == "LT" else x0 + 128
            self.create_text(p(lx, 15), text=side, fill=c("muted"), font=self.app.font_small)
        for name, x0, text in (("lb", 50, "LB"), ("rb", 290, "RB")):
            pressed = name in btn
            self.create_rectangle(p(x0, 30, x0 + 100, 46), fill=live if pressed else c("track"),
                                  outline="")
            self.create_text(p(x0 + 50, 38), text=text, font=self.app.font_small,
                             fill=c("bg") if pressed else c("muted"))
        for (cx, cy), xa, ya, click in (((110, 112), "left_x", "left_y", "ls"),
                                        ((275, 172), "right_x", "right_y", "rs")):
            r = 34
            ring = live if click in btn else c("track")
            self.create_oval(p(cx - r, cy - r, cx + r, cy + r), fill=c("bg"), outline=ring,
                             width=4 if click in btn else 2)
            dx, dy = a.get(xa, 0.0) * (r - 10), -a.get(ya, 0.0) * (r - 10)
            self.create_oval(p(cx + dx - 11, cy + dy - 11, cx + dx + 11, cy + dy + 11),
                             fill=live, outline="")
        for name, dx, dy in (("dpad_up", 0, -14), ("dpad_down", 0, 14), ("dpad_left", -14, 0),
                             ("dpad_right", 14, 0)):
            x, y = 165 + dx, 172 + dy
            self.create_rectangle(p(x - 7, y - 7, x + 7, y + 7),
                                  fill=live if name in btn else c("track"), outline="")
        for name, x, label in (("back", 195, "View"), ("start", 245, "Menu")):
            self.create_oval(p(x - 9, 103, x + 9, 121), fill=live if name in btn else c("track"),
                             outline="")
            self.create_text(p(x, 133), text=label, fill=c("muted"), font=self.app.font_small)
        for name, x, y in (("y", 330, 88), ("x", 306, 112), ("b", 354, 112), ("a", 330, 136)):
            pressed = name in btn
            self.create_oval(p(x - 12, y - 12, x + 12, y + 12), fill=c(name) if pressed else c("bg"),
                             outline=c(name), width=2)
            self.create_text(p(x, y), text=name.upper(), font=self.app.font_small_bold,
                             fill=c("bg") if pressed else c(name))


# --- dialogs ---

class AskDialog(tk.Toplevel):
    """A small themed dialog: a text box and, optionally, a list to pick from."""

    def __init__(self, app, title, prompt, initial="", choices=None, choice_label=""):
        super().__init__(app.root)
        self.result = None
        self.title(title)
        self.transient(app.root)
        self.resizable(False, False)
        frame = ttk.Frame(self, padding=app.px(20))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=prompt).grid(row=0, column=0, columnspan=2, sticky="w")
        self.text = ttk.Entry(frame, width=36)
        self.text.insert(0, initial)
        self.text.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(app.px(6), app.px(12)))
        self.choice = None
        if choices:
            ttk.Label(frame, text=choice_label).grid(row=2, column=0, columnspan=2, sticky="w")
            self.choice = ttk.Combobox(frame, values=choices, state="readonly", width=34)
            self.choice.current(0)
            self.choice.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(app.px(6), app.px(12)))
        ttk.Button(frame, text="OK", style="Accent.TButton", command=self._ok).grid(
            row=4, column=0, sticky="ew", padx=(0, app.px(6)))
        ttk.Button(frame, text="Cancel", command=self.destroy).grid(row=4, column=1, sticky="ew")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.text.focus_set()
        self.text.select_range(0, "end")
        self.wait_visibility()
        self.grab_set()
        app.root.wait_window(self)

    def _ok(self):
        name = self.text.get().strip()
        if name:
            self.result = (name, self.choice.current() if self.choice else None)
            self.destroy()


# --- tabs ---

class DashboardTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=app.px(16))
        self.app = app
        px = app.px
        bike = ttk.LabelFrame(self, text="Your bike", padding=px(16))
        bike.grid(row=0, column=0, sticky="nsew", padx=(0, px(16)))
        game = ttk.LabelFrame(self, text="What the game sees", padding=px(16))
        game.grid(row=0, column=1, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        ttk.Label(bike, text="Handlebars", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        self.steer_text = ttk.Label(bike, style="Big.TLabel")
        self.steer_text.grid(row=1, column=0, sticky="w")
        self.steer_meter = Meter(bike, app, width=470, height=30, centred=True)
        self.steer_meter.grid(row=2, column=0, sticky="w", pady=(px(4), px(18)))

        ttk.Label(bike, text="Brakes", style="Heading.TLabel").grid(row=3, column=0, sticky="w")
        brakes = ttk.Frame(bike)
        brakes.grid(row=4, column=0, sticky="w", pady=(px(4), px(18)))
        self.brake_meters, self.brake_texts = [], []
        for i, side in enumerate(("Left", "Right")):
            ttk.Label(brakes, text=side, width=6).grid(row=i, column=0, sticky="w")
            m = Meter(brakes, app, width=400, height=22)
            m.grid(row=i, column=1, pady=px(3))
            t = ttk.Label(brakes, width=6, anchor="e")
            t.grid(row=i, column=2, sticky="e")
            self.brake_meters.append(m)
            self.brake_texts.append(t)

        ttk.Label(bike, text="Pedalling", style="Heading.TLabel").grid(row=5, column=0, sticky="w")
        self.ped_text = ttk.Label(bike, style="Big.TLabel")
        self.ped_text.grid(row=6, column=0, sticky="w")
        self.ped_detail = ttk.Label(bike, style="Muted.TLabel")
        self.ped_detail.grid(row=7, column=0, sticky="w")
        self.effort_meter = Meter(bike, app, width=470, height=22)
        self.effort_meter.grid(row=8, column=0, sticky="w", pady=(px(8), px(2)))
        self.effort_text = ttk.Label(bike, style="Muted.TLabel")
        self.effort_text.grid(row=9, column=0, sticky="w")

        self.controller = ControllerView(game, app)
        self.controller.grid(row=0, column=0)
        self.profile_text = ttk.Label(game, style="Heading.TLabel")
        self.profile_text.grid(row=1, column=0, pady=(px(10), 0))
        self.note = ttk.Label(game, style="Muted.TLabel", wraplength=px(420), justify="center")
        self.note.grid(row=2, column=0, pady=(px(6), 0))
        self.zwift_text = ttk.Label(game, style="Heading.TLabel", wraplength=px(480), justify="center")
        self.zwift_text.grid(row=3, column=0, pady=(px(18), 0))

    def update_view(self, snap, profile):
        app = self.app
        v = snap.values
        deg = snap.steering_deg
        if not snap.bike_connected:
            self.steer_text.configure(text="Bike not connected")
        elif not snap.calibrated:
            self.steer_text.configure(text="Not calibrated yet")
        elif deg is None:
            self.steer_text.configure(text="Waiting for the steering sensor")
        elif abs(deg) < 0.5:
            self.steer_text.configure(text="Straight")
        else:
            self.steer_text.configure(text=f"{abs(deg):.0f}° {'right' if deg > 0 else 'left'}")
        self.steer_meter.show(v.get("steering", 0.0))
        for i, key in enumerate(("left_brake", "right_brake")):
            self.brake_meters[i].show(v.get(key, 0.0))
            self.brake_texts[i].configure(text=f"{v.get(key, 0.0) * 100:.0f}%")

        ped = profile.pedalling
        label, unit, _, _ = PEDALLING_MEASURES[ped.measure]
        tr = snap.trainer
        raw = {"power": tr.power_w, "speed": tr.speed_kmh, "cadence": tr.cadence_rpm}[ped.measure]
        if snap.trainer_status == "connected":
            self.ped_text.configure(text=f"{raw:.0f} {unit}")
            self.ped_detail.configure(
                text=f"{tr.power_w:.0f} W  ·  {tr.speed_kmh:.1f} km/h  ·  {tr.cadence_rpm:.0f} rpm")
        elif snap.trainer_status == "off":
            self.ped_text.configure(text="Not used by this profile")
            self.ped_detail.configure(text="Zwift reads your trainer directly." if profile.zwift_steering
                                      else "Add a Pedalling row in Controls to use the trainer.")
        else:
            self.ped_text.configure(text="Trainer not connected")
            self.ped_detail.configure(text=snap.trainer_status.capitalize())
        effort = v.get("pedalling", 0.0)
        self.effort_meter.show(effort)
        self.effort_text.configure(
            text=f"{effort * 100:.0f}% effort  (100% = {ped.full:.0f} {unit} of {label.lower()}, "
                 f"set in Tuning)")

        self.controller.show(snap.outputs, snap.controller_on and snap.controls_on)
        self.profile_text.configure(text=f"Profile: {profile.name}")
        has_keys = any(b.target == KEYBOARD for b in profile.bindings)
        if not profile.bindings:
            note = "This profile has no controls, so games don't see a controller."
        elif not app.engine.want_controller:
            note = "Game controls are off, so games can't see the bike. Switch them on at the top right."
        elif app.engine.calibrating:
            note = "Paused while calibrating."
        elif not snap.calibrated:
            note = "Calibrate the bike first (Calibrate tab)."
        elif not profile.uses_controller():
            note = "This profile only presses keyboard keys and mouse buttons."
        else:
            note = "Games see this as an Xbox 360 controller" + (", plus key presses." if has_keys else ".")
        if has_keys and snap.controls_on:
            held = ", ".join(KEYS[k] for k in sorted(snap.outputs.keys)) or "none"
            note += f"\nKeys held now: {held}"
        self.note.configure(text=note)
        if profile.zwift_steering:
            _, status = ZWIFT_STATUS.get(snap.zwift_state, ZWIFT_STATUS[0])
            self.zwift_text.configure(text=f"Zwift steering: {angle_text(snap.zwift_angle)}\n{status}")
        else:
            self.zwift_text.configure(text="")


class ControlsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=app.px(16))
        self.app = app
        px = app.px
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.name = ttk.Label(top, style="Big.TLabel")
        self.name.pack(side="left")
        for text, cmd in (("Delete", app.delete_profile), ("Restore defaults", app.restore_profile),
                          ("Rename…", app.rename_profile), ("Copy…", app.copy_profile),
                          ("New profile…", app.new_profile)):
            ttk.Button(top, text=text, command=cmd).pack(side="right", padx=(px(6), 0))
        ttk.Label(self, style="Muted.TLabel", wraplength=px(1100), justify="left", text=(
            "Each row links one part of the bike to one control: a stick, trigger or button on the "
            "virtual Xbox controller, or a keyboard key or mouse button. Buttons and keys can be held "
            "while you squeeze or pedal (Hold), or switch on with one squeeze and off with the next "
            "(Toggle). A bike input can drive several controls, and several inputs can share a stick "
            "(their amounts add up). How sensitive each input feels is set in the Tuning tab.")
        ).pack(fill="x", pady=(px(6), px(14)))
        self.rows = ttk.Frame(self)
        self.rows.pack(fill="x")
        buttons = ttk.Frame(self)
        buttons.pack(anchor="w", pady=(px(12), 0))
        ttk.Button(buttons, text="+  Add a row", command=self.add_row).pack(side="left")
        ttk.Button(buttons, text="Swap left and right triggers", command=self._swap_triggers).pack(
            side="left", padx=(px(10), 0))
        self.live = []

        zwift = ttk.LabelFrame(self, text="Zwift", padding=px(14))
        zwift.pack(fill="x", pady=(px(24), 0))
        self.zwift_var = tk.BooleanVar()
        ttk.Checkbutton(zwift, text="Send handlebar steering to Zwift", variable=self.zwift_var,
                        command=self._zwift_toggled).grid(row=0, column=0, sticky="w")
        self.zwift_status = ttk.Label(zwift)
        self.zwift_status.grid(row=0, column=1, sticky="w", padx=(px(24), 0))
        ttk.Label(zwift, style="Muted.TLabel", wraplength=px(1000), justify="left", text=(
            "The bike then shows up in Zwift as a steering device called STERZO (it pretends to be an "
            "Elite Sterzo Smart). In Zwift's pairing screen, choose it under Steer. How far you turn "
            "for full steering is set in Tuning → Handlebars.")).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(px(6), 0))

    def rebuild(self):
        app, px = self.app, self.app.px
        for w in self.rows.winfo_children():
            w.destroy()
        profile = app.settings.profile()
        self.name.configure(text=profile.name)
        self.zwift_var.set(profile.zwift_steering)
        for col, text in enumerate(("Bike input", "", "Sends", "Options", "Now")):
            if profile.bindings:
                ttk.Label(self.rows, text=text, style="Muted.TLabel").grid(
                    row=0, column=col, sticky="w", padx=(0, px(10)))
        self.live = []
        for i, b in enumerate(profile.bindings, start=1):
            src = ttk.Combobox(self.rows, values=list(SOURCES.values()), state="readonly", width=22)
            src.current(SOURCE_KEYS.index(b.source))
            src.bind("<<ComboboxSelected>>",
                     lambda e, b=b, w=src: self._change(b, source=SOURCE_KEYS[w.current()]))
            src.grid(row=i, column=0, pady=px(4), padx=(0, px(10)))
            ttk.Label(self.rows, text="→").grid(row=i, column=1, padx=(0, px(10)))
            tgt = ttk.Combobox(self.rows, values=list(TARGETS.values()), state="readonly", width=24)
            tgt.current(TARGET_KEYS.index(b.target))
            tgt.bind("<<ComboboxSelected>>",
                     lambda e, b=b, w=tgt: self._change(b, target=TARGET_KEYS[w.current()]))
            tgt.grid(row=i, column=2, padx=(0, px(10)))
            opts = ttk.Frame(self.rows)
            opts.grid(row=i, column=3, sticky="w", padx=(0, px(10)))
            if b.target in AXES:
                var = tk.BooleanVar(value=b.invert)
                ttk.Checkbutton(opts, text="Reverse direction", variable=var,
                                command=lambda b=b, var=var: self._change(b, invert=var.get())
                                ).pack(side="left")
            elif b.pressable:
                if b.target == KEYBOARD:
                    key = ttk.Combobox(opts, values=list(KEYS.values()), state="readonly", width=15)
                    key.current(KEY_IDS.index(b.key))
                    key.bind("<<ComboboxSelected>>",
                             lambda e, b=b, w=key: self._change(b, key=KEY_IDS[w.current()]))
                    key.pack(side="left", padx=(0, px(8)))
                mode = ttk.Combobox(opts, values=list(MODES.values()), state="readonly", width=6)
                mode.current(MODE_KEYS.index(b.mode))
                mode.bind("<<ComboboxSelected>>",
                          lambda e, b=b, w=mode: self._change(b, mode=MODE_KEYS[w.current()]))
                mode.pack(side="left")
                ttk.Label(opts, text="at").pack(side="left", padx=(px(8), 0))
                var = tk.StringVar(value=f"{b.threshold * 100:.0f}")
                spin = ttk.Spinbox(opts, from_=5, to=100, increment=5, width=3, textvariable=var,
                                   command=lambda b=b, var=var: self._threshold(b, var))
                spin.bind("<FocusOut>", lambda e, b=b, var=var: self._threshold(b, var))
                spin.bind("<Return>", lambda e, b=b, var=var: self._threshold(b, var))
                spin.pack(side="left", padx=px(6))
                ttk.Label(opts, text="%").pack(side="left")
            meter = Meter(self.rows, app, width=90, height=14, centred=b.target in AXES)
            meter.grid(row=i, column=4, padx=(0, px(10)))
            ttk.Button(self.rows, text="\u2715", width=3, command=lambda b=b: self._remove(b)).grid(
                row=i, column=5)
            self.live.append((b, meter))
        if not profile.bindings:
            ttk.Label(self.rows, text="No controls yet. Add a row to start.").grid(
                row=1, column=0, columnspan=4, sticky="w", pady=px(8))

    def _change(self, b, **fields):
        for k, v in fields.items():
            setattr(b, k, v)
        self.app.schedule_save()
        if "target" in fields:
            self.after_idle(self.rebuild)

    def _threshold(self, b, var):
        try:
            b.threshold = min(max(float(var.get()), 5.0), 100.0) / 100
        except ValueError:
            return
        self.app.schedule_save()

    def _remove(self, b):
        self.app.settings.profile().bindings.remove(b)
        self.app.schedule_save()
        self.rebuild()

    def _zwift_toggled(self):
        self.app.settings.profile().zwift_steering = self.zwift_var.get()
        self.app.schedule_save()

    def _swap_triggers(self):
        profile = self.app.settings.profile()
        if not any(b.target in TRIGGERS for b in profile.bindings):
            messagebox.showinfo(APP_NAME, "This profile has no trigger rows to swap.")
            return
        swap_triggers(profile)
        self.app.schedule_save()
        self.rebuild()

    def add_row(self):
        self.app.settings.profile().bindings.append(Binding("pedalling", "right_trigger"))
        self.app.schedule_save()
        self.rebuild()

    def update_view(self, snap, profile):
        if profile.zwift_steering:
            _, status = ZWIFT_STATUS.get(snap.zwift_state, ZWIFT_STATUS[0])
            self.zwift_status.configure(text=f"Now: {angle_text(snap.zwift_angle)}  ·  {status}")
        else:
            self.zwift_status.configure(text="")
        for b, meter in self.live:
            v = snap.values.get(b.source, 0.0)
            if b.target in AXES:
                meter.show(-v if b.invert else v)
            elif b.pressable:
                pressed = (b.key in snap.outputs.keys if b.target == KEYBOARD
                           else b.target in snap.outputs.buttons)
                meter.show(v, marks=(b.threshold,), color="good" if pressed else "accent")
            else:
                meter.show(max(v, 0.0))


class Slider:
    """A labelled slider bound to one profile setting. Takes three grid rows:
    label and value, the slider, and a hint."""

    def __init__(self, app, parent, row, label, lo, hi, step, fmt, get, put, hint):
        px = app.px
        self.app, self.step, self.fmt, self.get, self.put = app, step, fmt, get, put
        top = ttk.Frame(parent)
        top.grid(row=row, column=0, sticky="ew", pady=(px(14), 0))
        ttk.Label(top, text=label).pack(side="left")
        self.value = ttk.Label(top, style="Value.TLabel")
        self.value.pack(side="right")
        self.scale = ttk.Scale(parent, from_=lo, to=hi, command=self._moved)
        self.scale.grid(row=row + 1, column=0, sticky="ew", pady=(px(2), 0))
        ttk.Label(parent, text=hint, style="Muted.TLabel", wraplength=px(330), justify="left").grid(
            row=row + 2, column=0, sticky="w")
        self._syncing = False

    def configure_range(self, lo, hi, step, fmt):
        self.step, self.fmt = step, fmt
        self.scale.configure(from_=lo, to=hi)

    def refresh(self):
        self._syncing = True
        self.scale.set(self.get())
        self._syncing = False
        self.value.configure(text=self.fmt.format(self.get()))

    def _moved(self, raw):
        if self._syncing:
            return
        v = round(round(float(raw) / self.step) * self.step, 4)
        if v != self.get():
            self.put(v)
            self.app.schedule_save()
        self.value.configure(text=self.fmt.format(v))


class TuningTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=app.px(16))
        self.app = app
        px = app.px
        prof = app.settings.profile
        ttk.Label(self, style="Muted.TLabel", wraplength=px(1000), justify="left", text=(
            "How each part of the bike feels in the game. These settings belong to the current "
            "profile, so every game can feel different. Changes apply straight away.")
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, px(12)))
        for col in range(3):
            self.columnconfigure(col, weight=1)

        bars = ttk.LabelFrame(self, text="Handlebars", padding=px(14))
        bars.grid(row=1, column=0, sticky="nsew", padx=(0, px(12)))
        self.steer_live = ttk.Label(bars, style="Heading.TLabel")
        self.steer_live.grid(row=0, column=0, sticky="w")
        self.steer_meter = Meter(bars, app, width=330, height=20, centred=True)
        self.steer_meter.grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        self.sliders = [
            Slider(app, bars, 2, "Full turn at", 5, 60, 1, "{:.0f}\u00b0",
                   lambda: prof().steering.full_lock_deg,
                   lambda v: setattr(prof().steering, "full_lock_deg", v),
                   "Turning the handlebars this far gives a full stick."),
            Slider(app, bars, 5, "Dead zone", 0, 10, 0.5, "{:.1f}\u00b0",
                   lambda: prof().steering.deadzone_deg,
                   lambda v: setattr(prof().steering, "deadzone_deg", v),
                   "Small turns near the centre count as straight, so you don't have to hold "
                   "the bars perfectly still."),
            Slider(app, bars, 8, "Response curve", 1.0, 3.0, 0.1, "{:.1f}",
                   lambda: prof().steering.curve,
                   lambda v: setattr(prof().steering, "curve", v),
                   "1.0 is even. Higher makes small turns gentler for fine control, while a full "
                   "turn stays the same."),
        ]

        ped = ttk.LabelFrame(self, text="Pedalling", padding=px(14))
        ped.grid(row=1, column=1, sticky="nsew", padx=(0, px(12)))
        self.ped_live = ttk.Label(ped, style="Heading.TLabel")
        self.ped_live.grid(row=0, column=0, sticky="w")
        self.ped_meter = Meter(ped, app, width=330, height=20)
        self.ped_meter.grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        ttk.Label(ped, text="Measured by").grid(row=2, column=0, sticky="w", pady=(px(14), 0))
        self.measure = tk.StringVar(value=prof().pedalling.measure)
        radios = ttk.Frame(ped)
        radios.grid(row=3, column=0, sticky="w", pady=(px(4), px(2)))
        for key, (label, unit, _, _) in PEDALLING_MEASURES.items():
            ttk.Radiobutton(radios, text=label, value=key, variable=self.measure,
                            command=self._measure_changed).pack(side="left", padx=(0, px(14)))
        ttk.Label(ped, style="Muted.TLabel", wraplength=px(330), justify="left", text=(
            "Power follows how hard you pedal and drops to zero within a second of stopping. "
            "Speed coasts down slowly with the trainer's flywheel.")).grid(
            row=4, column=0, sticky="w")
        self.full = Slider(app, ped, 5, "100% at", 10, 600, 5, "{:.0f} W",
                           lambda: prof().pedalling.full,
                           lambda v: setattr(prof().pedalling, "full", max(v, 1)),
                           "Pedalling this hard gives a full trigger or stick. Lower it if full "
                           "throttle takes too much effort.")
        self.start = Slider(app, ped, 8, "Starts at", 0, 100, 1, "{:.0f} W",
                            lambda: prof().pedalling.start,
                            lambda v: setattr(prof().pedalling, "start", v),
                            "Anything below this counts as not pedalling.")
        self.smooth = Slider(app, ped, 11, "Smoothing", 0, 2, 0.1, "{:.1f} s",
                             lambda: prof().pedalling.smoothing_s,
                             lambda v: setattr(prof().pedalling, "smoothing_s", v),
                             "Evens out the trainer's jumpy readings. Higher is smoother but "
                             "reacts more slowly.")
        self.sliders += [self.full, self.start, self.smooth]

        brakes = ttk.LabelFrame(self, text="Brakes", padding=px(14))
        brakes.grid(row=1, column=2, sticky="nsew")
        self.brake_meters, self.brake_texts = [], []
        for i, side in enumerate(("Left", "Right")):
            row = ttk.Frame(brakes)
            row.grid(row=i, column=0, sticky="w", pady=px(2))
            ttk.Label(row, text=side, width=6).pack(side="left")
            m = Meter(row, app, width=230, height=16)
            m.pack(side="left")
            t = ttk.Label(row, width=6, anchor="e")
            t.pack(side="left")
            self.brake_meters.append(m)
            self.brake_texts.append(t)
        self.sliders += [
            Slider(app, brakes, 2, "Dead zone", 0, 30, 1, "{:.0f}%",
                   lambda: prof().brakes.deadzone * 100,
                   lambda v: setattr(prof().brakes, "deadzone", v / 100),
                   "Lever travel ignored at the start, so resting your fingers on the levers "
                   "doesn't brake."),
            Slider(app, brakes, 5, "Response curve", 1.0, 3.0, 0.1, "{:.1f}",
                   lambda: prof().brakes.curve,
                   lambda v: setattr(prof().brakes, "curve", v),
                   "Higher gives finer control of light braking."),
            ]
        ttk.Label(brakes, style="Muted.TLabel", wraplength=px(330), justify="left", text=(
            "If a brake can't reach 100%, recalibrate the brakes in the Calibrate tab.")).grid(
            row=8, column=0, sticky="w", pady=(px(16), 0))
        for frame in (bars, ped, brakes):
            frame.columnconfigure(0, weight=1)

    def _measure_ranges(self):
        key = self.app.settings.profile().pedalling.measure
        _, unit, full_max, start_max = PEDALLING_MEASURES[key]
        step = 5 if key == "power" else 1
        self.full.configure_range(step * 2, full_max, step, "{:.0f} " + unit)
        self.start.configure_range(0, start_max, 1, "{:.0f} " + unit)

    def _measure_changed(self):
        ped = self.app.settings.profile().pedalling
        key = self.measure.get()
        if key == ped.measure:
            return
        defaults = {"power": (250, 10), "speed": (30, 2), "cadence": (90, 20)}
        ped.measure = key
        ped.full, ped.start = defaults[key]
        self.app.schedule_save()
        self.refresh()

    def refresh(self):
        self.measure.set(self.app.settings.profile().pedalling.measure)
        self._measure_ranges()
        for s in self.sliders:
            s.refresh()

    def update_view(self, snap, profile):
        v, deg = snap.values, snap.steering_deg
        angle = "-" if deg is None else f"{abs(deg):.0f}° {'right' if deg > 0 else 'left'}"
        self.steer_live.configure(text=f"Now: {angle}  →  stick {v.get('steering', 0.0):+.2f}")
        self.steer_meter.show(v.get("steering", 0.0))
        ped = profile.pedalling
        _, unit, _, _ = PEDALLING_MEASURES[ped.measure]
        tr = snap.trainer
        raw = {"power": tr.power_w, "speed": tr.speed_kmh, "cadence": tr.cadence_rpm}[ped.measure]
        effort = v.get("pedalling", 0.0)
        self.ped_live.configure(text=f"Now: {raw:.0f} {unit}  →  {effort * 100:.0f}%")
        self.ped_meter.show(effort)
        for i, key in enumerate(("left_brake", "right_brake")):
            self.brake_meters[i].show(v.get(key, 0.0))
            self.brake_texts[i].configure(text=f"{v.get(key, 0.0) * 100:.0f}%")


class TrainerTab(ttk.Frame):
    """Trainer resistance from the truck games: the plugin, and per-profile settings."""
    STATUS_EVERY = 60  # ticks between plugin file checks (~2 s)

    def __init__(self, parent, app):
        super().__init__(parent, padding=app.px(16))
        self.app = app
        px = app.px
        prof = app.settings.profile
        ttk.Label(self, style="Muted.TLabel", wraplength=px(1100), justify="left", text=(
            "Let the trainer feel the game's hills: climbs make pedalling heavier. This works with "
            "American Truck Simulator and Euro Truck Simulator 2, through a small plugin that tells "
            "this app the truck's road slope.")).grid(row=0, column=0, columnspan=2, sticky="w",
                                                      pady=(0, px(12)))
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        game = ttk.LabelFrame(self, text="Game", padding=px(14))
        game.grid(row=1, column=0, sticky="nsew", padx=(0, px(12)))
        self.game_live = ttk.Label(game, style="Heading.TLabel", wraplength=px(480), justify="left")
        self.game_live.grid(row=0, column=0, sticky="w")
        self.plugins = ttk.Label(game, justify="left", wraplength=px(480))
        self.plugins.grid(row=1, column=0, sticky="w", pady=(px(12), px(8)))
        ttk.Button(game, text="Install the game plugin", style="Accent.TButton",
                   command=self._install).grid(row=2, column=0, sticky="w")
        ttk.Label(game, style="Muted.TLabel", wraplength=px(480), justify="left", text=(
            "Close the game before installing or updating the plugin. The first time the game starts "
            "with it, the game shows a message about advanced SDK features: click OK.")).grid(
            row=3, column=0, sticky="w", pady=(px(10), 0))

        res = ttk.LabelFrame(self, text="Trainer resistance (this profile)", padding=px(14))
        res.grid(row=1, column=1, sticky="nsew")
        res.columnconfigure(0, weight=1)
        self.res_live = ttk.Label(res, style="Heading.TLabel", wraplength=px(480), justify="left")
        self.res_live.grid(row=0, column=0, sticky="w")
        self.mode = tk.StringVar(value=prof().resistance.mode)
        radios = ttk.Frame(res)
        radios.grid(row=1, column=0, sticky="w", pady=(px(10), 0))
        for key, label in RESISTANCE_MODES.items():
            ttk.Radiobutton(radios, text=label, value=key, variable=self.mode,
                            command=self._mode_changed).pack(side="left", padx=(0, px(16)))
        self.sliders = [
            Slider(app, res, 2, "Strength", 10, 100, 5, "{:.0f}% of the game's slope",
                   lambda: prof().resistance.strength * 100,
                   lambda v: setattr(prof().resistance, "strength", v / 100),
                   "Trucks climb long grades, so about half the real slope usually feels right."),
            Slider(app, res, 5, "Extra while braking", 0, 10, 0.5, "+{:.1f}% slope",
                   lambda: prof().resistance.brake_pct,
                   lambda v: setattr(prof().resistance, "brake_pct", v),
                   "Adds resistance while the truck brakes, at full brake. 0 turns it off."),
            Slider(app, res, 8, "Maximum slope", 2, 16, 1, "{:.0f}%",
                   lambda: prof().resistance.max_pct,
                   lambda v: setattr(prof().resistance, "max_pct", v),
                   "Caps the trainer's slope uphill and downhill. The Flux 2 can simulate up to 16%."),
        ]
        ttk.Label(res, style="Muted.TLabel", wraplength=px(480), justify="left", text=(
            "Downhill makes pedalling as light as the trainer allows; it can't pedal for you. "
            "When the game is paused or closed, the trainer goes back to flat.")).grid(
            row=11, column=0, sticky="w", pady=(px(14), 0))
        self._ticks = 0

    def _mode_changed(self):
        self.app.settings.profile().resistance.mode = self.mode.get()
        self.app.schedule_save()

    def refresh(self):
        self.mode.set(self.app.settings.profile().resistance.mode)
        for s in self.sliders:
            s.refresh()
        self._show_plugins()

    def _show_plugins(self):
        found = games.find_games()
        if not found:
            self.plugins.configure(text="No American Truck Simulator or Euro Truck Simulator 2 found "
                                        "in your Steam libraries.")
            return
        words = {"installed": "plugin installed \u2714", "outdated": "plugin out of date: install again",
                 "missing": "plugin not installed"}
        self.plugins.configure(text="\n".join(f"{games.GAMES[k]}: {words[games.plugin_status(folder)]}"
                                              for k, folder in found.items()))

    def _install(self):
        found = games.find_games()
        if not found:
            messagebox.showinfo(APP_NAME, "No truck games found in your Steam libraries.")
            return
        done, failed = [], []
        for key, folder in found.items():
            try:
                games.install_plugin(folder)
                done.append(games.GAMES[key])
            except OSError as e:
                failed.append(f"{games.GAMES[key]}: {e.strerror or e}")
        self._show_plugins()
        if failed:
            messagebox.showwarning(APP_NAME, "Couldn't install the plugin for:\n" + "\n".join(failed) +
                                   "\n\nClose the game and try again.")
        else:
            messagebox.showinfo(APP_NAME, "Plugin installed for " + " and ".join(done) + ".")

    def update_view(self, snap, profile):
        self._ticks += 1
        if self._ticks % self.STATUS_EVERY == 1:
            self._show_plugins()
        g = snap.game
        if self.app.engine.game.error:
            self.game_live.configure(text=self.app.engine.game.error)
        elif not g.game:
            self.game_live.configure(text="No game sending data")
        else:
            name = games.GAMES.get(g.game, g.game)
            hill = "uphill" if g.grade_pct > 0.5 else "downhill" if g.grade_pct < -0.5 else "flat"
            state = "paused" if g.paused else f"{abs(g.speed_kmh):.0f} km/h, road {g.grade_pct:+.1f}% ({hill})"
            self.game_live.configure(text=f"{name}: {state}")
        if profile.resistance.mode == "off":
            text = "Off for this profile"
        elif snap.trainer_status != "connected":
            text = "Trainer not connected"
        else:
            accepted = "" if snap.grade_set is None else f"  (trainer set to {snap.grade_set:+.1f}%)"
            text = f"Trainer slope: {snap.grade_wanted or 0.0:+.1f}%{accepted}"
        self.res_live.configure(text=text)


class CalibrateTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=app.px(16))
        self.app = app
        px = app.px
        ttk.Label(self, wraplength=px(1000), justify="left", text=(
            "Calibration teaches the app how your bike's sensors behave. Do a full calibration "
            "after moving a sensor, the bike or the trainer. Redo just the brakes if they don't "
            "reach 100% or feel off. The virtual controller pauses while you calibrate.")
        ).pack(fill="x")
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(px(12), px(16)))
        self.full_btn = ttk.Button(buttons, text="Full calibration (about a minute)",
                                   style="Accent.TButton", command=lambda: self._start("full"))
        self.full_btn.pack(side="left")
        self.brakes_btn = ttk.Button(buttons, text="Brakes only", command=lambda: self._start("brakes"))
        self.brakes_btn.pack(side="left", padx=px(10))
        self.cancel_btn = ttk.Button(buttons, text="Cancel", command=app.engine.cancel_calibration)
        self.cancel_btn.pack(side="left")

        card = ttk.LabelFrame(self, text="Calibration", padding=px(18))
        card.pack(fill="both", expand=True)
        self.step = ttk.Label(card, style="Heading.TLabel")
        self.step.pack(anchor="w")
        self.instruction = ttk.Label(card, style="Big.TLabel", wraplength=px(980), justify="left")
        self.instruction.pack(anchor="w", pady=(px(4), px(10)))
        self.live = ttk.Label(card, style="Muted.TLabel")
        self.live.pack(anchor="w")
        self.results = ttk.Label(card, justify="left", wraplength=px(980))
        self.results.pack(anchor="w", pady=(px(14), 0))
        self.summary = ttk.Label(self, style="Muted.TLabel", justify="left", wraplength=px(1000))
        self.summary.pack(anchor="w", pady=(px(12), 0))

    def _start(self, kind):
        if kind == "brakes" and not self.app.engine.cal:
            messagebox.showinfo(APP_NAME, "Do a full calibration first.")
            return
        self.app.engine.start_calibration(kind)

    def update_view(self, snap, profile):
        engine = self.app.engine
        run = engine.calibration
        busy = engine.calibrating
        for b in (self.full_btn, self.brakes_btn):
            b.state(["disabled"] if busy or not snap.bike_connected else ["!disabled"])
        self.cancel_btn.state(["!disabled"] if busy else ["disabled"])
        if busy:
            self.step.configure(text=run.step)
            self.instruction.configure(text=run.instruction)
        elif run and run.error:
            self.step.configure(text="Calibration stopped")
            self.instruction.configure(text=f"{run.error} Press a calibration button to start again.")
        elif run:
            self.step.configure(text="Calibration saved ✔")
            self.instruction.configure(text="All done. The new calibration is in use.")
        else:
            self.step.configure(text="Ready" if snap.bike_connected else "Bike not connected")
            self.instruction.configure(text="Choose a calibration above.")
        self.results.configure(text="\n".join(run.results) if run else "")
        mpu = "ok" if snap.mpu_ok else "NOT RESPONDING"
        self.live.configure(text=f"Sensors now: left brake {snap.brakes_mv[0]} mV  ·  right brake "
                                 f"{snap.brakes_mv[1]} mV  ·  steering sensor {mpu}")
        cal = engine.cal
        if cal:
            when = time.strftime("%d %b %Y, %H:%M", time.localtime(CALIBRATION_PATH.stat().st_mtime))
            self.summary.configure(text=(
                f"Current calibration ({when}): steering axis tilted {cal.steering_tilt_deg():.0f}° "
                f"from vertical; brakes {cal.brake_rest_mv[0]}→{cal.brake_full_mv[0]} mV (left) and "
                f"{cal.brake_rest_mv[1]}→{cal.brake_full_mv[1]} mV (right)."))
        else:
            self.summary.configure(text="Not calibrated yet.")


HELP = [
    ("Getting started", [
        "1.  Switch on Game controls (top right). Games then see the bike as an Xbox 360 "
        "controller, and any keys you've set up get pressed.",
        "2.  Pick a game profile (top right). Each profile keeps its own controls and tuning.",
        "3.  Start the game and choose its controller or gamepad option if it has one.",
    ]),
    ("Games", [
        "•  American Truck Simulator and Euro Truck Simulator 2: Options → Controls, choose "
        "the gamepad option. Use “Driving games”. The keyboard still works for everything else.",
        "•  Minecraft (Bedrock, the Microsoft Store version): controllers work straight away. Use "
        "“Minecraft”.",
        "•  Minecraft Java Edition: needs a controller mod, such as Controlify.",
        "•  Zwift: pick the “Zwift” profile here. In Zwift's pairing screen, choose "
        "STERZO under Steer. Zwift connects to your trainer itself, so this app lets go of the "
        "trainer while a profile without pedalling (like Zwift) is selected.",
    ]),
    ("Keys, buttons and triggers", [
        "\u2022  Any row in Controls can press a keyboard key or mouse button instead of a controller "
        "input, so each brake can have its own key.",
        "\u2022  Hold presses the key while you squeeze; Toggle turns it on with one squeeze and off "
        "with the next. \u201cat\u201d sets how far you squeeze (or how hard you pedal) to press it.",
        "\u2022  If a game accelerates when you brake, use \u201cSwap left and right triggers\u201d in "
        "Controls.",
        "\u2022  Calibration works out which brake sensor is on which lever by itself.",
    ]),
    ("Trainer resistance", [
        "\u2022  In the Trainer tab, install the game plugin once (with the game closed), then pick "
        "\u201cFollow the game's hills\u201d for the profile. Climbs in American Truck Simulator or Euro "
        "Truck Simulator 2 then make pedalling heavier.",
    ]),
    ("If something isn't working", [
        "•  “Steering sensor not responding”: its wiring has lost contact. Check the four "
        "wires and their pins. The blue light on the ESP32 blinks while it's disconnected.",
        "•  Steering is off-centre or wrong after moving the bike or trainer: do a full calibration.",
        "•  A brake doesn't reach 100%: calibrate the brakes again, or adjust it in Tuning.",
        "•  “Trainer not connected”: check it's plugged in, pedal to wake it, and close other "
        "apps using it.",
        "•  “Bike not connected”: check the USB cable to the ESP32.",
    ]),
]


class HelpTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=app.px(20))
        px = app.px
        for i, (heading, lines) in enumerate(HELP):
            ttk.Label(self, text=heading, style="Heading.TLabel").pack(
                anchor="w", pady=(px(18) if i else 0, px(6)))
            for line in lines:
                ttk.Label(self, text=line, justify="left", wraplength=px(1040)).pack(
                    anchor="w", pady=px(2))

    def update_view(self, snap, profile):
        pass


# --- the window ---

class App:
    def __init__(self, root, settings, engine, tab=None):
        self.root, self.settings, self.engine = root, settings, engine
        self.theme = "dark" if windows_dark_mode() else "light"
        sv_ttk.set_theme(self.theme)
        self.scale = root.winfo_fpixels("1i") / 96
        self.font_small = ("Segoe UI", 10)
        self.font_small_bold = ("Segoe UI Semibold", 11)
        style = ttk.Style()
        style.configure("Big.TLabel", font=("Segoe UI Semibold", 22))
        style.configure("Heading.TLabel", font=("Segoe UI Semibold", 12))
        style.configure("Value.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Muted.TLabel", foreground=self.color("muted"))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 18))
        self._save_job = None

        root.title(APP_NAME)
        if ICON_PATH.exists():
            root.iconbitmap(default=str(ICON_PATH))
        root.geometry(f"{self.px(1180)}x{self.px(760)}")
        root.minsize(self.px(1100), self.px(720))
        root.protocol("WM_DELETE_WINDOW", self.close)

        header = ttk.Frame(root, padding=(self.px(20), self.px(14), self.px(20), self.px(6)))
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        self.dots = [StatusDot(header, self) for _ in range(3)]
        for d in self.dots:
            d.pack(side="left", padx=(self.px(22), 0))
        self.controller_var = tk.BooleanVar(value=engine.want_controller)
        ttk.Checkbutton(header, text="Game controls", style="Switch.TCheckbutton",
                        variable=self.controller_var, command=self._toggle_controller).pack(
            side="right")
        self.profile_box = ttk.Combobox(header, state="readonly", width=24)
        self.profile_box.bind("<<ComboboxSelected>>", self._pick_profile)
        self.profile_box.pack(side="right", padx=(self.px(8), self.px(24)))
        ttk.Label(header, text="Profile").pack(side="right")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=self.px(20), pady=(self.px(6), self.px(20)))
        self.tabs = {}
        for name, cls in (("Dashboard", DashboardTab), ("Controls", ControlsTab),
                          ("Tuning", TuningTab), ("Trainer", TrainerTab), ("Calibrate", CalibrateTab),
                          ("Help", HelpTab)):
            page = cls(self.notebook, self)
            self.notebook.add(page, text=f"  {name}  ")
            self.tabs[name] = page
        root.report_callback_exception = self._report_error
        self.profile_changed()
        if tab in self.tabs:
            self.notebook.select(self.tabs[tab])
        elif not engine.cal:
            self.notebook.select(self.tabs["Calibrate"])
        engine.log(f"Window opened (asked for tab {tab!r}, showing {self.notebook.tab('current', 'text').strip()})")
        self.root.after(TICK_MS, self.tick)

    def _report_error(self, kind, value, tb):
        import traceback
        self.engine.log("Window error:\n" + "".join(traceback.format_exception(kind, value, tb)))

    def color(self, name):
        return PALETTES[self.theme][name]

    def px(self, n):
        return int(round(n * self.scale))

    def schedule_save(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(SAVE_DELAY_MS, self.settings.save)

    def tick(self):
        self.root.after(TICK_MS, self.tick)  # first, so an error can't stop the updates
        snap = self.engine.snapshot
        profile = self.settings.profile()
        bike, sensor, trainer = self.dots
        bike.set("good" if snap.bike_connected else "bad",
                 "Bike connected" if snap.bike_connected else "Bike not connected")
        if not snap.bike_connected:
            sensor.set("muted", "Steering sensor")
        else:
            sensor.set("good" if snap.mpu_ok else "bad",
                       "Steering sensor OK" if snap.mpu_ok else "Steering sensor not responding")
        if snap.trainer_status == "connected":
            trainer.set("good", f"Trainer: {snap.trainer_name or 'connected'}")
        elif snap.trainer_status == "off":
            trainer.set("muted", "Trainer: not in use")
        else:
            trainer.set("warn", "Trainer: " + ("searching…" if snap.trainer_status == "searching"
                                                else "not connected"))
        if self.controller_var.get() != self.engine.want_controller:
            self.controller_var.set(self.engine.want_controller)
        current = self.notebook.nametowidget(self.notebook.select())
        current.update_view(snap, profile)

    def _toggle_controller(self):
        self.engine.set_controller(self.controller_var.get())

    # --- profiles ---

    def profile_changed(self):
        self.profile_box.configure(values=self.settings.names())
        self.profile_box.set(self.settings.profile().name)
        self.tabs["Controls"].rebuild()
        self.tabs["Tuning"].refresh()
        self.tabs["Trainer"].refresh()

    def _pick_profile(self, _=None):
        self.settings.current = self.profile_box.get()
        self.settings.save()
        self.profile_changed()

    def _add(self, profile):
        self.settings.profiles.append(profile)
        self.settings.current = profile.name
        self.settings.save()
        self.profile_changed()

    def new_profile(self):
        keys = list(PRESETS)
        d = AskDialog(self, "New profile", "Name for the new profile:", "My game",
                      choices=list(PRESETS.values()), choice_label="Start from:")
        if d.result:
            name, choice = d.result
            self._add(make_preset(keys[choice], self.settings.unique_name(name)))

    def copy_profile(self):
        p = self.settings.profile()
        d = AskDialog(self, "Copy profile", "Name for the copy:", f"{p.name} copy")
        if d.result:
            self._add(copy_profile(p, self.settings.unique_name(d.result[0])))

    def rename_profile(self):
        p = self.settings.profile()
        d = AskDialog(self, "Rename profile", "New name:", p.name)
        if d.result and d.result[0] != p.name:
            p.name = self.settings.unique_name(d.result[0])
            self.settings.current = p.name
            self.settings.save()
            self.profile_changed()

    def delete_profile(self):
        p = self.settings.profile()
        if len(self.settings.profiles) == 1:
            messagebox.showinfo(APP_NAME, "This is the only profile, so it can't be deleted.")
            return
        if messagebox.askyesno(APP_NAME, f"Delete the profile “{p.name}”?"):
            self.settings.profiles.remove(p)
            self.settings.current = self.settings.profiles[0].name
            self.settings.save()
            self.profile_changed()

    def restore_profile(self):
        p = self.settings.profile()
        if p.preset not in PRESETS:
            messagebox.showinfo(APP_NAME, "This profile has no built-in defaults to restore.")
            return
        if messagebox.askyesno(APP_NAME, f"Put “{p.name}” back to its original controls "
                                         f"and tuning ({PRESETS[p.preset]})?"):
            fresh = make_preset(p.preset, p.name)
            p.steering, p.pedalling, p.brakes, p.bindings = (fresh.steering, fresh.pedalling,
                                                             fresh.brakes, fresh.bindings)
            self.settings.save()
            self.profile_changed()

    def close(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
            self.settings.save()
        self.engine.shutdown()
        self.root.destroy()


def main(port=None, tab=None):
    """Opens the window. `tab` names a tab to show first (e.g. "Tuning")."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "BikeControllerSingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, "Bike Controller is already running.", APP_NAME, 0x40)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
    settings = Settings()
    engine = Engine(settings, port=port)
    root = tk.Tk()
    App(root, settings, engine, tab)
    root.mainloop()
