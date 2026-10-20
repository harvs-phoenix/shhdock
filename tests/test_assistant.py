"""Full-stack assistant test: real microphone, real vosk model, real
llama.cpp server — the exact wiring the app uses, minus someone speaking.

Checks: mic opens and delivers frames, typed question gets a real answer,
statements get ignored, mic stops cleanly.
"""
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine      # noqa: E402
import assistant   # noqa: E402


def main():
    settings = engine.load_settings()
    events = queue.Queue()
    server = engine.LlamaServer(events.put)
    asst = assistant.Assistant(events, settings, server)

    model_id = settings.get("model_id", "qwen2.5-3b")
    print("starting server (%s) ..." % model_id)
    assert server.start(model_id, gpu=bool(settings.get("gpu")))
    asst.start()

    got = {"stt": None, "llm": None, "meter": False}
    deadline = time.time() + 90
    while time.time() < deadline and not (got["stt"] == "listening"
                                          and got["llm"] == "ready"
                                          and got["meter"]):
        try:
            ev = events.get_nowait()
        except queue.Empty:
            time.sleep(0.05)
            continue
        if ev.get("t") == "stt":
            got["stt"] = ev.get("state")
            print("   stt: %s %s" % (got["stt"], ev.get("msg", "")))
            assert got["stt"] != "error", ev
        elif ev.get("t") == "llm":
            got["llm"] = ev.get("state")
            print("   llm: %s" % got["llm"])
            assert got["llm"] not in ("failed", "timeout")
        elif ev.get("t") == "meter":
            got["meter"] = True
    assert got["stt"] == "listening", got
    assert got["llm"] == "ready", got
    assert got["meter"], "no audio frames arrived from the microphone"
    print("mic is live and model is ready")

    # typed question -> real model answer
    asst.submit_text("What is the capital of France? Answer in one word.")
    answer = None
    deadline = time.time() + 60
    while time.time() < deadline and answer is None:
        try:
            ev = events.get_nowait()
        except queue.Empty:
            time.sleep(0.05)
            continue
        if ev.get("t") == "answer":
            answer = ev.get("text")
    print("answer: %s" % answer)
    assert answer and "paris" in answer.lower(), answer

    # statement -> correctly ignored
    asst.submit_text("I really like pizza with extra cheese")
    heard = None
    deadline = time.time() + 10
    while time.time() < deadline and heard is None:
        try:
            ev = events.get_nowait()
        except queue.Empty:
            time.sleep(0.05)
            continue
        if ev.get("t") == "heard":
            heard = ev
    assert heard and heard.get("answered") is False, heard
    print("statement ignored as expected")

    asst.stop()
    server.stop()
    print("assistant test OK")


if __name__ == "__main__":
    main()
