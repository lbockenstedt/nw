"""Tests for the minimal VT100 screen emulator (transports/vtscreen.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from transports.vtscreen import TerminalScreen, DA_REPLY  # noqa: E402


def test_plain_text_renders_and_tracks_cursor():
    s = TerminalScreen()
    s.feed("hello")
    assert s.cursor_line() == "hello"
    assert (s.cx, s.cy) == (5, 0)


def test_crlf_advances_row_and_resets_column():
    s = TerminalScreen()
    s.feed("l1\r\nl2")
    assert s.text() == "l1\nl2"
    assert s.cy == 1 and s.cx == 2


def test_cup_sets_absolute_position_1_based():
    s = TerminalScreen()
    s.feed("\x1b[5;10H")
    assert (s.cy, s.cx) == (4, 9)


def test_dsr_reply_reflects_actual_cursor_not_hardcoded():
    # The whole point of the emulator: a DSR is answered with the REAL cursor
    # position, wherever the device parked it — not a fixed constant.
    s = TerminalScreen()
    s.feed("\x1b[10;20H\x1b[6n")
    assert s.pending_replies() == "\x1b[10;20R"


def test_dsr_reply_clamps_offscreen_probe_to_screen_bounds():
    # The AOS-S auto-size probe parks far off-screen (1920;1920) then asks; the
    # reply is clamped to the 24x80 screen — the size the device learns.
    s = TerminalScreen(cols=80, rows=24)
    s.feed("\x1b[1920;1920H\x1b[6n")
    assert s.pending_replies() == "\x1b[24;80R"


def test_dsr_status_report_ok():
    s = TerminalScreen()
    s.feed("\x1b[5n")
    assert s.pending_replies() == "\x1b[0n"


def test_primary_da_request_answered():
    s = TerminalScreen()
    s.feed("\x1b[c")
    assert s.pending_replies() == DA_REPLY


def test_query_split_across_feeds_is_buffered_then_answered():
    s = TerminalScreen()
    s.feed("\x1b[1920;1920H\x1b")   # trailing incomplete escape
    assert s.pending_replies() == ""
    s.feed("[6n")                    # completes the DSR
    assert s.pending_replies() == "\x1b[24;80R"


def test_erase_display_clears_screen():
    s = TerminalScreen()
    s.feed("junk\r\nmore junk")
    s.feed("\x1b[2J\x1b[1;1HCLEAN")
    assert s.text() == "CLEAN"


def test_cursor_positioned_prompt_renders_on_its_line():
    # A switch that clears the screen and cursor-positions its prompt: the
    # prompt is rendered on the cursor's row, not buried after control bytes.
    s = TerminalScreen()
    s.feed("\x1b[2J\x1b[24;1HDIST-SW# ")
    assert s.cursor_line() == "DIST-SW#"
    assert s.last_nonempty_line() == "DIST-SW#"


def test_scrollback_preserves_lines_beyond_screen_height():
    s = TerminalScreen(cols=80, rows=4)
    lines = [f"line{i}" for i in range(10)]
    s.feed("\r\n".join(lines))
    rendered = s.text().splitlines()
    # All 10 lines survive (6 in scrollback + 4 on screen).
    assert rendered == lines


def test_sgr_and_unknown_sequences_dropped_not_printed():
    s = TerminalScreen()
    s.feed("\x1b[1;32mgreen\x1b[0m text")
    assert s.cursor_line() == "green text"


def test_backspace_and_tab():
    s = TerminalScreen()
    s.feed("abc\b\bX")
    assert s.cursor_line() == "aXc"
    s2 = TerminalScreen()
    s2.feed("a\tb")
    assert s2.cx == 9  # tab to next 8-col stop, then 'b'
