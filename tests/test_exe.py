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


def find_window(pid):
    """The visible top-level window belonging to our process (the dock's
    title is blank now — find it by PID, like any other lister would)."""
    found = []

    def cb(hwnd, _lparam):
        wpid = ctypes.c_ulong(0)
        if user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid)) and \
                wpid.value == pid and user32.IsWindowVisible(hwnd):
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
        hwnd = find_window(proc.pid)
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
