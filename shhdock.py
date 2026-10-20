"""ShhDock — a tiny always-on-top dock that's invisible to screen shares,
hears you through your mic, and answers questions with a local model.

Run:  python shhdock.py
"""
import queue
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine      # noqa: E402
import dockui      # noqa: E402


def main():
    engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    settings = engine.load_settings()
    engine.resolve_model_id(settings)
    engine.save_settings(settings)

    events = queue.Queue()
    server = engine.LlamaServer(events.put)
    app = dockui.Dock(events, settings, server)
    try:
        app.mainloop()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
