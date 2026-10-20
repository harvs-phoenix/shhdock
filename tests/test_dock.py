"""Dock UI end-to-end (in-process): constructs the real Dock, verifies
self-capture-hiding, drives events through the queue, checks the log/bubble
wiring, then screenshots the open panel.
"""
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine    # noqa: E402
import dockui    # noqa: E402

SHOT = Path(__file__).parent / "dock_open.png"


def main():
    settings = engine.load_settings()
    events = queue.Queue()
    server = engine.LlamaServer(events.put)
    # keep this test hermetic: no real mic, no real server
    orig_boot = dockui.Dock._boot
    dockui.Dock._boot = lambda self: None
    app = dockui.Dock(events, settings, server)
    app.update_idletasks()
    app.update()

    # 1) dock window exists at the top of the screen
    assert app.winfo_exists()
    y = app.winfo_y()
    assert 0 <= y <= 10, y
    print("dock at y=%d, size %dx%d" % (y, app.winfo_width(),
                                        app.winfo_height()))

    # 2) self-hiding from capture is active on our own top-level window
    #    (the dock retries WDA until the window is truly mapped — wait for it)
    app.update_idletasks()
    affinity = None
    for _ in range(40):
        app.update()
        affinity = dockui.get_display_affinity(app.winfo_id())
        if affinity == 0x11:
            break
        time.sleep(0.1)
    print("WDA of own window: 0x%x" % (affinity or 0))
    assert affinity == 0x11, "dock is not hidden from capture!"

    # 3) open the panel via the same code path as the button
    app._toggle_panel()
    app.update()
    assert app.panel_open
    assert app.winfo_height() > 300
    print("panel open, height %d" % app.winfo_height())

    # 4) pump synthetic events exactly like the assistant/server would
    for ev in ({"t": "meter", "level": 0.55},
               {"t": "stt", "state": "listening"},
               {"t": "llm", "state": "ready"},
               {"t": "speech_start"},
               {"t": "partial", "text": "what time"},
               {"t": "speech_end", "text": "what time is it"},
               {"t": "thinking"},
               {"t": "answer", "text": "It is about 3 pm on your clock."}):
        events.put(ev)
    for _ in range(6):
        app.update()
        time.sleep(0.06)
    log_text = app.log.get("1.0", "end")
    print("--- panel log ---")
    print(log_text.strip().encode("ascii", "replace").decode())
    assert "what time is it" in log_text
    assert "about 3 pm" in log_text
    assert app.status.cget("text") == "listening"

    # 5) bubble path with panel closed
    app._toggle_panel()
    app.update()
    events.put({"t": "answer", "text": "The answer pops up as a bubble."})
    for _ in range(4):
        app.update()
        time.sleep(0.05)
    assert app.bubble is not None, "bubble did not appear"
    app.update_idletasks()
    app.bubble.update_idletasks()
    print("bubble geometry: %s" % app.bubble.geometry())

    # screenshot with panel open (re-open for the shot).  WDA hides the dock
    # from ALL capture APIs including ImageGrab, so lift it for the shot.
    app._toggle_panel()
    app.update()
    app._close_bubble()

    dockui.set_hidden_from_capture(app.winfo_id(), False)
    time.sleep(0.2)
    app.update()

    from PIL import ImageGrab
    rx, ry = app.winfo_rootx(), app.winfo_rooty()
    img = ImageGrab.grab(bbox=(rx, ry, rx + app.winfo_width(),
                               ry + app.winfo_height()), all_screens=True)
    img.save(SHOT)
    print("screenshot: %s (%dx%d)" % (SHOT, img.width, img.height))

    # re-hide and verify it stuck
    dockui.set_hidden_from_capture(app.winfo_id(), True)
    time.sleep(0.1)
    assert dockui.get_display_affinity(app.winfo_id()) == 0x11

    # 6) settings window renders too
    app._open_settings()
    app.update()
    time.sleep(0.2)
    app.update()
    swin = app.settings_win
    assert swin is not None
    from PIL import ImageGrab as IG2
    swin.update_idletasks()
    sx, sy = swin.winfo_rootx(), swin.winfo_rooty()
    img2 = IG2.grab(bbox=(sx, sy, sx + swin.winfo_width(),
                          sy + swin.winfo_height()), all_screens=True)
    shot2 = Path(__file__).parent / "dock_settings.png"
    img2.save(shot2)
    print("settings screenshot: %s (%dx%d)" % (shot2, img2.width,
                                               img2.height))

    app._quit()
    print("dock test OK")


if __name__ == "__main__":
    main()
