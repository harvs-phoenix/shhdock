"""ShhDock UI: a slim always-on-top dock at the top of the screen that is
invisible to screen capture, hears you through the mic (or a text box),
and pops a text bubble with local-model answers.

No background thread ever touches tk — everything flows through the event
queue which this class drains with after().
"""
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import engine

BG = "#14171f"
PANEL = "#1a1e28"
PANEL2 = "#202634"
BORDER = "#2a3040"
TEXT = "#e7e9ee"
MUTED = "#8a92a3"
ACCENT = "#3d7eff"
GREEN = "#34d399"
AMBER = "#fbbf24"
RED = "#ef4444"
HOVER = "#262c3a"

FONT = ("Segoe UI", 9)
FONT_B = ("Segoe UI", 9, "bold")
FONT_S = ("Segoe UI", 8)

PILL_W = 460
PILL_H = 38
PANEL_H = 340
BUBBLE_W = 460


# --- capture hiding ----------------------------------------------------------

def _capture_hwnds(hwnd):
    """The window's own handle, plus its parent (Tk sometimes wraps)."""
    import ctypes
    user32 = ctypes.windll.user32
    try:
        user32.GetParent.restype = ctypes.c_void_p
        parent = user32.GetParent(ctypes.c_void_p(hwnd)) or 0
    except Exception:
        parent = 0
    return [h for h in (hwnd, parent) if h]


def set_hidden_from_capture(hwnd, hidden):
    """WDA_EXCLUDEFROMCAPTURE (0x11) on our own top-level window.

    Returns the handle it was applied to, or 0 on failure.
    """
    import ctypes
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowDisplayAffinity.argtypes = (ctypes.c_void_p,
                                                    ctypes.c_uint)
        user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
        aff = 0x11 if hidden else 0
        for h in _capture_hwnds(hwnd):
            if user32.SetWindowDisplayAffinity(ctypes.c_void_p(h), aff):
                return h
    except Exception:
        pass
    return 0


def get_display_affinity(hwnd):
    """Effective affinity of the window (or its parent). None = unknown."""
    import ctypes
    try:
        user32 = ctypes.windll.user32
        user32.GetWindowDisplayAffinity.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
        user32.GetWindowDisplayAffinity.restype = ctypes.c_bool
        for h in _capture_hwnds(hwnd):
            buf = ctypes.c_uint(0)
            if user32.GetWindowDisplayAffinity(ctypes.c_void_p(h),
                                               ctypes.byref(buf)):
                return buf.value
    except Exception:
        pass
    return None


def gpu_name():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5)
        return out.decode().strip().splitlines()[0]
    except Exception:
        return None


def _hover(widget, bg=HOVER):
    base = widget.cget("bg")
    widget.bind("<Enter>", lambda e: widget.configure(bg=bg))
    widget.bind("<Leave>", lambda e: widget.configure(bg=base))


def _flat_button(parent, label, command, **kw):
    btn = tk.Button(parent, text=label, command=command,
                    relief="flat", bd=0, bg=PANEL2, fg=TEXT,
                    activebackground=HOVER, activeforeground=TEXT,
                    font=FONT_S, cursor="hand2", padx=8, pady=2, **kw)
    _hover(btn)
    return btn


# --- dock --------------------------------------------------------------------

class Dock(tk.Tk):
    def __init__(self, events, settings, server):
        super().__init__()
        self.events = events
        self.settings = settings
        self.server = server
        self.assistant = None
        self.llm_state = "off"
        self.stt_state = "off"
        self.panel_open = False
        self.bubble = None
        self.bubble_after = None
        self.settings_win = None
        self.setup_win = None
        self.dl_sink = None            # callable(label, done, total)
        self._drag = None
        self._ask_id = None            # pending agent confirmation

        self.withdraw()
        self.title("")                # blank: window listers show no name
        self.overrideredirect(True)
        self.configure(bg=BG)
        self._build_pill()
        self._position_top()
        self.deiconify()
        self.topmost(True)
        self.after(300, self._apply_capture_affinity)
        self._capture_windows = [self]      # every window we must keep hidden

        self.after(50, self._drain)
        self.after(200, self._boot)

    # -- construction ---------------------------------------------------------

    def topmost(self, on=True):
        self.attributes("-topmost", bool(on))

    def _position_top(self):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry("%dx%d+%d+%d" % (
            PILL_W, PILL_H, (sw - PILL_W) // 2, 4))

    def _apply_capture_affinity(self, attempt=0):
        """Tk's HWND isn't stable until the window is mapped, and a set on a
        premature handle 'succeeds' yet evaporates — so keep setting and
        verify by reading the effective affinity back."""
        hidden = bool(self.settings.get("hide_from_share", True))
        if not hidden:
            set_hidden_from_capture(self.winfo_id(), False)
            return
        set_hidden_from_capture(self.winfo_id(), True)
        if get_display_affinity(self.winfo_id()) == 0x11 or attempt >= 40:
            return
        self.after(150, lambda: self._apply_capture_affinity(attempt + 1))

    def _secure_window(self, win, attempt=0):
        """Every ShhDock toplevel gets the bar's treatment: capture-hidden
        (verified by reading the affinity back, retried while the window
        maps) and registered so the privacy toggle can re-apply it."""
        try:
            if not win.winfo_exists():
                return
        except tk.TclError:
            return
        if win not in self._capture_windows:
            self._capture_windows.append(win)
        if not self.settings.get("hide_from_share", True):
            set_hidden_from_capture(win.winfo_id(), False)
            return
        set_hidden_from_capture(win.winfo_id(), True)
        if get_display_affinity(win.winfo_id()) == 0x11 or attempt >= 40:
            return
        self.after(150, lambda: self._secure_window(win, attempt + 1))

    def _secure_all_windows(self):
        """Re-apply capture-hiding across every known window (privacy toggle
        can flip while the settings window itself is open)."""
        for win in list(self._capture_windows):
            self._secure_window(win)

    def _build_pill(self):
        root = tk.Frame(self, bg=BG, highlightthickness=1,
                        highlightbackground=BORDER)
        root.pack(fill="both", expand=True)
        self.root_frame = root

        grip = tk.Label(root, text="  ⠿ ", bg=BG, fg="#464d5c", font=FONT_S,
                        cursor="fleur")
        grip.pack(side="left")
        grip.bind("<Button-1>", self._drag_start)
        grip.bind("<B1-Motion>", self._drag_move)

        self.meter = tk.Canvas(root, width=52, height=16, bg=BG,
                               highlightthickness=0)
        self.meter.pack(side="left", padx=(0, 6), pady=10)
        self._meter_val = 0.0

        self.dot = tk.Label(root, text="●", bg=BG, fg="#464d5c", font=FONT_S)
        self.dot.pack(side="left")

        self.status = tk.Label(root, text="starting…", bg=BG, fg=MUTED,
                               font=FONT, anchor="w")
        self.status.pack(side="left", padx=(6, 0))

        x = tk.Button(root, text="✕", command=self._quit, relief="flat", bd=0,
                      bg=BG, fg=MUTED, activebackground=RED,
                      activeforeground=TEXT, font=FONT, cursor="hand2",
                      padx=6)
        _hover(x, bg="#3a1d24")
        x.pack(side="right", pady=8)
        self.handset_btn = None
        for label, cmd, tip in (
                ("📞", self._toggle_call, "call listening"),
                ("⚙", self._open_settings, "settings"),
                ("🪟", self._open_hider, "hide windows"),
                ("💬", self._toggle_panel, "answers")):
            lit = label == "📞" and bool(self.settings.get("call_listen"))
            b = tk.Button(root, text=label, command=cmd, relief="flat", bd=0,
                          bg=BG, fg=GREEN if lit else TEXT,
                          activebackground=HOVER,
                          activeforeground=TEXT, font=FONT, cursor="hand2",
                          padx=6)
            _hover(b)
            b.pack(side="right", pady=8)
            self._tip(b, tip)
            if label == "📞":
                self.handset_btn = b

        root.bind("<Button-3>", lambda e: self._quit())

    def _tip(self, widget, text):
        tip = None

        def show(_e):
            nonlocal tip
            if tip is not None:
                return
            tip = tk.Toplevel(self)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.configure(bg=BORDER)
            tk.Label(tip, text=text, bg=BORDER, fg=TEXT, font=FONT_S,
                     padx=6, pady=2).pack()
            x = widget.winfo_rootx() - 20
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tip.geometry("+%d+%d" % (x, y))
            tip.title("")
            self.after(120, lambda: self._secure_window(tip))

        def hide(_e=None):
            nonlocal tip
            if tip is not None:
                tip.destroy()
                tip = None
        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def _drag_start(self, event):
        self._drag = (event.x_root, event.y_root,
                      self.winfo_x(), self.winfo_y())

    def _drag_move(self, event):
        if not self._drag:
            return
        dx = event.x_root - self._drag[0]
        dy = event.y_root - self._drag[1]
        self.geometry("+%d+%d" % (self._drag[2] + dx, self._drag[3] + dy))

    # -- panel ----------------------------------------------------------------

    def _toggle_panel(self):
        if self.panel_open:
            self.panel.pack_forget()
            self.geometry("%dx%d+%d+%d" % (
                PILL_W, PILL_H, self.winfo_x(), self.winfo_y()))
            self.panel_open = False
            self._close_bubble()
        else:
            if getattr(self, "panel", None) is None:
                self.panel = self._build_panel(self)
            self.panel.pack(fill="both", expand=True)
            y = self.winfo_y()
            self.geometry("%dx%d+%d+%d" % (
                PILL_W, PILL_H + PANEL_H, self.winfo_x(), y))
            self.panel_open = True
            self._close_bubble()

    def _build_panel(self, parent):
        panel = tk.Frame(parent, bg=PANEL)
        head = tk.Frame(panel, bg=PANEL)
        head.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(head, text="ShhDock", bg=PANEL, fg=TEXT,
                 font=FONT_B).pack(side="left")
        self.engine_label = tk.Label(head, text="", bg=PANEL, fg=MUTED,
                                     font=FONT_S)
        self.engine_label.pack(side="right")
        self._refresh_engine_label()

        self.log = tk.Text(panel, height=10, wrap="word", bg=PANEL, fg=TEXT,
                           relief="flat", bd=0, font=FONT,
                           insertbackground=TEXT, highlightthickness=1,
                           highlightbackground=BORDER, state="disabled",
                           padx=10, pady=7)
        self.log.pack(fill="both", expand=True, padx=10, pady=(2, 4))
        self.log.tag_config("you", foreground=MUTED, lmargin1=2,
                            lmargin2=18, spacing1=3)
        self.log.tag_config("shh", foreground=TEXT, lmargin1=2,
                            lmargin2=18, spacing1=7)
        self.log.tag_config("them", foreground="#b48ead", lmargin1=2,
                            lmargin2=34, spacing1=3)
        self.log.tag_config("tool", foreground="#5c6474", lmargin1=2,
                            lmargin2=22, spacing1=2)
        self.log.tag_config("meta", foreground="#5c6474", justify="center")
        self.log.tag_config("who_you", foreground="#6b7280")
        self.log.tag_config("who_shh", foreground=ACCENT)
        self.log.tag_config("who_them", foreground="#7d6487")
        self.log.tag_config("who_tool", foreground="#464d5c")
        self._log_empty_hint()

        # agent confirmation bar (packed on demand, above the entry row)
        self.ask_bar = tk.Frame(panel, bg="#2a2318", highlightthickness=1,
                                highlightbackground=AMBER)
        tk.Label(self.ask_bar, text="⚠", bg="#2a2318", fg=AMBER,
                 font=FONT).pack(side="left", padx=(8, 4))
        self.ask_label = tk.Label(self.ask_bar, text="", bg="#2a2318",
                                  fg=TEXT, font=FONT_S, anchor="w")
        self.ask_label.pack(side="left", fill="x", expand=True)
        allow = _flat_button(self.ask_bar, "✓ Allow",
                             lambda: self._resolve_ask(True))
        allow.configure(bg=GREEN, fg="#0c1a10",
                        activebackground="#2e8b57")
        _hover(allow)                    # re-capture the new base color
        allow.pack(side="left", padx=(6, 2), pady=4)
        deny = _flat_button(self.ask_bar, "✕ Deny",
                            lambda: self._resolve_ask(False))
        deny.configure(bg=RED, activebackground="#7f1d2a")
        _hover(deny)
        deny.pack(side="left", padx=(0, 8), pady=4)

        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill="x", padx=10, pady=(0, 8))
        self.entry_row = row
        self.entry = tk.Entry(row, bg=PANEL2, fg=TEXT, insertbackground=TEXT,
                              relief="flat", bd=0, font=FONT)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
        self.entry.bind("<Return>", lambda _e: self._send())
        _flat_button(row, "Ask", self._send).pack(side="right")
        tk.Label(row, text="or just talk — mic is live", bg=PANEL, fg="#5c6474",
                 font=("Segoe UI", 7)).pack(side="right", padx=(0, 8))
        return panel

    def _refresh_engine_label(self):
        if not hasattr(self, "engine_label"):
            return
        st = engine.install_state(self.settings)
        bits = []
        bits.append("model ✓" if st["model"] else "model ✗")
        bits.append("mic ✓" if st["voice"] else "mic ✗")
        self.engine_label.configure(text="  ·  ".join(bits))

    _HINT = ("Ask me anything — type below or just talk.\n"
             "I run entirely on this PC, and I'm hidden from your stream.")

    def _log_empty_hint(self):
        self.log.configure(state="normal")
        self.log.insert("end", self._HINT + "\n", "meta")
        self.log.configure(state="disabled")
        self._hint_shown = True

    def _log_clear_hint(self):
        if getattr(self, "_hint_shown", False):
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
            self._hint_shown = False

    def _log(self, tag, text):
        if not self.panel_open:
            return
        self._log_clear_hint()
        self.log.configure(state="normal")
        if tag == "you":
            self.log.insert("end", "you  ", "who_you")
            self.log.insert("end", text + "\n", "you")
        elif tag == "shh":
            self.log.insert("end", "◆  ", "who_shh")
            self.log.insert("end", text + "\n", "shh")
        elif tag == "them":
            self.log.insert("end", "them  ", "who_them")
            self.log.insert("end", text + "\n", "them")
        elif tag == "tool":
            self.log.insert("end", "▸ ", "who_tool")
            self.log.insert("end", text + "\n", "tool")
        else:
            self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _send(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        if self.llm_state == "missing":
            self._log("meta", "Shh: setup isn't finished — open ⚙ settings")
            self._show_bubble("Model not installed yet — finish setup first.")
            return
        self._log("you", text)
        if self.assistant:
            self.assistant.submit_text(text)

    def _open_hider(self):
        script = Path(__file__).resolve().parent.parent / \
            "hide_from_capture" / "hide_from_capture.py"
        if sys.frozen:
            exe = Path(sys.executable).with_name("HideFromCapture.exe")
            if exe.exists():
                subprocess.Popen([str(exe)])
                return
        if script.exists():
            subprocess.Popen([sys.executable, str(script)],
                             creationflags=getattr(
                                 subprocess, "CREATE_NO_WINDOW", 0))
        else:
            self._show_bubble("Hide-windows tool not found next to ShhDock.")

    # -- bubble ---------------------------------------------------------------

    def _show_bubble(self, text):
        if self.panel_open:
            return
        self._close_bubble()
        b = tk.Toplevel(self)
        b.overrideredirect(True)
        b.attributes("-topmost", True)
        b.configure(bg=BORDER)
        inner = tk.Frame(b, bg=PANEL, highlightthickness=0)
        inner.pack(padx=1, pady=1)
        tk.Label(inner, text="Shh", bg=PANEL, fg=ACCENT, font=FONT_S,
                 anchor="w").pack(fill="x", padx=8, pady=(6, 0))
        lbl = tk.Label(inner, text=text, bg=PANEL, fg=TEXT, font=FONT,
                       wraplength=BUBBLE_W - 30, justify="left", anchor="w")
        lbl.pack(padx=8, pady=(2, 8))
        b.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - b.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + self.winfo_height() + 6
        b.geometry("+%d+%d" % (max(4, x), y))
        b.title("")
        self.after(120, lambda: self._secure_window(b))   # answers are hidden
        b.bind("<Button-1>", lambda _e: self._close_bubble())
        lbl.bind("<Button-1>", lambda _e: self._close_bubble())
        self.bubble = b
        self.bubble_after = self.after(9000, self._close_bubble)

    def _close_bubble(self):
        if self.bubble_after:
            self.after_cancel(self.bubble_after)
            self.bubble_after = None
        if self.bubble is not None:
            try:
                self.bubble.destroy()
            except Exception:
                pass
            self.bubble = None

    # -- settings -------------------------------------------------------------

    def _open_settings(self):
        if self.settings_win is not None:
            try:
                self.settings_win.lift()
                return
            except Exception:
                self.settings_win = None
        win = tk.Toplevel(self)
        win.title("Settings")           # bland: nothing recognizable listed
        win.attributes("-toolwindow", True)   # no taskbar / Alt-Tab entry
        win.configure(bg=PANEL)
        win.geometry("520x872")
        win.resizable(False, False)
        self.settings_win = win
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_settings(win))
        self._build_settings(win)
        self.after(200, lambda: self._secure_window(win))

    def _close_settings(self, win):
        self.dl_sink = None
        self.settings_win = None
        win.destroy()

    def _section(self, parent, title):
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(f, text=title, bg=PANEL, fg=ACCENT, font=FONT_S,
                 anchor="w").pack(fill="x")
        body = tk.Frame(f, bg=PANEL2, highlightthickness=1,
                        highlightbackground=BORDER)
        body.pack(fill="x", pady=(3, 0), ipady=6)
        return body

    def _build_settings(self, win):
        st = engine.install_state(self.settings)
        ram_now = engine.installed_ram_gb()

        # RAM & model
        body = self._section(win, "RAM  ·  picks the model that fits")
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10, pady=2)
        self.ram_var = tk.StringVar(value=self.settings.get("ram_mode",
                                                            "auto"))
        self.ram_gb_var = tk.StringVar(
            value=str(self.settings.get("ram_gb", 16)))
        tk.Label(row, text="Your RAM:", bg=PANEL2, fg=TEXT,
                 font=FONT).pack(side="left")
        modes = [("Auto (%.1f GB detected)" % ram_now, "auto"),
                 ("Preset", "preset"), ("Custom", "custom")]
        for label, mode in modes:
            tk.Radiobutton(row, text=label, variable=self.ram_var, value=mode,
                           bg=PANEL2, fg=TEXT, selectcolor=PANEL,
                           activebackground=PANEL2, activeforeground=TEXT,
                           font=FONT, command=self._ram_changed,
                           cursor="hand2").pack(side="left", padx=(8, 0))
        row2 = tk.Frame(body, bg=PANEL2)
        row2.pack(fill="x", padx=10, pady=(2, 2))
        self.preset = tk.StringVar(value=str(int(self.settings.get(
            "ram_gb", 16))))
        self.preset_menu = tk.OptionMenu(row2, self.preset, "4", "8", "12",
                                         "16", "24", "32", "48", "64",
                                         command=lambda _v: self._ram_changed())
        self.preset_menu.configure(bg=PANEL2, fg=TEXT, highlightthickness=0,
                                   relief="flat", font=FONT, padx=4)
        self.preset_menu.pack(side="left")
        self.custom_entry = tk.Entry(row2, width=6, bg=PANEL, fg=TEXT,
                                     insertbackground=TEXT, relief="flat",
                                     font=FONT)
        self.custom_entry.insert(0, self.ram_gb_var.get())
        self.custom_entry.pack(side="left", padx=(6, 0), ipady=3)
        self.custom_entry.bind("<Return>", lambda _e: self._ram_changed())
        tk.Label(row2, text="GB total RAM", bg=PANEL2, fg=MUTED,
                 font=FONT_S).pack(side="left", padx=(4, 0))
        self.model_card = tk.Label(body, text="", bg=PANEL2, fg=TEXT,
                                   font=FONT, anchor="w", justify="left")
        self.model_card.pack(fill="x", padx=10, pady=(4, 2))
        self.ram_changed_row = tk.Frame(body, bg=PANEL2)
        self.ram_changed_row.pack(fill="x", padx=10, pady=(0, 2))
        self._ram_changed()
        self._sync_ram_widgets()

        # Engine
        body = self._section(win, "Engine  ·  llama.cpp")
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10)
        self.engine_status = tk.Label(
            row, text="✓ installed" if st["engine"] else "✗ not installed",
            bg=PANEL2, fg=GREEN if st["engine"] else RED, font=FONT)
        self.engine_status.pack(side="left")
        self.gpu_var = tk.BooleanVar(value=bool(self.settings.get("gpu")))
        gpu = gpu_name()
        tk.Checkbutton(row, text="Use GPU%s (experimental)" % (
            " — " + gpu if gpu else ""), variable=self.gpu_var, bg=PANEL2,
            fg=TEXT, selectcolor=PANEL, activebackground=PANEL2,
            activeforeground=TEXT, font=FONT, cursor="hand2",
            command=self._gpu_changed).pack(side="right")

        # Assistant
        body = self._section(win, "Assistant")
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10)
        self.mode_var = tk.StringVar(value=self.settings.get("answer_mode",
                                                             "questions"))
        for label, mode in (("Answer questions only", "questions"),
                            ("Answer everything I say", "everything")):
            tk.Radiobutton(row, text=label, variable=self.mode_var, value=mode,
                           bg=PANEL2, fg=TEXT, selectcolor=PANEL,
                           activebackground=PANEL2, activeforeground=TEXT,
                           font=FONT, cursor="hand2",
                           command=self._mode_changed).pack(side="left",
                                                            padx=(0, 14))
        row2 = tk.Frame(body, bg=PANEL2)
        row2.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(row2, text="Microphone:", bg=PANEL2, fg=TEXT,
                 font=FONT).pack(side="left")
        self.mic_var = tk.StringVar(value=self.settings.get("mic_device",
                                                            "default"))
        names = ["default"] + self._mic_names()
        self.mic_menu = tk.OptionMenu(row2, self.mic_var, *names,
                                      command=lambda _v: self._mic_changed())
        self.mic_menu.configure(bg=PANEL2, fg=TEXT, highlightthickness=0,
                                relief="flat", font=FONT, padx=4)
        self.mic_menu.pack(side="left", padx=(8, 0))
        self.mic_meter = tk.Canvas(row2, width=60, height=12, bg=PANEL2,
                                   highlightthickness=0)
        self.mic_meter.pack(side="right")

        # Agent
        body = self._section(win, "Agent  ·  can operate this PC")
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10)
        self.agent_var = tk.StringVar(value=self.settings.get("agent", "ask"))
        for label, mode in (("Off", "off"), ("Ask first", "ask"),
                            ("Full auto", "auto")):
            tk.Radiobutton(row, text=label, variable=self.agent_var,
                           value=mode, bg=PANEL2, fg=TEXT, selectcolor=PANEL,
                           activebackground=PANEL2, activeforeground=TEXT,
                           font=FONT, cursor="hand2",
                           command=self._agent_changed).pack(side="left",
                                                             padx=(0, 14))
        tk.Label(body, text="Tools: run commands, open apps, browse files, "
                 "manage windows, type. \"Ask first\" shows a ✓/✕ prompt in "
                 "the panel — destructive commands always ask. Every action "
                 "is logged in actions.log.",
                 bg=PANEL2, fg=MUTED, font=FONT_S, anchor="w", justify="left",
                 wraplength=470).pack(fill="x", padx=10, pady=(2, 4))

        # Call listening
        body = self._section(win, "Call listening  ·  Discord / Zoom / game")
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10)
        self.call_var = tk.BooleanVar(value=bool(
            self.settings.get("call_listen")))
        tk.Checkbutton(row, text="Transcribe what plays on my speakers",
                       variable=self.call_var, bg=PANEL2, fg=TEXT,
                       selectcolor=PANEL, activebackground=PANEL2,
                       activeforeground=TEXT, font=FONT, cursor="hand2",
                       command=self._call_toggled).pack(side="left")
        self.call_status = tk.Label(row, text="", bg=PANEL2, fg=MUTED,
                                    font=FONT_S)
        self.call_status.pack(side="right")
        row2 = tk.Frame(body, bg=PANEL2)
        row2.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(row2, text="Device:", bg=PANEL2, fg=TEXT,
                 font=FONT).pack(side="left")
        self.call_device_var = tk.StringVar(value=self.settings.get(
            "call_device", "default"))
        try:
            import callaudio as _ca
            dev_names = _ca.enumerate_render_outputs()
        except Exception:
            dev_names = ["default"]
        self.call_menu = tk.OptionMenu(row2, self.call_device_var,
                                       *dev_names,
                                       command=lambda _v:
                                       self._call_device_changed())
        self.call_menu.configure(bg=PANEL2, fg=TEXT, highlightthickness=0,
                                 relief="flat", font=FONT, padx=4)
        self.call_menu.pack(side="left", padx=(8, 0))
        self.call_meter = tk.Canvas(row2, width=60, height=12, bg=PANEL2,
                                    highlightthickness=0)
        self.call_meter.pack(side="right")

        # Browser link (Chrome extension)
        body = self._section(win, "Browser link  ·  control Chrome")
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10)
        self.link_status = tk.Label(row, text="checking…", bg=PANEL2,
                                    fg=MUTED, font=FONT_S)
        self.link_status.pack(side="left")
        _flat_button(row, "Install extension",
                     self._install_ext).pack(side="right")
        tk.Label(body, text="Load the ShhDock extension once: open "
                 "chrome://extensions → Developer mode → Load unpacked → "
                 "pick the folder that opens. Then the agent can read the "
                 "open page, Google things, click and type. Everything stays "
                 "on 127.0.0.1.",
                 bg=PANEL2, fg=MUTED, font=FONT_S, anchor="w", justify="left",
                 wraplength=470).pack(fill="x", padx=10, pady=(2, 4))
        self._poll_link_status(win)

        # Privacy
        body = self._section(win, "Privacy")
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10)
        self.hide_var = tk.BooleanVar(value=bool(
            self.settings.get("hide_from_share", True)))
        tk.Checkbutton(row, text="Dock is invisible on screen shares "
                       "(Discord / OBS)", variable=self.hide_var, bg=PANEL2,
            fg=TEXT, selectcolor=PANEL, activebackground=PANEL2,
            activeforeground=TEXT, font=FONT, cursor="hand2",
            command=self._hide_changed).pack(side="left")

        # Data
        body = self._section(win, "Data")
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10)
        _flat_button(row, "Open folder", self._open_data).pack(side="left")
        _flat_button(row, "Remove downloads",
                     self._remove_downloads).pack(side="left", padx=8)

        tk.Button(win, text="Close", command=lambda: self._close_settings(win),
                  relief="flat", bd=0, bg=ACCENT, fg="#ffffff",
                  activebackground="#2f66cc", activeforeground="#ffffff",
                  font=FONT_B, cursor="hand2", pady=4).pack(fill="x",
                                                            padx=12, pady=12)

    def _mic_names(self):
        try:
            import sounddevice as sd
            return [str(d["name"]) for d in sd.query_devices()
                    if d["max_input_channels"] > 0]
        except Exception:
            return []

    # -- settings actions -----------------------------------------------------

    # -- agent & call listening -----------------------------------------------

    def _agent_changed(self):
        self.settings["agent"] = self.agent_var.get()
        engine.save_settings(self.settings)

    def _toggle_call(self):
        on = not bool(self.settings.get("call_listen"))
        self.settings["call_listen"] = on
        engine.save_settings(self.settings)
        if hasattr(self, "call_var"):
            self.call_var.set(on)
        if on:
            self._start_call()
        else:
            self._stop_call()

    def _call_toggled(self):
        on = bool(self.call_var.get())
        self.settings["call_listen"] = on
        engine.save_settings(self.settings)
        if on:
            self._start_call()
        else:
            self._stop_call()

    def _call_device_changed(self):
        self.settings["call_device"] = self.call_device_var.get()
        engine.save_settings(self.settings)
        if self.settings.get("call_listen"):
            self._stop_call()
            self._start_call()

    def _install_ext(self):
        """Open the extension folder (and keep it refreshed from the app)."""
        import os
        try:
            import browserlink
            path = browserlink.extension_dir()      # copies the latest files
            os.startfile(path)
        except Exception as exc:
            self._log("meta", "(extension folder failed: %s)" % exc)

    def _poll_link_status(self, win):
        """Live 'connected / waiting' light for the Browser link section."""
        if not win.winfo_exists():
            return
        if hasattr(self, "link_status"):
            try:
                import browserlink
                browserlink.start()
                st = browserlink.status()
                if st["connected"]:
                    self.link_status.configure(text="● extension connected",
                                               fg=GREEN)
                elif st["running"]:
                    self.link_status.configure(
                        text="● waiting for Chrome (load the extension)",
                        fg=AMBER)
                else:
                    self.link_status.configure(
                        text="● bridge not running", fg=RED)
            except Exception as exc:
                try:
                    self.link_status.configure(text="● %s" % str(exc)[:36],
                                               fg=RED)
                except tk.TclError:
                    return
        try:
            win.after(1500, lambda: self._poll_link_status(win))
        except tk.TclError:
            pass

    def _start_call(self):
        self._set_handset(True)
        try:
            import callaudio
            callaudio.start(self.events, self.settings)
        except Exception as exc:
            self._set_handset(False)
            self._log("meta", "(call listening failed: %s)" % exc)

    def _stop_call(self):
        self._set_handset(False)
        try:
            import callaudio
            callaudio.stop()
        except Exception:
            pass
        if hasattr(self, "call_status"):
            self.call_status.configure(text="off", fg=MUTED)

    def _set_handset(self, on):
        if getattr(self, "handset_btn", None) is not None:
            try:
                self.handset_btn.configure(fg=GREEN if on else TEXT)
            except tk.TclError:
                pass

    def _on_call_state(self, ev):
        state = ev.get("state")
        msg = ev.get("msg", "")
        if hasattr(self, "call_status"):
            if state == "listening":
                self.call_status.configure(text="listening", fg=GREEN)
            elif state == "loading":
                self.call_status.configure(text="starting…", fg=AMBER)
            elif state == "error":
                self.call_status.configure(text="error: " + msg[:40], fg=RED)
            else:
                self.call_status.configure(text="off", fg=MUTED)
        if state == "listening":
            self._set_handset(True)
            self._log("meta", "(call listening on — %s)" %
                      self.settings.get("call_device", "default"))
        elif state == "error":
            self._set_handset(False)
            self._log("meta", "(call listening error: %s)" % msg)
        elif state == "off":
            self._set_handset(False)

    # -- agent confirmation bar -----------------------------------------------

    def _show_ask(self, ev):
        if not self.panel_open:
            self._toggle_panel()
        self._ask_id = ev.get("id")
        text = ev.get("summary", "")
        if ev.get("danger"):
            text = "⚠ destructive? " + text
        self.ask_label.configure(text=text[:120])
        try:
            self.ask_bar.pack(fill="x", padx=10, pady=(0, 6),
                              before=self.entry_row)
        except tk.TclError:
            self.ask_bar.pack(fill="x", padx=10, pady=(0, 6))
        self._log("meta", "(needs approval: %s)" % text[:80])
        self._set_status("needs approval…", AMBER)

    def _resolve_ask(self, approved):
        ask_id = self._ask_id
        self._ask_id = None
        try:
            self.ask_bar.pack_forget()
        except tk.TclError:
            pass
        try:
            import agent
            agent.resolve_ask(ask_id, approved)
        except Exception:
            pass
        self._log("meta", "(approved)" if approved else "(denied)")
        self.dot.configure(fg=GREEN)
        self._set_status("listening", GREEN)

    def _sync_ram_widgets(self):
        mode = self.ram_var.get()
        state_preset = "normal" if mode == "preset" else "disabled"
        state_custom = "normal" if mode == "custom" else "disabled"
        try:
            self.preset_menu.configure(state=state_preset)
        except tk.TclError:
            pass
        self.custom_entry.configure(state=state_custom)

    def _ram_changed(self):
        self._sync_ram_widgets()
        mode = self.ram_var.get()
        self.settings["ram_mode"] = mode
        if mode == "custom":
            try:
                self.settings["ram_gb"] = max(1.0, min(
                    512.0, float(self.custom_entry.get() or "16")))
            except ValueError:
                self.settings["ram_gb"] = 16.0
            self.preset.set(str(int(self.settings["ram_gb"])))
        elif mode == "preset":
            self.settings["ram_gb"] = float(self.preset.get())
        model_id = engine.resolve_model_id(self.settings)
        engine.save_settings(self.settings)
        self._model_card(model_id)
        self._refresh_engine_label()

    def _model_card(self, model_id):
        if not hasattr(self, "model_card"):
            return
        m = engine.MODELS[model_id]
        have = engine.gguf_path(model_id).exists()
        self.model_card.configure(
            text="→ %s · %s · %.1f GB  %s" % (
                m["label"], m["quant"], m["gb"],
                "✓ installed" if have else "· not downloaded yet"))
        for child in self.ram_changed_row.winfo_children():
            child.destroy()
        if not have:
            _flat_button(self.ram_changed_row, "Download model",
                         lambda: self._download_model(model_id)).pack(
                side="left")
            self.ram_changed_row.pack(fill="x", padx=10, pady=(0, 6))
        else:
            self.ram_changed_row.pack_forget()

    def _download_model(self, model_id):
        def work():
            self.events.put({"t": "step", "label": "Downloading model…"})
            try:
                engine.ensure_model(model_id, self.events.put)
                self.events.put({"t": "installed",
                                 "state": engine.install_state(
                                     self.settings)})
            except Exception as exc:
                self.events.put({"t": "dlerror", "msg": str(exc)})
        threading.Thread(target=work, daemon=True).start()

    def _gpu_changed(self):
        self.settings["gpu"] = bool(self.gpu_var.get())
        engine.save_settings(self.settings)
        if self.settings["gpu"]:
            def work():
                try:
                    engine.ensure_engine(self.events.put, gpu=True)
                    self.events.put({"t": "installed", "state":
                                     engine.install_state(self.settings)})
                    self._restart_server()
                except Exception as exc:
                    self.events.put({"t": "dlerror", "msg": str(exc)})
            threading.Thread(target=work, daemon=True).start()
        else:
            self._restart_server()

    def _restart_server(self):
        self.server.stop()
        threading.Thread(
            target=lambda: self.server.start(self.settings["model_id"],
                                             gpu=bool(self.settings.get(
                                                 "gpu"))),
            daemon=True).start()

    def _mode_changed(self):
        self.settings["answer_mode"] = self.mode_var.get()
        engine.save_settings(self.settings)

    def _mic_changed(self):
        self.settings["mic_device"] = self.mic_var.get()
        engine.save_settings(self.settings)
        if self.assistant:
            self.assistant.restart()

    def _hide_changed(self):
        self.settings["hide_from_share"] = bool(self.hide_var.get())
        engine.save_settings(self.settings)
        set_hidden_from_capture(self.winfo_id(),
                                self.settings["hide_from_share"])
        self._secure_all_windows()      # bubble / settings / setup too

    def _open_data(self):
        subprocess.Popen(["explorer", str(engine.DATA_DIR)])

    def _remove_downloads(self):
        if getattr(self, "_confirm_rm", False):
            self._confirm_rm = False
            if self.assistant:
                self.assistant.stop()
            self.server.stop()
            import shutil
            try:
                shutil.rmtree(engine.MODEL_DIR, ignore_errors=True)
            except Exception:
                pass
            self.events.put({"t": "installed",
                             "state": engine.install_state(self.settings)})
            self._set_status("downloads removed", MUTED)
            return
        self._confirm_rm = True
        self._set_status("click 'Remove downloads' again to confirm", AMBER)

    # -- first-run setup ------------------------------------------------------

    def _open_setup(self):
        if self.setup_win is not None:
            try:
                self.setup_win.lift()
                return
            except Exception:
                self.setup_win = None
        win = tk.Toplevel(self)
        win.title("Setup")
        win.attributes("-toolwindow", True)   # no taskbar / Alt-Tab entry
        win.configure(bg=PANEL)
        win.geometry("500x440")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_setup(win))
        self.setup_win = win
        self._build_setup(win)
        self.after(200, lambda: self._secure_window(win))

    def _close_setup(self, win):
        self.dl_sink = None
        self.setup_win = None
        win.destroy()

    def _build_setup(self, win):
        head = tk.Frame(win, bg=PANEL)
        head.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(head, text="Welcome to ShhDock", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Label(head, text="first run", bg=PANEL, fg=MUTED,
                 font=FONT_S).pack(side="right")
        tk.Label(win, text="Pick your RAM — ShhDock downloads a local model\n"
                 "that fits and answers without the internet.",
                 bg=PANEL, fg=MUTED, font=FONT, justify="left",
                 anchor="w").pack(fill="x", padx=14, pady=(4, 0))

        ram_now = engine.installed_ram_gb()
        body = self._section(win, "How much RAM does this PC have?")
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10)
        self.setup_ram = tk.StringVar(value="auto")
        for label, mode in (("Auto (%.1f GB)" % ram_now, "auto"),
                            ("4 GB", "4"), ("8 GB", "8"), ("16 GB", "16")):
            tk.Radiobutton(row, text=label, variable=self.setup_ram,
                           value=mode, bg=PANEL2, fg=TEXT, selectcolor=PANEL,
                           activebackground=PANEL2, activeforeground=TEXT,
                           font=FONT, cursor="hand2",
                           command=self._setup_model_card).pack(
                side="left", padx=(0, 10))
        row = tk.Frame(body, bg=PANEL2)
        row.pack(fill="x", padx=10, pady=(2, 0))
        for label, mode in (("32 GB", "32"), ("64 GB", "64"),
                            ("Custom", "custom")):
            tk.Radiobutton(row, text=label, variable=self.setup_ram,
                           value=mode, bg=PANEL2, fg=TEXT, selectcolor=PANEL,
                           activebackground=PANEL2, activeforeground=TEXT,
                           font=FONT, cursor="hand2",
                           command=self._setup_model_card).pack(
                side="left", padx=(0, 10))
        self.setup_custom = tk.Entry(row, width=6, bg=PANEL, fg=TEXT,
                                     insertbackground=TEXT, relief="solid",
                                     bd=1, highlightthickness=0,
                                     disabledbackground=PANEL,
                                     disabledforeground=MUTED, font=FONT,
                                     state="disabled")
        self.setup_custom.pack(side="left", ipady=3)
        self.setup_custom.bind("<Return>", lambda _e: self._setup_model_card())
        tk.Label(row, text="GB", bg=PANEL2, fg=MUTED, font=FONT_S).pack(
            side="left", padx=(4, 0))
        self.setup_model = tk.Label(body, text="", bg=PANEL2, fg=TEXT,
                                    font=FONT_B, anchor="w")
        self.setup_model.pack(fill="x", padx=10, pady=(4, 4))
        self._setup_model_card()

        body = self._section(win, "What gets installed  (one time, offline "
                            "afterwards)")
        self.setup_items = {}
        for key, label in (("engine", "Engine (llama.cpp binaries)  ~60 MB"),
                           ("voice", "Voice recognition  ~40 MB"),
                           ("model", "Chat model (sized to your RAM)")):
            row = tk.Frame(body, bg=PANEL2)
            row.pack(fill="x", padx=10, pady=1)
            dot = tk.Label(row, text="○", bg=PANEL2, fg=MUTED, font=FONT)
            dot.pack(side="left")
            tk.Label(row, text=label, bg=PANEL2, fg=TEXT,
                     font=FONT).pack(side="left", padx=(4, 0))
            self.setup_items[key] = dot
        self.setup_bar = tk.Canvas(body, width=440, height=10, bg=PANEL,
                                   highlightthickness=0)
        self.setup_bar.pack(padx=10, pady=(6, 2))
        self.setup_status = tk.Label(body, text="ready when you are",
                                     bg=PANEL2, fg=MUTED, font=FONT_S,
                                     anchor="w")
        self.setup_status.pack(fill="x", padx=10, pady=(2, 6))
        # reflect what's already on disk (returning user, or pre-seeded)
        st = engine.install_state(self.settings)
        for key, dot in self.setup_items.items():
            if st.get(key):
                dot.configure(text="●", fg=GREEN)
        self._setup_model_card()

        row = tk.Frame(win, bg=PANEL)
        row.pack(fill="x", padx=14, pady=12)
        self.setup_btn = tk.Button(row, text="Install & start",
                                   command=self._start_install,
                                   relief="flat", bd=0, bg=ACCENT, fg="#fff",
                                   activebackground="#2f66cc",
                                   activeforeground="#fff", font=FONT_B,
                                   cursor="hand2", pady=5, padx=14)
        self.setup_btn.pack(side="left")
        _flat_button(row, "Skip for now",
                     lambda: self._close_setup(win)).pack(side="right")
        if all(st.get(k) for k in ("engine", "voice", "model")):
            self.setup_status.configure(
                text="everything is already installed", fg=GREEN)
            self.setup_btn.configure(text="Start")

    def _setup_ram_gb(self):
        mode = self.setup_ram.get()
        if mode == "auto":
            return engine.installed_ram_gb()
        if mode == "custom":
            try:
                return max(1.0, min(512.0, float(
                    self.setup_custom.get() or "16")))
            except ValueError:
                return 16.0
        return float(mode)

    def _setup_model_card(self):
        mode = self.setup_ram.get()
        self.setup_custom.configure(
            state="normal" if mode == "custom" else "disabled")
        gb = self._setup_ram_gb()
        model_id = engine.model_for_ram(gb)
        m = engine.MODELS[model_id]
        have = engine.gguf_path(model_id).exists()
        self.setup_model.configure(
            text="%s  ·  %s  ·  %.1f GB download%s" % (
                m["label"], m["quant"], m["gb"],
                "  ✓ already have it" if have else ""))
        if hasattr(self, "setup_items"):
            self.setup_items["model"].configure(
                text="●" if have else "○",
                fg=GREEN if have else MUTED)

    def _start_install(self):
        settings = self.settings
        mode = self.setup_ram.get()
        settings["ram_mode"] = "auto" if mode == "auto" else (
            "custom" if mode == "custom" else "preset")
        settings["ram_gb"] = self._setup_ram_gb()
        engine.resolve_model_id(settings)
        engine.save_settings(settings)
        self.setup_btn.configure(state="disabled", text="Installing…")
        self.dl_sink = self._setup_progress

        def work():
            try:
                engine.ensure_installed(settings, self.events.put)
            except Exception as exc:
                self.events.put({"t": "dlerror", "msg": str(exc)})
        threading.Thread(target=work, daemon=True).start()

    def _setup_progress(self, label, done, total):
        if self.setup_win is None:
            return
        c = self.setup_bar
        c.delete("all")
        w = 440
        frac = 1.0 if total <= 0 else max(0.0, min(1.0, done / total))
        c.create_rectangle(0, 0, w, 10, fill=PANEL, width=0)
        c.create_rectangle(0, 0, w * frac, 10, fill=ACCENT, width=0)
        if total > 0 and done < total:
            self.setup_status.configure(
                text="%s — %.1f / %.1f MB" % (label, done / 1048576,
                                              total / 1048576))
        else:
            self.setup_status.configure(text=label + " …")

    # -- boot / event loop ----------------------------------------------------

    def _boot(self):
        st = engine.install_state(self.settings)
        have_all = st["engine"] and st["voice"] and st["model"]
        if have_all:
            if not self.settings.get("setup_done"):
                self.settings["setup_done"] = True
                engine.save_settings(self.settings)
            self._start_runtime()
            return
        self._set_status("setup needed", AMBER)
        self._open_setup()

    def _start_runtime(self):
        model_id = self.settings.get("model_id", "qwen2.5-3b")
        gpu = bool(self.settings.get("gpu"))
        self._set_status("loading model…", AMBER)
        threading.Thread(target=lambda: self.server.start(model_id,
                                                          gpu=gpu),
                         daemon=True).start()
        try:
            import assistant as assistant_mod
            self.assistant = assistant_mod.Assistant(
                self.events, self.settings, self.server)
            self.assistant.start()
        except Exception as exc:
            self._set_status("mic failed: %s" % exc, RED)
        try:
            import browserlink
            browserlink.start()          # local bridge for the Chrome ext
        except Exception:
            pass
        if self.settings.get("call_listen"):
            self._start_call()

    def _drain(self):
        try:
            while True:
                ev = self.events.get_nowait()
                try:
                    self.handle_event(ev)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(50, self._drain)

    def handle_event(self, ev):
        t = ev.get("t")
        if t == "meter":
            self._draw_meter(ev["level"])
            if hasattr(self, "mic_meter"):
                self._draw_mic_bar(ev["level"])
        elif t == "call_level":
            if hasattr(self, "call_meter"):
                self._draw_mic_bar(ev["level"], canvas=self.call_meter)
        elif t == "call_state":
            self._on_call_state(ev)
        elif t == "call_text":
            self._log("them", ev["text"])
        elif t == "tool":
            self._log("tool", ev.get("text", ""))
        elif t == "ask":
            self._show_ask(ev)
        elif t == "partial":
            self._set_status("hearing: " + ev["text"][:70], MUTED)
        elif t == "speech_start":
            self.dot.configure(fg=GREEN)
        elif t == "speech_end":
            self.dot.configure(fg=GREEN if self.stt_state == "listening"
                               else "#5c6474")
            if ev.get("text"):
                self._log("you", ev["text"])
            self._set_status("listening", GREEN)
        elif t == "heard":
            if not ev.get("answered"):
                self._log("meta", "(ignored: %s)" % ev["text"])
        elif t == "thinking":
            self.dot.configure(fg=AMBER)
            self._set_status("thinking…", AMBER)
        elif t == "answer":
            self.dot.configure(fg=GREEN)
            self._set_status("listening", GREEN)
            self._log("shh", ev["text"])
            self._show_bubble(ev["text"])
        elif t == "busy":
            self._log("meta", "(still answering the previous question)")
        elif t == "stt":
            state = ev.get("state")
            self.stt_state = state
            if state == "listening":
                self.dot.configure(fg=GREEN)
                self._set_status("listening", GREEN)
            elif state == "loading":
                self._set_status("waking up the mic…", AMBER)
            elif state == "error":
                self.dot.configure(fg=RED)
                self._set_status(ev.get("msg", "mic error"), RED)
            elif state == "missing":
                self._set_status("voice model missing — run setup", RED)
        elif t == "llm":
            state = ev.get("state")
            self.llm_state = state
            self._refresh_engine_label()
            if state == "ready":
                self._set_status("listening", GREEN)
            elif state == "loading":
                self._set_status("loading model…", AMBER)
            elif state in ("failed", "timeout"):
                self._set_status("model failed — see llama-server.log", RED)
            elif state == "missing":
                self.llm_state = "missing"
                self._set_status("model missing — run setup", RED)
        elif t == "dl":
            if self.dl_sink:
                self.dl_sink(ev.get("label", ""), ev.get("done", 0),
                             ev.get("total", 0))
        elif t == "step":
            if self.setup_win is not None and self.dl_sink:
                self.dl_sink(ev.get("label", ""), 0, 0)
            self._set_status(ev.get("label", ""), MUTED)
        elif t == "installed":
            self._refresh_engine_label()
            if hasattr(self, "engine_status"):
                have = ev.get("state", {}).get("engine")
                self.engine_status.configure(
                    text="✓ installed" if have else "✗ not installed",
                    fg=GREEN if have else RED)
            if self.setup_win is not None:
                self._setup_model_card()
                for key, dot in self.setup_items.items():
                    if ev.get("state", {}).get(key):
                        dot.configure(text="●", fg=GREEN)
                self.setup_btn.configure(state="normal", text="Done ✓")
                self.setup_status.configure(text="all set — closing…")
                win = self.setup_win
                self.after(900, lambda: (self._close_setup(win)
                                         if self.setup_win is win else None))
                self.settings["setup_done"] = True
                engine.save_settings(self.settings)
                self.after(400, self._start_runtime)
            elif not self.settings.get("setup_done"):
                self.settings["setup_done"] = True
                engine.save_settings(self.settings)
        elif t == "dlerror":
            self._set_status("download failed: " + ev.get("msg", "")[:60], RED)
            if self.setup_win is not None:
                self.setup_btn.configure(state="normal",
                                         text="Retry install")
                self.setup_status.configure(
                    text="failed: " + ev.get("msg", "")[:80])

    def _draw_meter(self, level):
        c = self.meter
        c.delete("all")
        w, h = 52, 16
        seg, gap = 3, 2                      # LED segment + spacing
        n = w // (seg + gap)
        lit = int(round(level * n))
        for i in range(n):
            x0 = i * (seg + gap)
            on = i < lit
            if on:
                color = GREEN if i < n * 0.7 else (
                    AMBER if i < n * 0.9 else RED)
            else:
                color = "#232833"
            c.create_rectangle(x0, 3, x0 + seg, h - 3, fill=color, width=0)

    def _draw_mic_bar(self, level, canvas=None):
        c = canvas if canvas is not None else self.mic_meter
        c.delete("all")
        w, h = 60, 12
        seg, gap = 3, 2
        n = w // (seg + gap)
        lit = int(round(level * n))
        for i in range(n):
            x0 = i * (seg + gap)
            on = i < lit
            color = (GREEN if i < n * 0.7 else (
                AMBER if i < n * 0.9 else RED)) if on else "#232833"
            c.create_rectangle(x0, 2, x0 + seg, h - 2, fill=color, width=0)

    def _set_status(self, text, color):
        self.status.configure(text=text, fg=color)

    def _quit(self):
        try:
            import callaudio
            callaudio.stop()
        except Exception:
            pass
        try:
            if self.assistant:
                self.assistant.stop()
        except Exception:
            pass
        try:
            self.server.stop()
        except Exception:
            pass
        self.destroy()
            