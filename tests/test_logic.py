"""Unit checks for engine + assistant logic (no downloads)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine   # noqa: E402
import assistant  # noqa: E402


def main():
    assert engine.model_for_ram(4) == "qwen2.5-0.5b"
    assert engine.model_for_ram(8) == "qwen2.5-1.5b"
    assert engine.model_for_ram(16) == "qwen2.5-3b"
    assert engine.model_for_ram(32) == "qwen2.5-7b"
    assert engine.model_for_ram(64) == "qwen2.5-14b"

    ram = engine.installed_ram_gb()
    assert 14 < ram < 18, ram

    assert assistant.wants_reply("what time is it")
    assert assistant.wants_reply("is the sky blue sometimes")
    assert assistant.wants_reply("How much RAM does this need?")
    assert not assistant.wants_reply("I love cheese")
    assert not assistant.wants_reply("hello there")
    assert assistant.wants_reply("I love cheese", "everything")
    assert not assistant.wants_reply("ok", "everything")

    model_id = engine.resolve_model_id({"ram_mode": "auto", "ram_gb": 16})
    assert model_id == "qwen2.5-3b", model_id

    # GGUF URLs for every tier must point at HuggingFace
    for mid in engine.MODEL_ORDER:
        url = engine.gguf_url(mid)
        assert url.startswith("https://huggingface.co/"), url

    print("RAM detected: %.1f GB -> default model %s" % (ram, model_id))
    print("unit checks all pass")


if __name__ == "__main__":
    main()
