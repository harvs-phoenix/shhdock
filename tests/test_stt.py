"""Speech recognition end-to-end with a known English wav (no mic needed).

Feeds the same 16 kHz int16 path the mic callback uses.
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine   # noqa: E402

TEST_WAV = Path(engine.MODEL_DIR) / "vosk-test.wav"
TEST_WAV_URL = ("https://github.com/alphacep/vosk-api/raw/master/"
                "python/example/test.wav")


def main():
    import vosk
    vosk.SetLogLevel(-1)

    model_path = engine.vosk_model_path()
    assert model_path, "run test_install.py first"
    print("loading voice model from %s ..." % model_path)
    model = vosk.Model(model_path)

    if not TEST_WAV.exists():
        TEST_WAV.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            TEST_WAV_URL, headers={"User-Agent": engine.USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            TEST_WAV.write_bytes(resp.read())
    raw = TEST_WAV.read_bytes()
    print("wav: %d bytes" % len(raw))

    # parse wav header minimally (PCM16 mono assumed)
    import struct
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    pos = 12
    fmt = None
    data = None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    assert fmt and data, "wav parse failed"
    channels, rate = fmt[1], fmt[2]
    print("fmt: %d ch, %d Hz, %d-bit" % (channels, rate, fmt[5]))

    rec = vosk.KaldiRecognizer(model, rate)
    words = []
    step = rate * 2  # 2 s chunks
    for off in range(0, len(data), step):
        chunk = data[off:off + step]
        if rec.AcceptWaveform(chunk):
            res = json.loads(rec.Result()).get("text", "")
            if res:
                words.append(res)
    tail = json.loads(rec.FinalResult()).get("text", "")
    if tail:
        words.append(tail)
    text = " ".join(words).strip()
    print("transcript: %s" % text)
    assert len(text.split()) >= 3, "transcript too short: %r" % text
    print("stt test OK")


if __name__ == "__main__":
    main()
