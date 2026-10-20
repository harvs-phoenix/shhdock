"""Smoke test for the built exe: launch, find its window, close it."""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

EXE = Path(__file__).resolve().parent.parent / "build_out" / "ShhDock.exe"
user32 = ctypes.windll.user32

WM_CLOSE = 0x0010
EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                              ctypes.c_void_p)


def child_pids(root):
    """A PyInstaller onefile app runs as root-bootloader -> child python,
    and the child owns the window. Collect root + children + grandchildren."""
    TH32CS_SNAPPROCESS = 0x2

    class PROCENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                    ("th32ProcessID", ctypes.c_ulong),
                    ("th32DefaultHeapID", ctypes.c_ulonglong),
                    ("th32ModuleID", ctypes.c_ulong),
                    ("cntThreads", ctypes.c_ulong),
                    ("th32ParentProcessID", ctypes.c_ulong),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.c_ulong),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    procs = []
    try:
        e = PROCENTRY32()
        e.dwSize = ctypes.sizeof(PROCENTRY32)
        if k32.Process32FirstW(snap, ctypes.byref(e)):
            while True:
                procs.append((e.th32ProcessID, e.th32ParentProcessID))
                if not k32.Process32NextW(snap, ctypes.byref(e)):
                    break
    finally:
        k32.CloseHandle(snap)
    kids = {pid for pid, ppid in procs if ppid == root}
    kids |= {pid for pid, ppid in procs if ppid in kids}
    kids.add(root)
    return kids


def find_window(pids):
    """The visible top-level window belonging to one of our processes (the
    dock's title is blank now — find it by PID, like any other lister)."""
    found = []

    def cb(hwnd, _lparam):
        wpid = ctypes.c_ulong(0)
        if user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid)) and \
                wpid.value in pids and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    user32.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else None


def main():
    print("starting %s" % EXE.name)
    proc = subprocess.Popen([str(EXE)],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW",
                                                  0))
    hwnd = None
    for _ in range(80):           # up to 16 s (onefile unpack + boot)
        time.sleep(0.2)
        hwnd = find_window(child_pids(proc.pid))
        if hwnd:
            break
    if not hwnd:
        proc.kill()
        raise SystemExit("FAIL: no ShhDock window appeared")
    print("window found: hwnd=0x%08x" % hwnd)

    time.sleep(6)                 # model + mic should be up by now
    if proc.poll() is not None:
        raise SystemExit("FAIL: process died with code %s" % proc.returncode)
    print("process alive after boot (pid %d)" % proc.pid)

    # visible windows on screen? EnumWindows found it; also check it's not
    # cloaked (a rendered, mapped window)
    DWMWA_CLOAKED = 14
    cloaked = ctypes.c_uint(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(hwnd), DWMWA_CLOAKED, ctypes.byref(cloaked),
        ctypes.sizeof(cloaked))
    print("cloaked=%d (0 = visible on screen)" % cloaked.value)

    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    for _ in range(40):
        time.sleep(0.25)
        if proc.poll() is not None:
            print("closed cleanly (exit code %s)" % proc.returncode)
            print("exe smoke test OK")
            return
    proc.kill()
    raise SystemExit("FAIL: window did not close")


if __name__ == "__main__":
    main()
