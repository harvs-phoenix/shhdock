"""browserlink - localhost bridge between ShhDock and the Chrome extension.

ShhDock runs a tiny HTTP server on 127.0.0.1:8765. The bundled Chrome
extension (chromeext/) long-polls GET /poll for work, runs it inside the
active tab, and POSTs the result back to /report. The agent's ``chrome``
tool enqueues a command with send() and waits for the answer, so the local
model can read and drive Chrome (Google search, pages, clicks, typing).

No third-party dependencies: http.server + threading + json only.
"""
import json
import os
import sys
import threading
import time
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote_plus

PORT = 8765
POLL_WAIT_S = 4.0            # how long /poll blocks when idle
CONNECTED_GRACE_S = 10.0     # extension is "connected" if it polled this recently
SEND_TIMEOUT_S = 25.0
OUT_CAP = 6000               # max chars of page text handed back to the model

_queue = queue.Queue()                 # commands waiting for the extension
_pending = {}                          # id -> {"event": Event, "result": dict}
_lock = threading.Lock()
_server = None
_thread = None
_last_poll = 0.0
_seq = 0


# --- extension folder --------------------------------------------------------

def source_dir():
    """Where the chromeext/ sources live (repo in dev, _MEIPASS when frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "chromeext")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromeext")


def extension_dir(create=True):
    """Real folder to load unpacked in chrome://extensions.

    Copies chromeext/ out of the bundle (a frozen temp dir disappears on
    exit, Chrome must keep reading the files). Returns the destination."""
    import shutil
    dest = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "ShhDock", "chromeext")
    src = source_dir()
    if create:
        os.makedirs(dest, exist_ok=True)
        if os.path.isdir(src):
            for name in os.listdir(src):
                s = os.path.join(src, name)
                if os.path.isfile(s):
                    shutil.copy2(s, os.path.join(dest, name))
    return dest


def install_steps():
    return ("Load it once: open chrome://extensions, turn on Developer mode "
            "(top right), click Load unpacked, pick %s" % extension_dir())


# --- server ------------------------------------------------------------------

def status():
    with _lock:
        pending = len(_pending)
        last = _last_poll
    return {"running": _server is not None,
            "connected": (time.time() - last) < CONNECTED_GRACE_S,
            "pending": pending, "port": PORT}


def connected():
    return status()["connected"]


class _Handler(BaseHTTPRequestHandler):
    server_version = "ShhDockLink"

    def log_message(self, fmt, *args):          # keep the console quiet
        pass

    def _allowed(self):
        host = (self.headers.get("Host") or "").lower()
        if not (host.startswith("127.0.0.1") or host.startswith("localhost")):
            return False
        origin = self.headers.get("Origin")
        if origin and not (origin.startswith("chrome-extension://") or
                           origin == "null" or
                           origin.startswith("http://127.0.0.1")):
            return False
        return True

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_OPTIONS(self):                       # CORS preflight from the ext
        if not self._allowed():
            return self._empty(403)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._allowed():
            return self._empty(403)
        path = urlparse(self.path).path
        if path == "/poll":
            global _last_poll
            _last_poll = time.time()
            try:
                cmd = _queue.get(timeout=POLL_WAIT_S)
            except queue.Empty:
                return self._empty(204)
            return self._json(200, cmd)
        if path == "/status":
            return self._json(200, status())
        return self._empty(404)

    def do_POST(self):
        if not self._allowed():
            return self._empty(403)
        if urlparse(self.path).path != "/report":
            return self._empty(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(n, 65536)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "out": "bad json"})
        cid = data.get("id")
        with _lock:
            rec = _pending.get(cid)
        if rec is None:
            return self._json(404, {"ok": False, "out": "no such command"})
        rec["result"] = {"ok": bool(data.get("ok")),
                         "out": str(data.get("out") or "")[:OUT_CAP]}
        rec["event"].set()
        return self._json(200, {"ok": True})


def start():
    """Start the bridge server once (idempotent, daemon thread)."""
    global _server, _thread
    if _server is not None:
        return True
    with _lock:
        if _server is not None:
            return True
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
        except OSError:
            return False                # another ShhDock already owns the port
        _server = srv
        _thread = threading.Thread(target=srv.serve_forever, daemon=True,
                                   name="shh-link")
        _thread.start()
    return True


def stop():
    global _server, _thread
    srv, _server, _thread = _server, None, None
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass


def send(cmd, timeout=SEND_TIMEOUT_S):
    """Hand one command to the extension and wait for its result."""
    if _server is None:
        start()
    global _seq
    with _lock:
        _seq += 1
        cid = _seq
        rec = {"event": threading.Event(), "result": None}
        _pending[cid] = rec
    _queue.put(dict(cmd, id=cid))
    done = rec["event"].wait(timeout)
    with _lock:
        _pending.pop(cid, None)
    if not done:
        return {"ok": False,
                "out": "browser link: the Chrome extension did not answer. "
                       "Is Chrome open with 'ShhDock Browser Link' enabled in "
                       "chrome://extensions? (%s)" % install_steps()}
    return rec["result"] or {"ok": False, "out": "no result"}


def search_url(query):
    return "https://www.google.com/search?q=" + quote_plus(query or "")
