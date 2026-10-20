<div align="center">

# 🫥 ShhDock

### An always-on-top dock that's invisible to screen share — and answers with a model on your own PC.

*Ask it anything. It hears you, thinks locally, and replies in a bubble your stream can't see.*

[![Windows](https://img.shields.io/badge/Windows-10%2F11-blue?style=for-the-badge&logo=windows)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Local](https://img.shields.io/badge/Runs%20100%25-local%20%E2%80%94%20no%20cloud-green?style=for-the-badge)](#)
[![Release](https://img.shields.io/github/v/release/harvs-phoenix/shhdock?style=for-the-badge&color=9146ff)](../../releases)

<img src="assets/dock-panel.png" width="720" alt="ShhDock panel open">

</div>

-----

## What it does

ShhDock is a slim bar docked at the top of your screen. While you game and stream, it does three things at once:

- **Vanishes from capture.** It sets `WDA_EXCLUDEFROMCAPTURE` on its own window, so Discord, OBS, and the Xbox Game Bar won't render it. Your viewers never see it. **You do.**
- **Always listens.** A voice-activity detector watches your mic and transcribes speech locally with Vosk. No audio ever leaves your machine.
- **Answers fast.** When you ask a question, a local **llama.cpp** model replies in a text bubble under the dock — typically in 1–3 seconds, entirely offline.

It also keeps the original tool around: the **🪟** button launches the window-hider so you can drop *other* apps out of the capture too.

<img src="assets/dock-bubble.png" width="720" alt="Answer bubble">

-----

## Picking a model

On first run, a tiny installer asks how much RAM you have and downloads the largest model that stays responsive:

| RAM | Model | Download |
| --- | --- | --- |
| up to 6 GB | Qwen2.5 0.5B | ~0.4 GB |
| up to 10 GB | Qwen2.5 1.5B | ~1.0 GB |
| up to 17 GB | Qwen2.5 3B | ~2.0 GB |
| up to 33 GB | Qwen2.5 7B | ~4.4 GB |
| more | Qwen2.5 14B | ~8.4 GB |

You can override it anytime — **Auto**, a **preset**, or a **custom** RAM number — in ⚙ settings. ShhDock downloads the new model before switching, so you're never left waiting mid-question.

<img src="assets/setup.png" width="560" alt="First-run installer">

-----

## Privacy, plainly

- **The mic is visible.** The dock shows a live level meter and a status word (`listening`, `thinking…`) whenever it's capturing. There's no silent mode.
- **Everything is local.** Speech recognition and the chat model both run on your CPU. No account, no telemetry, no server.
- **It only hears while it's running.** Close the dock and the microphone stream stops.

> Note: hiding a window from a screen share doesn't hide the *audio* your PC captures. If you're streaming, the usual rules apply — be in control of what your mic picks up.

-----

## Using it

| Control | Does |
| --- | --- |
| 🗣️ **Just talk** | Ask a question out loud; watch the words appear, then the answer |
| **💬** | Open the chat panel and history |
| **the text box** | Type a question instead (handy when you're already in voice chat) |
| **🪟** | Launch the hide-other-windows tool |
| **⚙** | Settings: RAM/model, mic, GPU, answer mode, capture-hiding |
| **✕** or right-click | Quit and release the mic |

In settings, **Answer questions only** (default) ignores stray chatter and only replies to real questions. Flip it to **Answer everything I say** if you'd rather it respond to every sentence.

<img src="assets/settings.png" width="560" alt="Settings">

-----

## Get it

Grab **`ShhDock.exe`** from [**Releases**](../../releases). It's a single portable file — no install, no dependencies.

On first launch it unpacks and offers the one-time model download (~2.5 GB total for a 3 GB model). After that it starts offline.

<details>
<summary><b>Run from source</b></summary>

```powershell
pip install vosk sounddevice numpy
python shhdock.py
```

To build the exe yourself:

```powershell
pip install pyinstaller pillow
py -m PyInstaller --onefile --noconsole --icon shhdock.ico `
  --name ShhDock --collect-all vosk --collect-all sounddevice shhdock.py
```

</details>

-----

## How it's built

- **Capture-hiding** — `SetWindowDisplayAffinity(hwnd, 0x11)` on the dock's own top-level window. Tk's `HWND` isn't stable until the window is mapped, so ShhDock sets it, reads it back, and retries until the affinity sticks.
- **Speech** — Vosk with an energy-based voice-activity detector. Utterances are cut on a short silence hangover, so answers fire as soon as you stop talking.
- **Chat** — llama.cpp's `llama-server` over its local OpenAI-compatible HTTP API, with the model sized to your RAM.
- **UI** — a single always-on-top tkinter frame that grows a panel downward. No background thread ever touches tkinter; everything flows through one event queue drained by `after()`.

<div align="center">

Made for people who stream.

**[⭐ Star](../../stargazers) · [📦 Releases](../../releases) · [🐛 Issues](../../issues)**

</div>
