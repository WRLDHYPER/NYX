"""
assistant_core.py - Voice assistant backend for Nyx/Nix.
Thread-safe: audio stream is opened in the same thread that runs the loop.
"""
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
import threading
import queue
from pathlib import Path
from collections import deque

import numpy as np
import pyaudio
import requests
from piper import PiperVoice
from faster_whisper import WhisperModel

# -------------------------------------------------------------------
# Default Configuration
# -------------------------------------------------------------------
DEFAULT_CONFIG = {
    "fast_model": "dolphin-llama3:8b",
    "smart_model": "qwen2.5:14b",
    "smart_model_triggers": [
        "deep think", "think deeply", "deep reasoning", "reason step by step",
        "use the smart model", "smart model", "analyze deeply", "be thoughtful",
        "think", "deep", "deep mode",
    ],
    "wake_word": "nix/nyx",
    "wake_word_threshold": 600.0,
    "stt_device": "cuda",
    "speech_speed": 0.5,
    "voices_dir": Path(r"C:\EDITH\voices"),
    "selected_voice": "en_GB-vctk-medium",
    "silence_threshold": 900,
    "silence_duration": 0.8,
    "max_record_time": 25.0,
    "listen_cooldown": 1.5,
    "pre_roll_chunks": 6,
    "whisper_context_prompt": (
        "Hey Nix, what time is it? Open YouTube. Open GitHub. Open Roblox. "
        "Read the file. Shut down the system. Cancel shutdown. Confirm. "
        "Use the smart model and think deeply about this. Back to normal mode. "
        "Hey Nyx, help me with this."
    ),
    "allowed_read_roots": None,
}

class VoiceAssistant:
    def __init__(self, config=None):
        self.config = DEFAULT_CONFIG.copy()
        if config:
            self.config.update(config)

        self.user_home = Path.home()
        if self.config["allowed_read_roots"] is None:
            self.config["allowed_read_roots"] = [
                Path.cwd().resolve(),
                (self.user_home / "Documents").resolve(),
                (self.user_home / "Desktop").resolve(),
                Path(r"C:\EDITH").resolve(),
            ]

        # State
        self.shutdown_confirmation_pending = False
        self.deep_mode_prefix = "normal"
        self.speaking_in_progress = False
        self.followup_window_until = 0.0
        self.post_speak_cooldown_until = 0.0
        self.last_trigger_time = 0.0
        self._stop_event = threading.Event()
        self._event_queue = None

        # Audio parameters
        self.format = pyaudio.paInt16
        self.channels = 1
        self.rate = 16000
        self.chunk = 1280

        # PyAudio instance (create in main thread, but stream will be opened in run)
        self.audio_interface = pyaudio.PyAudio()
        self.mic_device_index = self._find_mic_index()
        if self.mic_device_index is None:
            raise RuntimeError("No working microphone found.")

        # Piper
        self.piper_voice = None
        self._init_piper()

        # Whisper
        self.stt_model = None
        self._init_whisper()

        # Pre-roll buffer (created in run)
        self.pre_roll_buffer = None

        print("[+] Assistant initialised (stream will be opened in the run thread).")

    # ---------- Microphone selection (improved) ----------
    def _find_mic_index(self):
        """Find a suitable microphone index with fallback to default and first device."""
        # 1. Try default input device
        try:
            default_info = self.audio_interface.get_default_input_device_info()
            default_idx = default_info['index']
            test = self.audio_interface.open(
                format=self.format, channels=1, rate=16000,
                input=True, input_device_index=default_idx, frames_per_buffer=1280
            )
            data = test.read(1280, exception_on_overflow=False)
            test.stop_stream()
            test.close()
            arr = np.frombuffer(data, dtype=np.int16)
            rms = math.sqrt(np.mean(np.square(arr.astype(np.float32))))
            if rms > 2.0:  # lowered threshold
                print(f"[+] Using default microphone (index {default_idx}) with RMS={rms:.2f}")
                return default_idx
            else:
                print(f"[!] Default mic has low RMS ({rms:.2f}), scanning...")
        except Exception as e:
            print(f"[!] Default mic failed: {e}, scanning...")

        # 2. Scan all devices
        preferred = ["hyperx", "duocast", "jbl", "steam streaming", "microphone"]
        candidates = []
        for i in range(self.audio_interface.get_device_count()):
            info = self.audio_interface.get_device_info_by_index(i)
            name = info.get("name", "").lower()
            if info.get("maxInputChannels", 0) <= 0:
                continue
            if any(token in name for token in ["virtual", "mapper", "stereo mix", "loopback", "wave"]):
                continue
            try:
                test = self.audio_interface.open(
                    format=self.format, channels=1, rate=16000,
                    input=True, input_device_index=i, frames_per_buffer=1280
                )
                data = test.read(1280, exception_on_overflow=False)
                test.stop_stream()
                test.close()
                arr = np.frombuffer(data, dtype=np.int16)
                rms = math.sqrt(np.mean(np.square(arr.astype(np.float32))))
                if rms <= 2.0:   # lowered threshold
                    continue
                score = sum(1 for p in preferred if p in name)
                candidates.append((score, rms, i, name))
            except:
                continue
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            chosen = candidates[0][2]
            print(f"[+] Selected microphone: {candidates[0][3]} (index {chosen}, RMS={candidates[0][1]:.2f})")
            return chosen

        # 3. Fallback: take the first device with input channels
        for i in range(self.audio_interface.get_device_count()):
            info = self.audio_interface.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                print(f"[+] Fallback: using first input device: {info.get('name')} (index {i})")
                return i

        return None

    # ---------- Initialisation ----------
    def _init_piper(self):
        voice_path = self.config["voices_dir"] / f"{self.config['selected_voice']}.onnx"
        if not voice_path.is_file():
            print(f"[-] Piper model {voice_path} not found.")
            return
        try:
            self.piper_voice = PiperVoice.load(voice_path)
            print("[+] Piper TTS loaded.")
        except Exception as e:
            print(f"[-] Piper load error: {e}")

    def _init_whisper(self):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        for model_name in ("large-v2", "large", "medium", "small", "tiny"):
            try:
                self.stt_model = WhisperModel(
                    model_name,
                    device=self.config["stt_device"],
                    compute_type="float16",
                    local_files_only=True,
                )
                print(f"[+] Whisper {model_name} loaded.")
                return
            except Exception as e:
                print(f"[-] Could not load {model_name}: {e}")
        raise RuntimeError("No local Whisper model found.")

    # ---------- Core speech functions ----------
    def speak(self, text, speaker_id=0, speed=None):
        """Synthesise and speak text; blocks until playback finishes."""
        if not text:
            return
        if speed is None:
            speed = self.config["speech_speed"]
        print(f"\n[Nyx]: {text}")

        if self.piper_voice is None:
            print("[-] TTS unavailable.")
            return

        self.speaking_in_progress = True
        try:
            sample_rate = getattr(self.piper_voice.config, 'sample_rate', 22050)
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                if hasattr(self.piper_voice, "synthesize_wav"):
                    try:
                        self.piper_voice.synthesize_wav(text, wf, speaker_id=speaker_id)
                    except TypeError:
                        self.piper_voice.synthesize_wav(text, wf)
                else:
                    self.piper_voice.synthesize(text, wf)

            wav_buffer.seek(0)
            with wave.open(wav_buffer, 'rb') as wf:
                out_channels = wf.getnchannels()
                out_width = wf.getsampwidth()
                out_rate = wf.getframerate()
                audio_bytes = wf.readframes(wf.getnframes())

            if not audio_bytes:
                return

            # Speed adjustment
            if speed != 1.0:
                try:
                    audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    try:
                        import librosa
                        audio_np = librosa.effects.time_stretch(audio_np, rate=speed)
                    except ImportError:
                        out_rate = int(out_rate * speed)
                    else:
                        audio_np = np.clip(audio_np, -1.0, 1.0) * 32767
                        audio_bytes = audio_np.astype(np.int16).tobytes()
                except Exception as e:
                    print(f"[-] Speed adjust failed: {e}")

            # Play
            out_stream = self.audio_interface.open(
                format=self.audio_interface.get_format_from_width(out_width),
                channels=out_channels,
                rate=out_rate,
                output=True
            )
            out_stream.write(audio_bytes)
            out_stream.stop_stream()
            out_stream.close()
        except Exception as e:
            print(f"[-] TTS error: {e}")
        finally:
            self.speaking_in_progress = False

    def _get_rms(self, chunk_data):
        count = len(chunk_data) // 2
        if count == 0:
            return 0.0
        shorts = struct.unpack(f"{count}h", chunk_data)
        sum_sq = sum(s*s for s in shorts)
        return math.sqrt(sum_sq / count)

    def _transcribe(self, wav_buffer):
        common_kwargs = dict(
            beam_size=8,
            best_of=5,
            temperature=[0.0, 0.2, 0.4],
            language="en",
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=300),
            initial_prompt=self.config["whisper_context_prompt"],
        )
        try:
            segments, _ = self.stt_model.transcribe(wav_buffer, hotwords="Nix Nyx", **common_kwargs)
        except TypeError:
            wav_buffer.seek(0)
            segments, _ = self.stt_model.transcribe(wav_buffer, **common_kwargs)
        return "".join(seg.text for seg in segments).strip()

    def _record_until_silence(self, mic_stream, prefix_frames=None):
        frames = list(prefix_frames) if prefix_frames else []
        silent_chunks = 0
        max_chunks = int(self.rate / self.chunk * self.config["max_record_time"])
        silence_limit = int(self.rate / self.chunk * self.config["silence_duration"])
        has_spoken = any(self._get_rms(f) > self.config["silence_threshold"] for f in frames)

        for _ in range(max_chunks):
            if self._stop_event.is_set():
                break
            data = mic_stream.read(self.chunk, exception_on_overflow=False)
            frames.append(data)
            rms = self._get_rms(data)
            if rms > self.config["silence_threshold"]:
                has_spoken = True
                silent_chunks = 0
            else:
                if has_spoken:
                    silent_chunks += 1
            if has_spoken and silent_chunks >= silence_limit:
                break
        return b''.join(frames)

    # ---------- Local tool handlers ----------
    def _normalize(self, text):
        if not text:
            return ""
        t = text.lower().replace("_", " ")
        t = t.replace("'", "")
        t = "".join(ch for ch in t if ch.isalpha() or ch.isspace() or ch in ",!?.")
        return " ".join(t.split())

    def _contains_wake(self, text):
        norm = self._normalize(text)
        if not norm:
            return False
        variants = ["hey nix", "hey nyx", "nix", "nyx"]
        for v in variants:
            if norm == v or norm.startswith(v) or f" {v} " in norm or norm.endswith(v):
                return True
        return False

    def _extract_command(self, text):
        norm = self._normalize(text)
        if not norm:
            return "hello"
        patterns = [r"(?:hey\s+)?(?:nix|nyx)\s*", r"\b(?:nix|nyx)\b\s*"]
        for pat in patterns:
            m = re.search(pat, norm, flags=re.IGNORECASE)
            if m:
                remainder = norm[m.end():].strip(" ,!?.")
                return remainder if remainder else "hello"
        return norm.strip(" ,!?.") or "hello"

    def _get_time_reply(self):
        now = datetime.datetime.now()
        return f"It is {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d, %Y')}."

    def _is_time_request(self, text):
        norm = self._normalize(text)
        return any(k in norm for k in ["time", "clock", "date", "today", "day"]) if norm else False

    def _extract_path(self, text):
        patterns = [
            r'(?i)(?:read|open|show)\s+(?:file|path|document)?\s*["\']([^"\']+)["\']',
            r'(?i)(?:read|open|show)\s+(?:file|path|document)?\s*([A-Za-z]:\\[^\s]+)',
            r'(?i)(?:read|open|show)\s+(?:file|path|document)?\s*([~./A-Za-z0-9_\\ -]+\\[^\s]+)',
            r'(?i)(?:read|open|show)\s+(?:file|path|document)?\s*([~./A-Za-z0-9_ -]+)',
        ]
        for p in patterns:
            m = re.search(p, text)
            if m:
                return m.group(1).strip()
        return None

    def _is_allowed_path(self, path_obj):
        resolved = path_obj.resolve(strict=False)
        for root in self.config["allowed_read_roots"]:
            try:
                if os.path.commonpath([str(root), str(resolved)]) == str(root):
                    return True
            except ValueError:
                continue
        return False

    def _safe_read_file(self, text):
        path_str = self._extract_path(text)
        if not path_str:
            return "I can read files, but need a path inside an allowed folder."
        expanded = os.path.expanduser(path_str)
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve(strict=False)
        if not candidate.exists() or not candidate.is_file():
            return f"File not found: {candidate}"
        if not self._is_allowed_path(candidate):
            return f"Access denied: {candidate} is outside allowed roots."
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
            preview = content[:800].strip()
            return preview if preview else "File is empty."
        except Exception as e:
            return f"Read error: {e}"

    def _safe_open_link(self, text):
        lower = text.lower()
        aliases = {
            "google": "https://www.google.com",
            "youtube": "https://www.youtube.com",
            "github": "https://github.com",
            "roblox": "https://www.roblox.com",
            "gmail": "https://mail.google.com",
            "docs": "https://docs.google.com",
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
        }
        url = None
        m_url = re.search(r'https?://[^\s]+', text)
        if m_url:
            url = m_url.group(0)
        else:
            for name, u in aliases.items():
                if re.search(rf'\b{name}\b', lower):
                    url = u
                    break
            if url is None:
                m_domain = re.search(r'\b([a-z0-9.-]+\.[a-z]{2,})(?:\s|$)', lower)
                if m_domain:
                    url = f"https://{m_domain.group(1)}"
        if not url:
            m = re.search(r'\b(?:open|visit|go to|launch)\s+([a-z0-9-]{2,})\b', lower)
            if m:
                url = f"https://www.{m.group(1)}.com"
        if not url:
            return "I need a site name or URL."
        try:
            webbrowser.open(url)
            return f"Opening {url}"
        except Exception as e:
            return f"Could not open link: {e}"

    def _handle_local_tools(self, command_text):
        text = command_text.strip()
        if not text:
            return None
        lower = text.lower()

        if "cancel shutdown" in lower:
            self.shutdown_confirmation_pending = False
            try:
                import subprocess
                subprocess.run(["shutdown", "/a"], check=False)
                return "Shutdown aborted."
            except Exception as e:
                return f"Failed to cancel: {e}"

        if self.shutdown_confirmation_pending:
            if re.search(r'\b(confirm|confirmed|yes)\b', lower):
                self.shutdown_confirmation_pending = False
                try:
                    import subprocess
                    subprocess.run(["shutdown", "/s", "/t", "10", "/c", "System shutdown confirmed."], check=False)
                    return "Shutting down in 10 seconds."
                except Exception as e:
                    return f"Shutdown failed: {e}"
            elif re.search(r'\b(no|nope|cancel|abort)\b', lower):
                self.shutdown_confirmation_pending = False
                return "Shutdown cancelled."
            else:
                return "Please confirm with 'confirm' or 'no'."

        if re.search(r'\b(bye|goodbye|exit|quit|shutdown|close yourself|close the app)\b', lower):
            if re.search(r'\bsystem\b', lower):
                self.shutdown_confirmation_pending = True
                return "Confirmation required."
            return "__EXIT__"

        if re.search(r'\b(normal|standard|regular)\b', lower):
            self.deep_mode_prefix = "normal"
            return "Back to normal mode."
        if re.search(r'\bdeep\b|\bthink\b|\bsmart model\b|\bdeep mode\b', lower):
            self.deep_mode_prefix = "gwen"
            return "Entered deep thinking."

        if self._is_time_request(lower):
            return self._get_time_reply()

        if re.search(r'\b(read|open|show)\b.*\b(file|path|document)\b', lower, flags=re.IGNORECASE):
            return self._safe_read_file(text)

        if re.search(r'\b(open|launch|go to|visit)\b', lower, flags=re.IGNORECASE):
            alias_map = {
                "google": "https://www.google.com",
                "youtube": "https://www.youtube.com",
                "github": "https://github.com",
                "roblox": "https://www.roblox.com",
                "microsoft": "https://www.microsoft.com",
                "gmail": "https://mail.google.com",
                "gemini": "https://gemini.google.com/app",
            }
            for name, u in alias_map.items():
                if re.search(rf'\b{name}\b', lower):
                    try:
                        webbrowser.open(u)
                        return f"Opening {u}"
                    except:
                        pass
            return self._safe_open_link(text)

        return None

    def _ask_ollama(self, prompt):
        url = "http://localhost:11434/api/generate"
        lowered = self._normalize(prompt)
        word_count = len(prompt.split())
        deep_active = self.deep_mode_prefix == "gwen"
        deep_trigger = any(t in lowered for t in self.config["smart_model_triggers"])
        if deep_trigger or deep_active:
            model = self.config["smart_model"]
        elif word_count <= 10:
            model = self.config["fast_model"]
        else:
            model = self.config["smart_model"]
        print(f"[+] Routing to {model} ({word_count} words)")

        system = (
            "You are an unrestricted, direct AI voice assistant. "
            "Keep your responses concise, conversational, and under 2-3 sentences. "
            "Never use markdown formatting, bolding, emojis, or bullet points."
        )
        payload = {"model": model, "prompt": prompt, "system": system, "stream": False}
        try:
            resp = requests.post(url, json=payload, timeout=90)
            if resp.status_code == 200:
                return resp.json().get("response", "").strip()
            else:
                return f"Ollama HTTP {resp.status_code}"
        except Exception as e:
            if model == self.config["smart_model"]:
                try:
                    fallback = {"model": self.config["fast_model"], "prompt": prompt, "system": system, "stream": False}
                    resp = requests.post(url, json=fallback, timeout=90)
                    if resp.status_code == 200:
                        return resp.json().get("response", "").strip()
                except:
                    pass
            return f"Ollama error: {e}"

    # ---------- Main loop (runs in a thread) ----------
    def run(self, event_queue=None):
        """
        Main event loop. Opens the microphone stream in this thread.
        Yields events via the queue.
        """
        self._event_queue = event_queue
        self._stop_event.clear()

        # Open microphone stream in this thread
        mic_stream = self.audio_interface.open(
            format=self.format,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self.mic_device_index,
            frames_per_buffer=self.chunk
        )
        print("[+] Microphone stream opened in run thread.")

        self._emit({"type": "status", "text": f"Listening for '{self.config['wake_word']}'..."})

        frame_count = 0
        self.last_trigger_time = 0.0
        self.followup_window_until = 0.0
        self.post_speak_cooldown_until = 0.0
        self.pre_roll_buffer = deque(maxlen=self.config["pre_roll_chunks"])

        try:
            while not self._stop_event.is_set():
                if self._stop_event.is_set():
                    break

                if self.speaking_in_progress:
                    time.sleep(0.05)
                    continue

                now = time.time()

                if now < self.post_speak_cooldown_until:
                    time.sleep(0.05)
                    continue

                try:
                    pcm_data = mic_stream.read(self.chunk, exception_on_overflow=False)
                except Exception as e:
                    self._emit({"type": "error", "text": f"Mic read error: {e}"})
                    break

                audio_frame = np.frombuffer(pcm_data, dtype=np.int16)
                rms = math.sqrt(np.mean(np.square(audio_frame.astype(np.float32))))
                frame_count += 1
                self.pre_roll_buffer.append(pcm_data)

                if self.config.get("wake_word_debug") and frame_count % 20 == 0:
                    print(f"[DEBUG] RMS={rms:.2f}")

                if now - self.last_trigger_time < self.config["listen_cooldown"]:
                    continue

                followup_allowed = (now < self.followup_window_until)

                if not followup_allowed and rms < self.config["wake_word_threshold"]:
                    continue

                self.last_trigger_time = now
                if not followup_allowed:
                    self._emit({"type": "status", "text": "Speech energy spike detected"})
                else:
                    self._emit({"type": "status", "text": "Follow-up speech detected"})

                recorded_pcm = self._record_until_silence(mic_stream, prefix_frames=list(self.pre_roll_buffer))

                wav_buffer = io.BytesIO()
                with wave.open(wav_buffer, 'wb') as wf:
                    wf.setnchannels(self.channels)
                    wf.setsampwidth(self.audio_interface.get_sample_size(self.format))
                    wf.setframerate(self.rate)
                    wf.writeframes(recorded_pcm)
                wav_buffer.seek(0)

                self._emit({"type": "status", "text": "Transcribing..."})
                user_text = self._transcribe(wav_buffer)

                if not user_text:
                    self._emit({"type": "status", "text": "No clear speech detected."})
                    self._emit({"type": "status", "text": f"Listening for '{self.config['wake_word']}'..."})
                    continue

                self._emit({"type": "transcript", "text": user_text})
                print(f"[User]: {user_text}")

                if followup_allowed:
                    command_text = user_text.strip()
                    self._emit({"type": "status", "text": "Follow-up mode accepted"})
                else:
                    if self._contains_wake(user_text):
                        command_text = self._extract_command(user_text)
                        if not command_text:
                            command_text = "hello"
                        self._emit({"type": "status", "text": "Wake phrase detected"})
                    else:
                        self._emit({"type": "status", "text": f"No wake phrase. Listening for '{self.config['wake_word']}'..."})
                        continue

                reply = self._handle_local_tools(command_text)
                if reply is None:
                    self._emit({"type": "status", "text": "Asking Ollama..."})
                    reply = self._ask_ollama(command_text)

                if reply == "__EXIT__":
                    self._emit({"type": "reply", "text": "Goodbye."})
                    self.speak("Goodbye.")
                    break

                self._emit({"type": "reply", "text": reply})
                self.speak(reply)

                self.post_speak_cooldown_until = time.time() + 0.3
                if self.shutdown_confirmation_pending:
                    self.followup_window_until = time.time() + 10.0
                elif reply.strip().endswith('?'):
                    self.followup_window_until = time.time() + 15.0
                else:
                    self.followup_window_until = 0.0

                self._emit({"type": "status", "text": f"Listening for '{self.config['wake_word']}'..."})

        finally:
            mic_stream.stop_stream()
            mic_stream.close()
            print("[+] Microphone stream closed.")
            self._emit({"type": "status", "text": "Assistant stopped."})
            self._stop_event.clear()

    def _emit(self, event):
        if self._event_queue is not None:
            try:
                self._event_queue.put_nowait(event)
            except queue.Full:
                pass

    def stop(self):
        self._stop_event.set()

    def close(self):
        self.stop()
        time.sleep(0.2)
        if self.audio_interface:
            self.audio_interface.terminate()
        print("[+] Assistant resources released.")