"""
nyx_settings.py - user settings shared by the app (nyx_app.py) and the engine
(assistant.py). Saved as settings.json next to this file. Change them in the
app (Settings button / Ctrl+,) or edit settings.json by hand.
"""
import json
from pathlib import Path

SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"

# App colour themes. Keys are used by nyx_app.py.
THEMES = {
    "Midnight": {
        "bg": "#07090f", "panel": "#0d111b", "panel_edge": "#1a2133",
        "text": "#e6ebf5", "muted": "#7b869c", "faint": "#3a4458",
        "user_bubble": "#16243d", "nyx_bubble": "#121a2a",
        "user_name": "#7dd3fc", "nyx_name": "#a7f3d0",
    },
    "Graphite": {
        "bg": "#0e0e10", "panel": "#17171a", "panel_edge": "#2a2a2f",
        "text": "#f4f4f5", "muted": "#a1a1aa", "faint": "#52525b",
        "user_bubble": "#2a2a30", "nyx_bubble": "#1f1f24",
        "user_name": "#d4d4d8", "nyx_name": "#a1a1aa",
    },
    "Purple Night": {
        "bg": "#0b0714", "panel": "#130d22", "panel_edge": "#251a3f",
        "text": "#ede9fe", "muted": "#9d8fc4", "faint": "#4a3f6b",
        "user_bubble": "#2a1d4d", "nyx_bubble": "#1b1430",
        "user_name": "#c4b5fd", "nyx_name": "#f0abfc",
    },
    "Forest": {
        "bg": "#06100c", "panel": "#0b1913", "panel_edge": "#173126",
        "text": "#e3f5ec", "muted": "#7fa596", "faint": "#365a4b",
        "user_bubble": "#123527", "nyx_bubble": "#0f251c",
        "user_name": "#86efac", "nyx_name": "#5eead4",
    },
    "Light": {
        "bg": "#eef1f6", "panel": "#ffffff", "panel_edge": "#d6dce8",
        "text": "#0f172a", "muted": "#5b6476", "faint": "#9aa3b5",
        "user_bubble": "#dbeafe", "nyx_bubble": "#eef2f7",
        "user_name": "#0369a1", "nyx_name": "#047857",
    },
}

# Orb colour per state. "Custom" is whatever the user picked by hand.
ORB_STATES = ("idle", "listening", "transcribing", "thinking", "speaking")
ORB_STATE_NAMES = {
    "idle": "Standing by", "listening": "Listening", "transcribing": "Understanding",
    "thinking": "Thinking", "speaking": "Speaking",
}
ORB_PRESETS = {
    "Nyx Blue": {"idle": "#38bdf8", "listening": "#22d3ee", "transcribing": "#fbbf24",
                 "thinking": "#a78bfa", "speaking": "#34d399"},
    "Emerald":  {"idle": "#10b981", "listening": "#34d399", "transcribing": "#facc15",
                 "thinking": "#2dd4bf", "speaking": "#4ade80"},
    "Sunset":   {"idle": "#f59e0b", "listening": "#fb923c", "transcribing": "#facc15",
                 "thinking": "#f472b6", "speaking": "#fbbf24"},
    "Violet":   {"idle": "#8b5cf6", "listening": "#a78bfa", "transcribing": "#f0abfc",
                 "thinking": "#c084fc", "speaking": "#e879f9"},
    "Ice":      {"idle": "#93c5fd", "listening": "#bfdbfe", "transcribing": "#c7d2fe",
                 "thinking": "#a5b4fc", "speaking": "#e0f2fe"},
}

# Orb colours while deep mode is on.
DEEP_PRESETS = {
    "Red":     {"idle": "#ef4444", "listening": "#f87171", "transcribing": "#fb7185",
                "thinking": "#dc2626", "speaking": "#f43f5e"},
    "Blood":   {"idle": "#b91c1c", "listening": "#dc2626", "transcribing": "#ef4444",
                "thinking": "#991b1b", "speaking": "#e11d48"},
    "Ember":   {"idle": "#f97316", "listening": "#fb923c", "transcribing": "#fdba74",
                "thinking": "#ea580c", "speaking": "#f59e0b"},
    "Magenta": {"idle": "#d946ef", "listening": "#e879f9", "transcribing": "#f0abfc",
                "thinking": "#c026d3", "speaking": "#ec4899"},
    "Gold":    {"idle": "#eab308", "listening": "#facc15", "transcribing": "#fde047",
                "thinking": "#ca8a04", "speaking": "#f59e0b"},
}

DEFAULTS = {
    "theme": "Graphite",
    "orb_preset": "Violet",
    "orb_colors": dict(ORB_PRESETS["Violet"]),
    "deep_colors_enabled": True,  # orb changes colour in deep mode
    "deep_preset": "Red",
    "deep_colors": dict(DEEP_PRESETS["Red"]),
    "muted_color": "grey",        # orb when the mic is off: "grey" or "mode" (current mode's colour)
    "chat_log_enabled": True,     # save conversations to previous_chats.txt
    "start_fullscreen": True,
    "speech_volume": 100,         # Nyx's voice, 0-100
}


def load():
    """Current settings: defaults overlaid with whatever settings.json holds."""
    settings = json.loads(json.dumps(DEFAULTS))   # deep copy
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if not isinstance(saved, dict):
        return settings
    if "deep_mode_red" in saved and "deep_colors_enabled" not in saved:   # older settings.json
        saved["deep_colors_enabled"] = saved["deep_mode_red"]
    for key, value in saved.items():
        if key in ("orb_colors", "deep_colors") and isinstance(value, dict):
            settings[key].update({k: v for k, v in value.items() if k in ORB_STATES})
        elif key in DEFAULTS:
            settings[key] = value
    if settings["theme"] not in THEMES:
        settings["theme"] = DEFAULTS["theme"]
    return settings


def save(settings):
    """Write settings.json (via a temp file, so a crash can't leave it half-written)."""
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def update(**changes):
    """Load, change some keys, save. Returns the new settings."""
    settings = load()
    settings.update(changes)
    save(settings)
    return settings
