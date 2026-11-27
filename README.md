# NYX – local voice assistant

Nyx is a voice assistant that runs entirely on your own PC. Say **"Nyx"** and
then a command. It can open apps and websites, set the volume and brightness,
tell you the time, read files, shut down the PC, and answer questions using a
local AI model. For things the model doesn't know (news, scores, prices, recent
events) it searches the web through DuckDuckGo; you can turn that off in Settings.

- Speech-to-text: **faster-whisper** (Whisper large-v3)
- Voice: **Piper** text-to-speech
- AI chat: **Ollama** (`dolphin-llama3:8b`; optionally `qwen2.5:14b` for deep mode on a strong GPU)

For everything you can say to Nyx, see **[COMMANDS.txt](COMMANDS.txt)**.

---

## Easy install: setup.bat

Put the NYX folder where you want it (for example `C:\NYX`) and double-click
**`setup.bat`**. It installs Nyx right there and downloads everything below by
itself: Python 3.11 (if missing), the Python packages, the NVIDIA CUDA libraries
(if you have an NVIDIA card), the Whisper model, Ollama and both AI models, and the
Piper voice. Then it puts a **NYX** shortcut on your desktop and in the Start Menu.
It's about 9–10 GB of downloads. Run it again any time to repair the install.

Setup also asks whether to install the **smart model** (`qwen2.5:14b`, ~9 GB more).
Nyx uses it for long questions and deep mode if it's installed, and the fast model
for everything otherwise. It's only worth it with a strong NVIDIA card.

To change the voice, AI models or Whisper models it downloads, edit the
**Settings** block at the top of `setup.bat`.

The steps below are the manual way to do the same thing.

---

## 1. What you need first

| What | Why | Where to get it |
|---|---|---|
| **Windows 10 or 11** | Nyx uses Windows features (volume, shutdown, app search) | — |
| **Python 3.11** | Runs Nyx | <https://www.python.org/downloads/> (tick **"Add python.exe to PATH"** and keep the **py launcher** option on) |
| **Ollama** | The AI that answers questions | <https://ollama.com/download> |
| **A microphone** | So Nyx can hear you | Any USB or headset mic |
| **NVIDIA graphics card + current driver** *(recommended)* | Makes speech recognition fast | <https://www.nvidia.com/Download/index.aspx> |

There's no NVIDIA card? Nyx still works but switches speech recognition to the
CPU automatically. It's slower, so a smaller Whisper model is a good idea
(see step 4).

Disk space: about **3 GB** for Whisper large-v3, **14 GB** for the two Ollama
models, and **~100 MB** per Piper voice.

---

## 2. Install the Python packages

Open **Command Prompt** (press `Win + R`, type `cmd`, press Enter) and run:

```cmd
cd /d C:\NYX
py -3.11 -m pip install --upgrade pip
py -3.11 -m pip install -r requirements.txt
```

If installing fails, check that you're using Python **3.11** (`py -3.11 --version`).
Most packages have ready-made versions for it.

---

## 3. Download the AI chat models (Ollama)

Once Ollama is installed (it runs in the background, so check for its icon
in the system tray), run:

```cmd
ollama pull dolphin-llama3:8b
ollama pull qwen2.5:14b
```

Check that they're there:

```cmd
ollama list
```

---

## 4. Download the Whisper speech-recognition model

Nyx runs Whisper **offline**, so the model has to be downloaded once first:

```cmd
py -3.11 -c "from faster_whisper import WhisperModel; WhisperModel('large-v3')"
```

On a slower PC or without an NVIDIA card, download a smaller model too.
Nyx automatically uses the best one it finds:

```cmd
py -3.11 -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
```

---

## 5. Download a Piper voice

Make a `voices` folder inside the NYX folder and put **both files** of the voice in it:

- `en_GB-vctk-medium.onnx`
- `en_GB-vctk-medium.onnx.json`

Download them from:
<https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_GB/vctk/medium>

Or download them straight from Command Prompt:

```cmd
cd /d C:\NYX
mkdir voices
curl -L -o voices\en_GB-vctk-medium.onnx https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/vctk/medium/en_GB-vctk-medium.onnx
curl -L -o voices\en_GB-vctk-medium.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/vctk/medium/en_GB-vctk-medium.onnx.json
```

To use another voice, put its files in `voices` and change `SELECTED_VOICE`
near the top of `assistant.py`. If the chosen voice is missing, Nyx uses
whichever voice it finds in the folder.

---

## 6. Start Nyx

**The app (recommended):** double-click **`NYX.exe`** (or `run_app.bat`). You can
right-click `NYX.exe` → *Pin to taskbar / Start*. `setup.bat` builds `NYX.exe`;
if you installed by hand, build it with `tools\build_exe.bat`. Or from Command Prompt:

```cmd
cd /d C:\NYX
py -3.11 nyx_app.py
```

**Console version (no window, just text):** double-click **`run_assistant.bat`**, or:

```cmd
cd /d C:\NYX
py -3.11 assistant.py
```

The first start takes a little while (loading the models). When Nyx says
*"Hello sir, all systems are online"*, say **"Nyx, what time is it?"** to test it.

---

## Command Prompt cheat sheet

| Command | What it does |
|---|---|
| `cd /d C:\NYX` | Go to the Nyx folder |
| `py -3.11 nyx_app.py` | Start the Nyx app |
| `py -3.11 assistant.py` | Start Nyx in the console |
| `py -3.11 -m pip install -r requirements.txt` | Install or repair the Python packages |
| `ollama list` | Show the AI models you have |
| `ollama pull qwen2.5:14b` | Download an AI model |
| `ollama serve` | Start Ollama by hand, if it isn't running |
| `py -3.11 -c "from faster_whisper import WhisperModel; WhisperModel('large-v3')"` | Download the Whisper model |
| `nvidia-smi` | Check that your NVIDIA card and driver work |
| `shutdown /a` | Cancel a PC shutdown by hand |

## Keyboard shortcuts

| App | Console |
|---|---|
| **Ctrl+M** mic on/off | **M** mic on/off |
| **Esc** stop Nyx talking / end task | **S** stop Nyx talking |
| | **E** or **Esc** end task while thinking |
| **Ctrl+H** minimize | **Space** cancel a shutdown |
| **F11** fullscreen on/off | |
| **Ctrl+L** clear chat | |
| **Ctrl+,** settings | |
| **Space** cancel a shutdown (while the countdown shows) | |
| **Alt+F4** close | |

---

## Troubleshooting

- **"No local Whisper model could be loaded"**: do step 4.
- **"Error connecting to Ollama backend"**: Ollama isn't running. Open it from the
  Start Menu or run `ollama serve`, then check `ollama list`.
- **Nyx doesn't wake up / wakes up too easily**: in `assistant.py`, lower or raise
  `NOISE_MULTIPLIER` and `MIN_TRIGGER_RMS`. Set `WAKE_WORD_DEBUG = True` to see
  live microphone levels in the console.
- **Wrong microphone or speaker**: pick them in the app under Settings (Ctrl+,) →
  Audio devices. A mic left on Automatic prefers HyperX/DuoCast, then the Windows
  default mic, then the loudest working mic. If a chosen device is unplugged, Nyx
  falls back to the Windows default.
- **No voice**: check that the two voice files from step 5 are in `voices`.
- **Volume control doesn't work**: run `py -3.11 -m pip install pycaw comtypes`, or install
  [NirCmd](https://www.nirsoft.net/utils/nircmd.html) and put it on your PATH.
- **Speech recognition is slow**: you're probably on the CPU. Run `nvidia-smi` to
  check the graphics driver, or use the smaller Whisper model from step 4.

## Files

| File | What it is |
|---|---|
| `nyx_app.py` | The app window (orb, chat, buttons, settings) |
| `nyx_settings.py` | Settings and the themes / orb colour presets |
| `settings.json` | Your settings (saved as soon as you change one in Settings) |
| `assistant.py` | The engine: listening, speech-to-text, commands, voice |
| `setup.bat` | Downloads and installs everything Nyx needs |
| `NYX.exe` | Starts the app (no console window), with the NYX icon. Built by `setup.bat`, not stored in git |
| `assets/` | The NYX logo (`nyx.ico`, `nyx.png`) |
| `tools/` | `make_icon.py` redraws the logo, `build_exe.bat` rebuilds `NYX.exe` |
| `run_app.bat` | Starts the app |
| `run_assistant.bat` | Starts the console version |
| `COMMANDS.txt` | Everything you can say to Nyx |
| `requirements.txt` | Python packages to install |
| `voices/` | Piper voice files |
| `previous_chats.txt` | Your conversation history (created automatically) |
