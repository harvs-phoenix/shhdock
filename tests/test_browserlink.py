"""Headless checks for browserlink.py: the localhost bridge + extension files.

A fake extension (plain urllib) plays the role of chromeext/background.js:
long-poll GET /poll, execute, POST /report.
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browserlink  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print("%-38s %s %s" % (name, "ok" if cond else "FAIL", extra))
    if not cond:
        fails.append(name)


BASE = "http://127.0.0.1:%d" % browserlink.PORT


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(path, obj, headers=None):
    body = json.dumps(obj).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# --- extension folder --------------------------------------------------------
dest = browserlink.extension_dir()
manifest = Path(dest) / "manifest.json"
background = Path(dest) / "background.js"
check("extension folder written", dest.endswith("chromeext"), dest)
check("manifest.json exists", manifest.is_file())
check("manifest is MV3", manifest.is_file() and
      json.loads(manifest.read_text())["manifest_version"] == 3)
check("background.js exists", background.is_file())
bg = background.read_text() if background.is_file() else ""
check("background.js polls /poll", "/poll" in bg)
check("background.js reports /report", "/report" in bg)
check("background.js talks only to the bridge", "http://127.0.0.1:8765" in bg)
steps = browserlink.install_steps()
check("install steps mention chrome://extensions",
      "chrome://extensions" in steps)

# --- server lifecycle --------------------------------------------------------
started = browserlink.start()
again = browserlink.start()
check("start() works", started is True)
check("start() idempotent", again is True)
st = browserlink.status()
check("status says running", st["running"] is True)
check("not connected before any poll", st["connected"] is False)

if not started:
    print("   (port %d busy - another ShhDock is running; "
          "round-trip checks skipped)" % browserlink.PORT)
else:
    # --- origin/host checks --------------------------------------------------
    code, _ = get("/status", headers={"Origin": "https://evil.example"})
    check("foreign Origin rejected", code == 403, str(code))
    code, body = get("/status")
    check("plain /status ok", code == 200, str(code))
    check("/status json has port", json.loads(body)["port"] ==
          browserlink.PORT)

    # --- the round trip ------------------------------------------------------
    seen = {}

    def fake_extension():
        code, body = get("/poll?wait=4")
        if code != 200:
            seen["err"] = "poll status %d" % code
            return
        cmd = json.loads(body)
        seen["cmd"] = cmd
        # "execute" it and report back
        post("/report", {"id": cmd["id"], "ok": True,
                         "out": "PAGE: fake tab"})

    th = threading.Thread(target=fake_extension, daemon=True)
    th.start()
    time.sleep(0.2)                      # let it reach /poll first
    res = browserlink.send({"op": "extract"}, timeout=8)
    th.join(timeout=8)
    check("send() got extension answer", res.get("ok") is True, str(res))
    check("answer text came back", "PAGE: fake tab" in (res.get("out") or ""),
          res.get("out", ""))
    check("command carried the op", seen.get("cmd", {}).get("op") == "extract",
          str(seen.get("cmd")))
    check("command had an id", isinstance(seen.get("cmd", {}).get("id"), int))
    st = browserlink.status()
    check("extension now connected", st["connected"] is True)
    check("no pending left", st["pending"] == 0, str(st["pending"]))

    # --- timeout when nobody answers ----------------------------------------
    t0 = time.time()
    res = browserlink.send({"op": "extract"}, timeout=1)
    took = time.time() - t0
    check("no-extension answer fails", res.get("ok") is False)
    check("failure explains install", "chrome://extensions" in
          (res.get("out") or ""))
    check("timeout honoured", 0.8 <= took < 4.0, "%.1fs" % took)
    check("pending cleaned up", browserlink.status()["pending"] == 0)

    # --- report for an unknown id is a 404, not a crash ----------------------
    code, _ = post("/report", {"id": 999999, "ok": True, "out": "x"})
    check("stale report -> 404", code == 404, str(code))

    # --- search url ----------------------------------------------------------
    u = browserlink.search_url("rtx 5060 ti price")
    check("google search url", u.startswith(
        "https://www.google.com/search?q=rtx+5060"), u)

print("-" * 50)
if fails:
    print("FAILED: %d -> %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("all browserlink checks passed")
