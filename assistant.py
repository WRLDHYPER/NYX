import os
import io
import re
import wave
import sys
import math
import time
import webbrowser
import datetime
import struct
import msvcrt
import shutil
import subprocess
from pathlib import Path
from collections import deque

import numpy as np
import librosa
import soundfile as sf
import pyaudio
import requests
import sounddevice as sd
from piper import PiperVoice
from faster_whisper import WhisperModel

# -------------------------------------------------------------------
# Optional libraries for volume/brightness control
# -------------------------------------------------------------------
try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    HAS_VOLUME = True
except ImportError:
    HAS_VOLUME = False

try:
    import screen_brightness_control as sbc
    HAS_BRIGHTNESS = True
except ImportError:
    HAS_BRIGHTNESS = False

# -------------------------------------------------------------------
# Platform-specific flags for detaching subprocesses (Windows)
# -------------------------------------------------------------------
if sys.platform == "win32":
    CREATE_NEW_CONSOLE = subprocess.CREATE_NEW_CONSOLE
    DETACHED_PROCESS = subprocess.DETACHED_PROCESS
else:
    CREATE_NEW_CONSOLE = 0
    DETACHED_PROCESS = 0

# -------------------------------------------------------------------
# Configuration Options
# -------------------------------------------------------------------
FAST_MODEL = "dolphin-llama3:8b"
SMART_MODEL = "qwen2.5:14b"
SMART_MODEL_TRIGGERS = [
    "deep think", "think deeply", "deep reasoning", "reason step by step",
    "use the smart model", "smart model", "analyze deeply", "be thoughtful",
    "think", "deep", "deep mode",
]
DEEP_MODE_PREFIX = "normal"
WAKE_WORD = "nix/nyx"
WAKE_WORD_THRESHOLD = 600.0
WAKE_WORD_DEBUG = False
STT_DEVICE = "cuda"
DEFAULT_SPEECH_SPEED = 0.5

# -------------------------------------------------------------------
# Piper Voice Configuration
# -------------------------------------------------------------------
VOICES_DIR = Path(r"C:\EDITH\voices")
SELECTED_VOICE = "en_GB-vctk-medium"
PIPER_MODEL_PATH = VOICES_DIR / f"{SELECTED_VOICE}.onnx"

if not PIPER_MODEL_PATH.is_file():
    print(f"[-] Model '{SELECTED_VOICE}.onnx' not found in {VOICES_DIR}.")
    available = list(VOICES_DIR.glob("*.onnx"))
    if available:
        print("[+] Available models:")
        for f in available:
            print(f"    - {f.stem}")
    else:
        print("[-] No .onnx models found at all.")
    PIPER_MODEL_PATH = None
else:
    print(f"[+] Using Piper model: {PIPER_MODEL_PATH}")

# Safe local access policy
USER_HOME = Path.home()
ALLOWED_READ_ROOTS = [
    (Path.cwd()).resolve(),
    (USER_HOME / "Documents").resolve(),
    (USER_HOME / "Desktop").resolve(),
    Path(r"C:\EDITH").resolve(),
]
ALLOWED_URL_HOSTS = {"*"}

# VAD Thresholds
SILENCE_THRESHOLD = 900
SILENCE_DURATION = 1.5
MAX_RECORD_TIME = 25.0
LISTEN_COOLDOWN_SECONDS = 1.5
PRE_ROLL_CHUNKS = 6

WHISPER_CONTEXT_PROMPT = (
    "Hey Nix, what time is it? Open YouTube. Open GitHub. Open Roblox. "
    "Read the file. Shut down the system. Cancel shutdown. Confirm. "
    "Use the smart model and think deeply about this. Back to normal mode. "
    "Hey Nyx, help me with this."
)

SHUTDOWN_CONFIRMATION_PENDING = False
SHUTDOWN_COUNTDOWN_ACTIVE = False
SPEAKING_IN_PROGRESS = False
POST_SPEAK_COOLDOWN = 0.3

# -------------------------------------------------------------------
# Chat History & Logging
# -------------------------------------------------------------------
chat_history = deque(maxlen=20)  # store last 20 exchanges (role, content)
last_assistant_reply = ""

# Determine script directory for log file
try:
    SCRIPT_DIR = Path(__file__).parent
except NameError:
    SCRIPT_DIR = Path.cwd()
CHAT_LOG_FILE = SCRIPT_DIR / "previous_chats.txt"

def log_conversation(user_text, assistant_reply):
    """Append user & assistant messages to the log file with timestamp."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(CHAT_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] User: {user_text}\n")
        f.write(f"[{timestamp}] Nyx: {assistant_reply}\n")
        f.write("-" * 40 + "\n")

def read_chat_log(full=False):
    """
    Return the contents of the chat log with a special prefix for console-only output.
    If full=True, returns the entire file. Otherwise, returns only the last 10 exchanges.
    """
    if not CHAT_LOG_FILE.exists():
        return "CHAT_LOG:No previous chats found yet."
    try:
        content = CHAT_LOG_FILE.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            return "CHAT_LOG:The chat log is empty."
        if full:
            return f"CHAT_LOG:{content}"

        # --- Extract last 10 exchanges (user + assistant pairs) ---
        lines = content.splitlines()
        exchanges = []
        current_user = None
        current_assistant = None
        for line in lines:
            line = line.strip()
            if not line or line.startswith("-" * 10):  # skip separator lines
                continue
            # Match user or assistant lines
            user_match = re.match(r'^\[.*?\]\s+User:\s+(.*)$', line)
            assistant_match = re.match(r'^\[.*?\]\s+Nyx:\s+(.*)$', line)
            if user_match:
                current_user = user_match.group(1).strip()
            elif assistant_match:
                current_assistant = assistant_match.group(1).strip()
                if current_user is not None:
                    exchanges.append((current_user, current_assistant))
                    current_user = None
                    current_assistant = None
        # Take last 10 pairs
        last_10 = exchanges[-10:] if exchanges else []
        if not last_10:
            return "CHAT_LOG:No complete exchanges found."

        # Format the output
        output = f"Last {len(last_10)} chat exchanges:\n"
        output += "=" * 60 + "\n"
        for i, (user, assistant) in enumerate(last_10, 1):
            output += f"[{i}] User: {user}\n"
            output += f"    Nyx: {assistant}\n"
            output += "-" * 40 + "\n"
        return f"CHAT_LOG:{output}"
    except Exception as e:
        return f"CHAT_LOG:Could not read chat log: {e}"

# -------------------------------------------------------------------
# Volume & Brightness control functions (UPDATED – no PowerShell)
# -------------------------------------------------------------------
def set_system_volume(level):
    """Set system volume to level (0-100) using best available method."""
    if level < 0 or level > 100:
        return "Volume must be between 0 and 100."

    # Try pycaw (modern)
    try:
        from pycaw.pycaw import AudioUtilities
        volume = AudioUtilities.GetSpeakers().GetEndpointVolume()
        volume.SetMasterVolumeLevelScalar(level / 100.0, None)
        return f"Volume set to {level}%."
    except Exception:
        pass

    # Try pycaw (old method)
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        devices = AudioUtilities.GetSpeakers()
        if hasattr(devices, 'Activate'):
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume.SetMasterVolumeLevelScalar(level / 100.0, None)
            return f"Volume set to {level}%."
    except Exception:
        pass

    # Try nircmd
    try:
        nircmd_path = shutil.which("nircmd")
        if nircmd_path:
            subprocess.run([nircmd_path, "setsysvolume", str(int(level * 655.35)), "0"], check=False, capture_output=True)
            return f"Volume set to {level}% (using nircmd)."
    except Exception:
        pass

    # Try SoundVolumeView (another Nirsoft tool)
    try:
        svc_path = shutil.which("SoundVolumeView.exe")
        if svc_path:
            subprocess.run([svc_path, "/SetVolume", "0", str(level)], check=False, capture_output=True)
            return f"Volume set to {level}% (using SoundVolumeView)."
    except Exception:
        pass

    # Final suggestion
    return ("Volume control failed. Please run the assistant as Administrator "
            "and ensure 'pycaw' is installed (pip install pycaw comtypes). "
            "Alternatively, install 'nircmd' (https://nircmd.nirsoft.net/) and place it in your PATH.")

def set_system_brightness(level):
    """Set system brightness to level (0-100). Returns a status message."""
    if level < 0 or level > 100:
        return "Brightness must be between 0 and 100."
    try:
        if HAS_BRIGHTNESS:
            sbc.set_brightness(level)
            return f"Brightness set to {level}%."
        else:
            # Fallback via PowerShell
            try:
                subprocess.run(
                    ["powershell", "-c",
                     f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(0,{level})"],
                    check=False, capture_output=True
                )
                return f"Brightness set to {level}%."
            except Exception:
                return "Brightness control failed. Please install 'screen-brightness-control' via: pip install screen-brightness-control"
    except Exception as e:
        return f"Failed to set brightness: {e}"

# -------------------------------------------------------------------
# 1. Initialize Piper Text-to-Speech Engine
# -------------------------------------------------------------------
print("[+] Loading Piper TTS voice model...")
piper_voice = None
try:
    piper_voice = PiperVoice.load(PIPER_MODEL_PATH)
    print("[+] Piper TTS loaded successfully.")
except Exception as err:
    print(f"[-] Could not load Piper model from '{PIPER_MODEL_PATH}': {err}")

def speak(text, speaker_id=0, speed=1):
    """Synthesize and speak text, muting microphone during playback."""
    global SPEAKING_IN_PROGRESS
    if not text:
        return

    print(f"\n[Nyx]: {text}")

    if piper_voice is None:
        print("[-] Piper TTS unavailable. Skipping speech.")
        return

    SPEAKING_IN_PROGRESS = True
    try:
        sample_rate = getattr(piper_voice.config, 'sample_rate', 22050)
        wav_buffer = io.BytesIO()

        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)

            if hasattr(piper_voice, "synthesize_wav"):
                try:
                    piper_voice.synthesize_wav(text, wav_file, speaker_id=speaker_id)
                except TypeError:
                    piper_voice.synthesize_wav(text, wav_file)
            else:
                piper_voice.synthesize(text, wav_file)

        wav_buffer.seek(0)

        with wave.open(wav_buffer, 'rb') as wf:
            out_channels = wf.getnchannels()
            out_width = wf.getsampwidth()
            out_rate = wf.getframerate()
            audio_bytes = wf.readframes(wf.getnframes())

        if not audio_bytes:
            print("[-] Piper synthesized empty audio buffer.")
            return

        if speed != 1.0:
            try:
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                try:
                    audio_np = librosa.effects.time_stretch(audio_np, rate=speed)
                    print(f"[+] Using librosa for speed {speed:.2f}x (pitch preserved)")
                except ImportError:
                    print("[-] librosa not found – falling back to resampling (quality loss)")
                    out_rate = int(out_rate * speed)
                else:
                    audio_np = np.clip(audio_np, -1.0, 1.0) * 32767
                    audio_bytes = audio_np.astype(np.int16).tobytes()
            except Exception as e:
                print(f"[-] Speed adjustment failed: {e}. Using original speed.")

        out_stream = audio_interface.open(
            format=audio_interface.get_format_from_width(out_width),
            channels=out_channels,
            rate=out_rate,
            output=True
        )
        out_stream.write(audio_bytes)
        out_stream.stop_stream()
        out_stream.close()

    except Exception as e:
        print(f"[-] Piper speech execution failed: {type(e).__name__} - {e}")
    finally:
        SPEAKING_IN_PROGRESS = False

# -------------------------------------------------------------------
# 2. Initialize Speech Recognition & Wake Word Engine
# -------------------------------------------------------------------
print("[+] Loading Faster-Whisper STT model on CUDA GPU...")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

stt_model = None
for model_name in ("large-v2", "large", "medium", "small", "tiny"):
    try:
        stt_model = WhisperModel(
            model_name,
            device=STT_DEVICE,
            compute_type="float16",
            local_files_only=True,
        )
        print(f"[+] Whisper model loaded successfully: {model_name}")
        break
    except Exception as e:
        print(f"[-] Could not load Whisper model '{model_name}' locally: {e}")

if stt_model is None:
    raise RuntimeError(
        "No local Whisper model could be loaded. Install a cached model first or set HF_TOKEN."
    )

oww_model = None
print("[+] OpenWakeWord disabled – using Whisper-based wake phrase detection.")

def normalize_transcript(text):
    if not text:
        return ""
    normalized = text.lower().replace("_", " ")
    normalized = normalized.replace("'", "")
    normalized = "".join(ch for ch in normalized if ch.isalpha() or ch.isspace() or ch in ",!?.")
    normalized = " ".join(normalized.split())
    return normalized

def contains_wake_phrase(text):
    normalized = normalize_transcript(text)
    if not normalized:
        return False
    wake_variants = ["hey nix", "hey nyx", "nix", "nyx"]
    for variant in wake_variants:
        if normalized == variant:
            return True
        if normalized.startswith(variant):
            return True
        if f" {variant} " in normalized:
            return True
        if normalized.endswith(variant):
            return True
    return False

# kept for compatibility, but not used anymore
def extract_command_text(text):
    normalized = normalize_transcript(text)
    if not normalized:
        return "hello"
    patterns = [
        r"(?:hey\s+)?(?:nix|nyx)\s*",
        r"\b(?:nix|nyx)\b\s*",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            remainder = normalized[match.end():].strip(" ,!?.")
            return remainder if remainder else "hello"
    return normalized.strip(" ,!?.") or "hello"

def looks_like_followup_answer(text):
    normalized = normalize_transcript(text)
    if not normalized:
        return False
    if len(normalized.split()) > 12:
        return False
    short_answers = {
        "yes", "no", "yeah", "yep", "sure", "okay", "ok",
        "maybe", "not really", "nah", "nope", "i do", "i dont",
        "i do not", "i dont know", "i know", "i think so", "probably",
        "definitely", "absolutely", "of course", "please", "yes please"
    }
    if normalized in short_answers:
        return True
    for answer in short_answers:
        if normalized.startswith(answer):
            return True
    return False

# -------------------------------------------------------------------
# 3. Audio Recording & Dynamic VAD Logic
# -------------------------------------------------------------------
def choose_microphone_index():
    preferred_names = ["hyperx", "duocast", "jbl", "steam streaming", "microphone"]
    candidates = []
    for i in range(audio_interface.get_device_count()):
        info = audio_interface.get_device_info_by_index(i)
        name = (info.get("name") or "").lower()
        max_channels = int(info.get("maxInputChannels", 0))
        if max_channels <= 0:
            continue
        if any(token in name for token in ["virtual", "mapper", "stereo mix", "loopback", "wave"]):
            continue
        try:
            test_stream = audio_interface.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                input_device_index=i,
                frames_per_buffer=1280,
            )
            test_data = test_stream.read(1280, exception_on_overflow=False)
            arr = np.frombuffer(test_data, dtype=np.int16)
            rms = math.sqrt(np.mean(np.square(arr.astype(np.float32))))
            test_stream.stop_stream()
            test_stream.close()
        except Exception:
            continue
        if rms <= 5.0:
            continue
        preference_score = 0
        for token in preferred_names:
            if token in name:
                preference_score += 1
        candidates.append((preference_score, float(rms), i, info.get("name")))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    _, rms_value, chosen, mic_name = candidates[0]
    print(f"[+] Selected microphone: {mic_name} (index {chosen}, rms={rms_value:.2f})")
    return chosen

FORMAT = pyaudio.paInt16
CHANNELS = 1
CHUNK = 1280

audio_interface = pyaudio.PyAudio()
MIC_DEVICE_INDEX = None
for i in range(audio_interface.get_device_count()):
    info = audio_interface.get_device_info_by_index(i)
    name = (info.get("name") or "").lower()
    if "hyperx" in name or "duocast" in name:
        MIC_DEVICE_INDEX = i
        break

if MIC_DEVICE_INDEX is None:
    MIC_DEVICE_INDEX = choose_microphone_index()

if MIC_DEVICE_INDEX is None:
    raise RuntimeError("No working microphone input device was found.")

RATE = 16000
print(f"[+] Using mic at {RATE} Hz on index {MIC_DEVICE_INDEX}")
speak("Hello sir, all systems are online.")

mic_stream = audio_interface.open(
    format=FORMAT,
    channels=CHANNELS,
    rate=RATE,
    input=True,
    input_device_index=MIC_DEVICE_INDEX,
    frames_per_buffer=CHUNK
)

def get_rms(chunk_data):
    count = len(chunk_data) / 2
    format_str = "%dh" % count
    shorts = struct.unpack(format_str, chunk_data)
    sum_squares = sum([s**2 for s in shorts])
    return math.sqrt(sum_squares / count)

def transcribe_full(wav_buffer):
    common_kwargs = dict(
        beam_size=8,
        best_of=5,
        temperature=[0.0, 0.2, 0.4],
        language="en",
        condition_on_previous_text=False,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=300),
        initial_prompt=WHISPER_CONTEXT_PROMPT,
    )
    try:
        segments, _ = stt_model.transcribe(wav_buffer, hotwords="Nix Nyx", **common_kwargs)
    except TypeError:
        wav_buffer.seek(0)
        segments, _ = stt_model.transcribe(wav_buffer, **common_kwargs)
    return "".join(s.text for s in segments).strip()

def record_until_silence(prefix_frames=None):
    print("[+] Listening... Speak your command now!")
    frames = list(prefix_frames) if prefix_frames else []
    silent_chunks = 0
    max_chunks = int(RATE / CHUNK * MAX_RECORD_TIME)
    silence_chunk_limit = int(RATE / CHUNK * SILENCE_DURATION)
    has_spoken = any(get_rms(f) > SILENCE_THRESHOLD for f in frames)
    for _ in range(max_chunks):
        data = mic_stream.read(CHUNK, exception_on_overflow=False)
        frames.append(data)
        rms = get_rms(data)
        if rms > SILENCE_THRESHOLD:
            has_spoken = True
            silent_chunks = 0
        else:
            if has_spoken:
                silent_chunks += 1
        if has_spoken and silent_chunks >= silence_chunk_limit:
            print("[+] Silence detected. End of voice input.")
            break
    return b''.join(frames)

# -------------------------------------------------------------------
# 4. Safe local tools: time, file reads, and approved links
# -------------------------------------------------------------------
def get_current_time_reply():
    now = datetime.datetime.now()
    return f"It is {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d, %Y')}."

def is_time_request(text):
    normalized = normalize_transcript(text or "")
    if not normalized:
        return False
    keywords = ["time", "clock", "date", "today", "day"]
    return any(keyword in normalized for keyword in keywords)

def extract_requested_path(command_text):
    candidate = None
    patterns = [
        r'(?i)(?:read|open|show)\s+(?:file|path|document)?\s*["\']([^"\']+)["\']',
        r'(?i)(?:read|open|show)\s+(?:file|path|document)?\s*([A-Za-z]:\\[^\s]+)',
        r'(?i)(?:read|open|show)\s+(?:file|path|document)?\s*([~./A-Za-z0-9_\\ -]+\\[^\s]+)',
        r'(?i)(?:read|open|show)\s+(?:file|path|document)?\s*([~./A-Za-z0-9_ -]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, command_text, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return candidate
    return None

def is_within_allowed_root(path_obj):
    resolved = path_obj.resolve(strict=False)
    for root in ALLOWED_READ_ROOTS:
        try:
            if os.path.commonpath([str(root), str(resolved)]) == str(root):
                return True
        except ValueError:
            continue
    return False

def safe_read_file(command_text):
    requested_path = extract_requested_path(command_text)
    if not requested_path:
        return "I can read files, but I need a path inside an allowed folder."
    expanded = os.path.expanduser(requested_path)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve(strict=False)
    if not candidate.exists() or not candidate.is_file():
        return f"I couldn't find that file: {candidate}."
    if not is_within_allowed_root(candidate):
        return f"Access denied. That file is outside the allowed folders: {candidate}."
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
        preview = text[:800].strip()
        return preview if preview else "The file is empty."
    except Exception as exc:
        return f"I can't read that file because of an error: {exc}"

def safe_open_link(command_text):
    text = (command_text or "").strip()
    lower_text = text.lower()
    site_aliases = {
        "google": "https://www.google.com",
        "youtube": "https://www.youtube.com",
        "github": "https://github.com",
        "gmail": "https://mail.google.com",
        "docs": "https://docs.google.com",
        "roblox": "https://www.roblox.com",
        "netflix": "https://www.netflix.com",
        "discord": "https://discord.com",
        "twitter": "https://x.com",
        "x": "https://x.com",
        "facebook": "https://www.facebook.com",
        "instagram": "https://www.instagram.com",
        "duckduckgo": "https://duckduckgo.com",
        "reddit": "https://www.reddit.com",
        "spotify": "https://open.spotify.com",
        "steam": "https://store.steampowered.com",
        "minecraft": "https://www.minecraft.net",
        "gemini": "https://gemini.google.com/app",
        "aternos": "https://www.aternos.org/en",
    }
    parsed_url = None
    url_match = re.search(r'https?://[^\s]+', text, flags=re.IGNORECASE)
    if url_match:
        parsed_url = url_match.group(0)
    else:
        for name, url in site_aliases.items():
            if re.search(rf'\b{name}\b', lower_text):
                parsed_url = url
                break
        if parsed_url is None:
            domain_match = re.search(r'\b([a-z0-9.-]+\.[a-z]{2,})(?:\s|$)', lower_text)
            if domain_match:
                domain = domain_match.group(1)
                parsed_url = f"https://{domain}"
    if not parsed_url:
        return "I can open a site, but I need a site name or URL."
    try:
        webbrowser.open(parsed_url)
        return f"Opening {parsed_url} in your browser."
    except Exception as exc:
        return f"I couldn't open the link: {exc}"

def quick_open_from_text(command_text):
    text = (command_text or "").strip()
    lower_text = text.lower()
    site_aliases = {
        "google": "https://www.google.com",
        "youtube": "https://www.youtube.com",
        "github": "https://github.com",
        "roblox": "https://www.roblox.com",
        "microsoft": "https://www.microsoft.com",
        "gmail": "https://mail.google.com",
        "gemini": "https://gemini.google.com/app",
        "aternos": "https://www.aternos.org/en",
    }
    for name, url in site_aliases.items():
        if re.search(rf'\b{name}\b', lower_text):
            try:
                webbrowser.open(url)
                return f"Opening {url} in your browser."
            except Exception as exc:
                return f"I couldn't open the link: {exc}"
    m = re.search(r'\b(?:open|visit|go to|launch)\s+([a-z0-9-]{2,})\b', lower_text)
    if m:
        candidate = m.group(1)
        candidate_url = f"https://www.{candidate}.com"
        try:
            webbrowser.open(candidate_url)
            return f"Opening {candidate_url} in your browser."
        except Exception as exc:
            return f"I couldn't open the link: {exc}"
    return None

# -------------------------------------------------------------------
# Enhanced Application Opener
# -------------------------------------------------------------------
def open_application(command_text):
    """
    Try to open a Windows application by name.
    Searches PATH, Start Menu, Program Files, System32, and common locations.
    Returns a success message, or None if no application is found.
    """
    print(f"[DEBUG] open_application called with: '{command_text}'")

    # Extract the app name after 'open', 'launch', or 'start'
    match = re.search(r'\b(?:open|launch|start)\s+(.+?)(?:\s+app)?\s*$', command_text, re.IGNORECASE)
    if not match:
        print("[DEBUG] No app name found in command.")
        return None
    app_name = match.group(1).strip()
    if not app_name:
        print("[DEBUG] App name is empty.")
        return None

    # Remove trailing "app" if present
    if app_name.lower().endswith(" app"):
        app_name = app_name[:-4].strip()
    elif app_name.lower().endswith("app"):
        app_name = app_name[:-3].strip()

    # Strip trailing punctuation (., !, ?, ;, :)
    app_name = re.sub(r'[.,!?;:]+$', '', app_name).strip()

    print(f"[DEBUG] Cleaned app name: '{app_name}'")

    # If it looks like a URL, skip app search
    if '://' in app_name or app_name.lower().startswith('http'):
        print("[DEBUG] Looks like a URL, skipping app search.")
        return None

    # --- Common alias mapping ---
    ALIASES = {
        "notepad": "notepad.exe",
        "calc": "calc.exe",
        "calculator": "calc.exe",
        "paint": "mspaint.exe",
        "word": "winword.exe",
        "excel": "excel.exe",
        "powerpoint": "powerpnt.exe",
        "outlook": "outlook.exe",
        "explorer": "explorer.exe",
        "file explorer": "explorer.exe",
        "cmd": "cmd.exe",
        "command prompt": "cmd.exe",
        "powershell": "powershell.exe",
        "terminal": "wt.exe",
        "snipping tool": "snippingtool.exe",
        "task manager": "taskmgr.exe",
        "control panel": "control.exe",
        "settings": "ms-settings:",
        "chrome": "chrome.exe",
        "firefox": "firefox.exe",
        "edge": "msedge.exe",
        "browser": "msedge.exe",
        "spotify": "spotify.exe",
        "discord": "Discord.exe",
        "slack": "slack.exe",
        "steam": "steam.exe",
        "vscode": "code.exe",
        "visual studio code": "code.exe",
    }

    # Check if the app name is in the alias map
    if app_name.lower() in ALIASES:
        exe_name = ALIASES[app_name.lower()]
        # Try to find the executable
        exe_path = shutil.which(exe_name)
        if exe_path:
            print(f"[DEBUG] Found alias '{app_name}' -> '{exe_name}' at {exe_path}")
            try:
                subprocess.Popen(
                    [exe_path],
                    creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL
                )
                return f"Opening {app_name}."
            except Exception as e:
                print(f"[DEBUG] Failed to launch alias: {e}")
        else:
            print(f"[DEBUG] Alias '{app_name}' not found in PATH, continuing search.")

    def launch_exe(path):
        try:
            subprocess.Popen(
                [path],
                creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL
            )
            return True
        except Exception as e:
            print(f"[DEBUG] launch_exe failed: {e}")
            return False

    def launch_shortcut(path):
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", path],
                shell=False,
                creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL
            )
            return True
        except Exception as e:
            print(f"[DEBUG] launch_shortcut failed: {e}")
            return False

    # 1. Try PATH (with .exe if needed)
    # If the app name doesn't have an extension, try with .exe
    exe_path = shutil.which(app_name)
    if not exe_path and not app_name.lower().endswith('.exe'):
        exe_path = shutil.which(app_name + '.exe')
    if exe_path:
        print(f"[DEBUG] Found in PATH: {exe_path}")
        if launch_exe(exe_path):
            return f"Opening {app_name}."

    # 2. Search common directories (including System32)
    search_dirs = [
        # Start Menu
        os.path.join(os.environ.get("ProgramData", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        # Program Files
        os.environ.get("ProgramFiles", "C:\\Program Files"),
        os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
        # AppData
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
        os.path.join(os.environ.get("APPDATA", ""), "Programs"),
        # Desktop
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
        os.path.join(os.environ.get("PUBLIC", ""), "Desktop"),
        # System folders (critical for Notepad, Calculator, etc.)
        os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32"),
        os.path.join(os.environ.get("SystemRoot", "C:\\Windows")),
        # Additional common
        r"C:\Program Files\WindowsApps",
        os.path.join(os.environ.get("SystemDrive", "C:"), "Program Files\\Common Files"),
        os.path.join(os.environ.get("SystemDrive", "C:"), "Program Files (x86)\\Common Files"),
    ]

    for base_dir in search_dirs:
        if not base_dir or not os.path.isdir(base_dir):
            continue
        print(f"[DEBUG] Searching in: {base_dir}")
        try:
            for root, dirs, files in os.walk(base_dir):
                # Limit depth to avoid too much scanning
                depth = root.replace(base_dir, '').count(os.sep)
                if depth > 4:
                    continue
                for file in files:
                    file_lower = file.lower()
                    # Check .lnk shortcuts
                    if file_lower.endswith(".lnk"):
                        name_no_ext = os.path.splitext(file)[0]
                        if app_name.lower() in name_no_ext.lower():
                            shortcut_path = os.path.join(root, file)
                            print(f"[DEBUG] Found shortcut: {shortcut_path}")
                            if launch_shortcut(shortcut_path):
                                return f"Opening {app_name}."
                    # Check .exe files
                    elif file_lower.endswith(".exe"):
                        name_no_ext = os.path.splitext(file)[0]
                        if app_name.lower() in name_no_ext.lower():
                            exe_full = os.path.join(root, file)
                            print(f"[DEBUG] Found exe: {exe_full}")
                            if launch_exe(exe_full):
                                return f"Opening {app_name}."
        except Exception as e:
            print(f"[DEBUG] Error scanning {base_dir}: {e}")
            continue

    # 3. Quick root scan in Program Files for launchers
    root_search = [
        os.environ.get("ProgramFiles", "C:\\Program Files"),
        os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
    ]
    for root_dir in root_search:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        print(f"[DEBUG] Scanning Program Files root: {root_dir}")
        try:
            for item in os.listdir(root_dir):
                item_path = os.path.join(root_dir, item)
                if os.path.isdir(item_path):
                    for f in os.listdir(item_path):
                        if f.lower().endswith(".exe"):
                            name_no_ext = os.path.splitext(f)[0]
                            if app_name.lower() in name_no_ext.lower():
                                exe_full = os.path.join(item_path, f)
                                print(f"[DEBUG] Found exe: {exe_full}")
                                if launch_exe(exe_full):
                                    return f"Opening {app_name}."
        except Exception as e:
            print(f"[DEBUG] Error scanning {root_dir}: {e}")
            continue

    print(f"[DEBUG] No application found for: {app_name}")
    return None
# -------------------------------------------------------------------
# Main tool dispatcher (UPDATED: volume/brightness handled by keyword)
# -------------------------------------------------------------------
def handle_local_tools(command_text):
    global DEEP_MODE_PREFIX, SHUTDOWN_CONFIRMATION_PENDING, SHUTDOWN_COUNTDOWN_ACTIVE
    global last_assistant_reply
    text = (command_text or "").strip()
    if not text:
        return None

    # DEBUG: print raw text before any processing
    print(f"[DEBUG] raw command_text: '{text}'")

    # --- Strip common wake words from the beginning (case-insensitive) ---
    wake_pattern = r'^(?:hey\s+)?(?:nix|nyx)\s*,?\s*'
    text = re.sub(wake_pattern, '', text, flags=re.IGNORECASE).strip()

    # Clean text: remove trailing punctuation and extra spaces
    clean_text = re.sub(r'[.,!?;:]+$', '', text).strip()
    lower_text = clean_text.lower()

    print(f"[DEBUG] cleaned lower_text: '{lower_text}'")

    # --- VOLUME CONTROL ---
    # First, check if the command is about volume
    if re.search(r'\b(?:volume|vol)\b', lower_text):
        # Try to extract the number
        vol_match = re.search(r'(?:volume|vol)\s*(?:to\s*)?(\d{1,3})\s*%?', lower_text)
        if vol_match:
            level = int(vol_match.group(1))
            if 0 <= level <= 100:
                print(f"[DEBUG] Volume command matched: {level}%")
                return set_system_volume(level)
            else:
                return "Volume must be between 0 and 100."
        else:
            # Volume command detected but no number found
            return "What volume level would you like? Please say a number between 0 and 100."

    # --- BRIGHTNESS CONTROL ---
    if re.search(r'\b(?:brightness|bright)\b', lower_text):
        bright_match = re.search(r'(?:brightness|bright)\s*(?:to\s*)?(\d{1,3})\s*%?', lower_text)
        if bright_match:
            level = int(bright_match.group(1))
            if 0 <= level <= 100:
                print(f"[DEBUG] Brightness command matched: {level}%")
                return set_system_brightness(level)
            else:
                return "Brightness must be between 0 and 100."
        else:
            return "What brightness level would you like? Please say a number between 0 and 100."

    # --- REPEAT LAST ASSISTANT REPLY ---
    if re.search(r'\b(?:repeat|say that again|what did you say|repeat that)\b', lower_text):
        if last_assistant_reply:
            return last_assistant_reply
        else:
            return "I haven't said anything yet."

    # --- SHUTDOWN CANCELLATION ---
    if re.search(r'\b(abort|cancel|override)\b', lower_text):
        if SHUTDOWN_CONFIRMATION_PENDING or SHUTDOWN_COUNTDOWN_ACTIVE:
            SHUTDOWN_CONFIRMATION_PENDING = False
            SHUTDOWN_COUNTDOWN_ACTIVE = False
            try:
                subprocess.run(["shutdown", "/a"], check=False)
                return "Shutdown Aborted."
            except Exception as exc:
                return f"Failed to cancel shutdown: {exc}"

    # --- PENDING SHUTDOWN CONFIRMATION ---
    if SHUTDOWN_CONFIRMATION_PENDING:
        if re.search(r'\b(confirm|confirmed|yes)\b', lower_text):
            SHUTDOWN_CONFIRMATION_PENDING = False
            SHUTDOWN_COUNTDOWN_ACTIVE = True
            try:
                subprocess.run(["shutdown", "/s", "/t", "20", "/c", "System shutdown confirmed."], check=False)
                return "Shutting down the system in 20 seconds. Say 'abort' to cancel."
            except Exception as exc:
                SHUTDOWN_COUNTDOWN_ACTIVE = False
                return f"Failed to shutdown: {exc}"
        elif re.search(r'\b(no|nope|cancel|abort|override)\b', lower_text):
            SHUTDOWN_CONFIRMATION_PENDING = False
            try:
                subprocess.run(["shutdown", "/a"], check=False)
                return "Shutdown cancelled."
            except Exception as exc:
                return f"Failed to cancel shutdown: {exc}"
        else:
            return "Please confirm with 'confirm' or cancel with 'abort'."

    if re.search(r'\b(bye|goodbye|exit|quit|shutdown|close yourself|close the app)\b', lower_text):
        if re.search(r'\bsystem\b', lower_text):
            SHUTDOWN_CONFIRMATION_PENDING = True
            return "Confirmation required."
        return "__EXIT__"

    if re.search(r'\b(normal|standard|regular)\b', lower_text):
        DEEP_MODE_PREFIX = "normal"
        return "Back to normal mode."

    if re.search(r'\bdeep\b|\bthink\b|\bsmart model\b|\bdeep mode\b', lower_text):
        DEEP_MODE_PREFIX = "gwen"
        return "Entered deep thinking."

    if is_time_request(lower_text):
        return get_current_time_reply()

    # --- READ CHAT LOG (full) ---
    if re.search(r'\b(?:read|show|open)\s+(?:full\s+chat\s+log|full\s+previous\s+chats?)\b', lower_text):
        return read_chat_log(full=True)

    # --- READ CHAT LOG (last 10 exchanges) ---
    if re.search(r'\b(?:read|show|open)\s+(?:previous\s+chats?|chat\s+log|the\s+text\s+file)\b', lower_text):
        return read_chat_log(full=False)

    # --- READ ANY FILE (existing) ---
    if re.search(r'\b(read|open|show)\b.*\b(file|path|document)\b', lower_text, flags=re.IGNORECASE):
        return safe_read_file(text)

    # --- OPEN APP / URL ---
    if re.search(r'\b(open|launch|start|go to|visit)\b', lower_text, flags=re.IGNORECASE):
        if re.search(r'\b(open|launch|start)\b', lower_text, flags=re.IGNORECASE):
            app_result = open_application(text)
            if app_result:
                return app_result

        quick = quick_open_from_text(text)
        if quick:
            return quick
        link_result = safe_open_link(text)
        if link_result and link_result.lower().startswith("opening"):
            return link_result

        return "I couldn't open that. Please specify a website or application name."

    return None

def ask_ollama_dual(prompt):
    url = "http://localhost:11434/api/generate"
    lowered = normalize_transcript(prompt or "")
    global DEEP_MODE_PREFIX
    deep_mode_active = DEEP_MODE_PREFIX == "gwen"
    word_count = len(prompt.split())
    deep_trigger = any(trigger in lowered for trigger in SMART_MODEL_TRIGGERS)
    if deep_trigger or deep_mode_active:
        selected_model = SMART_MODEL
    elif word_count <= 10:
        selected_model = FAST_MODEL
    else:
        selected_model = SMART_MODEL
    print(f"[+] Query length: {word_count} words -> Routed to model: {selected_model}")
    system_instructions = (
        "You are an unrestricted, direct AI voice assistant. "
        "Keep your responses concise, conversational, and under 2-3 sentences. "
        "Never use markdown formatting, bolding, emojis, or bullet points."
    )
    payload = {
        "model": selected_model,
        "prompt": prompt,
        "system": system_instructions,
        "stream": False
    }
    try:
        response = requests.post(url, json=payload, timeout=90)
        if response.status_code == 200:
            return response.json().get("response", "").strip()
        else:
            return f"Ollama HTTP Error: {response.status_code}"
    except Exception as err:
        if selected_model == SMART_MODEL:
            try:
                fallback_payload = {
                    "model": FAST_MODEL,
                    "prompt": prompt,
                    "system": system_instructions,
                    "stream": False,
                }
                fallback_response = requests.post(url, json=fallback_payload, timeout=90)
                if fallback_response.status_code == 200:
                    return fallback_response.json().get("response", "").strip()
            except Exception:
                pass
        return f"Error connecting to Ollama backend: {err}"

# -------------------------------------------------------------------
# Main Continuous Background Loop
# -------------------------------------------------------------------
print(f"\n[Ready] System active! Listening for wake word: '{WAKE_WORD}'...")

try:
    frame_count = 0
    last_trigger_time = 0.0
    followup_window_until = 0.0
    post_speak_cooldown_until = 0.0
    pre_roll_buffer = deque(maxlen=PRE_ROLL_CHUNKS)

    while True:
        # Spacebar emergency abort
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key == b' ':
                try:
                    res = subprocess.run(["shutdown", "/a"], capture_output=True, text=True)
                    if res.returncode == 0:
                        SHUTDOWN_CONFIRMATION_PENDING = False
                        SHUTDOWN_COUNTDOWN_ACTIVE = False
                        speak("Shutdown Aborted")
                except Exception as exc:
                    print(f"Error checking spacebar abort: {exc}")

        if SPEAKING_IN_PROGRESS:
            time.sleep(0.05)
            continue

        now = time.time()

        if now < post_speak_cooldown_until:
            time.sleep(0.05)
            continue

        pcm_data = mic_stream.read(CHUNK, exception_on_overflow=False)
        audio_frame = np.frombuffer(pcm_data, dtype=np.int16)
        rms = math.sqrt(np.mean(np.square(audio_frame.astype(np.float32))))
        frame_count += 1

        pre_roll_buffer.append(pcm_data)

        if WAKE_WORD_DEBUG and frame_count % 20 == 0:
            print(f"[DEBUG] mic_rms={rms:.2f}")

        if now - last_trigger_time < LISTEN_COOLDOWN_SECONDS:
            continue

        followup_allowed = (now < followup_window_until) or SHUTDOWN_COUNTDOWN_ACTIVE

        if not followup_allowed and rms < WAKE_WORD_THRESHOLD:
            continue

        if not followup_allowed:
            print(f"\n[!] Speech energy spike detected! RMS={rms:.2f}")
        else:
            print("\n[!] Follow-up speech detected.")

        last_trigger_time = now

        recorded_pcm = record_until_silence(prefix_frames=list(pre_roll_buffer))

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(audio_interface.get_sample_size(FORMAT))
            wf.setframerate(RATE)
            wf.writeframes(recorded_pcm)
        wav_buffer.seek(0)

        print("[+] Transcribing speech to text...")
        user_text = transcribe_full(wav_buffer)

        if user_text:
            print(f"[User Prompt]: {user_text}")

            valid = False
            if followup_allowed:
                valid = True
                command_text = user_text.strip()
                print("[+] Follow-up mode: accepted command without wake word.")
            else:
                # Must contain wake phrase
                if contains_wake_phrase(user_text):
                    valid = True
                    # Pass the full user_text (including numbers) to handle_local_tools
                    command_text = user_text.strip()
                else:
                    print("[-] Heard speech, but no wake phrase detected.")
                    print(f"\n[Ready] Listening for '{WAKE_WORD}' again...")
                    continue

            if not valid:
                continue

            reply = handle_local_tools(command_text)
            if reply is None:
                reply = ask_ollama_dual(command_text)

            if reply == "__EXIT__":
                speak("Goodbye.")
                print("\n[Exiting] Assistant shutdown requested by user.")
                break

            # --- Handle chat log (print only, don't speak) ---
            if reply.startswith("CHAT_LOG:"):
                log_content = reply[9:]  # strip the prefix
                print("\n" + "=" * 60)
                print("CHAT LOG CONTENTS:")
                print("=" * 60)
                print(log_content)
                print("=" * 60 + "\n")
                # Set last_assistant_reply to a short confirmation
                last_assistant_reply = "Chat log printed to console."
                # Log the user command and this confirmation
                chat_history.append({"role": "user", "content": command_text})
                chat_history.append({"role": "assistant", "content": last_assistant_reply})
                log_conversation(command_text, last_assistant_reply)
                # Do NOT call speak()
                post_speak_cooldown_until = time.time() + POST_SPEAK_COOLDOWN
                # No follow-up window for this command
                followup_window_until = 0.0
            else:
                # Normal reply – speak it
                if reply:
                    last_assistant_reply = reply
                    chat_history.append({"role": "user", "content": command_text})
                    chat_history.append({"role": "assistant", "content": reply})
                    log_conversation(command_text, reply)

                speak(reply)

                post_speak_cooldown_until = time.time() + POST_SPEAK_COOLDOWN

                if SHUTDOWN_CONFIRMATION_PENDING:
                    followup_window_until = time.time() + 60.0
                elif str(reply).rstrip().endswith('?'):
                    followup_window_until = time.time() + 15.0
                else:
                    followup_window_until = 0.0
        else:
            print("[-] No clear speech detected.")

        print(f"\n[Ready] Listening for '{WAKE_WORD}' again...")

except KeyboardInterrupt:
    print("\n[Exiting] Shutting down background streams...")
    mic_stream.stop_stream()
    mic_stream.close()
    audio_interface.terminate()
    sys.exit(0)