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

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
BG = "#07090f"
PANEL = "#0d111b"
PANEL_EDGE = "#1a2133"
TEXT = "#e6ebf5"
MUTED = "#7b869c"
FAINT = "#3a4458"
USER_BUBBLE = "#16243d"
NYX_BUBBLE = "#121a2a"

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

# Deep mode turns Nyx red.
DEEP_RED = "#ef4444"
DEEP_COLORS = {
    "idle":         "#ef4444",
    "listening":    "#f87171",
    "transcribing": "#fb7185",
    "thinking":     "#dc2626",
    "speaking":     "#f43f5e",
}
DEEP_HINT = {"idle": 'Deep mode  ·  say "Nyx, normal mode" to exit'}


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
        for item in (rect, self.texts["button"]):
            self.c.tag_bind(item, "<Button-1>", lambda e: command())
            self.c.tag_bind(item, "<Enter>", lambda e: self.c.configure(cursor="hand2"))
            self.c.tag_bind(item, "<Leave>", lambda e: self.c.configure(cursor=""))

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

        self.f_ui = _pick_font(root, "Segoe UI Variable Text", "Segoe UI", "Helvetica")
        self.f_head = _pick_font(root, "Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI", "Helvetica")
        self.f_mono = _pick_font(root, "Cascadia Mono", "Consolas", "Courier New")

        root.title("Nyx")
        root.configure(bg=BG)
        root.geometry("1060x680")
        root.minsize(900, 600)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.attributes("-fullscreen", True)   # F11 toggles

        self._build()
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
        self.engine_label = tk.Label(brand, text="starting…", bg=BG, fg=MUTED, font=(self.f_ui, 9))
        self.engine_label.pack(side="left", padx=(12, 0), pady=(6, 0))
        self.deep_badge = tk.Label(brand, text=" DEEP MODE ", bg=DEEP_RED, fg="#ffffff",
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
        self.mic_name = tk.Label(top, text="", bg=BG, fg=FAINT, font=(self.f_ui, 8))
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
                            insertbackground=PANEL, selectbackground="#2b3a5c")
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

        self._system("Nyx is starting up…")

    def _button(self, parent, text, command, accent=False):
        bg = STATE_STYLE["idle"][0] if accent else PANEL
        fg = BG if accent else TEXT
        hover = _mix(bg, "#ffffff", 0.15)
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

    def show_countdown(self, deadline):
        self.shutdown_deadline = deadline
        if self.root.state() == "iconic":
            self.restore()
        if getattr(self, "countdown_box", None) is None:
            w, h = self._box_size()
            big = max(70, int(self.root.winfo_screenheight() * 0.11))
            box = GlassBox(self.root, w, h, "#ef4444", tint="#dc2626")
            box.text(0.17, "SYSTEM SHUTDOWN", (self.f_head, 28, "bold"), tag="title")
            box.text(0.47, "20", (self.f_head, big, "bold"), tag="number")
            box.text(0.73, 'Say "Nyx, abort"  ·  press Space  ·  or click ABORT',
                     (self.f_ui, 13), tag="sub")
            box.button(0.87, "ABORT", (self.f_head, 16, "bold"), self.abort_shutdown)
            self.countdown_box = box
            self.root.bind("<space>", lambda e: self.abort_shutdown())
            self._tick_countdown()

    def _tick_countdown(self):
        box = getattr(self, "countdown_box", None)
        if box is None:
            return
        left = self.shutdown_deadline - time.time()
        box.set_text("number", str(max(0, math.ceil(left))))
        if left <= 0:
            box.set_text("sub", "Shutting down…")
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

    def abort_shutdown(self):
        # The engine emits a "shutdown" event, which hides the countdown
        # and shows the blue "aborted" box.
        if self.engine is not None:
            msg = self.engine.abort_shutdown()
            threading.Thread(target=self.engine.speak, args=(msg,), daemon=True).start()
        else:
            self.hide_countdown()

    def show_aborted(self):
        """Blue 'SHUTDOWN ABORTED' box for a few seconds, then fade out."""
        old = getattr(self, "aborted_box", None)
        if old is not None:
            old.destroy()
        w, h = self._box_size()
        box = GlassBox(self.root, w, int(h * 0.5), "#60a5fa", tint="#2563eb")
        box.text(0.42, "SHUTDOWN ABORTED", (self.f_head, 34, "bold"))
        box.text(0.70, "Your PC will stay on.", (self.f_ui, 14))
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
        self._bubble("YOU", text, "right", USER_BUBBLE, "#7dd3fc")

    def add_nyx(self, text, kind=None):
        self._bubble("NYX", text, "left", NYX_BUBBLE, "#a7f3d0", kind)

    def clear_chat(self):
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
        elif kind == "level":
            self.level_rms = ev.get("rms", 0.0)
            self.threshold_rms = max(1.0, ev.get("threshold", 1.0))
        elif kind == "user":
            self.add_user(ev.get("text", ""))
        elif kind == "heard":
            pass  # the cleaned command is shown via "user"
        elif kind == "reply":
            self.add_nyx(ev.get("text", ""), ev.get("kind"))
        elif kind == "mic":
            self.mic_enabled = ev.get("enabled", True)
            self._refresh_mic_button()
            self.set_state(self.state)
        elif kind == "shutdown":
            if ev.get("active"):
                self.show_countdown(ev.get("deadline", time.time() + 20))
            else:
                self.hide_countdown()
                if ev.get("aborted"):
                    self.show_aborted()
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
            self.mic_name.configure(text=str(ev.get("value", ""))[:42])
        elif kind == "ready":
            self._system("Online. Say \"Nyx\" followed by a command, or type below.")
            model = os.path.basename(str(ev.get("model") or ""))
            self.engine_label.configure(text=f"whisper {model}" if model else "online")
        elif kind == "error":
            self._system("Error: " + ev.get("text", ""))
            self.set_state("error")
            self.hint_label.configure(text=ev.get("text", "")[:60])
        elif kind == "finished":
            if self.state != "error":
                self.set_state("stopped")
            if not self.stop_event.is_set() and self.state != "error":
                # The user said goodbye.
                self.root.after(1200, self.on_close)

    def set_state(self, value):
        self.state = value
        if value not in ("idle", "listening"):
            self.level_rms = 0.0   # no level events arrive in these states
        shown ="muted" if (not self.mic_enabled and value == "idle") else value
        colour, label, hint = STATE_STYLE.get(shown, STATE_STYLE["idle"])
        if self.deep_mode and shown in DEEP_COLORS:
            colour = DEEP_COLORS[shown]
            hint = DEEP_HINT.get(shown, hint)
        self.orb.set_state(shown, colour)
        self.state_label.configure(text=label)
        if hint:
            self.hint_label.configure(text=hint)
        self.meter.itemconfig(self.meter_fill, fill=colour)

    def _refresh_mic_button(self):
        self.mic_btn.configure(text="●  Mic on" if self.mic_enabled else "○  Mic off",
                               fg=TEXT if self.mic_enabled else STATE_STYLE["muted"][0])

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

    def toggle_mic(self):
        if self.engine is None:
            return
        self.engine.set_listening(not self.engine.LISTENING_ENABLED)

    def skip_speech(self):
        """Esc: stop talking, or end the task if Nyx is still thinking."""
        if self.engine is not None:
            if not self.engine.cancel_thinking():
                self.engine.stop_speaking()

    def on_close(self):
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


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)   # crisp text on high-DPI screens
    except Exception:
        pass
    root = tk.Tk()
    NyxApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
