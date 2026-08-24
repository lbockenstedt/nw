"""Minimal, dependency-free VT100/ANSI terminal screen emulator.

Screen-oriented network CLIs (ArubaOS-Switch full-screen mode, and others)
don't just stream text — they drive the session like a real terminal: they
position the cursor with CUP (``ESC[row;colH``), clear regions (ED/EL), set
scroll margins (DECSTBM), and — critically — *probe* the terminal by issuing
device queries that BLOCK the device until the terminal answers:

  * DSR  — Device Status Report, ``ESC[6n`` — "where is the cursor?" The device
           parks the cursor off-screen (``ESC[1920;1920H``) then asks, using the
           clamped answer to auto-detect the window size. Unanswered, the switch
           sits mute after the login banner and the prompt never renders.
  * DA   — Primary Device Attributes, ``ESC[c`` — "what terminal are you?"

A dumb byte reader can't answer these, and answering with a *hard-coded*
position (the old ``ESC[24;80R`` constant) only works by luck when the device
happens to park at the bottom-right. This emulator answers **generically**: it
tracks the real cursor as it processes the stream, so a DSR is replied with the
*actual* cursor position, whatever the device parked it at — no per-device
hard-coding.

Scope: only the control subset a network CLI actually uses is implemented;
anything else is parsed and ignored (never emitted as literal bytes). It keeps a
``rows x cols`` grid plus scrollback so ``text()`` renders bulk ``show`` output
that scrolled off the top, but the nw transport uses it primarily for its
generic query replies (``pending_replies``); the captured command output stays
the raw device bytes so existing parsers see exactly what the device sent.
"""
from __future__ import annotations

from typing import List

# Primary DA response: identify as a plain VT100 with no options. Enough to
# satisfy a device that gates on "is there really a terminal here?".
DA_REPLY = "\x1b[?1;0c"


class TerminalScreen:
    """A tiny VT100 screen: a character grid + cursor + scrollback, fed a byte
    stream via :meth:`feed`. Answers terminal queries generically — collect the
    host-bound replies with :meth:`pending_replies`."""

    def __init__(self, cols: int = 80, rows: int = 24):
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self.reset()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def reset(self) -> None:
        self._grid = [[" "] * self.cols for _ in range(self.rows)]
        self.cx = 0
        self.cy = 0
        self._top = 0                 # scroll region (inclusive), rows
        self._bottom = self.rows - 1
        self._scrollback: List[str] = []
        self._replies: List[str] = []
        self._pending = ""            # unparsed trailing bytes (split escape)

    # ── feeding the stream ───────────────────────────────────────────────────
    def feed(self, data: str) -> None:
        """Process a chunk of terminal output, updating the grid/cursor and
        queuing any query replies. Handles control sequences split across chunk
        boundaries (an incomplete trailing escape is buffered until completed)."""
        s = self._pending + data
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == "\x1b":
                consumed = self._parse_escape(s, i)
                if consumed == 0:          # incomplete — wait for more bytes
                    break
                i += consumed
            elif c == "\r":
                self.cx = 0
                i += 1
            elif c == "\n" or c == "\x0b" or c == "\x0c":  # LF / VT / FF
                self._linefeed()
                i += 1
            elif c == "\b":
                self.cx = max(0, self.cx - 1)
                i += 1
            elif c == "\t":
                self.cx = min(self.cols - 1, ((self.cx // 8) + 1) * 8)
                i += 1
            elif c == "\x07":              # BEL — no visual effect
                i += 1
            elif c < " ":                  # other C0 control — ignore
                i += 1
            else:
                # Fast-path a run of printable characters.
                j = i
                while j < n:
                    cj = s[j]
                    if cj < " " or cj == "\x7f" or cj == "\x1b":
                        break
                    j += 1
                self._write(s[i:j])
                i = j
        self._pending = s[i:]

    # ── query replies ────────────────────────────────────────────────────────
    def pending_replies(self) -> str:
        """Return (and clear) the bytes the emulator owes the host in answer to
        terminal queries (DSR cursor-position report, DA, …)."""
        if not self._replies:
            return ""
        out = "".join(self._replies)
        self._replies = []
        return out

    def cursor_report(self) -> str:
        """The CPR (Cursor Position Report) for the current cursor — 1-based
        ``ESC[row;colR`` — as a device would receive in answer to ``ESC[6n``."""
        return f"\x1b[{self.cy + 1};{self.cx + 1}R"

    # ── rendered views ───────────────────────────────────────────────────────
    def text(self) -> str:
        """Rendered visible text: scrollback + on-screen rows, each right-
        stripped, with trailing blank lines removed."""
        lines = list(self._scrollback)
        lines += ["".join(row).rstrip() for row in self._grid]
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    def cursor_line(self) -> str:
        """The on-screen row the cursor currently sits on, right-stripped."""
        return "".join(self._grid[self.cy]).rstrip()

    def last_nonempty_line(self) -> str:
        for row in reversed(self._grid):
            s = "".join(row).rstrip()
            if s:
                return s
        for line in reversed(self._scrollback):
            if line.strip():
                return line.rstrip()
        return ""

    # ── internals ────────────────────────────────────────────────────────────
    def _write(self, text: str) -> None:
        for ch in text:
            if self.cx >= self.cols:       # autowrap
                self.cx = 0
                self._linefeed()
            self._grid[self.cy][self.cx] = ch
            self.cx += 1

    def _linefeed(self) -> None:
        if self.cy == self._bottom:
            self._scroll_up()
        elif self.cy < self.rows - 1:
            self.cy += 1

    def _scroll_up(self) -> None:
        # The line leaving the top of the scroll region: if the region starts at
        # the top of the screen, it scrolls into scrollback (bulk `show` output);
        # a sub-region scroll just discards it (banner/pager redraw area).
        leaving = self._grid[self._top]
        if self._top == 0:
            self._scrollback.append("".join(leaving).rstrip())
        del self._grid[self._top]
        self._grid.insert(self._bottom, [" "] * self.cols)

    def _parse_escape(self, s: str, i: int) -> int:
        """Parse the escape sequence at ``s[i]`` (``s[i] == ESC``). Returns the
        number of characters consumed, or 0 if the sequence is incomplete (needs
        more bytes)."""
        n = len(s)
        if i + 1 >= n:
            return 0
        c1 = s[i + 1]
        if c1 == "[":
            return self._parse_csi(s, i)
        if c1 == "]":                      # OSC — consume up to BEL or ST
            return self._parse_osc(s, i)
        if c1 in "()*+":                   # charset designation: ESC ( <char>
            if i + 2 >= n:
                return 0
            return 3
        if c1 == "c":                      # RIS — full reset
            self.reset()
            return 2
        # Other 2-byte escapes (keypad ESC=/ESC>, index ESC D/M/E, …) — ignore.
        return 2

    def _parse_csi(self, s: str, i: int) -> int:
        n = len(s)
        k = i + 2
        priv = ""
        if k < n and s[k] in "<=>?":
            priv = s[k]
            k += 1
        pstart = k
        while k < n and (s[k].isdigit() or s[k] in ";:"):
            k += 1
        while k < n and " " <= s[k] <= "/":   # intermediate bytes
            k += 1
        if k >= n:
            return 0                        # incomplete
        final = s[k]
        if "@" <= final <= "~":
            self._dispatch_csi(priv, s[pstart:k].split(";"), final)
            return (k - i) + 1
        # Malformed — swallow ESC[ so it can't leak as literal text.
        return 2

    def _parse_osc(self, s: str, i: int) -> int:
        n = len(s)
        k = i + 2
        while k < n:
            if s[k] == "\x07":              # BEL terminator
                return (k - i) + 1
            if s[k] == "\x1b" and k + 1 < n and s[k + 1] == "\\":  # ST
                return (k - i) + 2
            if s[k] == "\x1b" and k + 1 >= n:
                return 0                    # possible split ST — wait
            k += 1
        return 0                            # unterminated — wait for more

    def _dispatch_csi(self, priv: str, params: List[str], final: str) -> None:
        def p(idx: int, default: int = 0) -> int:
            if idx < len(params) and params[idx] not in ("", None):
                try:
                    return int(params[idx])
                except ValueError:
                    return default
            return default

        # Private-mode sequences (DECSET/DECRST etc.) don't affect the grid.
        if priv == "?":
            return
        if final in ("H", "f"):             # CUP — cursor position (1-based)
            self.cy = _clamp(p(0, 1) - 1, 0, self.rows - 1)
            self.cx = _clamp(p(1, 1) - 1, 0, self.cols - 1)
        elif final == "A":                  # CUU
            self.cy = max(0, self.cy - max(1, p(0, 1)))
        elif final == "B":                  # CUD
            self.cy = min(self.rows - 1, self.cy + max(1, p(0, 1)))
        elif final == "C":                  # CUF
            self.cx = min(self.cols - 1, self.cx + max(1, p(0, 1)))
        elif final == "D":                  # CUB
            self.cx = max(0, self.cx - max(1, p(0, 1)))
        elif final in ("G", "`"):           # CHA — column
            self.cx = _clamp(p(0, 1) - 1, 0, self.cols - 1)
        elif final == "d":                  # VPA — row
            self.cy = _clamp(p(0, 1) - 1, 0, self.rows - 1)
        elif final == "J":                  # ED — erase in display
            self._erase_display(p(0, 0))
        elif final == "K":                  # EL — erase in line
            self._erase_line(p(0, 0))
        elif final == "r":                  # DECSTBM — scroll region
            top = _clamp(p(0, 1) - 1, 0, self.rows - 1)
            bottom = _clamp(p(1, self.rows) - 1, 0, self.rows - 1)
            if top < bottom:
                self._top, self._bottom = top, bottom
                self.cx = self.cy = 0
        elif final == "n":                  # DSR — device status report
            if p(0, 0) == 6:
                self._replies.append(self.cursor_report())
            elif p(0, 0) == 5:
                self._replies.append("\x1b[0n")   # "OK"
        elif final == "c":                  # DA — device attributes request
            self._replies.append(DA_REPLY)
        # SGR ('m'), mode set/reset ('h'/'l'), insert/delete, etc.: no-op for
        # our text-capture purposes — parsed and dropped, never printed.

    def _erase_display(self, mode: int) -> None:
        if mode == 0:                       # cursor → end of screen
            self._blank_line_from(self.cy, self.cx)
            for y in range(self.cy + 1, self.rows):
                self._grid[y] = [" "] * self.cols
        elif mode == 1:                     # start of screen → cursor
            for y in range(0, self.cy):
                self._grid[y] = [" "] * self.cols
            self._blank_line_to(self.cy, self.cx)
        else:                               # 2/3 — whole screen
            self._grid = [[" "] * self.cols for _ in range(self.rows)]

    def _erase_line(self, mode: int) -> None:
        if mode == 0:
            self._blank_line_from(self.cy, self.cx)
        elif mode == 1:
            self._blank_line_to(self.cy, self.cx)
        else:
            self._grid[self.cy] = [" "] * self.cols

    def _blank_line_from(self, y: int, x: int) -> None:
        for xx in range(x, self.cols):
            self._grid[y][xx] = " "

    def _blank_line_to(self, y: int, x: int) -> None:
        for xx in range(0, min(x + 1, self.cols)):
            self._grid[y][xx] = " "


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v
