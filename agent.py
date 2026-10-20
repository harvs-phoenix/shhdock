"""ShhDock agent: the layer that lets the local model actually operate the
computer — answer questions with live system context, and (when enabled) run
commands, manage files, open apps, control windows, and type.

Safety model
------------
- Read-only tools (system_info, screen_text, mouse move/scroll, chrome
  extract, ...) never ask.
- Side-effect tools (run, open, window_hide/show, send_keys, volume set,
  mouse click, chrome actions) ask for approval in the default "ask" mode; "auto" skips the prompt, but
  destructive-looking shell commands ALWAYS ask, even in auto mode.
- Every action is shown in the dock panel and appended to actions.log.

The model protocol is deliberately simple text-JSON so even the small tiers
can emit it:  {"tool": "<name>", "args": {...}}   on a line of its own.
Anything else the model says is treated as the final answer.
"""
import ctypes
import json
import os
import re
import subprocess
import threading
import time
import winreg
from ctypes import wintypes as wt
from datetime import datetime
from pathlib import Path

import numpy as np

DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ShhDock"
ACTION_LOG = DATA_DIR / "actions.log"

MAX_TOOL_TURNS = 6
ASK_TIMEOUT_S = 120
RESULT_CAP = 4000          # chars of tool output fed back to the model

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# --- ask (confirmation) registry --------------------------------------------

_asks = {}
_asks_lock = threading.Lock()
_ask_seq = [0]


def new_ask(summary, tool, args):
    with _asks_lock:
        _ask_seq[0] += 1
        ask_id = _ask_seq[0]
        ev = threading.Event()
        _asks[ask_id] = {"ev": ev, "approved": False,
                         "summary": summary, "tool": tool, "args": args}
    return ask_id


def resolve_ask(ask_id, approved):
    """Called from the UI thread when the user clicks Allow / Deny."""
    with _asks_lock:
        rec = _asks.get(ask_id)
        if rec is None:
            return False
        rec["approved"] = bool(approved)
        rec["ev"].set()
    _log_action("ASK  %s -> %s" % (rec["summary"],
                                   "approved" if approved else "DENIED"))
    return True


def wait_ask(ask_id, timeout=ASK_TIMEOUT_S):
    with _asks_lock:
        rec = _asks.get(ask_id)
    if rec is None:
        return False
    rec["ev"].wait(timeout)
    with _asks_lock:
        _asks.pop(ask_id, None)
    return rec["ev"].is_set() and rec["approved"]


def pending_ask_count():
    with _asks_lock:
        return len(_asks)


def _log_action(line):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(ACTION_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


# --- safety ------------------------------------------------------------------

# Commands that look destructive: always confirm, even in full-auto mode.
DANGER_RE = re.compile(
    r"\b(format|diskpart|bcdedit|mkfs|reg\s+delete|shutdown|"
    r"restart-computer|stop-computer|remove-item|clear-disk|format-volume|"
    r"set-disk|remove-partition|taskkill|stop-process|stop-job)\b|"
    r"\brm\s+-[a-z]*r[a-z]*f|\bdel\s+/[afs]|\brd\s+/s",
    re.IGNORECASE)


def is_dangerous(cmd):
    return bool(DANGER_RE.search(cmd or ""))


READ_ONLY = {"system_info", "processes", "list_dir", "read_file",
             "search_files", "window_list", "volume_get", "screen_text",
             "mouse_move", "mouse_scroll"}


def gate(tool, args, mode):
    """-> 'allow' | 'confirm' | 'block'"""
    if mode == "off":
        return "block"
    if tool == "run" and is_dangerous(args.get("cmd", "")):
        return "confirm"                      # destructive: always ask
    if tool == "chrome" and \
            str(args.get("action", "extract")).lower() == "extract":
        return "allow"                        # reading a page is harmless
    if tool in READ_ONLY:
        return "allow"
    if mode == "auto":
        return "allow"
    return "confirm"


# --- Win32 helpers -----------------------------------------------------------

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _foreground_title():
    try:
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(1, n + 1))
        user32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value or ""
    except Exception:
        return ""


def _cpu_percent(sample_s=0.15):
    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wt.DWORD),
                    ("dwHighDateTime", wt.DWORD)]

    def read_times():
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                ctypes.byref(user))
        def val(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return val(idle), val(kernel), val(user)

    try:
        a = read_times()
        time.sleep(sample_s)
        b = read_times()
        kernel_total = (b[1] - a[1]) + (b[2] - a[2])
        idle_total = b[0] - a[0]
        if kernel_total <= 0:
            return 0.0
        return round(100.0 * (1.0 - idle_total / kernel_total), 1)
    except Exception:
        return 0.0


def _cpu_name():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except Exception:
        return "unknown CPU"


def _mem():
    st = _MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    total = st.ullTotalPhys / (1024.0 ** 3)
    avail = st.ullAvailPhys / (1024.0 ** 3)
    return total, avail, st.dwMemoryLoad


def _disk(path="C:\\"):
    import ctypes.wintypes as _wt
    free = ctypes.c_ulonglong()
    total = ctypes.c_ulonglong()
    ok = kernel32.GetDiskFreeSpaceExW(path, ctypes.byref(free), ctypes.byref(
        total), None)
    if not ok:
        return None
    return free.value / (1024.0 ** 3), total.value / (1024.0 ** 3)


def _uptime_h():
    try:
        ms = kernel32.GetTickCount64()
        return ms / 3600000.0
    except Exception:
        return 0.0


def _top_processes(limit=6):
    """Top processes by working set (no per-process CPU: too slow for a
    snappy snapshot)."""
    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010

    class PROCENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_ulonglong),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                    ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    out = []
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return []
    try:
        entry = PROCENTRY32()
        entry.dwSize = ctypes.sizeof(PROCENTRY32)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return []
        while True:
            pid = entry.th32ProcessID
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION |
                                     PROCESS_VM_READ, False, pid)
            ws = 0
            if h:
                try:
                    pmc = PMC()
                    pmc.cb = ctypes.sizeof(PMC)
                    if psapi_GetProcessMemoryInfo(h, ctypes.byref(pmc),
                                                  pmc.cb):
                        ws = pmc.WorkingSetSize
                finally:
                    kernel32.CloseHandle(h)
            if ws and entry.szExeFile:
                out.append((ws, pid, entry.szExeFile))
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)
    out.sort(reverse=True)
    seen = {}
    for ws, pid, name in out:
        seen[name] = seen.get(name, 0) + ws
    top = sorted(seen.items(), key=lambda kv: -kv[1])[:limit]
    return ["%s %.2f GB" % (n, ws / (1024.0 ** 3)) for n, ws in top]


psapi = ctypes.windll.psapi
psapi_GetProcessMemoryInfo = psapi.GetProcessMemoryInfo


# --- tools -------------------------------------------------------------------

MAX_READ_BYTES = 20 * 1024
_SEARCH_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv",
                     "venv", ".cache"}


def tool_system_info(args):
    total, avail, pct = _mem()
    disk = _disk()
    parts = [
        "OS: Windows build %d" % sys_build(),
        "CPU: %s (%.0f%% busy now)" % (_cpu_name(), _cpu_percent()),
        "RAM: %.1f of %.1f GB used (%.0f%%)" % (total - avail, total, pct),
    ]
    if disk:
        parts.append("Disk C: %.0f of %.0f GB free" % disk)
    up = _uptime_h()
    parts.append("uptime: %dh%02dm" % (int(up), int((up % 1) * 60)))
    fg = _foreground_title()
    if fg:
        parts.append("focused window: %s" % fg[:60])
    top = _top_processes(5)
    if top:
        parts.append("top apps by RAM: " + ", ".join(top))
    return "; ".join(parts)


def sys_build():
    try:
        return int(platform_version_build())
    except Exception:
        return 0


def platform_version_build():
    try:
        return winreg.QueryValueEx(
            winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"),
            "CurrentBuildNumber")[0]
    except Exception:
        return "0"


def tool_processes(args):
    try:
        limit = max(1, min(60, int((args or {}).get("limit", 12))))
    except (TypeError, ValueError):
        limit = 12
    return "\n".join(_top_processes_full(limit)) or "(no data)"


def _top_processes_full(limit=12):
    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_ulonglong),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                    ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    def proc_times(h):
        class FILETIME(ctypes.Structure):
            _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]
        c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                        ctypes.byref(k), ctypes.byref(u)):
            return None
        return ((u.hi << 32 | u.lo) + (k.hi << 32 | k.lo))

    entries = []
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return []
    try:
        entry = PROCENTRY32()
        entry.dwSize = ctypes.sizeof(PROCENTRY32)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return []
        while True:
            entries.append((entry.th32ProcessID, entry.szExeFile))
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)

    handles = []
    for pid, name in entries:
        if not pid or not name:
            continue
        h = kernel32.OpenProcess(0x1000, False, pid)
        if h:
            handles.append((h, pid, name))

    # sample cpu over a short window for the live set
    t0 = {}
    for h, pid, name in handles:
        t = proc_times(h)
        if t is not None:
            t0[h] = t
    time.sleep(0.2)
    rows = []
    for h, pid, name in handles:
        ws = 0
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if psapi_GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            ws = pmc.WorkingSetSize
        cpu = 0.0
        if h in t0:
            t1 = proc_times(h)
            if t1 is not None:
                ticks = t1 - t0[h]           # 100-ns units across 0.2 s
                cpu = max(0.0, min(99.0, ticks / 2e6 / 0.2 * 100.0))
        kernel32.CloseHandle(h)
        if ws or cpu:
            rows.append((ws + cpu * 1e9, "%-30s pid %-6d %6.1f%% cpu %7.2f GB"
                         % (name[:30], pid, cpu, ws / (1024.0 ** 3))))
    rows.sort(reverse=True)
    return [r[1] for r in rows[:limit]]


def tool_list_dir(args):
    raw = args.get("path") or os.path.expanduser("~")
    p = Path(raw).expanduser()
    if not p.is_dir():
        return "not a folder: %s" % raw
    items = []
    try:
        for child in p.iterdir():
            try:
                if child.is_dir():
                    items.append("📁 " + child.name)
                else:
                    items.append("%s  %.1f KB" % (child.name,
                                                  child.stat().st_size / 1024.0))
            except OSError:
                continue
    except PermissionError:
        return "permission denied: %s" % raw
    items.sort(key=lambda s: (s[0] != "\U0001F4C1", s.lower()))
    head = "folder %s (%d entries%s):" % (p, len(items),
                                          "" if len(items) <= 40 else ", showing 40")
    return head + "\n" + "\n".join(items[:40])


def tool_read_file(args):
    raw = args.get("path") or ""
    p = Path(raw).expanduser()
    if not p.is_file():
        return "no such file: %s" % raw
    try:
        data = p.read_bytes()[:MAX_READ_BYTES]
    except PermissionError:
        return "permission denied: %s" % raw
    if b"\x00" in data[:2048]:
        return "binary file (%.1f KB) - not showing content" % (
            p.stat().st_size / 1024.0)
    text = data.decode("utf-8", "replace")
    note = "" if p.stat().st_size <= MAX_READ_BYTES else "\n...(truncated)"
    return text[:RESULT_CAP] + note


def tool_search_files(args):
    query = (args.get("query") or "").strip()
    if not query:
        return "no query given"
    root = Path(args.get("dir") or os.path.expanduser("~")).expanduser()
    if not root.is_dir():
        return "not a folder: %s" % root
    want_content = bool(args.get("content"))
    try:
        pattern = re.compile(".*".join(
            re.escape(part) for part in query.split("*")), re.I)
    except re.error:
        pattern = re.compile(re.escape(query), re.I)

    hits, scanned, content_hits = [], 0, []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP_DIRS
                       and not d.startswith(".")]
        for fn in filenames:
            scanned += 1
            if scanned > 60000:
                return "searched 60000 files - stopping\n" + "\n".join(hits[:30])
            if pattern.search(fn):
                hits.append(os.path.join(dirpath, fn))
                if len(hits) >= 30:
                    break
        if len(hits) >= 30:
            break
        if want_content and len(content_hits) < 12 and scanned < 60000:
            for fn in filenames[:6]:
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) > 512 * 1024:
                        continue
                    with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                        for i, line in enumerate(fh):
                            if i > 4000:
                                break
                            if pattern.search(line):
                                content_hits.append("%s: %s" % (
                                    fp, line.strip()[:120]))
                                break
                except OSError:
                    continue
    out = ["name matches (%d):" % len(hits)] if hits else ["no name matches"]
    out += ["  " + h for h in hits[:30]]
    if want_content:
        out.append("content matches (%d):" % len(content_hits))
        out += ["  " + c for c in content_hits[:12]]
    return "\n".join(out)


def tool_window_list(args):
    rows = []

    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        if buf.value.strip():
            rows.append("%-7d %s" % (hwnd, buf.value[:70]))
        return True
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows(EnumProc(cb), 0)
    return "visible windows:\n" + "\n".join(rows[:25])


WDA_EXCLUDEFROMCAPTURE = 0x11


def _set_affinity(hwnd, hide):
    try:
        hwnd = int(hwnd)
    except (TypeError, ValueError):
        return "invalid hwnd"
    if not user32.IsWindow(hwnd):
        return "no such window: %s" % hwnd
    r = user32.SetWindowDisplayAffinity(hwnd,
                                        WDA_EXCLUDEFROMCAPTURE if hide else 0)
    return ("hidden from capture" if r else "SetWindowDisplayAffinity failed") \
        + " (hwnd %d)" % hwnd


def tool_window_hide(args):
    return _set_affinity(args.get("hwnd"), True)


def tool_window_show(args):
    try:
        hwnd = int(args.get("hwnd"))
    except (TypeError, ValueError):
        return "invalid hwnd"
    _set_affinity(hwnd, False)
    user32.ShowWindow(hwnd, 9)              # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return "shown (hwnd %d)" % hwnd


def tool_open(args):
    target = (args.get("target") or "").strip()
    if not target:
        return "nothing to open"
    SEE_MASK_FLAG_NO_UI = 0x400
    rc = user32.ShellExecuteW(None, "open", target, None, None,
                              SEE_MASK_FLAG_NO_UI)
    code = ctypes.cast(rc, ctypes.c_void_p).value or 0
    if code <= 32:
        return "could not open %s (code %d)" % (target, code)
    _log_action("OPEN %s" % target)
    return "opened %s" % target


def tool_run(args):
    cmd = (args.get("cmd") or "").strip()
    if not cmd:
        return "no command given"
    timeout = max(5, min(60, int(args.get("timeout_s", 25))))
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             cmd],
            capture_output=True, text=True, errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return "command timed out after %ds" % timeout
    except OSError as exc:
        return "could not run: %s" % exc
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append("(stderr) " + err)
    if proc.returncode:
        parts.append("exit code %d" % proc.returncode)
    body = "\n".join(parts) or "(no output)"
    _log_action("RUN %s -> rc=%d" % (cmd[:120], proc.returncode))
    return body[:RESULT_CAP]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

_VK_TOKENS = {"{enter}": 0x0D, "{tab}": 0x09, "{esc}": 0x1B,
              "{backspace}": 0x08, "{space}": 0x20}


def tool_send_keys(args):
    text = (args.get("text") or "")
    if not text:
        return "nothing to type"
    inputs = []
    # build inputs: {token} special keys, everything else unicode chars
    i = 0
    while i < len(text):
        if text[i] == "}":
            i += 1
            continue
        if text[i] == "{":
            end = text.find("}", i)
            if end != -1:
                token = text[i:end + 1].lower()
                vk = _VK_TOKENS.get(token)
                if vk:
                    inputs.append((vk, 0))                       # key down
                    inputs.append((vk, KEYEVENTF_KEYUP))         # key up
                    i = end + 1
                    continue
        ch = text[i]
        inputs.append(("u", ord(ch)))
        i += 1
    if not inputs:
        return "nothing typeable"
    arr = (_INPUT * len(inputs))()
    for idx, item in enumerate(inputs):
        if item[0] == "u":
            arr[idx].type = INPUT_KEYBOARD
            arr[idx].ki = _KEYBDINPUT(0, item[1],
                                      KEYEVENTF_UNICODE, 0, 0)
        else:
            arr[idx].type = INPUT_KEYBOARD
            arr[idx].ki = _KEYBDINPUT(item[0], 0, item[1], 0, 0)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
    _log_action("KEYS %r -> %d inputs" % (text[:60], sent))
    return "typed %d events into the focused window" % sent


# --- mouse -------------------------------------------------------------------

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


class _POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


def _mouse(flags, dx=0, dy=0, data=0):
    arr = (_INPUT * 1)()
    arr[0].type = INPUT_MOUSE
    arr[0].mi = _MOUSEINPUT(int(dx), int(dy), int(data) & 0xFFFFFFFF,
                            int(flags), 0, 0)
    return user32.SendInput(1, arr, ctypes.sizeof(_INPUT))


def _cursor_pos():
    p = _POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _to_absolute(x, y):
    """screen px -> SendInput 0..65535 coords on the virtual desktop"""
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = max(user32.GetSystemMetrics(78), 1), max(user32.GetSystemMetrics(79), 1)
    return (int((x - vx) * 65535 / max(vw - 1, 1)),
            int((y - vy) * 65535 / max(vh - 1, 1)))


def _move_to(args):
    """Move the cursor if the args carry x/y (absolute) — returns pos."""
    if "x" in args or "y" in args:
        x, y = int(args.get("x", 0)), int(args.get("y", 0))
        ax, ay = _to_absolute(x, y)
        _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE |
               MOUSEEVENTF_VIRTUALDESK, ax, ay)
        time.sleep(0.05)
        return x, y
    if "dx" in args or "dy" in args:
        _mouse(MOUSEEVENTF_MOVE, int(args.get("dx", 0)), int(args.get("dy", 0)))
        time.sleep(0.03)
    return _cursor_pos()


def tool_mouse_move(args):
    x, y = _move_to(args)
    return "cursor at %d,%d" % (x, y)


def tool_mouse_click(args):
    button = str(args.get("button") or "left").lower()
    flags = _BUTTON_FLAGS.get(button)
    if flags is None:
        return "unknown button %r (use left, right or middle)" % button
    x, y = _move_to(args)
    double = bool(args.get("double"))
    for _ in range(2 if double else 1):
        _mouse(flags[0])
        time.sleep(0.04)
        _mouse(flags[1])
        time.sleep(0.05)
    _log_action("CLICK %s%s at %d,%d" % (button, " x2" if double else "", x, y))
    return "%s%s clicked at %d,%d" % (button, " double" if double else "",
                                      x, y)


def tool_mouse_scroll(args):
    clicks = int(args.get("dy", -3))
    at_point = "x" in args or "y" in args
    if at_point:
        x, y = _move_to(args)
    _mouse(MOUSEEVENTF_WHEEL, 0, 0, clicks * 120)
    if at_point:
        return "scrolled %d clicks at %d,%d" % (clicks, x, y)
    return "scrolled %d clicks" % clicks


# --- screen reading ----------------------------------------------------------

WM_GETTEXT = 0x000D

try:
    user32.SendMessageTimeoutW.argtypes = [wt.HWND, ctypes.c_uint,
                                           ctypes.c_size_t, ctypes.c_void_p,
                                           ctypes.c_uint, ctypes.c_uint,
                                           ctypes.c_void_p]
    user32.SendMessageTimeoutW.restype = ctypes.c_size_t
except (AttributeError, OSError):
    pass


def _exe_of(pid):
    try:
        kernel32.OpenProcess.restype = ctypes.c_void_p
    except (AttributeError, OSError):
        pass
    h = kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFO
    if not h:
        return ""
    try:
        size = wt.DWORD(512)
        buf = ctypes.create_unicode_buffer(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return ""


def tool_screen_text(args):
    """What is the window in front showing? Title + its control text."""
    fg = user32.GetForegroundWindow()
    if not fg:
        return "nothing is in front"
    n = user32.GetWindowTextLengthW(fg)
    tbuf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(fg, tbuf, len(tbuf))
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    lines = ["FOCUSED: '%s' [%s] hwnd=%d" % (
        (tbuf.value or "(untitled)")[:90], _exe_of(pid.value) or "?", fg)]

    texts = []
    EnumChild = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def child_cb(ch, _l):
        if user32.IsWindowVisible(ch):
            buf = ctypes.create_unicode_buffer(512)
            sent = ctypes.c_size_t()
            if user32.SendMessageTimeoutW(ch, WM_GETTEXT, 511, buf,
                                          0x0002, 200,  # SMTO_ABORTIFHUNG
                                          ctypes.byref(sent)) and buf.value.strip():
                t = " ".join(buf.value.split())
                if t and t not in texts:
                    texts.append(t)
        return True
    user32.EnumChildWindows(fg, EnumChild(child_cb), 0)

    others = []

    def top_cb(hwnd, _l):
        if hwnd != fg and user32.IsWindowVisible(hwnd):
            nn = user32.GetWindowTextLengthW(hwnd)
            if nn > 0:
                b = ctypes.create_unicode_buffer(nn + 1)
                user32.GetWindowTextW(hwnd, b, len(b))
                if b.value.strip():
                    others.append(b.value[:70])
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(
        top_cb), 0)

    if texts:
        lines.append("on screen: " + " | ".join(texts)[:3000])
    if others:
        lines.append("also open: " + ", ".join(others[:8]))
    return "\n".join(lines)[:RESULT_CAP]


# --- browser control (Chrome extension bridge) -------------------------------

CHROME_OPS = ("extract", "open", "search", "click", "type", "press", "scroll")


def tool_chrome(args):
    import browserlink
    action = str(args.get("action") or "extract").lower().strip()
    if action not in CHROME_OPS:
        return "unknown chrome action %r; use one of: %s" % (
            action, ", ".join(CHROME_OPS))
    cmd = {"op": action}
    for key in ("url", "query", "text", "selector", "key", "dy"):
        if args.get(key) not in (None, ""):
            cmd[key] = args[key]
    if action in ("open", "search") and not browserlink.connected():
        url = (browserlink.search_url(cmd.get("query", ""))
               if action == "search" else str(cmd.get("url") or ""))
        if not url:
            return "chrome %s needs a %s" % (action, action)
        tool_open({"target": url})
        _log_action("CHROME %s %s (default browser - extension offline)"
                    % (action, url[:80]))
        return ("opened in the default browser, but the ShhDock Chrome "
                "extension is not connected so I can't read the page back. "
                + browserlink.install_steps())
    if not browserlink.connected():
        for _ in range(6):                       # give it a few seconds
            time.sleep(0.5)                      # to show up
            if browserlink.connected():
                break
        else:
            return ("chrome %s failed: the extension is not connected - is "
                    "Chrome open with 'ShhDock Browser Link' enabled in "
                    "chrome://extensions? %s" % (action,
                                                 browserlink.install_steps()))
    res = browserlink.send(cmd, timeout=25)
    ok = bool(res.get("ok"))
    out = (res.get("out") or "").strip() or ("chrome %s done" % action)
    _log_action("CHROME %s -> %s" % (action, "ok" if ok else "fail"))
    if not ok:
        return "chrome %s failed: %s" % (action, out)
    return out[:RESULT_CAP]


_MMDEV_CLSID = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"   # MMDeviceEnumerator
_IID_IMMDEVICE_ENUM = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IAUDIO_EPVOL = "{5CDF2C82-841E-4546-9722-0CF74078229A}"


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s):
    g = _GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


def _default_render_device():
    """(enumerator, default render IMMDevice) via raw COM."""
    ctypes.windll.ole32.CoInitializeEx(None, 0)
    clsid = _guid(_MMDEV_CLSID)
    iid = _guid(_IID_IMMDEVICE_ENUM)
    enumerator = ctypes.c_void_p()
    hr = ctypes.windll.ole32.CoCreateInstance(
        ctypes.byref(clsid), None, 23,  # CLSCTX_ALL
        ctypes.byref(iid), ctypes.byref(enumerator))
    if hr != 0 or not enumerator.value:
        raise RuntimeError("CoCreateInstance(MMDeviceEnumerator) 0x%08x" % hr)
    # interface ptr -> vtable ptr -> function table
    vptr = ctypes.cast(enumerator, ctypes.POINTER(ctypes.c_void_p))[0]
    vtbl = ctypes.cast(vptr, ctypes.POINTER(ctypes.c_void_p * 40))[0]
    get_default = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p))(vtbl[4])
    device = ctypes.c_void_p()
    hr = get_default(enumerator, 0, 0, ctypes.byref(device))
    if hr != 0 or not device.value:
        raise RuntimeError("GetDefaultAudioEndpoint 0x%08x" % hr)
    return enumerator, device


def _activate_audio_endpoint_volume(device):
    vptr = ctypes.cast(device, ctypes.POINTER(ctypes.c_void_p))[0]
    vtbl = ctypes.cast(vptr, ctypes.POINTER(ctypes.c_void_p * 40))[0]
    # IMMDevice::Activate(REFIID, DWORD clsCtx, PROPVARIANT *pParams,
    #                     void **ppInterface)
    activate = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID),
        ctypes.c_int, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p))(vtbl[3])
    epvol = ctypes.c_void_p()
    iid = _guid(_IAUDIO_EPVOL)
    hr = activate(device, ctypes.byref(iid), 23, None,
                  ctypes.byref(epvol))
    if hr != 0 or not epvol.value:
        raise RuntimeError("Activate(IAudioEndpointVolume) 0x%08x" % hr)
    return epvol


def tool_volume(args):
    action = (args.get("action") or "get").split()[0].lower()
    try:
        _enum, device = _default_render_device()
        epvol = _activate_audio_endpoint_volume(device)
        epvol_fptr = ctypes.cast(epvol, ctypes.POINTER(ctypes.c_void_p))[0]
        fns = ctypes.cast(epvol_fptr,
                          ctypes.POINTER(ctypes.c_void_p * 40))[0]
        # vtable (endpointvolume.idl): 7=SetMasterVolumeLevelScalar,
        # 9=GetMasterVolumeLevelScalar
        get_scalar = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_float))(fns[9])
        set_scalar = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                      ctypes.c_float)(fns[7])
        if action in ("get", "level"):
            val = ctypes.c_float()
            if get_scalar(epvol, ctypes.byref(val)) != 0:
                return "could not read volume"
            return "master volume: %.0f%%" % (val.value * 100)
        # set
        nums = re.findall(r"\d+", args.get("action", ""))
        if not nums:
            return "usage: volume set 40"
        level = max(0, min(100, int(nums[0]))) / 100.0
        if set_scalar(epvol, level) != 0:
            return "could not set volume"
        _log_action("VOLUME set %.0f%%" % (level * 100))
        return "volume set to %.0f%%" % (level * 100)
    except RuntimeError as exc:
        return "volume control failed: %s" % exc


# name -> (func, args help, description)
TOOLS = {
    "system_info": (lambda a: tool_system_info(a), "{}",
                    "CPU, RAM, disk, OS, focused window, top apps"),
    "processes": (tool_processes, "{}",
                  "running programs with CPU and memory"),
    "list_dir": (tool_list_dir, '{"path": "C:\\\\folder"}',
                 "list a folder"),
    "read_file": (tool_read_file, '{"path": "C:\\\\file.txt"}',
                  "read a text file (max 20 KB)"),
    "search_files": (tool_search_files,
                     '{"query": "report*.docx", "dir": "C:\\\\Users", '
                     '"content": false}', "find files by name/content"),
    "window_list": (lambda a: tool_window_list(a), "{}",
                    "visible windows with hwnd ids"),
    "window_hide": (tool_window_hide, '{"hwnd": 12345}',
                    "hide a window from screen share"),
    "window_show": (tool_window_show, '{"hwnd": 12345}',
                    "show a hidden window again"),
    "open": (tool_open, '{"target": "notepad or https://... or a path"}',
             "open an app, file or website"),
    "run": (tool_run, '{"cmd": "Get-Process | Select -First 5"}',
            "run a PowerShell command (asks permission)"),
    "send_keys": (tool_send_keys, '{"text": "hello {enter}"}',
                  "type text into the focused window (asks permission)"),
    "volume": (tool_volume, '{"action": "get or set 40"}',
               "read or set the master speaker volume"),
    "screen_text": (tool_screen_text, "{}",
                    "read the text the window in front is showing"),
    "mouse_move": (tool_mouse_move, '{"x": 600, "y": 300} or {"dx": 20, "dy": 0}',
                   "move the mouse cursor"),
    "mouse_click": (tool_mouse_click,
                    '{"x": 600, "y": 300, "button": "left", "double": false}',
                    "click at a point (asks permission)"),
    "mouse_scroll": (tool_mouse_scroll, '{"dy": -3}',
                     "scroll the mouse wheel"),
    "chrome": (tool_chrome,
               '{"action": "extract|open|search|click|type|press|scroll", '
               '"url/query/text/selector/key/dy": "..."}',
               "drive Chrome through the extension (most actions ask)"),
}


def execute_tool(name, args):
    rec = TOOLS.get(name)
    if rec is None:
        return "unknown tool: %s" % name
    try:
        return rec[0](args or {})
    except Exception as exc:
        return "tool %s failed: %s" % (name, exc)


def summary_for(name, args):
    if name == "run":
        return "run: %s" % (args.get("cmd", "")[:90])
    if name == "open":
        return "open: %s" % (args.get("target", "")[:80])
    if name == "send_keys":
        return "type: %s" % (args.get("text", "")[:80])
    if name == "window_hide":
        return "hide window hwnd %s" % args.get("hwnd")
    if name == "window_show":
        return "show window hwnd %s" % args.get("hwnd")
    if name == "volume":
        return "volume: %s" % (args.get("action", "get")[:40])
    if name == "read_file":
        return "read: %s" % args.get("path", "")[:80]
    if name == "list_dir":
        return "list: %s" % args.get("path", "")[:80]
    if name == "search_files":
        return "search: %s" % args.get("query", "")[:60]
    if name == "screen_text":
        return "read what's on screen"
    if name == "mouse_move":
        return "move mouse to %s,%s" % (args.get("x", "?"), args.get("y", "?"))
    if name == "mouse_scroll":
        return "scroll %s" % args.get("dy", -3)
    if name == "mouse_click":
        return "click %s at %s,%s" % (args.get("button", "left"),
                                      args.get("x", "?"), args.get("y", "?"))
    if name == "chrome":
        detail = (args.get("query") or args.get("url") or args.get("text") or
                  args.get("selector") or args.get("key") or "")
        return "chrome %s %s" % (args.get("action", "extract"),
                                 str(detail)[:60])
    return name


# --- prompt ------------------------------------------------------------------

BASE_PROMPT = (
    "You are ShhDock, a fast assistant in a tiny hidden bar at the top of "
    "the user's screen while they game and stream. You run fully on their "
    "PC. Answer in plain text only: no markdown, no bullets, no emojis, no "
    "preamble. Under 55 words unless asked for more. Be direct.\n\n"
    "CURRENT STATE OF THIS PC:\n{snapshot}\n"
)

AGENT_PROTOCOL = """

TOOLS: you can operate this computer. To use one, reply with EXACTLY one
line and nothing else:  {"tool":"NAME","args":{ARGS}}
Available:
system_info {} - live CPU/RAM/disk/window info
processes {} - running programs with cpu and memory
list_dir {"path":"..."} - list a folder
read_file {"path":"..."} - read a text file
search_files {"query":"name*.txt","dir":"C:\\Users","content":false}
window_list {} - visible windows and their hwnd ids
window_hide {"hwnd":123} / window_show {"hwnd":123} - capture-hide a window
open {"target":"app or URL or path"} - open something
run {"cmd":"PowerShell command"} - run a command (always asks permission)
send_keys {"text":"type this {enter}"} - type into the focused window (asks)
volume {"action":"get or set 40"} - master speaker volume
screen_text {} - read what the window in front of you is showing
mouse_move {"x":600,"y":300} or {"dx":20,"dy":0} - move the cursor
mouse_scroll {"dy":-3} - scroll the wheel (these two need no permission)
mouse_click {"x":600,"y":300,"button":"left"} - click there (asks permission)
chrome {"action":"extract"} - read the active Chrome tab (read-only)
chrome {"action":"search","query":"..."} - Google it in a new tab (asks)
chrome {"action":"open","url":"https://..."} / {"action":"click","text":"link"} /
  {"action":"type","text":"hi"} / {"action":"press","key":"Enter"} /
  {"action":"scroll","dy":-600} - drive Chrome through the extension (asks)
After a call you will receive TOOL RESULT. Then either call another tool or
answer in plain text. One tool per message. Never invent tool results. If a
tool was declined, do not retry it - answer a different way.
"""

def parse_tool(text):
    """Find a {"tool": ..., "args": {...}} object in the model's reply.
    Scans brace candidates from each '{' to the LAST '}' so nested args
    survive; falls back to per-line parsing."""
    if not text:
        return None
    cleaned = text.replace("```json", "").replace("```", "")
    candidates = []
    start = cleaned.find("{")
    while start != -1:
        end = cleaned.rfind("}")
        if end > start:
            candidates.append(cleaned[start:end + 1])
        start = cleaned.find("{", start + 1)
    candidates += [ln.strip() for ln in cleaned.splitlines()
                   if ln.strip().startswith("{")]
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool")
        if not isinstance(name, str) or name not in TOOLS:
            continue
        args = obj.get("args")
        if args is not None and not isinstance(args, dict):
            args = {}
        return {"tool": name, "args": args or {}}
    return None


def system_snapshot():
    total, avail, pct = _mem()
    disk = _disk()
    bits = [
        datetime.now().strftime("%a %b %d, %H:%M"),
        "Windows build %s" % platform_version_build(),
        "CPU %s at %.0f%%" % (_cpu_name(), _cpu_percent(0.12)),
        "RAM %.1f/%.1f GB used" % (total - avail, total),
    ]
    if disk:
        bits.append("C: %.0f GB free" % disk[0])
    up = _uptime_h()
    bits.append("up %dh%02dm" % (int(up), int((up % 1) * 60)))
    fg = _foreground_title()
    if fg:
        bits.append("focused: '%s'" % fg[:48])
    top = _top_processes(4)
    if top:
        bits.append("top: " + ", ".join(top))
    return " | ".join(bits)


def _call_context_lines():
    try:
        import callaudio
        lines = callaudio.recent_lines(5)
        if lines:
            return "\nrecent call audio (what others said): " + " // ".join(
                lines)
    except Exception:
        pass
    return ""


# --- the agent loop ----------------------------------------------------------

def run_agent(question, server, events, settings):
    """Answer one question, using tools when the model asks for them.
    Emits 'tool' / 'ask' / 'answer' (or 'answer' with an error) events."""
    mode = settings.get("agent", "ask")
    snap = system_snapshot() + _call_context_lines()
    protocol = AGENT_PROTOCOL if mode != "off" else ""
    system = BASE_PROMPT.replace("{snapshot}", snap) + protocol
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": question}]
    final = None
    for _turn in range(MAX_TOOL_TURNS):
        reply, err = server.chat(messages, max_tokens=320)
        if err or not reply:
            final = "Hmm, no answer: %s" % (err or "empty")
            break
        call = parse_tool(reply) if mode != "off" else None
        if call is None:
            final = reply
            break
        name, args = call["tool"], call["args"]
        summary = summary_for(name, args)
        events.put({"t": "tool", "phase": "start", "text": summary})
        decision = gate(name, args, mode)
        if decision == "block":
            result = "tool %s is disabled in settings" % name
        elif decision == "confirm":
            ask_id = new_ask(summary, name, args)
            events.put({"t": "ask", "id": ask_id, "summary": summary,
                        "danger": name == "run" and is_dangerous(
                            args.get("cmd", ""))})
            approved = wait_ask(ask_id)
            if approved:
                result = execute_tool(name, args)
                _log_action("ALLOW %s" % summary)
            else:
                result = "The user DECLINED this action. Do not retry it."
        else:
            result = execute_tool(name, args)
        events.put({"t": "tool", "phase": "result", "text":
                    "%s -> %s" % (summary, result[:160])})
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user",
                         "content": "TOOL RESULT: " + result[:RESULT_CAP]})
    else:
        final = "I kept calling tools without finishing - please ask again."
    final = (final or "").strip() or "(no answer)"
    events.put({"t": "answer", "text": final})
    return final
