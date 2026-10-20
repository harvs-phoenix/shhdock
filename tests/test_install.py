"""Real first-run install: engine binaries + voice model + chat model.

Downloads ~2.1 GB the first time (idempotent afterwards).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine   # noqa: E402


def main():
    settings = engine.load_settings()
    settings["ram_mode"] = "auto"
    engine.resolve_model_id(settings)
    print("installing for model: %s" % settings["model_id"])

    start = time.time()
    last_label = [""]
    last_print = [0.0]

    def emit(ev):
        t = ev.get("t")
        if t == "dl":
            label = ev.get("label", "")
            done, total = ev.get("done", 0), ev.get("total", 0)
            now = time.time()
            if now - last_print[0] >= 1.0 and total > 0:
                last_print[0] = now
                print("  %-28s %6.1f / %.1f MB" % (
                    label[:28], done / 1048576, total / 1048576))
        elif t == "step":
            if ev.get("label") != last_label[0]:
                last_label[0] = ev.get("label", "")
                print("-> %s" % last_label[0])
        elif t == "installed":
            print("   install complete")

    engine.ensure_installed(settings, emit)

    st = engine.install_state(settings)
    assert st["engine"] and st["voice"] and st["model"], st
    exe = engine.llama_server_exe()
    assert exe and exe.exists(), exe
    size = engine.gguf_path(settings["model_id"]).stat().st_size
    print("engine: %s" % exe)
    print("gguf:   %.2f GB" % (size / 1073741824))
    print("install test OK in %.0f s" % (time.time() - start))


if __name__ == "__main__":
    main()
