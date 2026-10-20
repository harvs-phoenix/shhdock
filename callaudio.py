"""ShhDock call listening: captures what plays THROUGH your speakers
(Discord / Zoom / game / YouTube — anything on the selected output device)
via raw WASAPI loopback, runs the same energy-VAD + vosk transcription as
the mic, and pushes 'call_*' events into the UI queue.

This is the audio-side twin of the mic listener: a second recognizer with
its own utterance state, tagged 'them' in the panel so you always see who
heard what.  Only ever active while the dock is running and the toggle is
on — the pill shows a green handset while it listens.
"""
import ctypes
import threading
import time
from collections import deque

import numpy as np

import engine
from assistant import VAD_FRAME_MS, SPEECH_START_FRAMES, METER_FPS

CALL_SILENCE_END_S = 1.0      # slightly longer hangover than the mic
CALL_EXTRA_DB = 18.0          # raise the gate: music shouldn't count as talk
SR = 16000

ole32 = ctypes.windll.ole32

# --- COM plumbing ------------------------------------------------------------


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s):
    g = GUID()
    ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


_MMDEV_CLSID = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_ENUM = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_AUDIOCLIENT = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
_IID_CAPTURE = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"
_IID_PROPSTORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
_PKEY_NAME = None  # built lazily

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_CROSSPROCESS = 0x00010000
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000   # NOT 0x10000 (that's crossprocess!)
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
DEVICE_STATE_ACTIVE = 0x1
eRender = 0
STGM_READ = 0

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_ushort),
                ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_ulong),
                ("nAvgBytesPerSec", ctypes.c_ulong),
                ("nBlockAlign", ctypes.c_ushort),
                ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r", ctypes.c_ushort * 3),
                ("p", ctypes.c_void_p)]


def _vtbl(obj):
    """interface ptr -> vtable function table (double dereference)."""
    vptr = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    return ctypes.cast(vptr, ctypes.POINTER(ctypes.c_void_p * 48))[0]


def _mm_device_enumerator():
    ole32.CoInitializeEx(None, 0)
    enum = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_guid(_MMDEV_CLSID)), None, 23,
                                ctypes.byref(_guid(_IID_ENUM)),
                                ctypes.byref(enum))
    if hr != 0 or not enum.value:
        raise RuntimeError("MMDeviceEnumerator unavailable (0x%08x)"
                           % (hr & 0xFFFFFFFF))
    return enum


def _device_name(device):
    """Friendly name of an IMMDevice via IPropertyStore/PKEY_Device_FriendlyName."""
    try:
        store = ctypes.c_void_p()
        OpenStore = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p))(_vtbl(device)[4])
        if OpenStore(device, STGM_READ, ctypes.byref(store)) != 0:
            return "?"
        pkey = PROPERTYKEY(_guid("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14)
        GetValue = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
            ctypes.POINTER(PROPVARIANT))(_vtbl(store)[5])
        pv = PROPVARIANT()
        if GetValue(store, ctypes.byref(pkey), ctypes.byref(pv)) == 0 and pv.p:
            return ctypes.wstring_at(pv.p)
        return "?"
    except Exception:
        return "?"


def _device_id(device):
    get_id = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p))(_vtbl(device)[5])
    p = ctypes.c_void_p()
    if get_id(device, ctypes.byref(p)) != 0 or not p.value:
        return ""
    s = ctypes.wstring_at(p)
    ole32.CoTaskMemFree(p)
    return s


def _enum_render_devices(enum):
    """[(endpoint_id, friendly_name)] for active render endpoints."""
    coll = ctypes.c_void_p()
    EnumEndpoints = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p))(_vtbl(enum)[3])
    if EnumEndpoints(enum, eRender, DEVICE_STATE_ACTIVE,
                     ctypes.byref(coll)) != 0:
        return []
    GetCount = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint))(_vtbl(coll)[3])
    GetItem = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p))(_vtbl(coll)[4])
    count = ctypes.c_uint()
    if GetCount(coll, ctypes.byref(count)) != 0:
        return []
    out = []
    for i in range(count.value):
        dev = ctypes.c_void_p()
        if GetItem(coll, i, ctypes.byref(dev)) == 0 and dev.value:
            out.append((_device_id(dev), _device_name(dev)))
    return out


def enumerate_render_outputs():
    """['default', <endpoint names>...] for the settings dropdown."""
    try:
        enum = _mm_device_enumerator()
        return ["default"] + [n for _id, n in _enum_render_devices(enum)]
    except Exception:
        return ["default"]


def _get_device(enum, wanted_name):
    """Resolve a friendly name (or 'default') to an IMMDevice."""
    GetDefault = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p))(_vtbl(enum)[4])
    if wanted_name in ("default", "", None):
        dev = ctypes.c_void_p()
        hr = GetDefault(enum, eRender, 0, ctypes.byref(dev))   # eConsole
        if hr != 0 or not dev.value:
            raise RuntimeError("no default output (0x%08x)" % (hr & 0xFFFFFFFF))
        return dev
    GetDevice = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_void_p))(_vtbl(enum)[5])
    for epid, name in _enum_render_devices(enum):
        if name == wanted_name:
            dev = ctypes.c_void_p()
            if GetDevice(enum, epid, ctypes.byref(dev)) == 0 and dev.value:
                return dev
    # fall back to default
    dev = ctypes.c_void_p()
    GetDefault(enum, eRender, 0, ctypes.byref(dev))
    if not dev.value:
        raise RuntimeError("output device not found: %s" % wanted_name)
    return dev


def _open_loopback(wanted_name):
    """Returns (client, capture, mix_rate, channels, block_align, fmt_tag)
    with the client already started."""
    enum = _mm_device_enumerator()
    device = _get_device(enum, wanted_name)
    client = ctypes.c_void_p()
    Activate = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.c_int,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(_vtbl(device)[3])
    hr = Activate(device, ctypes.byref(_guid(_IID_AUDIOCLIENT)), 23, None,
                  ctypes.byref(client))
    if hr != 0 or not client.value:
        raise RuntimeError("IAudioClient activate failed (0x%08x)"
                           % (hr & 0xFFFFFFFF))
    fns = _vtbl(client)
    GetMixFormat = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)))(fns[8])
    mix = ctypes.POINTER(WAVEFORMATEX)()
    if GetMixFormat(client, ctypes.byref(mix)) != 0 or not mix:
        raise RuntimeError("GetMixFormat failed")
    fmt = mix.contents
    fmt_tag = fmt.wFormatTag
    if fmt_tag == WAVE_FORMAT_EXTENSIBLE and fmt.cbSize >= 22:
        # WAVEFORMATEXTENSIBLE (packed): Format(18) + Samples(2) +
        # dwChannelMask(4) -> SubFormat GUID at offset 24; its first
        # DWORD is the real format tag (3 = IEEE_FLOAT, 1 = PCM).
        raw = ctypes.string_at(ctypes.addressof(fmt) + 24, 4)
        fmt_tag = int.from_bytes(raw, "little")
    Initialize = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_longlong, ctypes.c_longlong,
        ctypes.POINTER(WAVEFORMATEX), ctypes.POINTER(GUID))(fns[3])
    hr = Initialize(client, AUDCLNT_SHAREMODE_SHARED,
                    AUDCLNT_STREAMFLAGS_LOOPBACK,
                    500000, 0, mix, None)          # 50 ms buffer
    if hr != 0:
        raise RuntimeError("IAudioClient::Initialize loopback failed "
                           "(0x%08x)" % (hr & 0xFFFFFFFF))
    capture = ctypes.c_void_p()
    GetService = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p))(fns[14])
    hr = GetService(client, ctypes.byref(_guid(_IID_CAPTURE)),
                    ctypes.byref(capture))
    if hr != 0 or not capture.value:
        raise RuntimeError("IAudioCaptureClient service failed (0x%08x)"
                           % (hr & 0xFFFFFFFF))
    Start = ctypes.CFUNCTYPE(ctypes.c_long,
                             ctypes.c_void_p)(fns[10])
    if Start(client) != 0:
        raise RuntimeError("IAudioClient::Start failed")
    return client, capture, int(fmt.nSamplesPerSec), int(fmt.nChannels), \
        int(fmt.nBlockAlign), fmt_tag


def _read_frames(capture, block_align, rate, channels, fmt_tag):
    """Drain every available packet -> one mono float32 np array (or None)."""
    GetNextPacket = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint))(_vtbl(capture)[5])
    GetBuffer = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_byte)),
        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong))(_vtbl(capture)[3])
    ReleaseBuffer = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.c_uint)(_vtbl(capture)[4])

    chunks = []
    while True:
        frames = ctypes.c_uint(0)
        if GetNextPacket(capture, ctypes.byref(frames)) != 0:
            break
        if frames.value == 0:
            break
        data = ctypes.POINTER(ctypes.c_byte)()
        nread = ctypes.c_uint(0)
        flags = ctypes.c_ulong(0)
        if GetBuffer(capture, ctypes.byref(data), ctypes.byref(nread),
                     ctypes.byref(flags), None, None) != 0:
            break
        n = int(nread.value)
        if n > 0:
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                chunks.append(np.zeros(n, dtype=np.float32))
            else:
                raw = ctypes.string_at(data, n * block_align)
                if fmt_tag == WAVE_FORMAT_IEEE_FLOAT:
                    arr = np.frombuffer(raw, dtype="<f4")
                else:                                   # PCM (usually 16-bit)
                    bits = 16 if block_align // max(1, channels) >= 2 else 8
                    if bits == 16:
                        arr = np.frombuffer(raw, dtype="<i2").astype(
                            np.float32) / 32768.0
                    else:
                        arr = np.frombuffer(raw, dtype=np.uint8).astype(
                            np.float32) / 128.0 - 1.0
                if channels > 1 and arr.size >= channels:
                    arr = arr.reshape(-1, channels).mean(axis=1)
                chunks.append(arr.astype(np.float32))
        ReleaseBuffer(capture, n)
    if not chunks:
        return None
    return np.concatenate(chunks)


def _resample_16k(block, dev_sr):
    n = block.shape[0]
    if n < 2:
        return None
    out_n = int(round(SR * n / dev_sr))
    if out_n < 2:
        return None
    x_old = np.linspace(0.0, 1.0, n, endpoint=False)
    x_new = np.linspace(0.0, 1.0, out_n, endpoint=False)
    return (np.interp(x_new, x_old, block) * 32767.0).astype(np.int16)


# --- listener state ----------------------------------------------------------

_recent = deque(maxlen=24)
_lock = threading.Lock()
_running = {"on": False, "thread": None, "stop": None}


def recent_lines(n=5):
    with _lock:
        return list(_recent)[-n:]


def is_running():
    return bool(_running["on"])


def start(events, settings):
    if _running["on"]:
        return True
    stop_ev = threading.Event()
    _running.update({"on": True, "stop": stop_ev})
    t = threading.Thread(target=_run, args=(events, settings, stop_ev),
                         daemon=True, name="shh-call")
    _running["thread"] = t
    t.start()
    return True


def stop():
    _running["on"] = False
    if _running.get("stop"):
        _running["stop"].set()
    t = _running.get("thread")
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=3.0)
    _running["thread"] = None


# --- the capture loop --------------------------------------------------------

def _run(events, settings, stop_ev):
    events.put({"t": "call_state", "state": "loading"})
    wanted = settings.get("call_device", "default")
    try:
        client, capture, rate, channels, block_align, fmt_tag = \
            _open_loopback(wanted)
    except Exception as exc:
        events.put({"t": "call_state", "state": "error",
                    "msg": str(exc)})
        return

    try:
        import vosk
        vosk.SetLogLevel(-1)
        model_path = engine.vosk_model_path()
        if not model_path:
            events.put({"t": "call_state", "state": "error",
                        "msg": "voice model missing"})
            return
        model = vosk.Model(model_path)
    except Exception as exc:
        events.put({"t": "call_state", "state": "error",
                    "msg": "vosk: %s" % exc})
        return

    events.put({"t": "call_state", "state": "listening"})
    rec = vosk.KaldiRecognizer(model, SR)
    floor_db = -60.0
    above = 0
    speech = False
    quiet_since = None
    chunks = []
    pending = []
    need = int(round(rate * VAD_FRAME_MS / 1000))
    last_meter = 0.0
    last_data = time.time()

    while not stop_ev.is_set():
        try:
            block = _read_frames(capture, block_align, rate, channels,
                                 fmt_tag)
        except Exception:
            block = None
        now = time.time()
        if block is None or block.size == 0:
            # no packets: if an utterance is open and audio went silent,
            # close it so pending speech isn't held hostage
            if speech and now - last_data > 1.5:
                if rec is not None:
                    try:
                        chunks.append(_json_text(rec.FinalResult()))
                    except Exception:
                        pass
                _finish(chunks, events)
                speech, chunks, quiet_since, above = False, [], None, 0
            time.sleep(0.03)
            continue
        last_data = now
        pending.append(block)
        joined = np.concatenate(pending) if len(pending) > 1 else pending[0]
        while joined.shape[0] >= need:
            frame = joined[:need]
            joined = joined[need:]
            rec, speech, chunks, quiet_since, above, floor_db, last_meter = \
                _process(frame, rec, model, speech, chunks, quiet_since,
                         above, floor_db, last_meter, rate, events)
        pending = [joined] if joined.shape[0] else []

    try:
        Stop = ctypes.CFUNCTYPE(ctypes.c_long,
                                ctypes.c_void_p)(_vtbl(client)[11])
        Stop(client)
    except Exception:
        pass
    events.put({"t": "call_state", "state": "off"})


def _process(frame, rec, model, speech, chunks, quiet_since, above, floor_db,
             last_meter, rate, events):
    import vosk
    rms = float(np.sqrt(np.mean(np.square(frame)))) or 1e-6
    db = 20.0 * np.log10(rms)
    now = time.time()
    if now - last_meter >= 1.0 / METER_FPS:
        last_meter = now
        events.put({"t": "call_level",
                    "level": max(0.0, min(1.0, (db + 60.0) / 50.0))})
    if db < floor_db + 16.0:
        floor_db = 0.97 * floor_db + 0.03 * db
    threshold = max(-44.0, floor_db + 14.0 + CALL_EXTRA_DB)
    talking = db > threshold

    sig = _resample_16k(frame, rate)
    if sig is None:
        return rec, speech, chunks, quiet_since, above, floor_db, last_meter

    if talking:
        above += 1
        quiet_since = None
        if not speech and above >= SPEECH_START_FRAMES:
            speech = True
            chunks = []
            try:
                rec = vosk.KaldiRecognizer(model, SR)   # fresh per utterance
            except Exception:
                pass
            events.put({"t": "call_speech", "state": "start"})
        if speech and rec is not None:
            if rec.AcceptWaveform(sig.tobytes()):
                chunks.append(_json_text(rec.Result()))
    else:
        above = 0
        if speech:
            if quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= CALL_SILENCE_END_S:
                if rec is not None:
                    try:
                        chunks.append(_json_text(rec.FinalResult()))
                    except Exception:
                        pass
                _finish(chunks, events)
                speech, chunks, quiet_since = False, [], None
    return rec, speech, chunks, quiet_since, above, floor_db, last_meter


def _finish(chunks, events):
    text = " ".join(t for t in chunks if t).strip()
    import re
    text = re.sub(r"\s+", " ", text)
    if text:
        with _lock:
            _recent.append(text)
        events.put({"t": "call_text", "text": text})


def _json_text(result_json):
    import json
    try:
        return json.loads(result_json).get("text", "")
    except Exception:
        return ""
