"""llama.cpp server end-to-end: boot, health, real chat completion."""
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine   # noqa: E402


def main():
    settings = engine.load_settings()
    model_id = settings.get("model_id", "qwen2.5-3b")
    events = queue.Queue()
    server = engine.LlamaServer(events.put)

    print("starting llama-server with %s ..." % model_id)
    started = time.time()
    assert server.start(model_id, gpu=False)

    # wait for ready, mirroring what the UI does via events
    deadline = time.time() + 180
    state = None
    while time.time() < deadline:
        try:
            while True:
                ev = events.get_nowait()
                if ev.get("t") == "llm":
                    state = ev.get("state")
                    print("   event: %s" % state)
        except queue.Empty:
            pass
        if state == "ready":
            break
        if state in ("failed", "timeout"):
            raise SystemExit("server failed to start, log: %s"
                             % engine.LLAMA_LOG)
        time.sleep(0.3)
    assert state == "ready", state
    print("ready in %.1f s" % (time.time() - started))

    t0 = time.time()
    reply, err = server.chat([
        {"role": "system", "content": "Answer in one short sentence."},
        {"role": "user", "content": "What is 2+2?"},
    ])
    dt = time.time() - t0
    print("Q: What is 2+2?")
    print("A: %s" % reply)
    print("(%.2f s)" % dt)
    assert err is None, err
    assert reply and "4" in reply, reply
    assert dt < 30, dt

    # a second, speedier call (model now fully warm)
    t0 = time.time()
    reply2, err2 = server.chat([
        {"role": "system", "content": "Answer in one short sentence."},
        {"role": "user", "content": "Name a good sensitivity for Valorant."},
    ])
    print("Q: Name a good sensitivity for Valorant.")
    print("A: %s  (%.2f s)" % (reply2, time.time() - t0))
    assert err2 is None and reply2

    server.stop()
    print("llm test OK")


if __name__ == "__main__":
    main()
