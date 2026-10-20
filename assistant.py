"""ShhDock assistant: always-on microphone, energy VAD, vosk streaming
transcription and the question gate that asks the local model.

All microphone work happens on background threads; results are pushed into
the event queue the UI drains (never touch tk from here).
"""
import queue
import re
import threading
import time

import numpy as np

import engine

VAD_FRAME_MS = 100
SPEECH_START_FRAMES = 2      # ~200 ms above threshold to open an utterance
SILENCE_END_S = 0.8          # quiet for this long closes the utterance
METER_FPS = 15

INTERROGATIVES = {
    "what", "what's", "whats", "why", "how", "when", "where", "who", "whose",
    "which", "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "should", "would", "will", "shall", "tell", "give", "explain", "define",
    "calculate", "compute", "convert", "compare", "recommend", "suggest",
    "help", "open", "search", "look",
}


def wants_reply(text, mode="questions"):
    """Decide whether a transcript deserves an answer."""
    t = (text or "").strip()
    if len(t.split()) < 2:
        return False
    if mode == "everything":
        return True
    if "?" in t:
        return True
    first = re.sub(r"^[^a-z]+", "", t.lower()).split(" ")
    first = first[0] if first else ""
    return first in INTERROGATIVES


SYSTEM_PROMPT = (
    "You are ShhDock, a fast assistant living in a tiny bar at the top of the "
    "user's screen while they game and stream. Answer in plain text only: no "
    "markdown, no bullet lists, no emojis, no preamble. Keep it under 60 words "
    "unless explicitly asked for more. Be direct and useful."
)


class Assistant:
    def __init__(self, events, settings, server):
        self.events = events
        self.settings = settings
        self.server = server
        self.stream = None
        self.recognizer = None
        self.model = None
        self.running = False
        self._thread = None
        self._sr = 16000
        self._dev_sr = 48000
        self._level_cb = None          # latest meter level (float 0..1)
        self._last_meter = 0.0
        # VAD state
        self._floor_db = -60.0
        self._above = 0
        self._speech = False
        self._quiet_since = None
        self._chunks = []
        self._pending = []              # raw samples waiting for a full frame
        self._answer_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="shh-mic")
        self._thread.start()

    def stop(self):
        self.running = False
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None

    def restart(self):
        self.stop()
        self.start()

    # -- background loop ------------------------------------------------------

    def _run(self):
        try:
            import sounddevice as sd
        except Exception as exc:
            self.events.put({"t": "stt", "state": "error",
                             "msg": "audio module missing: %s" % exc})
            return

        try:
            import vosk
        except Exception as exc:
            self.events.put({"t": "stt", "state": "error",
                             "msg": "vosk missing: %s" % exc})
            return
        vosk.SetLogLevel(-1)

        model_path = engine.vosk_model_path()
        if not model_path:
            self.events.put({"t": "stt", "state": "missing"})
            return
        try:
            self.events.put({"t": "stt", "state": "loading"})
            self.model = vosk.Model(model_path)
        except Exception as exc:
            self.events.put({"t": "stt", "state": "error",
                             "msg": "voice model failed: %s" % exc})
            return

        device = self.settings.get("mic_device", "default")
        sd_dev = None if device in ("default", "", None) else device
        try:
            info = sd.query_devices(sd_dev, "input")
            self._dev_sr = int(info["default_samplerate"])
            self.stream = sd.InputStream(
                device=sd_dev,
                samplerate=self._dev_sr, channels=1, dtype="float32",
                blocksize=max(256, int(self._dev_sr * VAD_FRAME_MS / 1000)),
                callback=self._audio)
            self.stream.start()
        except Exception as exc:
            self.events.put({"t": "stt", "state": "error",
                             "msg": "microphone: %s" % exc})
            return

        self._new_recognizer()
        self.events.put({"t": "stt", "state": "listening"})
        # one persistent recognizer; Reset() per utterance (vosk-python has
        # no Start/EndUtterance calls, AcceptWaveform ends segments itself)
        while self.running:
            time.sleep(0.05)
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass

    def _new_recognizer(self):
        import vosk
        self.recognizer = vosk.KaldiRecognizer(self.model, 16000)
        self._chunks = []

    # -- audio ----------------------------------------------------------------

    def _audio(self, indata, frames, time_info, status):
        if not self.running:
            return
        mono = indata[:, 0] if indata.ndim > 1 else indata
        self._pending.append(mono.copy())
        joined = np.concatenate(self._pending) if len(self._pending) > 1 \
            else self._pending[0]
        need = max(1, int(round(self._dev_sr * VAD_FRAME_MS / 1000)))
        while joined.shape[0] >= need:
            block = joined[:need]
            joined = joined[need:]
            self._process_block(block)
        self._pending = [joined] if joined.shape[0] else []

    def _process_block(self, block):
        # level (for the meter + adaptive threshold)
        rms = float(np.sqrt(np.mean(np.square(block)))) or 1e-6
        db = 20.0 * np.log10(rms)
        now = time.time()
        if now - self._last_meter >= 1.0 / METER_FPS:
            self._last_meter = now
            level = max(0.0, min(1.0, (db + 60.0) / 50.0))
            self.events.put({"t": "meter", "level": level})

        # adaptive noise floor
        if db < self._floor_db + 16.0:
            self._floor_db = 0.97 * self._floor_db + 0.03 * db
        threshold = max(-48.0, self._floor_db + 14.0)
        talking = db > threshold

        # resample linearly to 16 kHz for vosk
        n = block.shape[0]
        if n < 2:
            return
        out_n = int(round(16000 * n / self._dev_sr))
        if out_n < 2:
            return
        x_old = np.linspace(0.0, 1.0, n, endpoint=False)
        x_new = np.linspace(0.0, 1.0, out_n, endpoint=False)
        sig = (np.interp(x_new, x_old, block) * 32767.0).astype(np.int16)

        if talking:
            self._above += 1
            self._quiet_since = None
            if not self._speech and self._above >= SPEECH_START_FRAMES:
                self._speech = True
                self._chunks = []
                self._new_recognizer()
                self.events.put({"t": "speech_start"})
            if self._speech and self.recognizer is not None:
                if self.recognizer.AcceptWaveform(sig.tobytes()):
                    res = self.recognizer.Result()
                    self._chunks.append(_json_text(res))
                else:
                    partial = _json_text(self.recognizer.PartialResult())
                    if partial:
                        self.events.put({"t": "partial", "text": " ".join(
                            [c for c in self._chunks if c] + [partial]).strip()})
        else:
            self._above = 0
            if self._speech:
                if self._quiet_since is None:
                    self._quiet_since = now
                elif now - self._quiet_since >= SILENCE_END_S:
                    self._close_utterance()

    def _close_utterance(self):
        if not self._speech:
            return
        self._speech = False
        if self.recognizer is not None:
            try:
                self._chunks.append(_json_text(self.recognizer.FinalResult()))
            except Exception:
                pass
        text = " ".join(t for t in self._chunks if t).strip()
        text = re.sub(r"\s+", " ", text)
        self._chunks = []
        self.events.put({"t": "speech_end", "text": text})
        if text:
            self.handle_utterance(text, source="mic")

    # -- question handling (mic + typed) --------------------------------------

    def handle_utterance(self, text, source="mic"):
        text = (text or "").strip()
        if not text:
            return
        mode = self.settings.get("answer_mode", "questions")
        # typed text is always worth answering: the user chose those words
        if source != "typed" and not wants_reply(text, mode):
            self.events.put({"t": "heard", "text": text, "answered": False})
            return
        self.events.put({"t": "heard", "text": text, "answered": True,
                         "source": source})
        threading.Thread(target=self._answer, args=(text,),
                         daemon=True, name="shh-answer").start()

    def submit_text(self, text):
        """Typed question from the dock panel."""
        self.handle_utterance(text, source="typed")

    def _answer(self, question):
        if not self._answer_lock.acquire(blocking=False):
            # one answer at a time; drop extra questions politely
            self.events.put({"t": "busy", "text": question})
            return
        try:
            self.events.put({"t": "thinking"})
            try:
                import agent as agent_mod
            except Exception:
                agent_mod = None
            if agent_mod is not None:
                # the agent loop answers with live PC context and may use
                # tools (it emits the final 'answer' event itself)
                agent_mod.run_agent(question, self.server, self.events,
                                    self.settings)
                return
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]
            reply, err = self.server.chat(messages)
            if err or not reply:
                self.events.put({"t": "answer", "text":
                                 "Hmm, no answer: %s" % (err or "empty")})
            else:
                self.events.put({"t": "answer", "text": reply})
        finally:
            self._answer_lock.release()


def _json_text(result_json):
    import json
    try:
        return json.loads(result_json).get("text", "")
    except Exception:
        return ""
