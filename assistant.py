import os
import io
import json
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
import threading
from pathlib import Path
from collections import deque

import difflib

# setup.exe installs the CUDA libraries (cuBLAS/cuDNN) as pip packages. Windows
# doesn't look inside site-packages for DLLs, so add their folders before
# faster-whisper/ctranslate2 tries to load them.
if sys.platform == "win32":
    import site
    for _sp in site.getsitepackages():
        _nv = Path(_sp) / "nvidia"
        if _nv.is_dir():
            for _bin in _nv.glob("*/bin"):
                os.add_dll_directory(str(_bin))
                os.environ["PATH"] = str(_bin) + os.pathsep + os.environ.get("PATH", "")

import numpy as np
import librosa
import pyaudio
import requests
import sounddevice as sd
from piper import PiperVoice
from faster_whisper import WhisperModel
from faster_whisper.vad import get_vad_model

try:
    from scipy.signal import butter, sosfilt
    _HIGHPASS_SOS = butter(4, 80, btype="highpass", fs=16000, output="sos")
except Exception:
    _HIGHPASS_SOS = None

# -------------------------------------------------------------------
# Event hook (used by the GUI). Console mode leaves this as None.
# -------------------------------------------------------------------
EVENT_SINK = None

def emit(event_type, /, **data):
    """Send an event to the GUI (if attached). Never raises.
    event_type is positional-only so events can carry their own 'kind' field."""
    if EVENT_SINK is None:
        return
    try:
        EVENT_SINK({"type": event_type, **data})
    except Exception:
        pass

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
# Used for deep mode and long questions, but only if it is installed in Ollama
# (setup.bat asks whether to download it). If not, FAST_MODEL answers everything.
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
APP_DIR = Path(__file__).resolve().parent   # works whatever the folder is called
VOICES_DIR = APP_DIR / "voices"
SELECTED_VOICE = "en_GB-vctk-medium"
PIPER_MODEL_PATH = VOICES_DIR / f"{SELECTED_VOICE}.onnx"

# Safe local access policy
USER_HOME = Path.home()
ALLOWED_READ_ROOTS = [
    (Path.cwd()).resolve(),
    (USER_HOME / "Documents").resolve(),
    (USER_HOME / "Desktop").resolve(),
    APP_DIR,
]
ALLOWED_URL_HOSTS = {"*"}

# -------------------------------------------------------------------
# Listening / VAD settings
# The trigger level adapts to the room: it is NOISE_MULTIPLIER x the
# measured background noise, but never below MIN_TRIGGER_RMS.
# A trigger is only accepted if the Silero neural VAD agrees it is speech.
# -------------------------------------------------------------------
MIN_TRIGGER_RMS = 180.0
NOISE_MULTIPLIER = 2.8
VAD_SPEECH_PROB = 0.5          # Silero probability counted as speech
SILENCE_DURATION = 0.9         # seconds of non-speech that end a command
NO_SPEECH_TIMEOUT = 3.0        # give up if nothing is said after a trigger
MAX_RECORD_TIME = 25.0
LISTEN_COOLDOWN_SECONDS = 0.4
PRE_ROLL_CHUNKS = 8            # ~0.64 s kept from before the trigger

# Whisper settings
WHISPER_MODELS = ("large-v3", "distil-large-v3", "large-v2", "medium", "small")
WHISPER_CONTEXT_PROMPT = "Nyx, open YouTube. Nyx, what time is it? Set volume to 40."
WHISPER_HOTWORDS = "Nyx"

# Things Whisper commonly invents from silence or noise.
HALLUCINATIONS = {
    "thank you", "thanks", "thank you.", "thanks for watching", "thank you for watching",
    "thanks for watching!", "please subscribe", "subscribe", "you", "bye", "okay",
    "so", "uh", "um", "hmm", "the end", ".", "...",
}

# Ways Whisper tends to spell the wake word "Nyx"/"Nix".
WAKE_ALIASES = {
    "nyx", "nix", "nicks", "nics", "niks", "nyxs", "knicks", "knix", "nyks",
    "nikks", "nick", "nicke", "nyck", "nicks,", "nixs", "nyxx", "onyx",
}

SHUTDOWN_CONFIRMATION_PENDING = False
PENDING_LEVEL = None   # "volume"/"brightness" after Nyx asks "what level?"
SHUTDOWN_COUNTDOWN_ACTIVE = False
SPEAKING_IN_PROGRESS = False
POST_SPEAK_COOLDOWN = 0.3
LISTENING_ENABLED = True
STOP_TTS = False
import nyx_settings
_settings = nyx_settings.load()
SPEECH_VOLUME = int(_settings["speech_volume"])  # 0-100, controls TTS gain
CHAT_LOG_ENABLED = bool(_settings["chat_log_enabled"])
DELETE_LOG_CONFIRMATION_PENDING = False
_SKIP_LOG_ONCE = False   # don't write the "chat log deleted" exchange into the new log

# -------------------------------------------------------------------
# Chat History & Logging
# -------------------------------------------------------------------
chat_history = deque(maxlen=20)
last_assistant_reply = ""

try:
    SCRIPT_DIR = Path(__file__).parent
except NameError:
    SCRIPT_DIR = Path.cwd()
CHAT_LOG_FILE = SCRIPT_DIR / "previous_chats.txt"

def log_conversation(user_text, assistant_reply):
    """Append user & assistant messages to the log file with timestamp."""
    global _SKIP_LOG_ONCE
    if _SKIP_LOG_ONCE:
        _SKIP_LOG_ONCE = False
        return
    if not CHAT_LOG_ENABLED:
        return
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(CHAT_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] User: {user_text}\n")
        f.write(f"[{timestamp}] Nyx: {assistant_reply}\n")
        f.write("-" * 40 + "\n")

def delete_chat_log(skip_next_log=True):
    """Delete previous_chats.txt and forget the conversation. Returns a message.
    skip_next_log: don't log the exchange that asked for it (voice command),
    so the deleted log doesn't come straight back."""
    global _SKIP_LOG_ONCE
    try:
        CHAT_LOG_FILE.unlink(missing_ok=True)
    except Exception as e:
        return f"Could not delete the chat history: {e}"
    chat_history.clear()
    _SKIP_LOG_ONCE = skip_next_log
    emit("chat_deleted")
    return "Chat history deleted."

def set_chat_log_enabled(enabled):
    """Turn saving to previous_chats.txt on or off (remembered in settings.json)."""
    global CHAT_LOG_ENABLED
    CHAT_LOG_ENABLED = bool(enabled)
    try:
        nyx_settings.update(chat_log_enabled=CHAT_LOG_ENABLED)
    except Exception as e:
        print(f"[-] Could not save settings: {e}")
    emit("settings_changed")
    return "Chat log turned on." if CHAT_LOG_ENABLED else "Chat log turned off. I won't save our conversations."

def read_chat_log(full=False):
    if not CHAT_LOG_FILE.exists():
        return "CHAT_LOG:No previous chats found yet."
    try:
        content = CHAT_LOG_FILE.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            return "CHAT_LOG:The chat log is empty."
        if full:
            return f"CHAT_LOG:{content}"

        lines = content.splitlines()
        exchanges = []
        current_user = None
        current_assistant = None
        for line in lines:
            line = line.strip()
            if not line or line.startswith("-" * 10):
                continue
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
        last_10 = exchanges[-10:] if exchanges else []
        if not last_10:
            return "CHAT_LOG:No complete exchanges found."

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
# Volume & Brightness control functions
# -------------------------------------------------------------------
def set_system_volume(level):
    if level < 0 or level > 100:
        return "Volume must be between 0 and 100."

    try:
        from pycaw.pycaw import AudioUtilities
        volume = AudioUtilities.GetSpeakers().GetEndpointVolume()
        volume.SetMasterVolumeLevelScalar(level / 100.0, None)
        return f"Volume set to {level}%."
    except Exception:
        pass

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

    try:
        nircmd_path = shutil.which("nircmd")
        if nircmd_path:
            val = int(level * 655.35)
            result = subprocess.run(
                [nircmd_path, "setsysvolume", str(val), "0"],
                check=False, capture_output=True, text=True
            )
            if result.returncode == 0:
                return f"Volume set to {level}% (using nircmd)."
            else:
                result2 = subprocess.run(
                    [nircmd_path, "setvolume", "0", str(level)],
                    check=False, capture_output=True, text=True
                )
                if result2.returncode == 0:
                    return f"Volume set to {level}% (using nircmd setvolume)."
    except Exception:
        pass

    try:
        svc_path = shutil.which("SoundVolumeView.exe")
        if svc_path:
            subprocess.run([svc_path, "/SetVolume", "0", str(level)], check=False, capture_output=True)
            return f"Volume set to {level}% (using SoundVolumeView)."
    except Exception:
        pass

    return ("Volume control failed. Please run the assistant as Administrator "
            "and ensure 'pycaw' is installed (pip install pycaw comtypes). "
            "Alternatively, install 'nircmd' (https://nircmd.nirsoft.net/) and place it in your PATH.")

def set_system_brightness(level):
    if level < 0 or level > 100:
        return "Brightness must be between 0 and 100."
    try:
        if HAS_BRIGHTNESS:
            sbc.set_brightness(level)
            return f"Brightness set to {level}%."
        else:
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
# Speech Volume (TTS volume) control
# -------------------------------------------------------------------
def set_speech_volume(level):
    """Set TTS volume (0-100). Returns a status message."""
    global SPEECH_VOLUME
    if level < 0 or level > 100:
        return "Speech volume must be between 0 and 100."
    SPEECH_VOLUME = level
    try:
        nyx_settings.update(speech_volume=level)
    except Exception as e:
        print(f"[-] Could not save settings: {e}")
    emit("settings_changed")
    return f"Speech volume set to {level}%."

# -------------------------------------------------------------------
# 1. Piper Text-to-Speech Engine (loaded in init_engine)
# -------------------------------------------------------------------
piper_voice = None

def load_piper():
    global piper_voice
    path = PIPER_MODEL_PATH
    if not path.is_file():
        available = list(VOICES_DIR.glob("*.onnx"))
        print(f"[-] Voice '{SELECTED_VOICE}.onnx' not found in {VOICES_DIR}.")
        if not available:
            print("[-] No .onnx voices found. Speech output disabled.")
            return
        path = available[0]
        print(f"[+] Falling back to voice: {path.stem}")
    print(f"[+] Loading Piper TTS voice: {path.name}")
    try:
        piper_voice = PiperVoice.load(path)
        print("[+] Piper TTS loaded successfully.")
    except Exception as err:
        print(f"[-] Could not load Piper model from '{path}': {err}")

TTS_LEAD_SILENCE = 0.15   # Bluetooth headphones often swallow the first syllable
TTS_TAIL_SILENCE = 0.6    # ...and the last words if the stream closes too early

# Speaker / microphone chosen in Settings ("" = Windows default / automatic).
SPEAKER_NAME = str(_settings.get("speaker", "") or "")
MIC_NAME_SETTING = str(_settings.get("microphone", "") or "")
_MIC_SWITCH_PENDING = False

def _speaker_device():
    """sounddevice index of the chosen speaker, or None for the Windows default."""
    if not SPEAKER_NAME:
        return None
    try:
        api = next((i for i, h in enumerate(sd.query_hostapis())
                    if "directsound" in h["name"].lower()), None)
        for i, d in enumerate(sd.query_devices()):
            if d["name"] == SPEAKER_NAME and d["max_output_channels"] > 0                     and (api is None or d["hostapi"] == api):
                return i
    except Exception:
        pass
    print(f"[-] Speaker '{SPEAKER_NAME}' not found - using the Windows default.")
    return None

def set_speaker(name):
    global SPEAKER_NAME
    SPEAKER_NAME = name or ""

def _play_audio(audio_np, sample_rate):
    global SPEAKING_IN_PROGRESS, STOP_TTS
    try:
        lead = np.zeros(int(sample_rate * TTS_LEAD_SILENCE), dtype=np.float32)
        tail = np.zeros(int(sample_rate * TTS_TAIL_SILENCE), dtype=np.float32)
        audio_np = np.concatenate([lead, audio_np.astype(np.float32), tail])
        duration = len(audio_np) / float(sample_rate)

        device = _speaker_device()
        try:
            sd.play(audio_np, samplerate=sample_rate, latency="high", device=device)
        except Exception as exc:
            if device is None:
                raise
            print(f"[-] Speaker '{SPEAKER_NAME}' failed ({exc}) - using the Windows default.")
            sd.play(audio_np, samplerate=sample_rate, latency="high")
        start = time.time()
        try:
            latency = float(sd.get_stream().latency or 0.0)
        except Exception:
            latency = 0.2
        # Wait for the whole clip (plus device latency) instead of trusting
        # stream.active, which goes false while audio is still buffered.
        while True:
            if STOP_TTS:
                print("[TTS] Skipped by user.")
                break
            if time.time() - start >= duration + latency + 0.1:
                break
            time.sleep(0.02)
    except Exception as e:
        print(f"[-] Playback error: {e}")
    finally:
        sd.stop()
        SPEAKING_IN_PROGRESS = False
        STOP_TTS = False
        emit("state", value="idle")

def speak(text, speaker_id=0, speed=1):
    global SPEAKING_IN_PROGRESS, STOP_TTS, SPEECH_VOLUME
    if not text:
        return

    print(f"\n[Nyx]: {text}")

    if piper_voice is None:
        print("[-] Piper TTS unavailable. Skipping speech.")
        return

    # Wait for the previous sentence to finish instead of dropping this one.
    deadline = time.time() + 60
    while SPEAKING_IN_PROGRESS and time.time() < deadline:
        time.sleep(0.05)

    SPEAKING_IN_PROGRESS = True
    STOP_TTS = False
    emit("state", value="speaking")

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
            out_rate = wf.getframerate()
            audio_bytes = wf.readframes(wf.getnframes())

        if not audio_bytes:
            print("[-] Piper synthesized empty audio buffer.")
            SPEAKING_IN_PROGRESS = False
            emit("state", value="idle")
            return

        audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        gain = SPEECH_VOLUME / 100.0
        if gain != 1.0:
            audio_np = np.clip(audio_np * gain, -1.0, 1.0)

        if speed != 1.0:
            try:
                audio_np = librosa.effects.time_stretch(audio_np, rate=speed)
            except Exception as e:
                print(f"[-] Speed adjustment failed: {e}. Using original speed.")

        thread = threading.Thread(target=_play_audio, args=(audio_np, out_rate))
        thread.daemon = True
        thread.start()

    except Exception as e:
        print(f"[-] Piper speech execution failed: {type(e).__name__} - {e}")
        SPEAKING_IN_PROGRESS = False
        STOP_TTS = False
        emit("state", value="idle")

def stop_speaking():
    """Interrupt the current TTS playback."""
    global STOP_TTS
    if SPEAKING_IN_PROGRESS:
        STOP_TTS = True
        sd.stop()
        return True
    return False

# -------------------------------------------------------------------
# 2. Speech Recognition (Faster-Whisper) + Silero VAD
# -------------------------------------------------------------------
stt_model = None
vad_model = None
STT_MODEL_NAME = None

def load_whisper():
    """Load the most accurate cached Whisper model the hardware can run."""
    global stt_model
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    attempts = []
    if STT_DEVICE == "cuda":
        attempts += [("cuda", "float16"), ("cuda", "int8_float16")]
    attempts += [("cpu", "int8")]
    for model_name in WHISPER_MODELS:
        for device, compute in attempts:
            try:
                stt_model = WhisperModel(
                    model_name, device=device, compute_type=compute, local_files_only=True,
                )
                global STT_MODEL_NAME
                STT_MODEL_NAME = model_name
                print(f"[+] Whisper '{model_name}' loaded on {device} ({compute}).")
                return
            except Exception as e:
                msg = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
                print(f"[-] Whisper '{model_name}' on {device}/{compute} unavailable: {msg}")
                # Model missing from cache: no point trying other devices.
                if "local" in msg.lower() or "snapshot" in msg.lower() or "not find" in msg.lower():
                    break
    raise RuntimeError(
        "No local Whisper model could be loaded. Download one first, e.g. "
        "py -3.11 -c \"from faster_whisper import WhisperModel; WhisperModel('large-v3')\""
    )

def load_vad():
    global vad_model
    try:
        vad_model = get_vad_model()
        print("[+] Silero VAD loaded.")
    except Exception as e:
        vad_model = None
        print(f"[-] Silero VAD unavailable ({e}); falling back to energy detection.")

def pcm_to_float(pcm_bytes):
    return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

def get_rms(chunk_data):
    arr = np.frombuffer(chunk_data, dtype=np.int16).astype(np.float32)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr * arr)))

def speech_probability(pcm_bytes):
    """Highest Silero speech probability in the given audio (0..1)."""
    if vad_model is None:
        return 1.0
    audio = pcm_to_float(pcm_bytes)
    usable = (audio.size // 512) * 512
    if usable == 0:
        return 0.0
    try:
        probs = vad_model(audio[-usable:].copy())
        return float(np.max(probs))
    except Exception:
        return 1.0

def preprocess_audio(pcm_bytes):
    """Clean up mic audio before Whisper: DC removal, rumble filter, gain."""
    audio = pcm_to_float(pcm_bytes)
    if audio.size == 0:
        return audio
    audio = audio - float(np.mean(audio))
    if _HIGHPASS_SOS is not None:
        audio = sosfilt(_HIGHPASS_SOS, audio).astype(np.float32)
    peak = float(np.max(np.abs(audio)))
    if 1e-4 < peak < 0.7:
        gain = min(0.7 / peak, 12.0)   # boost quiet speakers, cap noise boost
        audio = audio * gain
    return np.clip(audio, -1.0, 1.0).astype(np.float32)

def _clean_segment_text(text):
    return re.sub(r"\s+", " ", text or "").strip()

def transcribe_full(pcm_bytes):
    """Transcribe raw 16 kHz int16 PCM. Returns '' for noise/hallucinations."""
    audio = preprocess_audio(pcm_bytes)
    if audio.size < RATE * 0.25:
        return ""
    kwargs = dict(
        language="en",
        beam_size=5,
        best_of=5,
        temperature=[0.0, 0.2, 0.4, 0.6],
        condition_on_previous_text=False,
        without_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400, speech_pad_ms=250, threshold=0.45),
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        initial_prompt=WHISPER_CONTEXT_PROMPT,
        hotwords=WHISPER_HOTWORDS,
    )
    try:
        segments, _ = stt_model.transcribe(audio, **kwargs)
        segments = list(segments)
    except TypeError:
        kwargs.pop("hotwords", None)
        segments, _ = stt_model.transcribe(audio, **kwargs)
        segments = list(segments)

    kept = []
    for seg in segments:
        text = _clean_segment_text(seg.text)
        if not text:
            continue
        # Low-confidence segments that Whisper also thinks are silence: drop.
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.7:
            continue
        if seg.avg_logprob < -1.2:
            continue
        kept.append(text)

    result = " ".join(kept).strip()
    lowered = result.lower().strip(" .!?,")
    if not lowered or lowered in HALLUCINATIONS:
        return ""
    # Whisper sometimes echoes the whole prompt back on noise.
    prompt = WHISPER_CONTEXT_PROMPT.lower().strip(" .!?,")
    if difflib.SequenceMatcher(None, lowered, prompt).ratio() > 0.85:
        return ""
    return result

# -------------------------------------------------------------------
# Wake word handling (fuzzy, tolerant of Whisper's spellings)
# -------------------------------------------------------------------
def normalize_transcript(text):
    if not text:
        return ""
    normalized = text.lower().replace("_", " ")
    normalized = normalized.replace("'", "")
    normalized = "".join(ch for ch in normalized if ch.isalpha() or ch.isspace() or ch in ",!?.")
    normalized = " ".join(normalized.split())
    return normalized

def _is_wake_token(token, near_start):
    token = token.strip(",.!?")
    if token in ("nyx", "nix"):
        return True
    if not near_start:
        return False
    if token in WAKE_ALIASES:
        return True
    if 3 <= len(token) <= 5:
        best = max(difflib.SequenceMatcher(None, token, w).ratio() for w in ("nyx", "nix", "nyxs"))
        return best >= 0.8
    return False

def find_wake_word(text):
    """Return (start, end) word indices of the wake phrase, or None."""
    words = normalize_transcript(text).split()
    for i, word in enumerate(words):
        if _is_wake_token(word, near_start=i < 4):
            start = i - 1 if i > 0 and words[i - 1].strip(",.!?") in ("hey", "hi", "okay", "ok", "yo") else i
            return start, i + 1
    return None

def contains_wake_phrase(text):
    return find_wake_word(text) is not None

def strip_wake_word(text):
    """Remove the wake phrase so only the command remains."""
    span = find_wake_word(text)
    if span is None:
        return (text or "").strip()
    words = (text or "").split()
    # normalize_transcript may drop punctuation-only tokens; fall back safely.
    if len(words) != len(normalize_transcript(text).split()):
        return re.sub(r'^\W*(?:hey\s+)?\w+[\s,.!?]*', '', text or "", count=1).strip()
    remainder = words[:span[0]] + words[span[1]:]
    return " ".join(remainder).strip(" ,.!?") or "hello"

def extract_command_text(text):
    return strip_wake_word(text)

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
    return any(normalized.startswith(answer) for answer in short_answers)

# -------------------------------------------------------------------
# 3. Microphone, adaptive noise floor & recording
# -------------------------------------------------------------------
FORMAT = pyaudio.paInt16
CHANNELS = 1
CHUNK = 1280            # 80 ms at 16 kHz
RATE = 16000

audio_interface = None
mic_stream = None
MIC_DEVICE_INDEX = None
noise_floor = 120.0

def trigger_threshold():
    return max(MIN_TRIGGER_RMS, noise_floor * NOISE_MULTIPLIER)

def update_noise_floor(rms):
    """Track background noise slowly; ignore loud frames (likely speech)."""
    global noise_floor
    if rms < noise_floor * 2.0 or rms < MIN_TRIGGER_RMS:
        noise_floor = 0.97 * noise_floor + 0.03 * max(rms, 20.0)

def test_microphone(index, chunks=6):
    """Open a device at 16 kHz and return its loudest level over a few chunks,
    or None if it can't be opened. Several chunks, because many built-in mics
    return silence for the first one while they warm up."""
    try:
        test_stream = audio_interface.open(
            format=pyaudio.paInt16, channels=1, rate=RATE, input=True,
            input_device_index=index, frames_per_buffer=CHUNK,
        )
        try:
            return max(get_rms(test_stream.read(CHUNK, exception_on_overflow=False))
                       for _ in range(chunks))
        finally:
            test_stream.stop_stream()
            test_stream.close()
    except Exception:
        return None

def choose_microphone_index():
    preferred_names = ["hyperx", "duocast", "jbl", "steam streaming", "microphone"]
    candidates = []
    for i in range(audio_interface.get_device_count()):
        info = audio_interface.get_device_info_by_index(i)
        name = (info.get("name") or "").lower()
        if int(info.get("maxInputChannels", 0)) <= 0:
            continue
        if any(token in name for token in ["virtual", "mapper", "stereo mix", "loopback", "wave"]):
            continue
        rms = test_microphone(i)
        if rms is None or rms <= 5.0:
            continue
        score = sum(1 for token in preferred_names if token in name)
        candidates.append((score, rms, i, info.get("name")))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    _, rms_value, chosen, mic_name = candidates[0]
    print(f"[+] Selected microphone: {mic_name} (index {chosen}, rms={rms_value:.2f})")
    return chosen

def _find_input_by_name(name):
    """PyAudio index of the mic called `name` in Settings.
    Settings lists DirectSound names (full length), but the mic is opened through
    MME: DirectSound returns garbage samples at 16 kHz. MME cuts names to 31 chars."""
    try:
        api = audio_interface.get_host_api_info_by_type(pyaudio.paMME)["index"]
    except Exception:
        api = None
    for i in range(audio_interface.get_device_count()):
        info = audio_interface.get_device_info_by_index(i)
        dev = info.get("name") or ""
        if (dev == name or dev == name[:31]) and int(info.get("maxInputChannels", 0)) > 0                 and (api is None or info.get("hostApi") == api):
            return i
    return None

def _windows_default_mic():
    try:
        index = int(audio_interface.get_default_input_device_info()["index"])
        return index if test_microphone(index) is not None else None
    except Exception:
        return None

def open_microphone():
    global audio_interface, mic_stream, MIC_DEVICE_INDEX
    audio_interface = pyaudio.PyAudio()
    MIC_DEVICE_INDEX = None

    # Choice from Settings. If it's missing or broken: Windows default, then automatic.
    if MIC_NAME_SETTING == nyx_settings.MIC_WINDOWS_DEFAULT:
        MIC_DEVICE_INDEX = _windows_default_mic()
        if MIC_DEVICE_INDEX is not None:
            print("[+] Using the Windows default microphone (from Settings).")
    elif MIC_NAME_SETTING:
        index = _find_input_by_name(MIC_NAME_SETTING)
        if index is not None and test_microphone(index) is not None:
            MIC_DEVICE_INDEX = index
        else:
            print(f"[-] Microphone '{MIC_NAME_SETTING}' isn't working - trying the Windows default.")
            MIC_DEVICE_INDEX = _windows_default_mic()
    if MIC_DEVICE_INDEX is None:
        # Automatic: HyperX first...
        for i in range(audio_interface.get_device_count()):
            name = (audio_interface.get_device_info_by_index(i).get("name") or "").lower()
            if "hyperx" in name or "duocast" in name:
                MIC_DEVICE_INDEX = i
                break
    if MIC_DEVICE_INDEX is None:
        # Next choice: the mic set as default in Windows sound settings.
        try:
            default_index = int(audio_interface.get_default_input_device_info()["index"])
            if test_microphone(default_index) is not None:
                MIC_DEVICE_INDEX = default_index
                print("[+] Using the Windows default microphone.")
        except Exception:
            pass
    if MIC_DEVICE_INDEX is None:
        MIC_DEVICE_INDEX = choose_microphone_index()
    if MIC_DEVICE_INDEX is None:
        raise RuntimeError("No working microphone input device was found.")
    mic_name = audio_interface.get_device_info_by_index(MIC_DEVICE_INDEX).get("name")
    print(f"[+] Using mic '{mic_name}' at {RATE} Hz on index {MIC_DEVICE_INDEX}")
    mic_stream = audio_interface.open(
        format=FORMAT, channels=CHANNELS, rate=RATE, input=True,
        input_device_index=MIC_DEVICE_INDEX, frames_per_buffer=CHUNK,
    )
    emit("mic_name", value=mic_name)

def set_microphone(name):
    """Switch mic (from Settings). The listening loop reopens it between commands."""
    global MIC_NAME_SETTING, _MIC_SWITCH_PENDING
    MIC_NAME_SETTING = name or ""
    _MIC_SWITCH_PENDING = True

def _reopen_microphone():
    global mic_stream, _MIC_SWITCH_PENDING
    _MIC_SWITCH_PENDING = False
    emit("state", value="loading", detail="Switching microphone")
    try:
        mic_stream.stop_stream()
        mic_stream.close()
    except Exception:
        pass
    try:
        audio_interface.terminate()   # a fresh PyAudio also sees newly plugged-in mics
    except Exception:
        pass
    mic_stream = None
    try:
        open_microphone()
        calibrate_noise()
    except Exception as exc:
        print(f"[-] Could not switch microphone: {exc}")
        emit("reply", text=f"I couldn't open a microphone: {exc}")
    emit("state", value="idle")

def calibrate_noise(seconds=1.2):
    """Measure room noise at startup so the trigger level fits the room."""
    global noise_floor
    levels = []
    for _ in range(int(RATE / CHUNK * seconds)):
        levels.append(get_rms(mic_stream.read(CHUNK, exception_on_overflow=False)))
    if levels:
        noise_floor = max(20.0, float(np.percentile(levels, 60)))
    print(f"[+] Noise floor {noise_floor:.0f} RMS -> trigger at {trigger_threshold():.0f} RMS")

def flush_mic():
    """Discard audio buffered while we weren't reading (e.g. our own voice)."""
    try:
        available = mic_stream.get_read_available()
        while available >= CHUNK:
            mic_stream.read(CHUNK, exception_on_overflow=False)
            available = mic_stream.get_read_available()
    except Exception:
        pass

def record_until_silence(prefix_frames=None):
    """Record until Silero VAD hears SILENCE_DURATION of non-speech."""
    print("[+] Listening... Speak your command now!")
    emit("state", value="listening")
    frames = list(prefix_frames) if prefix_frames else []
    max_chunks = int(RATE / CHUNK * MAX_RECORD_TIME)
    silence_limit = max(1, int(RATE / CHUNK * SILENCE_DURATION))
    no_speech_limit = int(RATE / CHUNK * NO_SPEECH_TIMEOUT)
    has_spoken = bool(frames) and speech_probability(b"".join(frames[-4:])) >= VAD_SPEECH_PROB
    silent_chunks = 0
    for n in range(max_chunks):
        data = mic_stream.read(CHUNK, exception_on_overflow=False)
        frames.append(data)
        rms = get_rms(data)
        emit("level", rms=rms, threshold=trigger_threshold())
        # Score the newest chunk with a little context from the previous one.
        prob = speech_probability(b"".join(frames[-2:]))
        is_speech = prob >= VAD_SPEECH_PROB or (vad_model is None and rms > trigger_threshold())
        if is_speech:
            has_spoken = True
            silent_chunks = 0
        elif has_spoken:
            silent_chunks += 1
        if has_spoken and silent_chunks >= silence_limit:
            print("[+] End of speech detected.")
            break
        if not has_spoken and n >= no_speech_limit:
            print("[-] No speech after trigger.")
            return b""
    return b"".join(frames)

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

SITE_ALIASES = {
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
    "microsoft": "https://www.microsoft.com",
}

def safe_open_link(command_text):
    text = (command_text or "").strip()
    lower_text = text.lower()
    site_aliases = SITE_ALIASES
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
    site_aliases = SITE_ALIASES
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
# Application opener
# Installed apps are indexed once in the background at start-up, so
# "open discord" is a dictionary lookup instead of a disk scan.
# -------------------------------------------------------------------
APP_ALIASES = {
    "notepad": "notepad.exe",
    "calc": "calc.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "snipping tool": "snippingtool.exe",
    "task manager": "taskmgr.exe",
    "control panel": "control.exe",
    "settings": "ms-settings:",
    "browser": "msedge.exe",
    "edge": "msedge.exe",
    "vscode": "code.exe",
    "vs code": "code.exe",
}

_APP_INDEX = {}                       # lowercase name -> launch target
_APP_INDEX_LOCK = threading.Lock()
_APP_INDEX_READY = threading.Event()
_APP_INDEX_BUILDING = threading.Event()
_APP_JUNK_WORDS = ("uninstall", "uninst", "setup", "installer", "update", "helper",
                   "crashpad", "crash reporter", "readme", "license", "release notes")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _walk_limited(base, exts, max_depth):
    """Yield files ending in exts, never descending deeper than max_depth."""
    base = os.path.normpath(base)
    base_depth = base.count(os.sep)
    for root, dirs, files in os.walk(base):
        if root.count(os.sep) - base_depth >= max_depth:
            dirs[:] = []  # prune here so os.walk doesn't visit the subtree
        for f in files:
            if f.lower().endswith(exts):
                yield os.path.join(root, f)


def _is_junk_app(name):
    n = name.lower()
    return any(w in n for w in _APP_JUNK_WORDS)


def build_app_index():
    """Scan Start Menu, Desktop, Store apps and Program Files once."""
    _APP_INDEX_BUILDING.set()
    started = time.time()
    env = os.environ.get
    index = {}
    try:
        # Lowest priority first: bare .exe files (only first match per name kept).
        for base in (env("ProgramFiles", r"C:\Program Files"),
                     env("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.path.join(env("LOCALAPPDATA", ""), "Programs")):
            if base and os.path.isdir(base):
                for path in _walk_limited(base, (".exe",), 3):
                    name = os.path.splitext(os.path.basename(path))[0]
                    if not _is_junk_app(name):
                        index.setdefault(name.lower(), path)

        # Everything in the Start menu, including Microsoft Store apps.
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
                capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
            ).stdout
            for line in out.splitlines():
                name, _, app_id = line.partition("|")
                name, app_id = name.strip(), app_id.strip()
                if name and app_id and not _is_junk_app(name):
                    index[name.lower()] = "shell:AppsFolder\\" + app_id
        except Exception as exc:
            print(f"[apps] Get-StartApps failed: {exc}")

        # Highest priority: real shortcuts (Start Menu + Desktop).
        for base in (os.path.join(env("ProgramData", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
                     os.path.join(env("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
                     os.path.join(env("USERPROFILE", ""), "Desktop"),
                     os.path.join(env("PUBLIC", ""), "Desktop")):
            if base and os.path.isdir(base):
                for path in _walk_limited(base, (".lnk", ".url"), 3):
                    name = os.path.splitext(os.path.basename(path))[0]
                    if not _is_junk_app(name):
                        index[name.lower()] = path
    finally:
        with _APP_INDEX_LOCK:
            _APP_INDEX.clear()
            _APP_INDEX.update(index)
        _APP_INDEX_READY.set()
        _APP_INDEX_BUILDING.clear()
        print(f"[apps] Indexed {len(index)} applications in {time.time() - started:.1f}s.")


def refresh_app_index():
    """Rebuild the index in the background (picks up newly installed apps)."""
    if not _APP_INDEX_BUILDING.is_set():
        threading.Thread(target=build_app_index, daemon=True).start()


def _find_app(app_name, exact_only=False):
    key = app_name.lower().strip()
    with _APP_INDEX_LOCK:
        if key in _APP_INDEX:
            return _APP_INDEX[key]
        if exact_only:
            return None
        items = list(_APP_INDEX.items())
    tests = [lambda n: n.startswith(key),
             lambda n: re.search(rf"\b{re.escape(key)}\b", n)]
    if len(key) >= 3:
        tests.append(lambda n: key in n)
    for test in tests:
        hits = [(n, p) for n, p in items if test(n)]
        if hits:
            return min(hits, key=lambda h: len(h[0]))[1]  # shortest name = best match
    close = difflib.get_close_matches(key, [n for n, _ in items], n=1, cutoff=0.85)
    if close:
        with _APP_INDEX_LOCK:
            return _APP_INDEX.get(close[0])
    return None


def _launch_target(target):
    try:
        if target.lower().startswith("shell:appsfolder"):
            subprocess.Popen(["explorer.exe", target], creationflags=_NO_WINDOW)
        elif target.lower().endswith(".exe") and os.path.isabs(target):
            os.startfile(target, cwd=os.path.dirname(target))
        else:
            os.startfile(target)
        return True
    except Exception as exc:
        print(f"[apps] Failed to launch {target}: {exc}")
        return False


def open_application(command_text):
    match = re.search(r'\b(?:open|launch|start)\s+(.+?)(?:\s+app)?\s*$', command_text, re.IGNORECASE)
    if not match:
        return None
    app_name = re.sub(r'[.,!?;:]+$', '', match.group(1)).strip()
    app_name = re.sub(r'\s*\bapp$', '', app_name, flags=re.IGNORECASE).strip()
    if not app_name or '://' in app_name or app_name.lower().startswith('http'):
        return None
    key = app_name.lower()

    # 1. Built-in Windows tools.
    alias = APP_ALIASES.get(key)
    if alias:
        target = alias if alias.endswith(":") else shutil.which(alias)
        if target and _launch_target(target):
            return f"Opening {app_name}."

    # 2. Installed apps (index is normally ready long before the first command).
    if not _APP_INDEX_READY.is_set():
        if not _APP_INDEX_BUILDING.is_set():
            refresh_app_index()
        _APP_INDEX_READY.wait(timeout=30)
    # Names that are also websites ("google", "youtube") only open an app on an
    # exact match, so "open google" doesn't launch Google Chrome.
    target = _find_app(app_name, exact_only=key in SITE_ALIASES)
    if target and _launch_target(target):
        return f"Opening {app_name}."

    # 3. Anything on PATH.
    target = shutil.which(app_name) or shutil.which(app_name + ".exe")
    if target and _launch_target(target):
        return f"Opening {app_name}."

    if key not in SITE_ALIASES:
        refresh_app_index()  # maybe it was installed after start-up
    return None

# -------------------------------------------------------------------
# PC shutdown abort (voice, GUI button, console spacebar)
# -------------------------------------------------------------------
SHUTDOWN_SECONDS = 20
POWER_ACTION = "shutdown"   # what the pending confirmation / countdown is for: "shutdown" or "restart"
_SYSTEM_RESTART_RE = re.compile(
    r"\b(?:restart|reboot)\s+(?:the\s+|my\s+|this\s+)?(?:system|computer|pc|laptop|windows)\b"
    r"|\b(?:system|computer|pc|laptop)\s+(?:restart|reboot)\b",
    re.IGNORECASE,
)

def request_power_action(action):
    """Ask for confirmation before shutting down or restarting the PC."""
    global SHUTDOWN_CONFIRMATION_PENDING, POWER_ACTION
    POWER_ACTION = action
    SHUTDOWN_CONFIRMATION_PENDING = True
    return "Confirmation required."

def start_power_countdown():
    """Confirmed: shut down / restart the PC after SHUTDOWN_SECONDS."""
    global SHUTDOWN_CONFIRMATION_PENDING, SHUTDOWN_COUNTDOWN_ACTIVE
    SHUTDOWN_CONFIRMATION_PENDING = False
    SHUTDOWN_COUNTDOWN_ACTIVE = True
    flag, verb = ("/r", "Restarting") if POWER_ACTION == "restart" else ("/s", "Shutting down")
    try:
        subprocess.run(["shutdown", flag, "/t", str(SHUTDOWN_SECONDS),
                        "/c", f"System {POWER_ACTION} confirmed."], check=False)
        emit("shutdown", active=True, deadline=time.time() + SHUTDOWN_SECONDS, action=POWER_ACTION)
        return f"{verb} the system in {SHUTDOWN_SECONDS} seconds. Say 'abort' to cancel."
    except Exception as exc:
        SHUTDOWN_COUNTDOWN_ACTIVE = False
        return f"Failed to {POWER_ACTION}: {exc}"

def abort_shutdown():
    global SHUTDOWN_CONFIRMATION_PENDING, SHUTDOWN_COUNTDOWN_ACTIVE
    was_counting = SHUTDOWN_COUNTDOWN_ACTIVE
    SHUTDOWN_CONFIRMATION_PENDING = False
    SHUTDOWN_COUNTDOWN_ACTIVE = False
    emit("shutdown", active=False, aborted=was_counting, action=POWER_ACTION)
    name = POWER_ACTION.capitalize()
    if not was_counting:
        return f"{name} cancelled."
    try:
        subprocess.run(["shutdown", "/a"], check=False, capture_output=True)
        return f"{name} Aborted."
    except Exception as exc:
        return f"Failed to cancel {POWER_ACTION}: {exc}"

# -------------------------------------------------------------------
# Minimize / restore (the GUI window, or the console window)
# -------------------------------------------------------------------
def set_window(action):
    if EVENT_SINK is not None:
        emit("window", action=action)
    elif sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 6 if action == "minimize" else 9)
                if action != "minimize":
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception as exc:
            print(f"[-] Window control failed: {exc}")
    return "Minimizing." if action == "minimize" else "I'm back."

# -------------------------------------------------------------------
# Toggle Listening Mode
# -------------------------------------------------------------------
def toggle_listening():
    global LISTENING_ENABLED
    LISTENING_ENABLED = not LISTENING_ENABLED
    if LISTENING_ENABLED:
        speak("Microphone on")
    else:
        speak("Microphone off")

# -------------------------------------------------------------------
# Main tool dispatcher
# -------------------------------------------------------------------
def handle_local_tools(command_text):
    global DEEP_MODE_PREFIX, SHUTDOWN_CONFIRMATION_PENDING, SHUTDOWN_COUNTDOWN_ACTIVE
    global last_assistant_reply
    text = (command_text or "").strip()
    if not text:
        return None

    print(f"[DEBUG] raw command_text: '{text}'")

    wake_pattern = r'^(?:hey\s+)?(?:nix|nyx)\s*,?\s*'
    text = re.sub(wake_pattern, '', text, flags=re.IGNORECASE).strip()

    clean_text = re.sub(r'[.,!?;:]+$', '', text).strip()
    lower_text = clean_text.lower()
    print(f"[DEBUG] cleaned lower_text: '{lower_text}'")

    # --- Answer to "What volume/brightness level would you like?" ---
    global PENDING_LEVEL
    pending, PENDING_LEVEL = PENDING_LEVEL, None
    if pending:
        bare_number = re.fullmatch(r'(?:to\s+|at\s+)?(\d{1,3})\s*(?:%|percent)?', lower_text)
        if bare_number:
            lower_text = f"{pending} {bare_number.group(1)}"

    # --- Answer to "Delete the whole chat history... Are you sure?" ---
    global DELETE_LOG_CONFIRMATION_PENDING
    if DELETE_LOG_CONFIRMATION_PENDING:
        DELETE_LOG_CONFIRMATION_PENDING = False
        if re.search(r"\b(?:no|nope|nah|cancel|keep|don'?t|stop)\b", lower_text):
            return "Okay, I kept the chat history."
        if re.search(r'\b(?:yes|yeah|yep|sure|confirm|confirmed|do it|delete it|go ahead)\b', lower_text):
            return delete_chat_log()
        # Anything else: forget the question and handle it as a normal command.

    # --- DELETE CHAT LOG (asks first) ---
    if re.search(r'\b(?:delete|clear|erase|wipe|remove)\s+(?:the\s+|my\s+|all\s+)?'
                 r'(?:chat\s+(?:log|history)|previous\s+chats?|conversation\s+history|chats?\s+history)\b',
                 lower_text):
        DELETE_LOG_CONFIRMATION_PENDING = True
        return "This deletes the whole chat history and can't be undone. Are you sure?"

    # --- CHAT LOG ON / OFF ---
    log_switch = re.search(r'\b(?:turn|switch)\s+(on|off)\s+(?:the\s+)?chat\s+(?:log|history)\b'
                           r'|\b(enable|disable)\s+(?:the\s+)?chat\s+(?:log|history)\b'
                           r'|\bchat\s+(?:log|history)\s+(on|off)\b', lower_text)
    if log_switch:
        word = next(g for g in log_switch.groups() if g)
        return set_chat_log_enabled(word in ("on", "enable"))

    # --- MINIMIZE / RESTORE WINDOW ---
    if re.search(r'\bminimi[sz]e\b|\bhide\s+(?:yourself|the\s+app|the\s+window)\b', lower_text):
        return set_window("minimize")
    if re.search(r'\b(?:restore|maximi[sz]e|show\s+yourself|show\s+the\s+app|come\s+back|full\s*screen)\b', lower_text):
        return set_window("restore")

    # --- SPEECH VOLUME CONTROL (handled locally) ---
    vol_match = re.search(r'(?:set\s+)?speech\s+volume\s+(?:to\s+)?(\d{1,3})\s*%?', lower_text)
    if vol_match:
        level = int(vol_match.group(1))
        return set_speech_volume(level)

    # --- SYSTEM VOLUME CONTROL ---
    if re.search(r'\b(?:volume|vol)\b', lower_text):
        # Any number in the sentence: "volume 50", "volume level at 50", "50% volume"...
        vol_match = re.search(r'\b(\d{1,3})\b', lower_text)
        if vol_match:
            level = int(vol_match.group(1))
            if 0 <= level <= 100:
                return set_system_volume(level)
            else:
                return "Volume must be between 0 and 100."
        else:
            PENDING_LEVEL = "volume"
            return "What volume level would you like? Please say a number between 0 and 100."

    # --- BRIGHTNESS CONTROL ---
    if re.search(r'\b(?:brightness|bright)\b', lower_text):
        # Any number in the sentence: "brightness 50", "brightness level 50", "50% brightness"...
        bright_match = re.search(r'\b(\d{1,3})\b', lower_text)
        if bright_match:
            level = int(bright_match.group(1))
            if 0 <= level <= 100:
                return set_system_brightness(level)
            else:
                return "Brightness must be between 0 and 100."
        else:
            PENDING_LEVEL = "brightness"
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
            return abort_shutdown()

    # --- PENDING SHUTDOWN CONFIRMATION ---
    if SHUTDOWN_CONFIRMATION_PENDING:
        if re.search(r'\b(confirm|confirmed|yes)\b', lower_text):
            return start_power_countdown()
        elif re.search(r'\b(no|nope|cancel|abort|override)\b', lower_text):
            return abort_shutdown()
        else:
            return "Please confirm with 'confirm' or cancel with 'abort'."

    # PC restart ("restart the computer") - checked before Nyx's own restart.
    if _SYSTEM_RESTART_RE.search(lower_text):
        return request_power_action("restart")

    if is_restart_request(lower_text):
        return "__RESTART__"

    if re.search(r'\b(bye|goodbye|exit|quit|shutdown|close yourself|close the app)\b', lower_text):
        if re.search(r'\bsystem\b', lower_text):
            return request_power_action("shutdown")
        return "__EXIT__"

    if re.search(r'\b(normal|standard|regular)\b', lower_text):
        DEEP_MODE_PREFIX = "normal"
        emit("mode", deep=False)
        return "Back to normal mode."

    if re.search(r'\bdeep\b|\bthink\b|\bsmart model\b|\bdeep mode\b', lower_text):
        DEEP_MODE_PREFIX = "gwen"
        emit("mode", deep=True)
        return "Entered deep thinking."

    if is_time_request(lower_text):
        return get_current_time_reply()

    # --- READ CHAT LOG (full) ---
    if re.search(r'\b(?:read|show|open)\s+(?:full\s+chat\s+log|full\s+previous\s+chats?)\b', lower_text):
        return read_chat_log(full=True)

    # --- READ CHAT LOG (last 10 exchanges) ---
    if re.search(r'\b(?:read|show|open)\s+(?:previous\s+chats?|chat\s+log|the\s+text\s+file)\b', lower_text):
        return read_chat_log(full=False)

    # --- READ ANY FILE ---
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

def _ollama_generate(url, payload):
    """Stream a reply from Ollama, giving up as soon as "end task" is requested.
    Closing the connection also makes Ollama stop generating."""
    payload = dict(payload, stream=True)
    with requests.post(url, json=payload, timeout=90, stream=True) as response:
        if response.status_code != 200:
            return f"Ollama HTTP Error: {response.status_code}"
        parts = []
        for line in response.iter_lines():
            if CANCEL_THINKING.is_set():
                return ""
            if not line:
                continue
            chunk = json.loads(line)
            parts.append(chunk.get("response", ""))
            if chunk.get("done"):
                break
        return "".join(parts).strip()

_smart_model_installed = None

def smart_model_available():
    """True if SMART_MODEL is downloaded in Ollama. Checked once, then cached."""
    global _smart_model_installed
    if _smart_model_installed is None:
        try:
            tags = requests.get("http://localhost:11434/api/tags", timeout=3).json()
            names = {m.get("name", "") for m in tags.get("models", [])}
            _smart_model_installed = SMART_MODEL in names or f"{SMART_MODEL}:latest" in names
        except Exception:
            return False   # Ollama not reachable right now; check again next time
    return _smart_model_installed

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
    if selected_model == SMART_MODEL and not smart_model_available():
        selected_model = FAST_MODEL
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
        return _ollama_generate(url, payload)
    except Exception as err:
        if selected_model == SMART_MODEL:
            try:
                fallback_payload = {
                    "model": FAST_MODEL,
                    "prompt": prompt,
                    "system": system_instructions,
                    "stream": False,
                }
                fallback_reply = _ollama_generate(url, fallback_payload)
                if not fallback_reply.startswith("Ollama HTTP Error"):
                    return fallback_reply
            except Exception:
                pass
        return f"Error connecting to Ollama backend: {err}"

# -------------------------------------------------------------------
# Engine start-up
# -------------------------------------------------------------------
ENGINE_READY = False

def init_engine(greet=True):
    """Load models and open the microphone. Safe to call once."""
    global ENGINE_READY
    if ENGINE_READY:
        return
    refresh_app_index()  # index installed apps in the background
    emit("state", value="loading", detail="Loading voice")
    load_piper()
    emit("state", value="loading", detail="Loading speech recognition")
    load_whisper()
    load_vad()
    emit("state", value="loading", detail="Opening microphone")
    open_microphone()
    emit("state", value="loading", detail="Measuring room noise")
    calibrate_noise()
    ENGINE_READY = True
    emit("state", value="idle")
    if greet:
        speak("Hello sir, all systems are online.")

def shutdown_engine():
    try:
        if mic_stream is not None:
            mic_stream.stop_stream()
            mic_stream.close()
        if audio_interface is not None:
            audio_interface.terminate()
    except Exception:
        pass

# -------------------------------------------------------------------
# Command processing (shared by voice and typed input)
# -------------------------------------------------------------------
followup_window_until = 0.0
post_speak_cooldown_until = 0.0

def set_listening(enabled):
    global LISTENING_ENABLED
    LISTENING_ENABLED = bool(enabled)
    emit("mic", enabled=LISTENING_ENABLED)

# -------------------------------------------------------------------
# Restart: "Nyx, restart" closes Nyx and starts a fresh copy of it
# -------------------------------------------------------------------
RESTART_REQUESTED = False
_RESTART_RE = re.compile(
    r"^\s*(?:please\s+)?(?:restart|reboot|relaunch)(?:\s+(?:please|now))?\s*$"
    r"|\b(?:restart|reboot|relaunch)\s+(?:yourself|nyx|nix|the\s+(?:assistant|app))\b",
    re.IGNORECASE,
)

def is_restart_request(text):
    return bool(_RESTART_RE.search(re.sub(r"[.,!?]", "", text or "")))

def relaunch_command():
    """Command line that starts this program again the same way it was started."""
    return [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:]

# -------------------------------------------------------------------
# "End task": interrupt Nyx while it is thinking
# -------------------------------------------------------------------
CANCEL_THINKING = threading.Event()
THINKING_IN_PROGRESS = False
USE_KEYBOARD = False
END_TASK_RE = re.compile(
    r"\b(?:end|stop|cancel|abort|kill)\s+(?:the\s+|this\s+|that\s+)?(?:task|thinking|request|question)\b"
    r"|\bstop\s+thinking\b|\bcancel\s+(?:that|it)\b|\bnever\s*mind\b|\bforget\s+it\b",
    re.IGNORECASE,
)

def is_end_task(text):
    return bool(END_TASK_RE.search(text or ""))

def cancel_thinking():
    """Stop the question Nyx is working on. Returns True if there was one."""
    if THINKING_IN_PROGRESS:
        CANCEL_THINKING.set()
        print("[!] End task requested.")
        return True
    return False

def _poll_for_cancel():
    """While thinking: watch the console key E/Esc and listen for "end task"."""
    if USE_KEYBOARD and msvcrt.kbhit():
        if msvcrt.getch() in (b"e", b"E", b"\x1b"):
            cancel_thinking()
        return
    if not LISTENING_ENABLED or mic_stream is None:
        time.sleep(0.05)
        return
    pcm = mic_stream.read(CHUNK, exception_on_overflow=False)  # also paces the loop
    rms = get_rms(pcm)
    if rms < trigger_threshold() or speech_probability(pcm) < VAD_SPEECH_PROB:
        return
    recorded = record_until_silence(prefix_frames=[pcm])
    if THINKING_IN_PROGRESS and not CANCEL_THINKING.is_set():
        emit("state", value="thinking")
    if not recorded or not THINKING_IN_PROGRESS:
        return
    heard = transcribe_full(recorded)
    print(f"[While thinking] Heard: {heard}")
    if is_end_task(heard):
        cancel_thinking()

def think_cancellable(prompt):
    """Ask Ollama in the background. Returns None if the user ended the task."""
    global THINKING_IN_PROGRESS
    CANCEL_THINKING.clear()
    result = {}
    worker = threading.Thread(target=lambda: result.update(reply=ask_ollama_dual(prompt)), daemon=True)
    THINKING_IN_PROGRESS = True
    worker.start()
    try:
        while worker.is_alive() and not CANCEL_THINKING.is_set():
            _poll_for_cancel()
    finally:
        THINKING_IN_PROGRESS = False
    if CANCEL_THINKING.is_set():
        return None
    return result.get("reply", "")

def process_command(command_text):
    """Run one command. Returns False if the assistant should exit or restart."""
    global last_assistant_reply, followup_window_until, post_speak_cooldown_until, RESTART_REQUESTED

    command_text = (command_text or "").strip()
    if not command_text:
        return True
    emit("user", text=command_text)
    lower = command_text.lower()

    if re.search(r'\b(?:microphone|mic)\s+on\b|\bturn\s+on\s+(?:the\s+)?(?:microphone|mic)\b', lower):
        if not LISTENING_ENABLED:
            set_listening(True)
            speak("Microphone on")
        else:
            speak("Already on")
        return True
    if re.search(r'\b(?:microphone|mic)\s+off\b|\bturn\s+off\s+(?:the\s+)?(?:microphone|mic)\b', lower):
        if LISTENING_ENABLED:
            set_listening(False)
            speak("Microphone off")
        else:
            speak("Already off")
        return True

    if is_end_task(command_text):
        # Nothing is thinking right now (a running task is cancelled before it
        # gets here), so just stop talking if Nyx is mid-sentence.
        reply = "Stopped." if stop_speaking() else "There's nothing running to stop."
        emit("reply", text=reply)
        if reply != "Stopped.":
            speak(reply)
        emit("state", value="idle")
        post_speak_cooldown_until = time.time() + POST_SPEAK_COOLDOWN
        followup_window_until = 0.0
        return True

    emit("state", value="thinking")
    reply = handle_local_tools(command_text)
    if reply is None:
        reply = think_cancellable(command_text)
        if reply is None:
            print("[!] Task ended by user.")
            emit("reply", text="Task ended.")
            speak("Okay, stopped.")
            if piper_voice is None:
                emit("state", value="idle")
            post_speak_cooldown_until = time.time() + POST_SPEAK_COOLDOWN
            followup_window_until = 0.0
            return True

    if reply == "__RESTART__":
        RESTART_REQUESTED = True
        emit("reply", text="Restarting…")
        speak("Restarting.")
        print("\n[Restarting] Restart requested by user.")
        while SPEAKING_IN_PROGRESS:
            time.sleep(0.05)
        return False

    if reply == "__EXIT__":
        emit("reply", text="Goodbye sir.")
        speak("Goodbye sir.")
        print("\n[Exiting] Assistant shutdown requested by user.")
        while SPEAKING_IN_PROGRESS:
            time.sleep(0.05)
        return False

    if reply.startswith("CHAT_LOG:"):
        log_content = reply[9:]
        print("\n" + "=" * 60 + "\nCHAT LOG CONTENTS:\n" + "=" * 60)
        print(log_content)
        print("=" * 60 + "\n")
        emit("reply", text=log_content, kind="log")
        last_assistant_reply = "Chat log printed."
        chat_history.append({"role": "user", "content": command_text})
        chat_history.append({"role": "assistant", "content": last_assistant_reply})
        log_conversation(command_text, last_assistant_reply)
        post_speak_cooldown_until = time.time() + POST_SPEAK_COOLDOWN
        followup_window_until = 0.0
        emit("state", value="idle")
        return True

    if reply:
        last_assistant_reply = reply
        chat_history.append({"role": "user", "content": command_text})
        chat_history.append({"role": "assistant", "content": reply})
        log_conversation(command_text, reply)
        emit("reply", text=reply)

    speak(reply)
    if piper_voice is None:
        emit("state", value="idle")

    post_speak_cooldown_until = time.time() + POST_SPEAK_COOLDOWN
    if SHUTDOWN_CONFIRMATION_PENDING:
        followup_window_until = time.time() + 60.0
    elif str(reply).rstrip().endswith('?'):
        followup_window_until = time.time() + 15.0
    else:
        followup_window_until = 0.0
    return True

def _handle_keyboard():
    """Console hotkeys: space = abort shutdown, M = mic toggle, S = skip speech.
    (E or Esc ends a task while Nyx is thinking - see _poll_for_cancel.)"""
    global SHUTDOWN_CONFIRMATION_PENDING, SHUTDOWN_COUNTDOWN_ACTIVE
    global followup_window_until, post_speak_cooldown_until
    if not msvcrt.kbhit():
        return
    key = msvcrt.getch()
    if key == b' ':
        if SHUTDOWN_CONFIRMATION_PENDING or SHUTDOWN_COUNTDOWN_ACTIVE:
            speak(abort_shutdown())
        else:
            # Still cancels a shutdown/restart that wasn't started by Nyx.
            try:
                if subprocess.run(["shutdown", "/a"], capture_output=True).returncode == 0:
                    speak("Aborted")
            except Exception as exc:
                print(f"Error checking spacebar abort: {exc}")
    elif key in (b'm', b'M'):
        set_listening(not LISTENING_ENABLED)
        speak("Microphone on" if LISTENING_ENABLED else "Microphone off")
    elif key in (b's', b'S'):
        if stop_speaking():
            followup_window_until = 0.0
            post_speak_cooldown_until = time.time() + 0.5
            print("[TTS] Skip requested.")
        else:
            print("[TTS] No speech to skip.")

# -------------------------------------------------------------------
# Main Continuous Background Loop
# -------------------------------------------------------------------
def run_loop(stop_event=None, text_queue=None, use_keyboard=False):
    """Listen forever (or until stop_event is set / user says goodbye)."""
    global post_speak_cooldown_until, USE_KEYBOARD
    USE_KEYBOARD = use_keyboard

    print(f"\n[Ready] System active! Listening for wake word: '{WAKE_WORD}'...")
    last_trigger_time = 0.0
    pre_roll_buffer = deque(maxlen=PRE_ROLL_CHUNKS)
    was_speaking = False
    level_tick = 0

    while stop_event is None or not stop_event.is_set():
        if use_keyboard:
            _handle_keyboard()

        # Typed commands from the GUI skip the wake word.
        if text_queue is not None:
            try:
                typed = text_queue.get_nowait()
            except Exception:
                typed = None
            if typed:
                if not process_command(typed):
                    break
                continue

        if SPEAKING_IN_PROGRESS:
            was_speaking = True
            time.sleep(0.05)
            continue

        if _MIC_SWITCH_PENDING:
            _reopen_microphone()
            pre_roll_buffer.clear()
            continue
        if mic_stream is None:   # switching failed and no mic is open
            time.sleep(0.2)
            continue

        now = time.time()
        if now < post_speak_cooldown_until:
            time.sleep(0.05)
            continue

        if was_speaking:
            # Drop audio captured while we were talking so we don't hear ourselves.
            flush_mic()
            pre_roll_buffer.clear()
            was_speaking = False

        pcm_data = mic_stream.read(CHUNK, exception_on_overflow=False)
        rms = get_rms(pcm_data)
        pre_roll_buffer.append(pcm_data)

        level_tick += 1
        if level_tick % 2 == 0:
            emit("level", rms=rms, threshold=trigger_threshold())
        if WAKE_WORD_DEBUG and level_tick % 20 == 0:
            print(f"[DEBUG] mic_rms={rms:.0f} floor={noise_floor:.0f} trigger={trigger_threshold():.0f}")

        if now - last_trigger_time < LISTEN_COOLDOWN_SECONDS:
            update_noise_floor(rms)
            continue

        followup_allowed = (now < followup_window_until) or SHUTDOWN_COUNTDOWN_ACTIVE

        if rms < trigger_threshold():
            update_noise_floor(rms)
            continue

        # Loud enough - make sure it is actually a voice, not a click or bump.
        if speech_probability(b"".join(list(pre_roll_buffer)[-3:])) < VAD_SPEECH_PROB:
            continue

        print(f"\n[!] {'Follow-up' if followup_allowed else 'Speech'} detected (RMS={rms:.0f}).")
        last_trigger_time = now

        recorded_pcm = record_until_silence(prefix_frames=list(pre_roll_buffer))
        pre_roll_buffer.clear()
        if not recorded_pcm:
            emit("state", value="idle")
            continue

        emit("state", value="transcribing")
        print("[+] Transcribing speech to text...")
        t0 = time.time()
        user_text = transcribe_full(recorded_pcm)
        print(f"[+] Transcribed in {time.time() - t0:.2f}s")

        if not user_text:
            print("[-] No clear speech detected.")
            emit("state", value="idle")
            continue

        print(f"[User Prompt]: {user_text}")
        emit("heard", text=user_text)

        if followup_allowed:
            command_text = strip_wake_word(user_text) if contains_wake_phrase(user_text) else user_text.strip()
            print("[+] Follow-up mode: accepted command without wake word.")
        elif contains_wake_phrase(user_text):
            command_text = strip_wake_word(user_text)
        else:
            print("[-] Heard speech, but no wake phrase detected.")
            emit("state", value="idle")
            continue

        if not LISTENING_ENABLED and not re.search(r'\b(?:mic|microphone)\b', command_text.lower()):
            print("[-] Microphone is off. Say 'microphone on' to enable.")
            emit("state", value="idle")
            continue

        if not process_command(command_text):
            break

        print(f"\n[Ready] Listening for '{WAKE_WORD}' again...")

    emit("state", value="stopped")


if __name__ == "__main__":
    try:
        init_engine()
        run_loop(use_keyboard=True)
    except KeyboardInterrupt:
        print("\n[Exiting] Shutting down background streams...")
    finally:
        shutdown_engine()
    if RESTART_REQUESTED:
        # The new copy shares this console window.
        subprocess.Popen(relaunch_command(), cwd=str(APP_DIR))
        os._exit(0)
