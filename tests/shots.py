"""Capture clean README screenshots.

Each shot isolates one window and grabs only its own bounding box, after
hiding every other visible window in the same screen region, so neighbouring
apps (ads, browsers) can't bleed into frame.  The dock's capture-hiding is
lifted only for the instant of the grab.
"""
import ctypes
import ctypes.wintypes as wt
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine   # noqa: E402
import dockui   # noqa: E402

from PIL import Image, ImageDraw, ImageGrab  # noqa: E402

OUT = Path(__file__).parent.parent / "assets"
OUT.mkdir(exist_ok=True)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
PW_RENDERFULLCONTENT = 0x00000002


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


def print_window(hwnd):
    """Capture a window's own rendered buffer via PrintWindow, so whatever
    overlaps it on screen can never bleed into the shot."""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
    gdi32.SelectObject(mem, old)
    user32.ReleaseDC(0, hdc)
    if not ok:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        return None
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h              # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
    return img.convert("RGB")


def tk_hwnd(widget):
    """The visible top-level HWND behind a Tk widget."""
    widget.update_idletasks()
    return user32.GetAncestor(widget.winfo_id(), 2)   # GA_ROOT = 2


def card_composite(dock_box, bubble_box, name):
    """Grab the dock and bubble separately, then lay them on a clean dark
    card so whatever sits behind them on screen can't bleed into the shot."""
    dock_img = ImageGrab.grab(bbox=dock_box, all_screens=True)
    bub_img = ImageGrab.grab(bbox=bubble_box, all_screens=True)
    pad = 8
    left = min(dock_box[0], bubble_box[0]) - pad
    top = min(dock_box[1], bubble_box[1]) - pad
    right = max(dock_box[2], bubble_box[2]) + pad
    bottom = max(dock_box[3], bubble_box[3]) + pad
    w, h = right - left, bottom - top
    card = Image.new("RGB", (w, h), (18, 20, 27))
    d = ImageDraw.Draw(card)
    for i in range(h):                       # faint vertical gradient
        t = i / max(1, h - 1)
        c = (int(18 + 8 * t), int(20 + 9 * t), int(27 + 13 * t))
        d.line([(0, i), (w, i)], fill=c)
    card.paste(dock_img, (dock_box[0] - left, dock_box[1] - top))
    card.paste(bub_img, (bubble_box[0] - left, bubble_box[1] - top))
    card.save(OUT / name)
    print("  %-18s %dx%d -> %s" % (name, w, h, name))


def solo_card(window, app, name, pad=14):
    """Capture one dialog via PrintWindow onto a clean card — no neighbours,
    no overlap, no desktop bleed. The dialogs are capture-hidden (that's the
    point), and PrintWindow honours that, so lift the affinity for the
    instant of the grab and put it straight back."""
    dockui.set_hidden_from_capture(app.winfo_id(), True)   # keep dock hidden
    hwnd = tk_hwnd(window)
    dockui.set_hidden_from_capture(hwnd, False)            # lift the dialog
    time.sleep(0.15)
    img = None
    for _ in range(6):                     # a couple of tries to realize it
        window.update_idletasks()
        app.update()
        img = print_window(hwnd)
        if img is not None:
            break
        time.sleep(0.08)
    dockui.set_hidden_from_capture(hwnd, True)             # hide it again
    if img is None:
        raise RuntimeError("PrintWindow failed for %s" % name)
    w, h = img.width + pad * 2, img.height + pad * 2
    card = Image.new("RGB", (w, h), (18, 20, 27))
    card.paste(img, (pad, pad))
    card.save(OUT / name)
    print("  %-18s %dx%d -> %s" % (name, w, h, name))


def tk_box(widget):
    """Screen-space bounding box of a Tk widget, from its own coordinates."""
    widget.update_idletasks()
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    return (x, y, x + w, y + h)


def grab_box(bbox, name, pad=3):
    box = (bbox[0] + pad, bbox[1] + pad, bbox[2] - pad, bbox[3] - pad)
    img = ImageGrab.grab(bbox=box, all_screens=True)
    img.save(OUT / name)
    print("  %-18s %dx%d -> %s" % (name, img.width, img.height, name))
    return img


def main():
    settings = engine.load_settings()
    events = queue.Queue()
    server = engine.LlamaServer(events.put)
    dockui.Dock._boot = lambda self: None      # no model/mic boot for shots
    app = dockui.Dock(events, settings, server)
    app.update()
    time.sleep(0.6)
    app.update()

    # park the dock near the top-left of the primary screen, over the clear
    # desktop margin, so no other window sits under it during the shot.
    try:
        app.geometry("+180+120")
        app.update()
        time.sleep(0.2)
    except Exception:
        pass

    try:
        app._toggle_panel()             # open panel (empty state)
        events.put({"t": "stt", "state": "listening"})
        events.put({"t": "meter", "level": 0.5})
        app.update()
        time.sleep(0.3)
        for _ in range(4):
            app.update()
            time.sleep(0.03)

        # 1) empty panel
        dockui.set_hidden_from_capture(app.winfo_id(), False)
        grab_box(tk_box(app), "panel-empty.png")

        # 2) a realistic conversation, now including what a callmate said
        #    and an agent tool action
        convo = [("you", "what time is it?"),
                 ("shh", "It's about 3 pm on your clock."),
                 ("them", "push A — I have ult in 10"),
                 ("tool", "run: Get-Volume"),
                 ("tool", "run: Get-Volume -> master volume: 100%"),
                 ("you", "how long have I been in this match?"),
                 ("shh", "Roughly 40 minutes — you dropped in around 2:20.")]
        for tag, text in convo:
            events.put({"t": "stt", "state": "listening"})
            if tag == "you":
                events.put({"t": "speech_end", "text": text})
            elif tag == "shh":
                events.put({"t": "answer", "text": text})
            elif tag == "them":
                events.put({"t": "call_text", "text": text})
            else:
                events.put({"t": "tool", "phase": "result", "text": text})
            for _ in range(3):
                app.update()
                time.sleep(0.05)
        events.put({"t": "meter", "level": 0.5})
        for _ in range(3):
            app.update()
            time.sleep(0.04)
        grab_box(tk_box(app), "dock-panel.png")

        # 2b) agent confirmation bar (a pending tool that needs a yes/no)
        import agent as agent_mod
        ask_id = agent_mod.new_ask("run: Get-Volume", "run",
                                   {"cmd": "Get-Volume"})
        events.put({"t": "tool", "phase": "start", "text": "run: Get-Volume"})
        events.put({"t": "ask", "id": ask_id, "summary": "run: Get-Volume",
                    "danger": False})
        for _ in range(6):
            app.update()
            time.sleep(0.05)
        grab_box(tk_box(app), "dock-agent.png")
        app._resolve_ask(False)             # hide the bar again
        app.update()

        # 3) bubble (panel closed, answer under the dock)
        app._toggle_panel()
        app.update()
        events.put({"t": "stt", "state": "listening"})
        events.put({"t": "meter", "level": 0.42})
        events.put({"t": "answer",
                    "text": "It's 8:22 PM — you've been in this match for "
                            "40 minutes."})
        for _ in range(6):
            app.update()
            time.sleep(0.05)
        assert app.bubble is not None
        for _ in range(4):
            app.bubble.update_idletasks()
            app.update()
            time.sleep(0.04)
        # the answer bubble is capture-hidden like the bar — lift it for
        # the shot, then put the hiding straight back
        dockui.set_hidden_from_capture(app.bubble.winfo_id(), False)
        time.sleep(0.15)
        dock_box = tk_box(app)
        bub_box = tk_box(app.bubble)
        card_composite(dock_box, bub_box, "dock-bubble.png")
        dockui.set_hidden_from_capture(app.bubble.winfo_id(), True)
        app._close_bubble()
        app.update()

        # 4) settings, on a clean card (call section shown live & consistent)
        settings["call_listen"] = True       # shot-only: box + status agree
        events.put({"t": "call_state", "state": "listening"})
        events.put({"t": "call_level", "level": 0.6})
        app._open_settings()
        for _ in range(8):
            app.update()
            time.sleep(0.05)
        swin = app.settings_win
        for _ in range(3):
            swin.update_idletasks()
            app.update()
            time.sleep(0.04)
        solo_card(swin, app, "settings.png")
        app._close_settings(swin)
        settings["call_listen"] = False     # restore (shot was fake)
        app.update()

        # 5) setup wizard, on a clean card
        app._open_setup()
        for _ in range(8):
            app.update()
            time.sleep(0.05)
        wwin = app.setup_win
        for _ in range(3):
            wwin.update_idletasks()
            app.update()
            time.sleep(0.04)
        solo_card(wwin, app, "setup.png")

        print("screenshots written to %s" % OUT)
    finally:
        app._quit()


if __name__ == "__main__":
    main()
