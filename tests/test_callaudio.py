"""Call-audio loopback: enumeration, raw WASAPI capture of played audio,
VAD state machine, and event flow.  Plays a tone into the default output
and asserts the loopback stream sees it."""
import queue
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import callaudio   # noqa: E402
import engine      # noqa: E402

fails = []


def check(name, cond, extra=""):
    print("%-42s %s %s" % (name, "ok" if cond else "FAIL", extra))
    if not cond:
        fails.append(name)


# --- enumeration ---
outs = callaudio.enumerate_render_outputs()
print("   render outputs:", outs[:6], "...")
check("enumeration non-trivial", len(outs) >= 3)
check("default listed first", outs[0] == "default")

# --- open loopback + capture a tone ---
events = queue.Queue()
client, capture, rate, channels, block_align, fmt_tag = \
    callaudio._open_loopback("default")
check("loopback opened", client.value and capture.value)
check("mix format sane", rate in (44100, 48000, 96000) and 1 <= channels <= 8,
      "(%d Hz, %d ch, tag %d)" % (rate, channels, fmt_tag))

# capture baseline (should be near-zero packets or silence)
import sounddevice as sd
tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(rate * 2) / rate)
        ).astype(np.float32) * 0.5
sd.play(tone, samplerate=rate)
time.sleep(0.8)
total = np.zeros(0, dtype=np.float32)
deadline = time.time() + 1.2
while time.time() < deadline:
    blk = callaudio._read_frames(capture, block_align, rate, channels,
                                 fmt_tag)
    if blk is not None and blk.size:
        total = np.concatenate([total, blk])
    time.sleep(0.03)
sd.stop()
rms = float(np.sqrt(np.mean(np.square(total)))) if total.size else 0.0
print("   loopback frames=%d rms=%.4f" % (total.size, rms))
check("loopback captured the tone", total.size > rate // 2)
check("loopback audio is loud", rms > 0.01)

# resample helper
sig = callaudio._resample_16k(total[:rate], rate)
check("resample -> 16k int16", sig is not None and sig.dtype == np.int16
      and abs(sig.size - 16000) <= 2)

# --- VAD state machine with synthetic loud/quiet frames ---
ev2 = queue.Queue()


def frames(sec, amp, rate=rate):
    n = int(rate * sec)
    x = (amp * np.sin(2 * np.pi * 220 * np.arange(n) / rate)).astype(
        np.float32)
    need = int(round(rate * callaudio.VAD_FRAME_MS / 1000))
    return [x[i:i + need] for i in range(0, n - need, need)]


rec, speech, ch, quiet, above, floor, meter = (
    None, False, [], None, 0, -60.0, 0.0)
model = None
try:
    import vosk
    vosk.SetLogLevel(-1)
    model = vosk.Model(engine.vosk_model_path())
except Exception as exc:
    print("   (vosk model unavailable, VAD-only check: %s)" % exc)

if model is not None:
    for f in frames(0.5, 0.5):                       # loud speech
        rec, speech, ch, quiet, above, floor, meter = callaudio._process(
            f, rec, model, speech, ch, quiet, above, floor, meter, rate, ev2)
    check("VAD opens on loud audio", speech is True)
    for f in frames(1.4, 0.0):                       # silence (real-time)
        rec, speech, ch, quiet, above, floor, meter = callaudio._process(
            f, rec, model, speech, ch, quiet, above, floor, meter, rate, ev2)
        time.sleep(0.1)      # hangover is wall-clock; pace like real audio
    kinds = []
    while not ev2.empty():
        kinds.append(ev2.get_nowait().get("t"))
    check("VAD closes on silence", speech is False)
    check("emits call_speech + maybe call_text", "call_speech" in kinds,
          str(kinds))

# --- recent_lines buffer ---
callaudio._recent.clear()
callaudio._finish(["hello from the call"], queue.Queue())
check("recent_lines records text", callaudio.recent_lines(3) ==
      ["hello from the call"])

if fails:
    print("\n%d FAILURES: %s" % (len(fails), fails))
    sys.exit(1)
print("\ncall audio checks all pass")
