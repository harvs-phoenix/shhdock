"""ShhDock engine: settings, auto-install of binaries/models, and the
llama.cpp chat server.

Everything lives in %LOCALAPPDATA%\\ShhDock.  Progress and status are
pushed as dicts into the callback the UI provides (thread-safe: UI puts
them on a queue itself if needed).
"""
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "ShhDock"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
MODEL_DIR = DATA_DIR / "models"
LLAMA_DIR = MODEL_DIR / "llama.cpp"
GGUF_DIR = MODEL_DIR / "gguf"
SETTINGS_FILE = DATA_DIR / "settings.json"
LLAMA_LOG = DATA_DIR / "llama-server.log"

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
VOSK_ZIP = MODEL_DIR / "vosk-small-en.zip"

LLAMA_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
LLAMA_DL = "https://github.com/ggml-org/llama.cpp/releases/download"
LLAMA_NIGHTLY_TXT = ("https://github.com/ggml-org/llama.cpp/releases/"
                     "latest/download/nightly-tag.txt")

USER_AGENT = "ShhDock/1.0 (+local voice assistant)"
CHAT_PORT = 8654

# --- model catalog -----------------------------------------------------------
# Sizes are download sizes for the Q4_K_M quantisation (fast + light).
MODELS = {
    "qwen2.5-0.5b": {
        "label": "Qwen2.5 0.5B Instruct", "quant": "Q4_K_M", "gb": 0.4,
        "repo": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "file": "qwen2.5-0.5b-instruct-q4_k_m.gguf"},
    "qwen2.5-1.5b": {
        "label": "Qwen2.5 1.5B Instruct", "quant": "Q4_K_M", "gb": 1.1,
        "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf"},
    "qwen2.5-3b": {
        "label": "Qwen2.5 3B Instruct", "quant": "Q4_K_M", "gb": 2.0,
        "repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "file": "qwen2.5-3b-instruct-q4_k_m.gguf"},
    "qwen2.5-7b": {
        "label": "Qwen2.5 7B Instruct", "quant": "Q4_K_M", "gb": 4.4,
        "repo": "bartowski/Qwen2.5-7B-Instruct-GGUF",
        "file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf"},
    "qwen2.5-14b": {
        "label": "Qwen2.5 14B Instruct", "quant": "Q4_K_M", "gb": 8.4,
        "repo": "bartowski/Qwen2.5-14B-Instruct-GGUF",
        "file": "Qwen2.5-14B-Instruct-Q4_K_M.gguf"},
}
MODEL_ORDER = ["qwen2.5-0.5b", "qwen2.5-1.5b", "qwen2.5-3b",
               "qwen2.5-7b", "qwen2.5-14b"]

# Biggest model that still fits comfortably for a given total-RAM class.
RAM_TIERS = [(6, "qwen2.5-0.5b"), (10, "qwen2.5-1.5b"), (17, "qwen2.5-3b"),
             (33, "qwen2.5-7b"), (9999, "qwen2.5-14b")]


def installed_ram_gb():
    """Physically installed RAM in GB via the Win32 API (correct arg: an
    out-pointer to ULONG kilobytes)."""
    try:
        kb = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(
            ctypes.byref(kb))
        if ok and kb.value:
            return round(kb.value / (1024.0 * 1024.0), 1)
    except Exception:
        pass
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return round(stat.ullTotalPhys / (1024.0 ** 3), 1)
    except Exception:
        return 16.0


def model_for_ram(gb):
    for limit, model_id in RAM_TIERS:
        if gb <= limit:
            return model_id
    return RAM_TIERS[-1][1]


def gguf_url(model_id):
    m = MODELS[model_id]
    return "https://huggingface.co/%s/resolve/main/%s" % (m["repo"], m["file"])


def gguf_path(model_id):
    return GGUF_DIR / (model_id + ".gguf")


# --- settings ----------------------------------------------------------------

DEFAULTS = {
    "setup_done": False,
    "ram_mode": "auto",          # auto | preset | custom
    "ram_gb": 16.0,              # used when ram_mode == custom
    "model_id": "qwen2.5-3b",
    "answer_mode": "questions",  # questions | everything
    "hide_from_share": True,
    "gpu": False,
    "mic_device": "default",
}


def load_settings():
    settings = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            settings.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def resolve_model_id(settings):
    """Current RAM selection -> model id (also updates settings)."""
    mode = settings.get("ram_mode", "auto")
    if mode == "custom":
        gb = float(settings.get("ram_gb", 16) or 16)
    elif mode == "preset":
        gb = float(settings.get("ram_gb", installed_ram_gb()))
    else:
        gb = installed_ram_gb()
    model_id = model_for_ram(max(1.0, min(gb, 512.0)))
    if settings.get("model_id") != model_id:
        settings["model_id"] = model_id
    return model_id


# --- downloads ---------------------------------------------------------------

def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def _download(url, dest, emit, label):
    """Stream url -> dest (.part then rename). Emits progress events."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    emit({"t": "dl", "label": label, "done": 0, "total": 0})
    with _http_get(url, timeout=60) as resp, open(part, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_emit = 0.0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last_emit >= 0.15:
                last_emit = now
                emit({"t": "dl", "label": label, "done": done,
                      "total": total})
    os.replace(part, dest)
    emit({"t": "dl", "label": label, "done": 1, "total": 1, "finished": True})


def _extract_zip(zip_path, dest_dir):
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)


# --- installation ------------------------------------------------------------

def llama_server_exe():
    if LLAMA_DIR.is_dir():
        for cand in (LLAMA_DIR / "llama-server.exe",):
            if cand.exists():
                return cand
        for found in LLAMA_DIR.rglob("llama-server.exe"):
            return found
    return None


VOSK_MODEL_DIRNAME = "vosk-model-small-en-us-0.15"


def vosk_model_path():
    """Directory vosk.Model can load; extracts the zip once if needed."""
    if not VOSK_ZIP.exists():
        return None
    inner = MODEL_DIR / VOSK_MODEL_DIRNAME
    if inner.is_dir():
        return str(inner)
    import zipfile
    try:
        with zipfile.ZipFile(VOSK_ZIP) as zf:
            zf.extractall(MODEL_DIR)
    except Exception:
        return None
    if inner.is_dir():
        return str(inner)
    return None


def install_state(settings):
    """What's present right now, for the installer / settings UI."""
    model_id = settings.get("model_id") or "qwen2.5-3b"
    return {
        "engine": llama_server_exe() is not None,
        "voice": VOSK_ZIP.exists(),
        "model": gguf_path(model_id).exists(),
        "model_id": model_id,
    }


def _nightly_tag():
    try:
        with _http_get(LLAMA_NIGHTLY_TXT, timeout=30) as resp:
            return resp.read(64).decode("ascii", "ignore").strip()
    except Exception:
        with _http_get(LLAMA_API, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = data.get("tag_name", "")
        return tag


def ensure_engine(emit, gpu=False):
    """Download + extract llama.cpp (CPU build, or CUDA 13.4 when gpu)."""
    if gpu and (LLAMA_DIR / "cudart.loaded").exists() and llama_server_exe():
        return llama_server_exe()
    if llama_server_exe() and not gpu:
        return llama_server_exe()

    tag = _nightly_tag()
    if not tag or not re.fullmatch(r"b\d+", tag):
        raise RuntimeError("could not resolve llama.cpp nightly build tag")

    if gpu:
        wanted = ["llama-%s-bin-win-cuda-13.4-x64.zip" % tag,
                  "cudart-llama-bin-win-cuda-13.4-x64.zip"]
    else:
        wanted = ["llama-%s-bin-win-cpu-x64.zip" % tag]
    for name in wanted:
        url = "%s/%s/%s" % (LLAMA_DL, tag, name)
        dest = MODEL_DIR / name
        if not dest.exists():
            _download(url, dest, emit, "Engine (%s)" % name.split("bin-win-")[-1])
        emit({"t": "step", "label": "Extracting %s" % name})
        _extract_zip(dest, LLAMA_DIR)
    if gpu:
        (LLAMA_DIR / "cudart.loaded").write_text(tag, encoding="utf-8")
    exe = llama_server_exe()
    if not exe:
        raise RuntimeError("llama-server.exe missing after extraction")
    return exe


def ensure_voice(emit):
    if VOSK_ZIP.exists():
        return str(VOSK_ZIP)
    emit({"t": "step", "label": "Downloading voice model (40 MB)"})
    _download(VOSK_URL, VOSK_ZIP, emit, "Voice model")
    return str(VOSK_ZIP)


def ensure_model(model_id, emit):
    dest = gguf_path(model_id)
    if dest.exists():
        return dest
    m = MODELS[model_id]
    emit({"t": "step",
          "label": "Downloading %s (%.1f GB)" % (m["label"], m["gb"])})
    _download(gguf_url(model_id), dest, emit, m["label"])
    return dest


def ensure_installed(settings, emit):
    """Full first-run install for the current settings. Raises on failure."""
    resolve_model_id(settings)
    save_settings(settings)
    state = install_state(settings)
    emit({"t": "step", "label": "Engine (llama.cpp binaries)"})
    if not state["engine"] or (settings.get("gpu") and
                               not (LLAMA_DIR / "cudart.loaded").exists()):
        ensure_engine(emit, gpu=bool(settings.get("gpu")))
    emit({"t": "step", "label": "Voice model (speech recognition)"})
    if not state["voice"]:
        ensure_voice(emit)
    ensure_model(settings["model_id"], emit)
    save_settings(settings)
    emit({"t": "installed", "state": install_state(settings)})


# --- llama.cpp server --------------------------------------------------------

class LlamaServer:
    def __init__(self, emit):
        self.emit = emit
        self.proc = None
        self.model_id = None
        self.ready = False
        self._lock = threading.Lock()
        self._last_log = ""

    def start(self, model_id, gpu=False):
        with self._lock:
            if self.proc and self.proc.poll() is None and \
                    self.model_id == model_id:
                return True
            self.stop_locked()
            exe = llama_server_exe()
            gguf = gguf_path(model_id)
            if not exe or not gguf.exists():
                self.emit({"t": "llm", "state": "missing"})
                return False
            args = [str(exe), "-m", str(gguf), "--host", "127.0.0.1",
                    "--port", str(CHAT_PORT), "-c", "4096", "-np", "1",
                    "--no-webui"]
            if gpu:
                args += ["-ngl", "99"]
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            log = open(LLAMA_LOG, "ab")
            try:
                self.proc = subprocess.Popen(
                    args, cwd=str(LLAMA_DIR), stdout=log, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            finally:
                log.close()
            self.model_id = model_id
            self.ready = False
            self.emit({"t": "llm", "state": "loading"})
            threading.Thread(target=self._wait_ready, daemon=True).start()
            return True

    def _wait_ready(self):
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                self.ready = False
                self.emit({"t": "llm", "state": "failed",
                           "log": str(LLAMA_LOG)})
                return
            try:
                with _http_get("http://127.0.0.1:%d/health" % CHAT_PORT,
                               timeout=3) as resp:
                    if resp.status == 200:
                        self.ready = True
                        self.emit({"t": "llm", "state": "ready"})
                        return
            except Exception:
                pass
            time.sleep(0.4)
        self.emit({"t": "llm", "state": "timeout", "log": str(LLAMA_LOG)})

    def stop(self):
        with self._lock:
            self.stop_locked()

    def stop_locked(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self.ready = False

    def chat(self, messages, timeout=180):
        """Blocking OpenAI-style chat call (run it on a worker thread)."""
        if not self.ready:
            return None, "model is still warming up, try again in a second"
        payload = json.dumps({
            "model": "local",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 240,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % CHAT_PORT,
            data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            return text, None
        except Exception as exc:
            return None, str(exc)
