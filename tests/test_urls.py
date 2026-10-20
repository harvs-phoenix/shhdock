"""Verify every download URL the installer will use (HEAD requests only)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine   # noqa: E402


def head(url):
    import urllib.request
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": engine.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        length = int(resp.headers.get("Content-Length") or 0)
        return resp.status, length


def main():
    # vosk voice model
    status, length = head(engine.VOSK_URL)
    print("vosk model: %s  %.1f MB" % (status, length / 1048576))
    assert status == 200 and length > 10 * 1024 * 1024

    # llama.cpp nightly chain
    tag = engine._nightly_tag()
    print("llama.cpp nightly tag: %s" % tag)
    assert tag.startswith("b"), tag
    for name in ("llama-%s-bin-win-cpu-x64.zip" % tag,
                 "llama-%s-bin-win-cuda-13.4-x64.zip" % tag,
                 "cudart-llama-bin-win-cuda-13.4-x64.zip"):
        url = "%s/%s/%s" % (engine.LLAMA_DL, tag, name)
        status, length = head(url)
        print("%-55s %s  %.1f MB" % (name, status, length / 1048576))
        assert status == 200, url

    # every GGUF tier on HuggingFace
    for mid in engine.MODEL_ORDER:
        url = engine.gguf_url(mid)
        status, length = head(url)
        m = engine.MODELS[mid]
        print("%-16s %s  %.2f GB  (catalog says %.1f GB)" % (
            mid, status, length / 1073741824, m["gb"]))
        assert status == 200, url
        assert abs(length / 1073741824 - m["gb"]) < 0.35, mid

    print("all download URLs verified")


if __name__ == "__main__":
    main()
