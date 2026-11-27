"""
nyx_app.py - desktop interface for the Nyx voice assistant.

Runs the engine from assistant.py on a background thread and shows:
  * an animated orb that reacts to your voice and shows what Nyx is doing
  * a live mic level meter with the adaptive trigger level
  * the conversation, plus a box to type commands without the wake word

Shortcuts:  Ctrl+M  mic on/off     Esc  stop speaking / end task     Ctrl+L  clear chat
            F11     fullscreen on/off     Ctrl+H  minimize
"""
import math
import os
import sys

# Launched with pythonw (no console window): stdout/stderr are None, and any
# library that writes to them would crash. Send that output to a log instead.
if sys.stdout is None or sys.stderr is None:
    _log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "nyx_app.log"),
                "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import re
from tkinter import colorchooser, messagebox

import nyx_settings

# ---------------------------------------------------------------------------
# Palette (filled in from the chosen theme by apply_palette)
# ---------------------------------------------------------------------------
BG = PANEL = PANEL_EDGE = TEXT = MUTED = FAINT = ""
USER_BUBBLE = NYX_BUBBLE = USER_NAME = NYX_NAME = ""

STATE_STYLE = {
    #  state         colour      label            hint
    "loading":      ("#5b6680", "Starting up",    ""),
    "idle":         ("#38bdf8", "Standing by",    'Say "Nyx" to wake me'),
    "listening":    ("#22d3ee", "Listening",      "Go ahead, I'm listening"),
    "transcribing": ("#fbbf24", "Understanding",  "Turning speech into text"),
    "thinking":     ("#a78bfa", "Thinking",       'Say "end task" or press Esc to stop'),
    "speaking":     ("#34d399", "Speaking",       "Esc to interrupt"),
    "muted":        ("#64748b", "Mic muted",      "Ctrl+M or say \"Nyx, mic on\""),
    "stopped":      ("#5b6680", "Offline",        "Engine stopped"),
    "error":        ("#f87171", "Error",          ""),
}

# Orb colours in deep mode (filled in from settings by apply_palette).
DEEP_COLORS = dict(nyx_settings.DEEP_PRESETS["Red"])
DEEP_HINT = {"idle": 'Deep mode  ·  say "Nyx, normal mode" to exit'}

_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def apply_palette(settings):
    """Load the theme and orb colours from settings into the globals above."""
    global BG, PANEL, PANEL_EDGE, TEXT, MUTED, FAINT
    global USER_BUBBLE, NYX_BUBBLE, USER_NAME, NYX_NAME
    t = nyx_settings.THEMES.get(settings.get("theme"), nyx_settings.THEMES[nyx_settings.DEFAULTS["theme"]])
    BG, PANEL, PANEL_EDGE = t["bg"], t["panel"], t["panel_edge"]
    TEXT, MUTED, FAINT = t["text"], t["muted"], t["faint"]
    USER_BUBBLE, NYX_BUBBLE = t["user_bubble"], t["nyx_bubble"]
    USER_NAME, NYX_NAME = t["user_name"], t["nyx_name"]
    colours = settings.get("orb_colors", {})
    for state in nyx_settings.ORB_STATES:
        colour = colours.get(state, "")
        if not _HEX.fullmatch(str(colour)):
            colour = nyx_settings.DEFAULTS["orb_colors"][state]
        STATE_STYLE[state] = (colour,) + STATE_STYLE[state][1:]
    deep = settings.get("deep_colors", {})
    for state in nyx_settings.ORB_STATES:
        colour = deep.get(state, "")
        DEEP_COLORS[state] = colour if _HEX.fullmatch(str(colour)) else nyx_settings.DEEP_PRESETS["Red"][state]


def accent():
    return STATE_STYLE["idle"][0]


def _readable_on(colour):
    """Near-black or white, whichever reads better on this background."""
    r, g, b = _hex_to_rgb(colour)
    return "#0b0f19" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#ffffff"


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(c1, c2, t):
    """Blend two hex colours (t=0 -> c1, t=1 -> c2)."""
    a, b = _hex_to_rgb(c1), _hex_to_rgb(c2)
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _pick_font(root, *names):
    available = set(tkfont.families(root))
    for name in names:
        if name in available:
            return name
    return "TkDefaultFont"


# ---------------------------------------------------------------------------
# Animated orb
# ---------------------------------------------------------------------------
class Orb:
    def __init__(self, canvas, cx, cy, radius):
        self.c = canvas
        self.cx, self.cy, self.r = cx, cy, radius
        self.color = STATE_STYLE["loading"][0]
        self.target_color = self.color
        self.level = 0.0          # smoothed 0..1 mic level
        self.phase = 0.0
        self.state = "loading"

        # Soft glow: a stack of rings fading to the background.
        self.glow = [canvas.create_oval(0, 0, 0, 0, outline="", fill=BG) for _ in range(7)]
        self.ring_outer = canvas.create_oval(0, 0, 0, 0, outline=FAINT, width=1)
        self.arcs = [canvas.create_arc(0, 0, 0, 0, style="arc", width=3, outline=FAINT,
                                       start=0, extent=0) for _ in range(3)]
        self.ring_inner = canvas.create_oval(0, 0, 0, 0, outline=FAINT, width=2)
        self.core = canvas.create_oval(0, 0, 0, 0, outline="", fill=FAINT)
        self.bars = [canvas.create_line(0, 0, 0, 0, fill=FAINT, width=3, capstyle="round")
                     for _ in range(48)]

    def _oval(self, item, r):
        self.c.coords(item, self.cx - r, self.cy - r, self.cx + r, self.cy + r)

    def set_state(self, state, colour=None):
        self.state = state
        self.target_color = colour or STATE_STYLE.get(state, STATE_STYLE["idle"])[0]

    def set_level(self, value):
        self.level = 0.6 * self.level + 0.4 * max(0.0, min(1.0, value))

    def tick(self, dt):
        self.phase += dt
        self.color = _mix(self.color, self.target_color, min(1.0, dt * 6))
        col = self.color
        r = self.r
        st = self.state

        # How "alive" the orb is.
        if st == "speaking":
            energy = 0.45 + 0.35 * (0.5 + 0.5 * math.sin(self.phase * 9)) * (0.5 + 0.5 * math.sin(self.phase * 3.7))
        elif st in ("listening",):
            energy = 0.25 + self.level * 0.9
        elif st in ("thinking", "transcribing"):
            energy = 0.35 + 0.15 * math.sin(self.phase * 4)
        elif st in ("muted", "stopped", "loading"):
            energy = 0.08
        else:
            energy = 0.12 + self.level * 0.5 + 0.04 * math.sin(self.phase * 1.6)

        # Glow
        for i, item in enumerate(self.glow):
            t = i / (len(self.glow) - 1)
            rr = r * (1.55 - 0.5 * t) + energy * 18 * (1 - t)
            self._oval(item, rr)
            self.c.itemconfig(item, fill=_mix(BG, col, 0.015 + 0.10 * t * t * (0.4 + energy)))

        # Outer ring + rotating arcs
        self._oval(self.ring_outer, r * 1.12)
        self.c.itemconfig(self.ring_outer, outline=_mix(BG, col, 0.35))
        speed = {"thinking": 220, "transcribing": 160, "loading": 120, "listening": 70}.get(st, 25)
        for i, arc in enumerate(self.arcs):
            rr = r * (1.02 + i * 0.06)
            self._oval(arc, rr)
            direction = -1 if i % 2 else 1
            start = (self.phase * speed * direction * (1 + i * 0.3) + i * 120) % 360
            extent = 40 + 30 * math.sin(self.phase * 1.3 + i)
            self.c.itemconfig(arc, start=start, extent=extent,
                              outline=_mix(BG, col, 0.55 + 0.15 * i))

        # Audio bars around the core
        n = len(self.bars)
        base = r * 0.68
        for i, bar in enumerate(self.bars):
            ang = (i / n) * 2 * math.pi - math.pi / 2
            wobble = (0.5 + 0.5 * math.sin(self.phase * 7 + i * 0.9)) * (0.5 + 0.5 * math.sin(self.phase * 2.3 + i * 0.37))
            length = 4 + energy * 26 * (0.35 + 0.65 * wobble)
            x1 = self.cx + math.cos(ang) * base
            y1 = self.cy + math.sin(ang) * base
            x2 = self.cx + math.cos(ang) * (base + length)
            y2 = self.cy + math.sin(ang) * (base + length)
            self.c.coords(bar, x1, y1, x2, y2)
            self.c.itemconfig(bar, fill=_mix(BG, col, 0.35 + 0.6 * energy))

        # Core
        self._oval(self.ring_inner, r * 0.6)
        self.c.itemconfig(self.ring_inner, outline=_mix(BG, col, 0.8))
        core_r = r * (0.42 + 0.12 * energy)
        self._oval(self.core, core_r)
        self.c.itemconfig(self.core, fill=_mix(BG, col, 0.25 + 0.5 * energy))


# ---------------------------------------------------------------------------
# See-through popup box (shutdown countdown / aborted)
# Tk can't make part of a window transparent, so a box is two borderless
# windows: a faint tinted layer underneath, and a fully transparent layer on
# top that only draws the outline and text.
# ---------------------------------------------------------------------------
_KEY = "#010101"   # colour Windows treats as "see-through"


class GlassBox:
    TINT_ALPHA = 0.16

    def __init__(self, root, w, h, colour, tint):
        self.w, self.h = w, h
        self.colour = colour
        root.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 2
        geo = f"{w}x{h}+{x}+{y}"

        self.back = tk.Toplevel(root)
        self.back.overrideredirect(True)
        self.back.geometry(geo)
        self.back.configure(bg=tint)
        self.back.attributes("-alpha", self.TINT_ALPHA, "-topmost", True)

        self.front = tk.Toplevel(root)
        self.front.overrideredirect(True)
        self.front.geometry(geo)
        self.front.configure(bg=_KEY)
        self.front.attributes("-transparentcolor", _KEY, "-topmost", True)
        self.c = tk.Canvas(self.front, width=w, height=h, bg=_KEY, highlightthickness=0)
        self.c.pack()
        self.border = self.c.create_rectangle(2, 2, w - 2, h - 2, outline=colour, width=3)
        self.texts = {}
        self.outlines = [self.border]
        self.front.lift()

    def text(self, rel_y, text, font, tag=None):
        item = self.c.create_text(self.w / 2, self.h * rel_y, text=text, font=font, fill=self.colour)
        self.texts[tag or f"t{item}"] = item

    def button(self, rel_y, text, font, command):
        bw, bh = 200, 52
        x0, y0 = self.w / 2 - bw / 2, self.h * rel_y - bh / 2
        rect = self.c.create_rectangle(x0, y0, x0 + bw, y0 + bh, outline=self.colour,
                                       width=2, fill="#140606")
        self.text(rel_y, text, font, tag="button")
        self.outlines.append(rect)

        # The box is two stacked windows, and Windows may put the tint window on
        # top, so it gets the clicks. Hit-test by screen position on both windows
        # instead of relying on canvas item bindings.
        def over_button(e):
            x = e.x_root - self.c.winfo_rootx()
            y = e.y_root - self.c.winfo_rooty()
            return x0 <= x <= x0 + bw and y0 <= y <= y0 + bh

        for win in (self.front, self.back):
            win.bind("<Button-1>", lambda e: command() if over_button(e) else None)
            win.bind("<Motion>", lambda e, w=win: w.configure(
                cursor="hand2" if over_button(e) else ""))

    def set_text(self, tag, value):
        self.c.itemconfigure(self.texts[tag], text=value)

    def set_colour(self, colour, tint=None):
        for item in self.outlines:
            self.c.itemconfigure(item, outline=colour)
        for item in self.texts.values():
            self.c.itemconfigure(item, fill=colour)
        if tint:
            self.back.configure(bg=tint)

    def set_opacity(self, value):
        value = max(0.0, min(1.0, value))
        self.back.attributes("-alpha", self.TINT_ALPHA * value)
        self.front.attributes("-alpha", value)

    def destroy(self):
        for win in (self.front, self.back):
            try:
                win.destroy()
            except tk.TclError:
                pass


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------
class SettingsWindow:
    """Theme, orb colours, deep mode colours, voice volume, chat log.
    Every change is applied and saved immediately - there is no Save button."""

    def __init__(self, app):
        self.app = app
        self.s = json_copy(app.settings)
        self._volume_job = None
        self._scan_devices()

        win = self.win = tk.Toplevel(app.root)
        win.title("Nyx settings")
        win.resizable(False, False)
        win.transient(app.root)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda e: self.close())

        self._build()
        win.update_idletasks()
        root = app.root
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.focus_force()

    # -------------------------------------------------- layout
    def _build(self):
        """(Re)create the window contents with the current palette."""
        for child in self.win.winfo_children():
            child.destroy()
        f_ui, f_head = self.app.f_ui, self.app.f_head
        self.win.configure(bg=PANEL)

        head = tk.Frame(self.win, bg=PANEL)
        head.pack(fill="x", padx=28, pady=(22, 0))
        tk.Label(head, text="Settings", bg=PANEL, fg=TEXT, font=(f_head, 18)).pack(side="left")
        tk.Label(head, text="Changes apply right away", bg=PANEL, fg=MUTED,
                 font=(f_ui, 9)).pack(side="left", padx=(14, 0), pady=(8, 0))

        cols = tk.Frame(self.win, bg=PANEL)
        cols.pack(fill="both", expand=True, padx=28, pady=(4, 6))
        left = tk.Frame(cols, bg=PANEL)
        left.pack(side="left", fill="y", anchor="n")
        tk.Frame(cols, bg=PANEL_EDGE, width=1).pack(side="left", fill="y", padx=26, pady=(18, 0))
        right = tk.Frame(cols, bg=PANEL)
        right.pack(side="left", fill="y", anchor="n")

        # ---- left: appearance
        self._section(left, "APPEARANCE")
        self._label(left, "Theme")
        row = tk.Frame(left, bg=PANEL)
        row.pack(anchor="w", pady=(4, 14))
        for name, t in nyx_settings.THEMES.items():
            on = name == self.s["theme"]
            chip = tk.Label(row, text=name, bg=t["bg"], fg=t["text"], padx=12, pady=7,
                            font=(f_ui, 10, "bold" if on else "normal"), cursor="hand2",
                            highlightthickness=2,
                            highlightbackground=accent() if on else PANEL_EDGE)
            chip.pack(side="left", padx=(0, 6))
            chip.bind("<Button-1>", lambda e, n=name: self._set(theme=n))

        self._label(left, "Orb colours")
        self._colour_picker(left, "orb_preset", "orb_colors", nyx_settings.ORB_PRESETS)

        self._section(left, "DEEP MODE COLOURS")
        self._check(left, "Change the orb colours while deep mode is on",
                    "deep_colors_enabled")
        self._colour_picker(left, "deep_preset", "deep_colors", nyx_settings.DEEP_PRESETS,
                            dim=not self.s["deep_colors_enabled"])

        # ---- right: voice, window, chat
        self._section(right, "VOICE")
        self._label(right, "Nyx's voice volume")
        self.volume = tk.IntVar(value=int(self.s["speech_volume"]))
        vol_row = tk.Frame(right, bg=PANEL)
        vol_row.pack(anchor="w", pady=(6, 0))
        # bd=0 + no value label: the widget is only trough + handle, so bg colours the handle.
        tk.Scale(vol_row, from_=0, to=100, orient="horizontal", variable=self.volume, length=280,
                 width=12, sliderlength=22, showvalue=False, bg=accent(),
                 troughcolor=_mix(PANEL, TEXT, 0.12), highlightthickness=0, bd=0,
                 activebackground=_mix(accent(), TEXT, 0.25), sliderrelief="flat",
                 command=lambda v: self._volume_changed()).pack(side="left")
        tk.Label(vol_row, textvariable=self.volume, bg=PANEL, fg=TEXT, width=4,
                 font=(f_ui, 10)).pack(side="left", padx=(10, 0))
        tk.Label(vol_row, text="%", bg=PANEL, fg=MUTED, font=(f_ui, 10)).pack(side="left")

        self._section(right, "AUDIO DEVICES")
        self._label(right, "Speaker")
        self._device_menu(right, "speaker",
                          [("", "Windows default")] + [(n, n) for n in self.speakers])
        self._label(right, "Microphone")
        self._device_menu(right, "microphone",
                          [("", "Automatic (HyperX, else Windows default)"),
                           (nyx_settings.MIC_WINDOWS_DEFAULT, "Windows default")]
                          + [(n, n) for n in self.mics])
        hint_row = tk.Frame(right, bg=PANEL)
        hint_row.pack(anchor="w", fill="x")
        tk.Label(hint_row, text="If a device is unplugged, Nyx uses the Windows default.",
                 bg=PANEL, fg=FAINT, font=(f_ui, 8)).pack(side="left")
        refresh = tk.Label(hint_row, text="↻ Refresh list", bg=PANEL, fg=accent(),
                           font=(f_ui, 8), cursor="hand2")
        refresh.pack(side="left", padx=(10, 0))
        refresh.bind("<Button-1>", lambda e: (self._scan_devices(), self._build()))

        self._section(right, "MIC MUTED")
        self._label(right, "Orb colour while the mic is off")
        row = tk.Frame(right, bg=PANEL)
        row.pack(anchor="w", pady=(4, 0))
        normal = self.s["orb_colors"].get("idle", accent())
        deep = self.s["deep_colors"].get("idle", normal)
        for value, text, dots in (("grey", "Grey", [STATE_STYLE["muted"][0]]),
                                  ("mode", "Current mode", [normal, deep])):
            on = self.s.get("muted_color", "grey") == value
            chip = tk.Frame(row, bg=_mix(PANEL, accent(), 0.18) if on else PANEL, cursor="hand2",
                            highlightthickness=2,
                            highlightbackground=accent() if on else PANEL_EDGE)
            chip.pack(side="left", padx=(0, 6))
            parts = [tk.Label(chip, text="●", bg=chip["bg"], fg=d, font=(f_ui, 10))
                     for d in dots]
            parts.append(tk.Label(chip, text=text, bg=chip["bg"], fg=TEXT, font=(f_ui, 10)))
            for i, part in enumerate(parts):
                part.pack(side="left", padx=(10 if i == 0 else 2, 10 if i == len(parts) - 1 else 0),
                          pady=6)
            for w in [chip] + parts:
                w.bind("<Button-1>", lambda e, v=value: self._set(muted_color=v))
        tk.Label(right, text="Current mode = the Standing by colour, or the deep\n"
                             "mode colour while deep mode is on.",
                 bg=PANEL, fg=FAINT, font=(f_ui, 8), justify="left").pack(anchor="w", pady=(4, 0))

        self._section(right, "INTERNET")
        self._check(right, "Search the web for things Nyx doesn't know\n(uses DuckDuckGo)",
                    "web_search_enabled")

        self._section(right, "WINDOW")
        self._check(right, "Start in fullscreen (F11 switches any time)", "start_fullscreen")

        self._section(right, "CHAT HISTORY")
        self._check(right, "Save conversations to the chat log\n(previous_chats.txt)",
                    "chat_log_enabled")
        danger = tk.Label(right, text="🗑  Delete chat history…", bg=PANEL, fg="#f87171",
                          font=(f_ui, 10), padx=12, pady=6, cursor="hand2",
                          highlightthickness=1, highlightbackground="#7f1d1d")
        danger.pack(anchor="w", pady=(8, 0))
        danger.bind("<Button-1>", lambda e: self._delete_history())
        self.delete_note = tk.Label(right, text="", bg=PANEL, fg=MUTED, font=(f_ui, 9))
        self.delete_note.pack(anchor="w", pady=(4, 0))

        # ---- bottom bar
        bar = tk.Frame(self.win, bg=PANEL)
        bar.pack(fill="x", padx=28, pady=(10, 22))
        reset = tk.Label(bar, text="Reset to defaults", bg=PANEL, fg=MUTED, font=(f_ui, 9),
                         cursor="hand2")
        reset.pack(side="left")
        reset.bind("<Button-1>", lambda e: self._reset())
        self.app._button(bar, "Done", self.close, accent=True).pack(side="right")

    def _colour_picker(self, parent, preset_key, colours_key, presets, dim=False):
        """Preset chips + one clickable swatch per orb state."""
        f_ui = self.app.f_ui
        current = self.s[preset_key]
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w", pady=(4, 8))
        for name in list(presets) + ["Custom"]:
            on = name == current
            dot = presets.get(name, {}).get("idle", MUTED)
            chip = tk.Label(row, text=("●  " if name != "Custom" else "✎  ") + name,
                            bg=_mix(PANEL, accent(), 0.18) if on else PANEL,
                            fg=TEXT if on else (_mix(PANEL, dot, 0.5) if dim else dot),
                            font=(f_ui, 10), padx=10, pady=6, cursor="hand2",
                            highlightthickness=2,
                            highlightbackground=accent() if on else PANEL_EDGE)
            chip.pack(side="left", padx=(0, 6))
            chip.bind("<Button-1>", lambda e, n=name: self._pick_preset(preset_key, colours_key,
                                                                        presets, n))
        swatches = tk.Frame(parent, bg=PANEL)
        swatches.pack(anchor="w", pady=(2, 0))
        for state in nyx_settings.ORB_STATES:
            colour = self.s[colours_key].get(state, MUTED)
            cell = tk.Frame(swatches, bg=PANEL)
            cell.pack(side="left", padx=(0, 14))
            sw = tk.Label(cell, width=8, height=2, cursor="hand2", highlightthickness=1,
                          bg=_mix(PANEL, colour, 0.45) if dim else colour,
                          highlightbackground=PANEL_EDGE)
            sw.pack()
            sw.bind("<Button-1>", lambda e, st=state: self._pick_colour(preset_key, colours_key, st))
            tk.Label(cell, text=nyx_settings.ORB_STATE_NAMES[state], bg=PANEL, fg=MUTED,
                     font=(f_ui, 8)).pack(pady=(3, 0))
        tk.Label(parent, text="Click a colour to pick your own.", bg=PANEL, fg=FAINT,
                 font=(f_ui, 8)).pack(anchor="w", pady=(4, 0))

    def _section(self, parent, text):
        tk.Label(parent, text=text, bg=PANEL, fg=MUTED,
                 font=(self.app.f_ui, 8, "bold")).pack(anchor="w", pady=(18, 6))

    def _label(self, parent, text):
        tk.Label(parent, text=text, bg=PANEL, fg=TEXT, font=(self.app.f_ui, 10)).pack(anchor="w")

    def _scan_devices(self):
        """List speakers and mics. Re-scans for newly plugged-in devices
        unless Nyx is talking (re-scanning would cut the audio off)."""
        engine = self.app.engine
        refresh = engine is None or not getattr(engine, "SPEAKING_IN_PROGRESS", False)
        try:
            self.speakers = nyx_settings.list_audio_devices("output", refresh=refresh)
            self.mics = nyx_settings.list_audio_devices("input")
        except Exception as exc:
            print(f"[settings] Could not list audio devices: {exc}")
            self.speakers, self.mics = [], []

    def _device_menu(self, parent, key, options):
        """Dropdown of (value, label) pairs; the saved value is selected."""
        current = self.s.get(key, "")
        if current and current not in [v for v, _ in options]:
            options = options + [(current, f"{current}  (not connected)")]
        labels = {v: text for v, text in options}
        var = tk.StringVar(value=labels.get(current, options[0][1]))
        menu = tk.OptionMenu(parent, var, *[text for _, text in options],
                             command=lambda text: self._set(**{key: next(
                                 v for v, t in options if t == text)}))
        menu.configure(bg=_mix(PANEL, TEXT, 0.06), fg=TEXT, activebackground=_mix(PANEL, TEXT, 0.12),
                       activeforeground=TEXT, highlightthickness=1, highlightbackground=PANEL_EDGE,
                       relief="flat", bd=0, anchor="w", width=40, font=(self.app.f_ui, 10),
                       cursor="hand2", padx=10, pady=5)
        menu["menu"].configure(bg=PANEL, fg=TEXT, activebackground=accent(),
                               activeforeground=BG, font=(self.app.f_ui, 10), bd=0)
        menu.pack(anchor="w", pady=(4, 10))

    def _check(self, parent, text, key):
        var = tk.BooleanVar(value=bool(self.s[key]))
        tk.Checkbutton(parent, text=text, variable=var, bg=PANEL, fg=TEXT, selectcolor=BG,
                       activebackground=PANEL, activeforeground=TEXT, bd=0, highlightthickness=0,
                       justify="left", font=(self.app.f_ui, 10), cursor="hand2",
                       command=lambda: self._set(**{key: bool(var.get())})).pack(anchor="w", pady=2)

    # -------------------------------------------------- changes
    def _set(self, **changes):
        """Apply + save a change. Colour changes also redraw both windows."""
        self.s.update(changes)
        visual = any(k in changes for k in ("theme", "orb_colors", "deep_colors", "muted_color",
                                            "deep_colors_enabled", "orb_preset", "deep_preset"))
        self.app.apply_settings(json_copy(self.s), rebuild=visual)
        if visual:
            self._build()

    def _pick_preset(self, preset_key, colours_key, presets, name):
        changes = {preset_key: name}
        if name in presets:
            changes[colours_key] = dict(presets[name])
        self._set(**changes)

    def _pick_colour(self, preset_key, colours_key, state):
        current = self.s[colours_key].get(state, accent())
        _, chosen = colorchooser.askcolor(color=current, parent=self.win,
                                          title=f"Colour: {nyx_settings.ORB_STATE_NAMES[state]}")
        if chosen:
            colours = dict(self.s[colours_key])
            colours[state] = chosen.lower()
            self._set(**{colours_key: colours, preset_key: "Custom"})

    def _volume_changed(self):
        # Nyx's voice changes at once; saving waits until the slider stops moving.
        value = int(self.volume.get())
        self.s["speech_volume"] = value
        if self.app.engine is not None:
            self.app.engine.SPEECH_VOLUME = value
        if self._volume_job is not None:
            self.win.after_cancel(self._volume_job)
        self._volume_job = self.win.after(400, lambda: self._set(speech_volume=value))

    def _reset(self):
        self.s = json_copy(nyx_settings.DEFAULTS)
        self.app.apply_settings(json_copy(self.s), rebuild=True)
        self._build()

    def _delete_history(self):
        if messagebox.askyesno("Delete chat history",
                               "Delete the whole chat history (previous_chats.txt)?\n"
                               "This can't be undone.", icon="warning", parent=self.win):
            self.delete_note.configure(text=self.app.delete_chat_history())

    def sync_engine_values(self, fresh):
        """Voice commands changed these while the window was open."""
        self.s["speech_volume"] = int(fresh["speech_volume"])
        self.s["chat_log_enabled"] = bool(fresh["chat_log_enabled"])
        self._build()

    def close(self):
        if self._volume_job is not None:          # save a pending volume change
            self.win.after_cancel(self._volume_job)
            self._volume_job = None
            self.app.apply_settings(json_copy(self.s), rebuild=False)
        self.app.settings_window = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        # Windows may hand focus to another program when this window closes.
        root = self.app.root
        root.lift()
        root.focus_force()


def json_copy(value):
    import json
    return json.loads(json.dumps(value))


def accent_of(settings):
    colour = settings.get("orb_colors", {}).get("idle", "")
    return colour if _HEX.fullmatch(str(colour)) else accent()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class NyxApp:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self.stop_event = threading.Event()
        self.engine = None
        self.engine_thread = None
        self.state = "loading"
        self.mic_enabled = True
        self.deep_mode = False
        self.last_frame = time.time()
        self.level_rms = 0.0
        self.threshold_rms = 1.0
        self.settings = nyx_settings.load()
        apply_palette(self.settings)
        self.history = []            # chat contents, so the UI can be rebuilt on a theme change
        self.engine_text = "starting…"
        self.mic_name_text = ""
        self.settings_window = None

        self.f_ui = _pick_font(root, "Segoe UI Variable Text", "Segoe UI", "Helvetica")
        self.f_head = _pick_font(root, "Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI", "Helvetica")
        self.f_mono = _pick_font(root, "Cascadia Mono", "Consolas", "Courier New")

        root.title("Nyx")
        root.configure(bg=BG)
        root.geometry("1060x680")
        root.minsize(900, 600)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.attributes("-fullscreen", bool(self.settings["start_fullscreen"]))   # F11 toggles

        self._build()
        self._system("Nyx is starting up…")
        self._bind_keys()
        self._start_engine()
        self.root.after(16, self._frame)
        self.root.after(50, self._drain_events)

    # ------------------------------------------------------------- layout
    def _build(self):
        root = self.root
        root.grid_columnconfigure(0, weight=2, minsize=420)
        root.grid_columnconfigure(1, weight=3)
        root.grid_rowconfigure(0, weight=1)

        # ---- left: orb + status
        left = tk.Frame(root, bg=BG)
        left.grid(row=0, column=0, sticky="nsew")

        brand = tk.Frame(left, bg=BG)
        brand.pack(fill="x", padx=28, pady=(24, 0))
        tk.Label(brand, text="N Y X", bg=BG, fg=TEXT, font=(self.f_head, 18, "bold")).pack(side="left")
        self.engine_label = tk.Label(brand, text=self.engine_text, bg=BG, fg=MUTED, font=(self.f_ui, 9))
        self.engine_label.pack(side="left", padx=(12, 0), pady=(6, 0))
        self.deep_badge = tk.Label(brand, text=" DEEP MODE ", bg=DEEP_COLORS["idle"],
                                   fg=_readable_on(DEEP_COLORS["idle"]),
                                   font=(self.f_ui, 8, "bold"))  # shown only in deep mode

        # Everything below the brand row stays vertically centred (matters in fullscreen).
        center = tk.Frame(left, bg=BG)
        center.pack(expand=True)
        left = center

        # Orb scales with the screen height (fullscreen on big monitors).
        size = max(380, min(640, int(root.winfo_screenheight() * 0.55)))
        self.canvas = tk.Canvas(left, width=size + 40, height=size, bg=BG, highlightthickness=0)
        self.canvas.pack(pady=(6, 0))
        self.orb = Orb(self.canvas, (size + 40) / 2, size / 2, size * 0.315)

        self.state_label = tk.Label(left, text="Starting up", bg=BG, fg=TEXT, font=(self.f_head, 22))
        self.state_label.pack()
        self.hint_label = tk.Label(left, text="Loading models…", bg=BG, fg=MUTED, font=(self.f_ui, 10))
        self.hint_label.pack(pady=(2, 18))

        # level meter
        meter_box = tk.Frame(left, bg=BG)
        meter_box.pack(fill="x", padx=20)
        top = tk.Frame(meter_box, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text="MIC", bg=BG, fg=MUTED, font=(self.f_ui, 8, "bold")).pack(side="left")
        self.mic_name = tk.Label(top, text=self.mic_name_text, bg=BG, fg=FAINT, font=(self.f_ui, 8))
        self.mic_name.pack(side="right")
        self.meter = tk.Canvas(meter_box, height=8, bg=PANEL, highlightthickness=0)
        self.meter.pack(fill="x", pady=(4, 0))
        self.meter_fill = self.meter.create_rectangle(0, 0, 0, 8, fill=STATE_STYLE["idle"][0], width=0)
        self.meter_mark = self.meter.create_line(0, 0, 0, 8, fill=TEXT, width=2)

        # controls
        controls = tk.Frame(left, bg=BG)
        controls.pack(pady=22)
        self.mic_btn = self._button(controls, "●  Mic on", self.toggle_mic)
        self.mic_btn.pack(side="left", padx=5)
        self._button(controls, "■  Stop talking", self.skip_speech).pack(side="left", padx=5)
        self._button(controls, "▁  Minimize", self.minimize).pack(side="left", padx=5)
        self._button(controls, "⚙  Settings", self.open_settings).pack(side="left", padx=5)

        # ---- right: conversation panel
        right = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=PANEL_EDGE)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 18), pady=18)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        head = tk.Frame(right, bg=PANEL)
        head.grid(row=0, column=0, sticky="ew", padx=22, pady=(16, 6))
        tk.Label(head, text="Conversation", bg=PANEL, fg=TEXT, font=(self.f_head, 13)).pack(side="left")
        clear = tk.Label(head, text="Clear", bg=PANEL, fg=MUTED, font=(self.f_ui, 9), cursor="hand2")
        clear.pack(side="right")
        clear.bind("<Button-1>", lambda e: self.clear_chat())

        body = tk.Frame(right, bg=PANEL)
        body.grid(row=1, column=0, sticky="nsew", padx=(16, 6))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.chat = tk.Text(body, bg=PANEL, fg=TEXT, bd=0, highlightthickness=0, wrap="word",
                            font=(self.f_ui, 11), padx=6, pady=6, cursor="arrow",
                            insertbackground=PANEL, selectbackground=_mix(PANEL, accent(), 0.35))
        self.chat.grid(row=0, column=0, sticky="nsew")
        scroll = tk.Scrollbar(body, command=self.chat.yview, width=10, bd=0,
                              troughcolor=PANEL, bg=PANEL_EDGE, activebackground=FAINT)
        scroll.grid(row=0, column=1, sticky="ns")
        self.chat.configure(yscrollcommand=scroll.set, state="disabled")
        self.chat.bind("<Configure>", self._resize_bubbles)

        # Bubbles are embedded widgets; these tags only align them.
        self.chat.tag_configure("right", justify="right", rmargin=8, spacing1=10)
        self.chat.tag_configure("left", justify="left", lmargin1=8, spacing1=10)
        self.bubbles = []          # message labels, re-wrapped on resize
        self.chat_width = 600
        self.chat.tag_configure("system", foreground=FAINT, font=(self.f_ui, 9, "italic"),
                                justify="center", spacing1=10, spacing3=4)
        self.chat.tag_configure("heard", foreground=MUTED, font=(self.f_ui, 9),
                                justify="center", spacing1=10)

        # input row
        bar = tk.Frame(right, bg=PANEL)
        bar.grid(row=2, column=0, sticky="ew", padx=16, pady=16)
        bar.grid_columnconfigure(0, weight=1)
        box = tk.Frame(bar, bg=BG, highlightthickness=1, highlightbackground=PANEL_EDGE,
                       highlightcolor=STATE_STYLE["idle"][0])
        box.grid(row=0, column=0, sticky="ew")
        self.entry = tk.Entry(box, bg=BG, fg=TEXT, bd=0, insertbackground=TEXT,
                              font=(self.f_ui, 11), highlightthickness=0)
        self.entry.pack(fill="x", padx=12, pady=10)
        self.entry.bind("<Return>", lambda e: self.send_text())
        self._placeholder = "Type a command — no wake word needed"
        self._show_placeholder()
        self.entry.bind("<FocusIn>", lambda e: self._hide_placeholder())
        self.entry.bind("<FocusOut>", lambda e: self._show_placeholder())
        send = self._button(bar, "Send  ➜", self.send_text, accent=True)
        send.grid(row=0, column=1, padx=(10, 0), sticky="ns")

    def rebuild_ui(self):
        """Redraw the whole window with the current palette, keeping the chat."""
        for child in self.root.winfo_children():
            if not isinstance(child, tk.Toplevel):
                child.destroy()
        self.root.configure(bg=BG)
        self.bubbles = []
        self._build()
        history, self.history = self.history, []
        for entry in history:
            if entry[0] == "system":
                self._system(entry[1])
            elif entry[0] == "user":
                self.add_user(entry[1])
            else:
                self.add_nyx(entry[1], entry[2])
        if self.deep_mode:
            self.deep_badge.pack(side="right", pady=(6, 0))
        self._refresh_mic_button()
        self.set_state(self.state)
        self.orb.color = self.orb.target_color   # no fade-in from grey

    def _button(self, parent, text, command, accent=False):
        bg = STATE_STYLE["idle"][0] if accent else PANEL
        fg = _readable_on(bg) if accent else TEXT
        hover = _mix(bg, TEXT, 0.12)
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=(self.f_ui, 10, "bold" if accent else "normal"),
                     padx=16, pady=8, cursor="hand2", highlightthickness=0 if accent else 1,
                     highlightbackground=PANEL_EDGE)
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.configure(bg=hover))
        b.bind("<Leave>", lambda e: b.configure(bg=b._base_bg))
        b._base_bg = bg
        return b

    def _wrap_width(self, kind=None):
        share = 0.9 if kind == "log" else 0.62
        return max(240, min(820, int(self.chat_width * share)))

    def _resize_bubbles(self, event):
        """Re-wrap bubbles so they stay readable on any screen width."""
        self.chat_width = event.width
        for label, kind in self.bubbles:
            try:
                label.configure(wraplength=self._wrap_width(kind))
            except tk.TclError:
                pass

    def _show_placeholder(self):
        if not self.entry.get():
            self.entry.insert(0, self._placeholder)
            self.entry.configure(fg=FAINT)

    def _hide_placeholder(self):
        if self.entry.get() == self._placeholder:
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT)

    def _bind_keys(self):
        self.root.bind("<Control-m>", lambda e: self.toggle_mic())
        self.root.bind("<Escape>", lambda e: self.skip_speech())
        self.root.bind("<Control-l>", lambda e: self.clear_chat())
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.root.bind("<Control-h>", lambda e: self.minimize())
        self.root.bind("<Control-comma>", lambda e: self.open_settings())
        self.root.bind("<Map>", self._on_map)

    def toggle_fullscreen(self):
        full = not bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", full)

    def minimize(self):
        # Windows can't iconify a fullscreen Tk window, so leave fullscreen first.
        self._restore_fullscreen = bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", False)
        self.root.after(30, self.root.iconify)

    def restore(self):
        self.root.deiconify()
        self._on_map()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _on_map(self, event=None):
        # Coming back from the taskbar (or voice): go fullscreen again if it was.
        if event is not None and event.widget is not self.root:
            return
        if getattr(self, "_restore_fullscreen", False):
            self._restore_fullscreen = False
            self.root.attributes("-fullscreen", True)

    # ------------------------------------------------------------- shutdown countdown
    def _box_size(self):
        sh = self.root.winfo_screenheight()
        return int(sh * 0.62), int(sh * 0.40)

    def show_countdown(self, deadline, action="shutdown"):
        self.shutdown_deadline = deadline
        self.power_action = action
        if self.root.state() == "iconic":
            self.restore()
        if getattr(self, "countdown_box", None) is None:
            w, h = self._box_size()
            big = max(70, int(self.root.winfo_screenheight() * 0.11))
            box = GlassBox(self.root, w, h, "#ef4444", tint="#dc2626")
            box.text(0.17, f"SYSTEM {action.upper()}", (self.f_head, 28, "bold"), tag="title")
            box.text(0.47, "20", (self.f_head, big, "bold"), tag="number")
            box.text(0.73, 'Say "Nyx, abort"  ·  press Space  ·  or click ABORT',
                     (self.f_ui, 13), tag="sub")
            box.button(0.87, "ABORT", (self.f_head, 16, "bold"), self.abort_shutdown)
            self.countdown_box = box
            self.root.bind("<space>", lambda e: self.abort_shutdown())
            for win in (box.front, box.back):   # in case clicking the box focused it
                win.bind("<space>", lambda e: self.abort_shutdown())
            self._start_space_hotkey()
            self._tick_countdown()

    def _tick_countdown(self):
        box = getattr(self, "countdown_box", None)
        if box is None:
            return
        left = self.shutdown_deadline - time.time()
        box.set_text("number", str(max(0, math.ceil(left))))
        if left <= 0:
            box.set_text("sub", "Restarting…" if getattr(self, "power_action", "") == "restart"
                         else "Shutting down…")
        # Flash red: faster in the last 5 seconds.
        period = 0.5 if left > 5 else 0.25
        on = int(time.time() / period) % 2 == 0
        box.set_colour("#ef4444" if on else "#7f1d1d", tint="#dc2626" if on else "#450a0a")
        self.root.after(50, self._tick_countdown)

    def hide_countdown(self):
        box = getattr(self, "countdown_box", None)
        self.countdown_box = None
        if box is not None:
            box.destroy()
        self.root.unbind("<space>")
        self._stop_space_hotkey()

    def _start_space_hotkey(self):
        """During a countdown, Space aborts even when another program is in front.
        (A system-wide hotkey, released as soon as the countdown ends.)"""
        if getattr(self, "_hotkey_stop", None) is not None:
            return
        stop = self._hotkey_stop = threading.Event()

        def worker():
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            HOTKEY_ID, VK_SPACE, WM_HOTKEY, PM_REMOVE = 0x4E59, 0x20, 0x0312, 1
            if not user32.RegisterHotKey(None, HOTKEY_ID, 0, VK_SPACE):
                print("[countdown] Space is taken by another program; it only works "
                      "while Nyx is the active window.")
                return
            try:
                msg = wintypes.MSG()
                while not stop.is_set():
                    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                        if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                            self.events.put_nowait({"type": "abort_hotkey"})
                    stop.wait(0.03)
            finally:
                user32.UnregisterHotKey(None, HOTKEY_ID)

        threading.Thread(target=worker, daemon=True).start()

    def _stop_space_hotkey(self):
        stop = getattr(self, "_hotkey_stop", None)
        self._hotkey_stop = None
        if stop is not None:
            stop.set()

    def abort_shutdown(self):
        # The engine emits a "shutdown" event, which hides the countdown
        # and shows the blue "aborted" box.
        if self.engine is not None:
            msg = self.engine.abort_shutdown()
            threading.Thread(target=self.engine.speak, args=(msg,), daemon=True).start()
        else:
            self.hide_countdown()

    def show_aborted(self, action="shutdown"):
        """Blue 'SHUTDOWN ABORTED' / 'RESTART ABORTED' box for a few seconds, then fade out."""
        old = getattr(self, "aborted_box", None)
        if old is not None:
            old.destroy()
        w, h = self._box_size()
        box = GlassBox(self.root, w, int(h * 0.5), "#60a5fa", tint="#2563eb")
        box.text(0.42, f"{action.upper()} ABORTED", (self.f_head, 34, "bold"))
        box.text(0.70, "Your PC won't restart." if action == "restart" else "Your PC will stay on.",
                 (self.f_ui, 14))
        self.aborted_box = box
        self.root.after(3500, lambda: self._fade_aborted(box, 0))

    def _fade_aborted(self, box, step):
        if box is not self.aborted_box:
            return
        if step >= 12:
            box.destroy()
            self.aborted_box = None
            return
        box.set_opacity(1 - step / 12)
        self.root.after(40, lambda: self._fade_aborted(box, step + 1))

    # ------------------------------------------------------------- chat
    def _append(self, *parts):
        self.chat.configure(state="normal")
        for text, tag in parts:
            self.chat.insert("end", text, tag)
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _system(self, text):
        self.history.append(("system", text))
        self._append((text + "\n", "system"))

    def _bubble(self, who, text, side, bg, who_fg, kind=None):
        row = tk.Frame(self.chat, bg=PANEL)
        anchor = "e" if side == "right" else "w"
        tk.Label(row, text=who, bg=PANEL, fg=who_fg,
                 font=(self.f_ui, 8, "bold")).pack(anchor=anchor, padx=4)
        font = (self.f_mono, 9) if kind == "log" else (self.f_ui, 11)
        msg = tk.Label(row, text=text.strip(), bg=bg, fg=TEXT, font=font, justify="left",
                       wraplength=self._wrap_width(kind), padx=14, pady=9)
        msg.pack(anchor=anchor, pady=(2, 0))
        for w in (row, msg):   # let the mouse wheel scroll the chat over bubbles
            w.bind("<MouseWheel>", lambda e: self.chat.yview_scroll(int(-e.delta / 120), "units"))
        self.bubbles.append((msg, kind))

        self.chat.configure(state="normal")
        start = self.chat.index("end-1c")
        self.chat.window_create("end", window=row)
        self.chat.insert("end", "\n")
        self.chat.tag_add(side, start, "end-1c")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def add_user(self, text):
        self.history.append(("user", text))
        self._bubble("YOU", text, "right", USER_BUBBLE, USER_NAME)

    def add_nyx(self, text, kind=None):
        self.history.append(("nyx", text, kind))
        self._bubble("NYX", text, "left", NYX_BUBBLE, NYX_NAME, kind)

    def clear_chat(self):
        self.history.clear()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        for label, _ in self.bubbles:
            try:
                label.master.destroy()
            except tk.TclError:
                pass
        self.bubbles.clear()

    # ------------------------------------------------------------- engine
    def _start_engine(self):
        def worker():
            try:
                import assistant as engine
                self.engine = engine
                engine.EVENT_SINK = self.events.put_nowait
                engine.init_engine()
                self.events.put_nowait({"type": "ready",
                                        "model": engine.STT_MODEL_NAME})
                engine.run_loop(stop_event=self.stop_event, text_queue=self.commands)
            except Exception as exc:
                self.events.put_nowait({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
            finally:
                self.events.put_nowait({"type": "finished"})

        self.engine_thread = threading.Thread(target=worker, daemon=True)
        self.engine_thread.start()

    def _drain_events(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(40, self._drain_events)

    def _handle(self, ev):
        kind = ev.get("type")
        if kind == "state":
            value = ev.get("value", "idle")
            if value == "loading":
                self.hint_label.configure(text=ev.get("detail", "") + "…")
            self.set_state(value)
            if value != "loading" and ev.get("detail"):   # e.g. "Searching the web"
                self.hint_label.configure(text=ev["detail"] + "…")
        elif kind == "level":
            self.level_rms = ev.get("rms", 0.0)
            self.threshold_rms = max(1.0, ev.get("threshold", 1.0))
        elif kind == "user":
            self.add_user(ev.get("text", ""))
        elif kind == "heard":
            pass  # the cleaned command is shown via "user"
        elif kind == "web":
            sites = ", ".join(ev.get("sources", []))
            msg = f"Searched the web for “{ev.get('query', '')}”" + (f"  ·  {sites}" if sites else "")
            links = ev.get("links") or []
            if links:
                msg += "\nProfiles found:\n" + "\n".join("  " + link for link in links)
            self._system(msg)
        elif kind == "reply":
            self.add_nyx(ev.get("text", ""), ev.get("kind"))
        elif kind == "mic":
            self.mic_enabled = ev.get("enabled", True)
            self._refresh_mic_button()
            self.set_state(self.state)
        elif kind == "abort_hotkey":
            if getattr(self, "countdown_box", None) is not None:
                self.abort_shutdown()
        elif kind == "shutdown":
            if ev.get("active"):
                self.show_countdown(ev.get("deadline", time.time() + 20),
                                    ev.get("action", "shutdown"))
            else:
                self.hide_countdown()
                if ev.get("aborted"):
                    self.show_aborted(ev.get("action", "shutdown"))
        elif kind == "window":
            if ev.get("action") == "minimize":
                self.root.after(600, self.minimize)   # let the reply show first
            else:
                self.restore()
        elif kind == "mode":
            self.deep_mode = bool(ev.get("deep"))
            if self.deep_mode:
                self.deep_badge.pack(side="right", pady=(6, 0))
            else:
                self.deep_badge.pack_forget()
            self.set_state(self.state)
        elif kind == "mic_name":
            self.mic_name_text = str(ev.get("value", ""))[:42]
            self.mic_name.configure(text=self.mic_name_text)
        elif kind == "chat_deleted":
            self.clear_chat()
        elif kind == "settings_changed":
            # Changed by voice (speech volume / chat log on-off).
            fresh = nyx_settings.load()
            for key in ("speech_volume", "chat_log_enabled"):
                self.settings[key] = fresh[key]
            if self.settings_window is not None:
                self.settings_window.sync_engine_values(fresh)
        elif kind == "ready":
            self._system("Online. Say \"Nyx\" followed by a command, or type below.")
            model = os.path.basename(str(ev.get("model") or ""))
            self.engine_text = f"whisper {model}" if model else "online"
            self.engine_label.configure(text=self.engine_text)
        elif kind == "error":
            self._system("Error: " + ev.get("text", ""))
            self.set_state("error")
            self.hint_label.configure(text=ev.get("text", "")[:60])
        elif kind == "finished":
            if self.state != "error":
                self.set_state("stopped")
            if not self.stop_event.is_set() and self.state != "error":
                if getattr(self.engine, "RESTART_REQUESTED", False):
                    self.root.after(300, self.restart)      # "Nyx, restart"
                else:
                    self.root.after(1200, self.on_close)    # the user said goodbye

    def set_state(self, value):
        self.state = value
        if value not in ("idle", "listening"):
            self.level_rms = 0.0   # no level events arrive in these states
        shown ="muted" if (not self.mic_enabled and value == "idle") else value
        colour, label, hint = STATE_STYLE.get(shown, STATE_STYLE["idle"])
        if self._deep_colours_on() and shown in DEEP_COLORS:
            colour = DEEP_COLORS[shown]
        if shown == "muted":
            colour = self.muted_colour()
        self._refresh_mic_button()
        if self.deep_mode:
            hint = DEEP_HINT.get(shown, hint)
        self.orb.set_state(shown, colour)
        self.state_label.configure(text=label)
        if hint:
            self.hint_label.configure(text=hint)
        self.meter.itemconfig(self.meter_fill, fill=colour)

    def _deep_colours_on(self):
        return self.deep_mode and self.settings.get("deep_colors_enabled", True)

    def muted_colour(self):
        """Orb colour while the mic is off: grey, or the current mode's colour."""
        if self.settings.get("muted_color") == "mode":
            return DEEP_COLORS["idle"] if self._deep_colours_on() else STATE_STYLE["idle"][0]
        return STATE_STYLE["muted"][0]

    def _refresh_mic_button(self):
        self.mic_btn.configure(text="●  Mic on" if self.mic_enabled else "○  Mic off",
                               fg=TEXT if self.mic_enabled else self.muted_colour())

    # ------------------------------------------------------------- actions
    def send_text(self):
        text = self.entry.get().strip()
        if not text or text == self._placeholder:
            return
        self.entry.delete(0, "end")
        if self.engine is None or not self.engine.ENGINE_READY:
            self._system("Still starting up — try again in a moment.")
            return
        # The engine is busy while thinking, so "end task" can't wait in the queue.
        if self.engine.is_end_task(text) and self.engine.cancel_thinking():
            return
        self.commands.put(text)

    def open_settings(self):
        if self.settings_window is not None:
            self.settings_window.win.lift()
            self.settings_window.win.focus_force()
            return
        self.settings_window = SettingsWindow(self)

    def apply_settings(self, settings, rebuild=True):
        """Save new settings and apply them right away.
        rebuild: redraw the window (needed for colour changes)."""
        try:
            nyx_settings.save(settings)
        except OSError as exc:
            self._system(f"Could not save settings: {exc}")
        self.settings = settings
        if self.engine is not None:
            self.engine.SPEECH_VOLUME = int(settings["speech_volume"])
            self.engine.CHAT_LOG_ENABLED = bool(settings["chat_log_enabled"])
            self.engine.WEB_SEARCH_ENABLED = bool(settings.get("web_search_enabled", True))
            self.engine.set_speaker(settings.get("speaker", ""))
            mic = settings.get("microphone", "")
            if mic != self.engine.MIC_NAME_SETTING:
                self.engine.set_microphone(mic)
        if rebuild:
            apply_palette(settings)
            self.rebuild_ui()

    def delete_chat_history(self):
        """Delete previous_chats.txt (Settings button). Returns a message."""
        if self.engine is not None:
            msg = self.engine.delete_chat_log(skip_next_log=False)
        else:
            try:
                (nyx_settings.SETTINGS_FILE.parent / "previous_chats.txt").unlink(missing_ok=True)
                msg = "Chat history deleted."
            except OSError as exc:
                msg = f"Could not delete the chat history: {exc}"
            self.clear_chat()
        self._system(msg)
        return msg

    def toggle_mic(self):
        if self.engine is None:
            return
        self.engine.set_listening(not self.engine.LISTENING_ENABLED)

    def skip_speech(self):
        """Esc: stop talking, or end the task if Nyx is still thinking."""
        if self.engine is not None:
            if not self.engine.cancel_thinking():
                self.engine.stop_speaking()

    def restart(self):
        """Close everything, then start a fresh copy of the app (no console window
        if this one had none, since it reuses the same python/pythonw)."""
        import subprocess
        cmd = self.engine.relaunch_command() if self.engine is not None else \
            [sys.executable, os.path.abspath(__file__)]
        self.on_close(after_shutdown=lambda: subprocess.Popen(
            cmd, cwd=os.path.dirname(os.path.abspath(__file__))))

    def on_close(self, after_shutdown=None):
        self.stop_event.set()
        if self.engine is not None:
            try:
                self.engine.stop_speaking()
            except Exception:
                pass
        if self.engine_thread is not None:
            self.engine_thread.join(timeout=1.5)
        if self.engine is not None:
            self.engine.shutdown_engine()
        self.root.destroy()
        if after_shutdown is not None:
            after_shutdown()
        os._exit(0)  # make sure model/audio threads can't keep the process alive

    # ------------------------------------------------------------- animation
    def _frame(self):
        now = time.time()
        dt = min(0.1, now - self.last_frame)
        self.last_frame = now

        ratio = self.level_rms / self.threshold_rms   # 1.0 == trigger level
        self.orb.set_level(min(1.0, ratio / 2.0))
        self.orb.tick(dt)

        w = max(1, self.meter.winfo_width())
        fill = min(1.0, ratio / 2.0) * w
        self.meter.coords(self.meter_fill, 0, 0, fill, 8)
        self.meter.coords(self.meter_mark, w / 2, 0, w / 2, 8)   # trigger level sits mid-bar

        self.root.after(16, self._frame)


APP_ID = "Nyx.Assistant"    # taskbar identity; NYX.exe stamps the same ID on its shortcuts
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _keep_taskbar_identity(root, icon):
    """Tk replaces its top-level window when fullscreen is switched on or off,
    which drops the taskbar properties - so re-apply them whenever that happens."""
    import ctypes
    last = [None]

    def check(event=None):
        if event is not None and event.widget is not root:
            return
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if hwnd and hwnd != last[0]:
            last[0] = hwnd
            try:
                _set_taskbar_identity(hwnd, icon)
            except Exception as e:
                print("Could not set taskbar identity:", e)

    root.update_idletasks()
    check()
    root.bind("<Configure>", check, add="+")
    root.bind("<Map>", check, add="+")


def _set_taskbar_identity(hwnd, icon):
    """Tell the taskbar this window is NYX: group it under APP_ID, and if it gets
    pinned, relaunch it with NYX.exe and show the NYX icon (not pythonw's)."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                    ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("val", ctypes.c_void_p), ("pad", ctypes.c_void_p)]

    # {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}: System.AppUserModel.*
    fmtid = GUID(0x9F4C2855, 0x9F79, 0x4B39, (ctypes.c_ubyte * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3))
    iid = GUID(0x886D8EEB, 0x8CF2, 0x4446, (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99))
    exe = os.path.join(APP_DIR, "NYX.exe")
    props = [(5, APP_ID)]                                   # ID
    if os.path.exists(exe):
        props += [(2, f'"{exe}"'),                          # RelaunchCommand
                  (4, "NYX")]                               # RelaunchDisplayNameResource
        if os.path.exists(icon):
            props.append((3, f"{icon},0"))                  # RelaunchIconResource
        # RelaunchCommand/RelaunchDisplayNameResource must be set before ID
        props = props[1:] + props[:1]

    store = ctypes.c_void_p()
    hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(
        wintypes.HWND(hwnd), ctypes.byref(iid), ctypes.byref(store))
    if hr != 0:
        raise OSError(f"SHGetPropertyStoreForWindow failed: {hr & 0xFFFFFFFF:#x}")
    vtbl = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0],
                       ctypes.POINTER(ctypes.c_void_p))
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                               ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    set_value = proto(vtbl[6])                              # IPropertyStore::SetValue
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    try:
        for pid, text in props:
            buf = ctypes.create_unicode_buffer(text)
            pv = PROPVARIANT(vt=31, val=ctypes.cast(buf, ctypes.c_void_p))   # VT_LPWSTR
            hr = set_value(store, ctypes.byref(PROPERTYKEY(fmtid, pid)), ctypes.byref(pv))
            if hr != 0:
                raise OSError(f"setting taskbar property {pid} failed: {hr & 0xFFFFFFFF:#x}")
    finally:
        release(store)


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)   # crisp text on high-DPI screens
    except Exception:
        pass
    try:
        from ctypes import windll
        # Own taskbar identity, so Windows shows the NYX icon rather than Python's.
        windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass
    root = tk.Tk()
    icon = os.path.join(APP_DIR, "assets", "nyx.ico")
    if os.path.exists(icon):
        try:
            root.iconbitmap(default=icon)    # default= also covers the settings window
        except tk.TclError:
            pass
    _keep_taskbar_identity(root, icon)
    NyxApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
