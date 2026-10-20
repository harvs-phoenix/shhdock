"""Headless checks for agent.py: tools, safety gates, parser, snapshot.

The full LLM loop is exercised in test_agent_llm.py against the real server.
"""
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent   # noqa: E402

fails = []


def check(name, cond, extra=""):
    print("%-38s %s %s" % (name, "ok" if cond else "FAIL", extra))
    if not cond:
        fails.append(name)


# --- struct sanity: SendInput needs a 40-byte INPUT on Win64 ---
check("sizeof(_INPUT) == 40", ctypes.sizeof(agent._INPUT) == 40,
      "(got %d)" % ctypes.sizeof(agent._INPUT))

# --- parser ---
t = agent.parse_tool('{"tool":"run","args":{"cmd":"Get-Process | '
                     'Select -First 5"}}')
check("parse nested json", t and t["tool"] == "run" and "Get-Process" in
      t["args"]["cmd"], str(t))
t = agent.parse_tool("Sure.\n```json\n{\"tool\":\"system_info\",\"args\":{}}\n"
                     "```")
check("parse fenced json", t and t["tool"] == "system_info")
t = agent.parse_tool("hello there, no tools here")
check("plain text -> None", t is None)
t = agent.parse_tool('{"tool":"nope","args":{}}')
check("unknown tool -> None", t is None)

# --- safety gate ---
check("gate read-only ask-mode allows",
      agent.gate("system_info", {}, "ask") == "allow")
check("gate run ask-mode confirms",
      agent.gate("run", {"cmd": "echo hi"}, "ask") == "confirm")
check("gate run auto-mode allows",
      agent.gate("run", {"cmd": "echo hi"}, "auto") == "allow")
check("gate danger always confirms in auto",
      agent.gate("run", {"cmd": "Remove-Item -Recurse C:\\"}, "auto")
      == "confirm")
check("danger rm -rf detected", agent.is_dangerous("rm -rf /"))
check("danger shutdown detected", agent.is_dangerous("shutdown /s"))
check("safe command not danger", not agent.is_dangerous(
    "Get-ChildItem | Select -First 3"))
check("gate off-mode blocks", agent.gate("run", {"cmd": "x"}, "off") == "block")

# --- read-only tools ---
info = agent.tool_system_info({})
print("   snapshot:", info[:110], "...")
check("system_info has RAM", "RAM" in info and "GB" in info)
check("system_info has CPU", "CPU" in info)
snap = agent.system_snapshot()
check("snapshot mentions OS build", "Windows build" in snap)

procs = agent.tool_processes({})
check("processes lists something", len(procs) > 20)
check("processes shows a known exe", "python" in procs.lower()
      or "chrome" in procs.lower() or "explorer" in procs.lower())

ld = agent.tool_list_dir({"path": str(Path(__file__).parent)})
check("list_dir shows this test file", "test_agent.py" in ld)

rf = agent.tool_read_file({"path": str(Path(__file__))})
check("read_file reads own source", "def check" in rf)

rf2 = agent.tool_read_file({"path": "C:\\no\\such\\file.txt"})
check("read_file missing -> message", "no such file" in rf2)

sf = agent.tool_search_files({"query": "test_agent*", "dir":
                              str(Path(__file__).parent), "content": False})
check("search finds own file", "test_agent.py" in sf)

wl = agent.tool_window_list({})
check("window_list non-empty", len(wl) > 20)

# --- confirm registry ---
aid = agent.new_ask("run: echo hi", "run", {"cmd": "echo hi"})
check("ask registered", agent.pending_ask_count() == 1)
import threading


def approve_soon():
    time.sleep(0.3)
    agent.resolve_ask(aid, True)


threading.Thread(target=approve_soon, daemon=True).start()
ok = agent.wait_ask(aid, timeout=5)
check("resolve approve unblocks", ok is True)
check("ask consumed", agent.pending_ask_count() == 0)

aid2 = agent.new_ask("run: echo no", "run", {"cmd": "echo no"})
agent.resolve_ask(aid2, False)
check("deny resolves as declined", agent.wait_ask(aid2, timeout=2) is False)

# --- volume via raw COM ---
vol = agent.tool_volume({"action": "get"})
print("   volume:", vol)
check("volume get works", "%" in vol and "failed" not in vol)

if fails:
    print("\n%d FAILURES: %s" % (len(fails), fails))
    sys.exit(1)
print("\nagent logic checks all pass")
