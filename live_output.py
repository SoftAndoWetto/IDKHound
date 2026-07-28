#!/usr/bin/env python3
"""
live_output.py

Shared subprocess-streaming helper used by installer.py (build steps - a
rusthound-style `cargo build --release` can take minutes and previously
just looked hung, since install steps ran with capture_output=True and
printed nothing until the whole thing finished) and runner.py (collector
tool execution, which previously dumped raw, unbounded stdout straight to
the terminal).

RollingPanel prints a boxed title once, then keeps a fixed-height window of
the last N output lines directly beneath it, redrawn in place via ANSI
cursor movement - so the terminal shows "still alive, here's what it's
doing right now" without scrolling thousands of lines. The full output is
still kept around internally so callers can dump it on failure - the
rolling window is a display choice, not a data-loss one.

Also exposes small colored status helpers (stage/ok_line/err_line/...) so
the text wrapped around a panel (tool headers, summaries) reads as part of
the same UI instead of clashing with it.
"""

import os
import shutil
import subprocess
import sys
import time
import threading
from collections import deque


# --------------------------------------------------------------------------- #
# Color / styling
# --------------------------------------------------------------------------- #

class C:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    BLUE = "\x1b[34m"
    MAGENTA = "\x1b[35m"
    CYAN = "\x1b[36m"
    GRAY = "\x1b[90m"

_CLEAR_LINE = "\x1b[2K"
_CURSOR_UP = "\x1b[1A"


def _use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, *codes: str) -> str:
    if not codes or not _use_color():
        return text
    return "".join(codes) + text + C.RESET

# Public alias - other modules (installer.py, runner.py) shouldn't reach
# into the underscored helper directly.
colorize = _c


def _term_width(default=100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def _truncate(line: str, width: int) -> str:
    line = line.rstrip("\n")
    if width > 1 and len(line) > width:
        return line[: width - 1] + "…"
    return line


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


# --------------------------------------------------------------------------- #
# Standalone status lines - for the text wrapped around a panel, so it
# reads as one consistent UI instead of a colored box next to plain text.
# --------------------------------------------------------------------------- #

def stage(title: str):
    """A section header, e.g. one per tool being installed/updated."""
    print(_c(f"\n▶ {title}", C.BOLD, C.MAGENTA))

def ok_line(msg: str):
    print(f"  {_c('✓', C.GREEN)} {msg}")

def err_line(msg: str):
    print(f"  {_c('✗', C.RED)} {msg}")

def warn_line(msg: str):
    print(f"  {_c('!', C.YELLOW)} {msg}")

def info_line(msg: str):
    print(f"  {_c('·', C.CYAN)} {msg}")


# --------------------------------------------------------------------------- #
# Rolling panel
# --------------------------------------------------------------------------- #

class RollingPanel:
    """
    Usage:
        panel = RollingPanel("rusthound-ce: cargo build --release")
        panel.push("   Compiling foo v0.1.0")
        ...
        panel.finish(ok=True)

    Renders as a small box:

        ┌─ rusthound-ce: cargo build --release ──────────────
        │  Compiling anyhow v1.0.75
        │  Compiling serde v1.0.188
        │  Compiling clap v4.4.6
        │  Compiling rusthound-ce v2.2.0
        │
        └─ ✓ done (47s) ──────────────────────────────────────

    Falls back to plain sequential printing (no cursor tricks, no box-
    drawing redraw) when stdout isn't a real terminal - e.g. piped into a
    log file or CI - since ANSI cursor-repositioning garbles non-tty
    output. Color is likewise skipped whenever NO_COLOR is set or stdout
    isn't a tty.
    """

    def __init__(self, title: str, num_lines: int = 5):
        self.title = title
        self.num_lines = num_lines
        self.buf = deque(maxlen=num_lines)
        self._is_tty = sys.stdout.isatty()
        self._started = False
        self._start_time = None

    def start(self):
        if self._started:
            return
        self._start_time = time.monotonic()
        width = max(20, _term_width())
        top_plain = f"┌─ {self.title} "
        fill = "─" * max(0, width - len(top_plain) - 1)
        print(_c("┌─ ", C.DIM) + _c(self.title, C.BOLD, C.CYAN) + " " + _c(fill, C.DIM))
        if self._is_tty:
            bar = _c("│", C.DIM)
            for _ in range(self.num_lines):
                sys.stdout.write(bar + "\n")
            sys.stdout.flush()
        self._started = True

    def push(self, line: str):
        line = line.rstrip("\n")
        if not line:
            return
        self.buf.append(line)
        if not self._started:
            self.start()
        self._redraw()

    def _redraw(self):
        bar = _c("│", C.DIM)
        if not self._is_tty:
            print(f"{bar}  {_c(self.buf[-1], C.DIM)}")
            return
        width = max(10, _term_width() - 5)
        sys.stdout.write(_CURSOR_UP * self.num_lines)
        for i in range(self.num_lines):
            sys.stdout.write(_CLEAR_LINE)
            if i < len(self.buf):
                sys.stdout.write(f"{bar}  {_c(_truncate(self.buf[i], width), C.DIM)}\n")
            else:
                sys.stdout.write(f"{bar}\n")
        sys.stdout.flush()

    def finish(self, ok: bool, summary: str = None):
        """Called once the underlying process has exited. Clears the live
        window and replaces it with a single boxed footer line, so it's
        obvious at a glance whether the step finished clean - and how long
        it took, which is the whole point for something like rusthound's
        build (previously looked hung with zero feedback)."""
        if not self._started:
            self.start()
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        msg = summary or ("done" if ok else "failed")
        icon_plain = "✓" if ok else "✗"
        icon_color = C.GREEN if ok else C.RED

        if self._is_tty:
            sys.stdout.write(_CURSOR_UP * self.num_lines)
            for _ in range(self.num_lines):
                sys.stdout.write(_CLEAR_LINE + "\n")
            sys.stdout.write(_CURSOR_UP * self.num_lines)
            sys.stdout.flush()

        width = max(20, _term_width())
        footer_plain = f"└─ {icon_plain} {msg} ({_fmt_duration(elapsed)}) "
        fill = "─" * max(0, width - len(footer_plain) - 1)
        print(
            _c("└─ ", C.DIM)
            + _c(icon_plain, icon_color)
            + " "
            + _c(f"{msg} ({_fmt_duration(elapsed)})", icon_color)
            + " "
            + _c(fill, C.DIM)
        )


def run_streaming(cmd, cwd=None, shell: bool = False, title: str = None,
                   num_lines: int = 5, timeout: float = None):
    """
    Runs `cmd` (list argv, or a string if shell=True), streaming combined
    stdout+stderr line-by-line through a RollingPanel.

    Returns (returncode, timed_out, full_output: list[str]). full_output is
    every line produced, regardless of what the rolling window actually
    displayed - callers use it to print a full failure dump without having
    to re-run anything.
    """
    display_title = title or (cmd if isinstance(cmd, str) else " ".join(cmd))
    panel = RollingPanel(display_title, num_lines=num_lines)
    panel.start()

    full_output = []
    timed_out_flag = {"flag": False}

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        panel.finish(ok=False, summary=f"failed to launch: {e}")
        return 1, False, [str(e)]

    timer = None
    if timeout:
        def _kill():
            timed_out_flag["flag"] = True
            proc.kill()
        timer = threading.Timer(timeout, _kill)
        timer.daemon = True
        timer.start()

    try:
        for line in iter(proc.stdout.readline, ""):
            full_output.append(line.rstrip("\n"))
            panel.push(line)
    finally:
        proc.stdout.close()
        proc.wait()
        if timer:
            timer.cancel()

    if timed_out_flag["flag"]:
        panel.finish(ok=False, summary=f"timed out after {timeout}s")
    else:
        panel.finish(ok=(proc.returncode == 0),
                      summary="done" if proc.returncode == 0 else f"exit code {proc.returncode}")

    return proc.returncode, timed_out_flag["flag"], full_output


def dump_tail(lines: list, n: int = 30, indent: str = "    "):
    """Print the last `n` lines of a captured full_output list - used on
    failure, so the rolling window not showing full history doesn't cost
    you the ability to debug what actually went wrong."""
    tail = lines[-n:] if len(lines) > n else lines
    if len(lines) > n:
        print(_c(f"{indent}... ({len(lines) - n} earlier line(s) omitted) ...", C.DIM))
    for line in tail:
        print(_c(f"{indent}{line}", C.DIM))