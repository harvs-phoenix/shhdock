"""Agent loop end-to-end.

Part 1 (deterministic): a scripted FakeServer drives run_agent through the
tool protocol — approval flow, decline flow, and the destructive-command
gate that must fire even in full-auto mode.

Part 2 (real): boots the actual llama.cpp server and checks that
computer-aware answers work with the live model.
"""
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent   # noqa: E402
import engine  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print("%-46s %s %s" % (name, "ok" if cond else "FAIL", extra))
    if not cond:
        fails.append(name)


class FakeServer:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def chat(self, messages, timeout=180, max_tokens=240):
        self.seen.append([dict(m) for m in messages])
        if self.replies:
            return self.replies.pop(0), None
        return "done.", None


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def responder(q, approve, seen):
    """Answer the first 'ask' event from run_agent, keeping every event
    it had to consume along the way so tests can still assert on them."""
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            ev = q.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(ev)
        if ev.get("t") == "ask":
            time.sleep(0.1)
            agent.resolve_ask(ev["id"], approve)
            return


# --- 1: approval flow executes the tool and feeds output back ---------------
q = queue.Queue()
srv = FakeServer(['{"tool":"run","args":{"cmd":"echo hi"}}', "It printed hi."])
seen1 = []
threading.Thread(target=responder, args=(q, True, seen1), daemon=True).start()
final = agent.run_agent("run echo please", srv, q, {"agent": "ask"})
evs = drain(q) + seen1
kinds = [e.get("t") for e in evs]
check("loop: final answer returned", final == "It printed hi.", final)
check("loop: tool start event", any(
    e.get("t") == "tool" and e.get("phase") == "start" and "echo hi" in
    e.get("text", "") for e in evs))
check("loop: ask event emitted", "ask" in kinds, str(kinds))
check("loop: tool result event", any(
    e.get("t") == "tool" and e.get("phase") == "result" for e in evs))
tool_msg = srv.seen[1][-1]["content"]
check("loop: TOOL RESULT fed to model", "TOOL RESULT" in tool_msg, tool_msg)
check("loop: echo output present", "hi" in tool_msg.lower(), tool_msg)

# --- 2: declined action is not executed, model told not to retry -----------
q2 = queue.Queue()
srv2 = FakeServer(['{"tool":"run","args":{"cmd":"echo secret"}}',
                   "Okay, skipping that."])
seen2 = []
threading.Thread(target=responder, args=(q2, False, seen2), daemon=True).start()
final2 = agent.run_agent("run it", srv2, q2, {"agent": "ask"})
tool_msg2 = srv2.seen[1][-1]["content"]
check("decline: told the model", "DECLINED" in tool_msg2, tool_msg2)
check("decline: answer still delivered", final2 == "Okay, skipping that.")

# --- 3: destructive command asks even in full-auto -------------------------
q3 = queue.Queue()
srv3 = FakeServer(['{"tool":"run","args":{"cmd":"Remove-Item -Recurse '
                   'C:\\\\Temp\\\\x"}}', "Okay."])
seen3 = []
threading.Thread(target=responder, args=(q3, False, seen3), daemon=True).start()
t0 = time.time()
final3 = agent.run_agent("clean up", srv3, q3, {"agent": "auto"})
evs3 = drain(q3) + seen3
check("auto mode: danger still asks", any(
    e.get("t") == "ask" and e.get("danger") for e in evs3))
check("auto mode: safe run would not ask", agent.gate(
    "run", {"cmd": "Get-Date"}, "auto") == "allow")

# --- 4: agent off = plain answer, no tool parsing --------------------------
q4 = queue.Queue()
srv4 = FakeServer(['{"tool":"run","args":{"cmd":"echo x"}}', "hi"])
final4 = agent.run_agent("q", srv4, q4, {"agent": "off"})
evs4 = drain(q4)
check("off mode: tool json treated as text", final4.startswith("{"))
check("off mode: no tool events", not any(
    e.get("t") in ("tool", "ask") for e in evs4))

# --- 5: real model, computer-aware answer ----------------------------------
settings = engine.load_settings()
events = queue.Queue()
server = engine.LlamaServer(events.put)
model_id = settings.get("model_id", "qwen2.5-3b")
print("booting real server (%s) ..." % model_id)
assert server.start(model_id, gpu=bool(settings.get("gpu")))

# wait for the model to finish warming up (start() returns once the
# process is up; 'ready' comes from the health check)
ready = False
deadline = time.time() + 120
while time.time() < deadline and not ready:
    try:
        ev = events.get(timeout=0.2)
        if ev.get("t") == "llm" and ev.get("state") == "ready":
            ready = True
    except queue.Empty:
        pass
check("real server warmed up", ready)

q5 = queue.Queue()
final5 = agent.run_agent(
    "How much RAM does this PC have? Answer in one short sentence.",
    server, q5, {"agent": "off"})
print("   real answer: %s" % final5.encode("ascii", "replace").decode())
check("real model: RAM answer mentions GB",
      "gb" in (final5 or "").lower(), final5)

# real model with agent ON: let it use tools if it wants (auto = no blocking)
q6 = queue.Queue()
final6 = agent.run_agent(
    "Run a PowerShell command that prints today's date, then tell me the "
    "date in one short sentence.",
    server, q6, {"agent": "auto"})
evs6 = drain(q6)
tool_used = any(e.get("t") == "tool" for e in evs6)
print("   real answer: %s (tool used: %s)" % (
    final6.encode("ascii", "replace").decode(), tool_used))
check("real model: agent answer delivered", bool(final6) and
      len(final6) > 10, final6)

server.stop()

if fails:
    print("\n%d FAILURES: %s" % (len(fails), fails))
    sys.exit(1)
print("\nagent loop checks all pass")
