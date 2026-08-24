"""Tests for the CLI transport: pure per-vendor parsers exercised with canned
show-command text, plus CliSession config validation. No asyncssh needed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402
from transports import cli_io  # noqa: E402


# ── AOS-S `show arp` ──────────────────────────────────────────────────────────
def test_parse_arp_aos_s():
    text = """
  IP Address  MAC Address      Type    Port
  10.0.0.5    aa-bb-cc-dd-ee-ff dynamic 1
  10.0.0.6    001122334455     dynamic Trk2
"""
    rows = cli_io.parse_arp_aos_s(text)
    assert {"ip": "10.0.0.5", "mac": "aa-bb-cc-dd-ee-ff", "interface": "1"} in rows
    assert {"ip": "10.0.0.6", "mac": "001122334455", "interface": "Trk2"} in rows


# ── AOS-S `show mac-address` ──────────────────────────────────────────────────
def test_parse_mac_aos_s():
    text = """
  MAC Address   VLAN    Port
  aabbccddeeff  10      1
  001122334455  20      Trk2
"""
    rows = cli_io.parse_mac_aos_s(text)
    assert {"mac": "aabbccddeeff", "vlan": "10", "interface": "1"} in rows
    assert {"mac": "001122334455", "vlan": "20", "interface": "Trk2"} in rows


# ── AOS-S `show interfaces brief` ─────────────────────────────────────────────
def test_parse_interfaces_aos_s():
    text = """
  Port    Admin Status  Physical Status  Speed/Type
  1       Enabled       Up               1000
  2       Enabled       Down             100
"""
    rows = cli_io.parse_interfaces_aos_s(text)
    byname = {r["name"]: r for r in rows}
    assert byname["1"]["status"] == "up"
    assert byname["2"]["status"] == "down"


# ── Junos `show arp` + `show interfaces descriptions` ─────────────────────────
def test_parse_arp_junos():
    text = """
00:11:22:33:44:55  10.0.0.7  ge-0/0/1
aa:bb:cc:dd:ee:ff  10.0.0.8  ge-0/0/2
"""
    rows = cli_io.parse_arp_junos(text)
    assert {"ip": "10.0.0.7", "mac": "00:11:22:33:44:55",
            "interface": "ge-0/0/1"} in rows


def test_parse_interfaces_junos():
    text = """
Interface   Admin  Link
ge-0/0/0    up     up
ge-0/0/1    up     down
"""
    rows = cli_io.parse_interfaces_junos(text)
    byname = {r["name"]: r for r in rows}
    assert byname["ge-0/0/0"]["status"] == "up"
    assert byname["ge-0/0/1"]["status"] == "down"


# ── PARSERS registry triple ───────────────────────────────────────────────────
def test_parsers_registry_has_all_families():
    for ot in ("aos_switch", "cx_switch", "ex_switch", "gateway"):
        assert ot in cli_io.PARSERS
        arp, mac, ifc = cli_io.PARSERS[ot]
        assert all(callable(f) for f in (arp, mac, ifc))


# ── CliSession requires address + username ────────────────────────────────────
def test_cli_session_requires_username():
    try:
        cli_io.CliSession({"id": "d1", "address": "10.0.0.1"})
        assert False, "expected CliError"
    except cli_io.CliError as e:
        assert "username" in str(e)


def test_cli_session_requires_address():
    try:
        cli_io.CliSession({"id": "d1", "username": "admin"})
        assert False, "expected CliError"
    except cli_io.CliError as e:
        assert "address" in str(e)


# ── best-effort info-token extractors ─────────────────────────────────────────
def test_serial_and_firmware_extractors():
    text = "Aruba JL658A Serial: SG123ABCD Firmware Version 16.02.0023"
    assert cli_io._serial_from(text) == "SG123ABCD"
    assert "16.02.0023" == cli_io._firmware_from(text)


def test_hostname_from_text_system_name():
    # AOS-S `show system-information` prints a 'System Name' field.
    text = "System Information\n  System Name : DIST-SW\n  System Contact :"
    assert cli_io._hostname_from_text(text) == "DIST-SW"
    # 'Hostname'/'Host Name' spellings also match.
    assert cli_io._hostname_from_text("Hostname: core-sw01") == "core-sw01"
    # No such line → empty (caller falls back to the prompt token).
    assert cli_io._hostname_from_text("Model: 6300M\nVersion 10.09") == ""


def test_base_mac_from_extractors():
    # AOS-S `show system-information` prints 'Base MAC Addr' in Aruba dash form.
    aos = "  Software revision  : WC.16.10\n  Base MAC Addr      : 3863bb-a1b2c3\n"
    assert cli_io._base_mac_from(aos) == "38:63:bb:a1:b2:c3"
    # AOS-CX / gateway spellings + colon form also normalize.
    assert cli_io._base_mac_from("System MAC : 38:63:bb:a1:b2:c3") == "38:63:bb:a1:b2:c3"
    assert cli_io._base_mac_from("MAC Address: 3863.bba1.b2c3") == "38:63:bb:a1:b2:c3"
    # No such line → empty.
    assert cli_io._base_mac_from("System Name : DIST-SW") == ""


def test_hostname_from_prompt_token():
    assert cli_io._hostname_from_prompt("DIST-SW# ") == "DIST-SW"
    assert cli_io._hostname_from_prompt("DIST-SW> ") == "DIST-SW"
    # Config-mode suffix is stripped.
    assert cli_io._hostname_from_prompt("DIST-SW(config)# ") == "DIST-SW"
    assert cli_io._hostname_from_prompt("DIST-SW(config-if)# ") == "DIST-SW"
    # ANSI-decorated prompt + multi-line: last clean line wins.
    assert cli_io._hostname_from_prompt("\x1b[1;1Hbanner line\nCORE-1# ") == "CORE-1"
    # Whitespace/banner noise (no clean single token) → empty.
    assert cli_io._hostname_from_prompt("Press any key to continue") == ""
    assert cli_io._hostname_from_prompt("") == ""


# ── _read_until_prompt: linear tail-window reader (FIX B) ─────────────────────
import asyncio  # noqa: E402
import time  # noqa: E402


class _FakeStdout:
    def __init__(self, chunks, eof=False):
        self._chunks = list(chunks)
        self._eof = eof

    async def read(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        if self._eof:
            return ""  # EOF
        raise RuntimeError("read past end of scripted output")


class _FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, s):
        self.writes.append(s)


class _FakeProc:
    def __init__(self, chunks, eof=False):
        self.stdout = _FakeStdout(chunks, eof=eof)
        self.stdin = _FakeStdin()


def _reader_session(chunks, eof=False):
    s = cli_io.CliSession({"address": "10.0.0.1", "username": "admin"})
    s._proc = _FakeProc(chunks, eof=eof)
    return s


def _chunked(text, size=4096):
    return [text[i:i + size] for i in range(0, len(text), size)]


def _read(s, timeout=5.0):
    return asyncio.get_event_loop().run_until_complete(
        s._read_until_prompt(timeout=timeout))


def test_read_large_output_intact_and_fast():
    # A multi-thousand-line MAC-table-sized response fed in 4KB chunks must come
    # back byte-identical (prompt detected on the last chunk) in linear time.
    lines = [f"  10.0.0.{i % 250}    aabbcc-dd{i:04x}  dynamic  {i % 48}"
             for i in range(5000)]
    text = "\n".join(lines) + "\nswitch# "
    s = _reader_session(_chunked(text))
    t0 = time.monotonic()
    res = _read(s)
    assert time.monotonic() - t0 < 2.0
    assert res == text


def test_read_pager_marker_split_across_chunk_boundary():
    # The overlap window must catch a "--  More  --" split across two chunks:
    # the pager gets advanced (space written) and the marker is stripped.
    part1 = "line one\nline two --  Mo"
    part2 = "re  --\nline three\nswitch# "
    s = _reader_session([part1, part2])
    res = _read(s)
    assert "More" not in res
    assert "line one\n" in res and "line three\n" in res
    assert res.endswith("switch# ")
    assert " " in s._proc.stdin.writes


def test_read_answers_aos_s_press_any_key_gate():
    # AOS-S (JL-series 2540/2930) prints a copyright legend ending in "Press any
    # key to continue" and BLOCKS until a key is sent; only then does the CLI
    # prompt render. _read_until_prompt must answer the gate (write a key), strip
    # the marker, and go on to detect the prompt — otherwise the device stalls
    # the full timeout and shows "unreachable" even though it is fully reachable.
    banner = ("Aruba JL354A 2540-24G-4SFP+ Switch\n"
              "  (C) Copyright 2026 Hewlett Packard Enterprise\n\n"
              "Press any key to continue")
    prompt = "\nDIST-SW# "
    s = _reader_session([banner, prompt])
    res = _read(s)
    assert " " in s._proc.stdin.writes             # gate answered with a key
    assert "Press any key to continue" not in res  # marker stripped from output
    assert res.endswith("DIST-SW# ")               # prompt reached after the gate


def test_read_answers_press_any_key_split_across_chunk_boundary():
    # The overlap window must also catch the continue gate split across chunks.
    part1 = "legend line\nPress any key to co"
    part2 = "ntinue\nDIST-SW# "
    s = _reader_session([part1, part2])
    res = _read(s)
    assert " " in s._proc.stdin.writes
    assert "Press any key" not in res
    assert res.endswith("DIST-SW# ")


def test_read_answers_dsr_cursor_position_request():
    # A full-screen AOS-S CLI auto-detects terminal size by parking the cursor
    # off-screen and issuing a DSR (ESC[6n). It BLOCKS on the reply before it
    # renders the CLI prompt; _read_until_prompt must answer with a cursor-
    # position report so the switch proceeds — otherwise it stalls the whole
    # timeout after the banner and shows "unreachable" despite a good login.
    banner = ("Your previous successful login (as manager) was on ...\n"
              " from 172.16.1.9\r\n\x1b[1920;1920H\x1b[6n")
    prompt = "\x1b[24;1HDIST-SW# "
    s = _reader_session([banner, prompt])
    res = _read(s)
    assert cli_io._DSR_REPLY in s._proc.stdin.writes  # answered the DSR query
    assert "\x1b[6n" not in res                        # DSR marker stripped
    assert res.rstrip().endswith("DIST-SW#")           # prompt reached


def test_read_prompt_detected_through_ansi_cursor_positioning():
    # A switch that cursor-positions its prompt (ESC[24;1H before "DIST-SW# ")
    # must still be detected — the prompt check ANSI-strips the candidate line.
    s = _reader_session(["\x1b[2J\x1b[24;1HDIST-SW# "])
    res = _read(s)
    assert res.endswith("DIST-SW# ")


def test_read_answers_dsr_split_across_chunk_boundary():
    # ESC[6n split across a chunk boundary is still caught by the overlap window.
    part1 = "banner text\r\n\x1b[1920;1920H\x1b"
    part2 = "[6nmore\n\x1b[24;1HDIST-SW# "
    s = _reader_session([part1, part2])
    res = _read(s)
    assert cli_io._DSR_REPLY in s._proc.stdin.writes
    assert "\x1b[6n" not in res
    assert res.rstrip().endswith("DIST-SW#")


def test_read_dsr_reply_is_generic_actual_cursor_position():
    # The reply is computed from the REAL cursor the device parked at, not a
    # hard-coded constant: a device that parks at row 10 col 20 before the DSR
    # gets ESC[10;20R (not ESC[24;80R). Proves the emulator-driven generalization.
    banner = "prep\r\n\x1b[10;20H\x1b[6n"
    prompt = "\x1b[24;1HSW# "
    s = _reader_session([banner, prompt])
    res = _read(s)
    assert "\x1b[10;20R" in s._proc.stdin.writes
    assert "\x1b[6n" not in res
    assert res.rstrip().endswith("SW#")


def test_read_answers_primary_da_query():
    # A device that asks "what terminal are you?" (Primary DA, ESC[c) and blocks
    # is answered generically with a terminal id; the query is stripped from the
    # captured output.
    banner = "hi\r\n\x1b[c"
    prompt = "\nSW# "
    s = _reader_session([banner, prompt])
    res = _read(s)
    from transports.vtscreen import DA_REPLY
    assert DA_REPLY in s._proc.stdin.writes
    assert "\x1b[c" not in res
    assert res.rstrip().endswith("SW#")


def test_read_prompt_on_complete_final_line():
    # A prompt followed by a newline (complete last line) must still terminate
    # the read immediately — the old splitlines()[-1] semantics.
    text = "some output\nswitch#\n"
    s = _reader_session(_chunked(text))
    res = _read(s)
    assert res == text


def test_read_crlf_output_intact():
    text = "l1\r\nl2\r\nswitch# "
    s = _reader_session(_chunked(text))
    res = _read(s)
    assert res == text


def test_read_eof_drops_trailing_partial_line():
    # EOF without a prompt: complete lines are returned, the dangling partial
    # line is dropped (unchanged legacy behavior).
    s = _reader_session(["a\nb\npartial"], eof=True)
    res = _read(s)
    assert res == "a\nb\n"


class _ClosedStdin:
    def write(self, s):
        # asyncssh raises exactly this once the peer closed the channel.
        raise BrokenPipeError("Channel not open for sending")


def test_send_on_closed_channel_raises_actionable_clierror():
    # A device that tears the session down right after login makes the first
    # command write fail with a bare BrokenPipeError. _send must translate it
    # into an operator-actionable CliError, not leak the opaque asyncssh text.
    s = cli_io.CliSession({"address": "10.0.0.1", "username": "admin"})
    s._proc = _FakeProc([])
    s._proc.stdin = _ClosedStdin()
    with pytest.raises(cli_io.CliError) as ei:
        asyncio.get_event_loop().run_until_complete(s._send("show version"))
    msg = str(ei.value)
    assert "CLI/exec privilege" in msg
    assert "Channel not open for sending" in msg

def test_session_closed_error_reports_session_pool_full():
    # When the AOS-S banner carries the "maximum number of sessions are active"
    # disconnect text, the enriched error must call that out specifically (so
    # the poll error tells the operator to free/raise sessions, not chase
    # credentials) and include a tail of what the device sent.
    s = cli_io.CliSession({"address": "10.0.0.1", "username": "admin"})
    s._banner = ("Sorry, the maximum number of sessions are active.  "
                 "Try again later.")
    msg = s._session_closed_error()
    assert "session pool is full" in msg
    assert "maximum number of sessions are active" in msg
    assert "device sent before closing" in msg


def test_session_closed_error_generic_without_banner():
    s = cli_io.CliSession({"address": "10.0.0.1", "username": "admin"})
    s._banner = ""
    msg = s._session_closed_error()
    assert "CLI/exec privilege" in msg
    assert "device sent before closing" not in msg


class _FakeConn:
    def __init__(self, proc):
        self._proc = proc
        self.closed = False

    async def create_process(self, **kw):
        return self._proc

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def test_connect_failure_does_not_leak_connection():
    # THE session-exhaustion bug: connect() runs inside __aenter__, so if it
    # raises after the SSH connection is established, close() would never fire
    # and the device's vty slot would leak. Verify connect() tears the
    # connection down itself before propagating.
    proc = _FakeProc([""], eof=True)  # banner read hits EOF → channel closed
    proc.exit_status = 0              # _channel_closed() → True
    conn = _FakeConn(proc)

    async def _fake_connect(*a, **k):
        return conn

    import types
    stub = types.ModuleType("asyncssh")
    stub.connect = _fake_connect
    prev = sys.modules.get("asyncssh")
    sys.modules["asyncssh"] = stub  # connect() does a runtime `import asyncssh`
    try:
        s = cli_io.CliSession({"address": "10.0.0.1", "username": "admin"})
        with pytest.raises(cli_io.CliError):
            asyncio.get_event_loop().run_until_complete(s.connect())
    finally:
        if prev is not None:
            sys.modules["asyncssh"] = prev
        else:
            del sys.modules["asyncssh"]
    assert conn.closed is True, "connection leaked: close() not called on failure"


def test_connect_nudges_with_cr_before_banner_read():
    """AOS-S renders its CLI prompt only after an Enter (it prints a copyright
    legend first), so connect() must send a bare CR before the prompt-anchored
    banner read — otherwise the read stalls the full timeout and the 3s fleet
    reachability probe marks the device unreachable. Verify the nudge is the
    first thing written, and that a device presenting a prompt then connects."""
    proc = _FakeProc(["switch# "])  # prompt present after the nudge
    conn = _FakeConn(proc)

    async def _fake_connect(*a, **k):
        return conn

    import types
    stub = types.ModuleType("asyncssh")
    stub.connect = _fake_connect
    prev = sys.modules.get("asyncssh")
    sys.modules["asyncssh"] = stub
    try:
        s = cli_io.CliSession({"address": "10.0.0.1", "username": "admin"})
        asyncio.get_event_loop().run_until_complete(s.connect())
    finally:
        if prev is not None:
            sys.modules["asyncssh"] = prev
        else:
            del sys.modules["asyncssh"]
    assert proc.stdin.writes and proc.stdin.writes[0] == "\r", \
        "connect() must send a CR nudge before reading the banner"


class _MuteUntilNudgedStdout:
    """Stays silent (raises the same TimeoutError an idle read raises) until the
    session has written >= ``need`` CRs, then yields the banner+prompt once.
    Models an AOS-S switch that renders its prompt only after enough Enters."""
    def __init__(self, stdin, need, banner):
        self._stdin = stdin
        self._need = need
        self._banner = banner
        self._sent = False

    async def read(self, n):
        if not self._sent and len(self._stdin.writes) >= self._need:
            self._sent = True
            return self._banner
        if self._sent:
            return ""  # EOF after the banner
        raise asyncio.TimeoutError  # simulate an idle (no-data) read second


def test_connect_renudges_until_mute_switch_answers():
    """A switch that swallows the first CR must still connect: connect() must
    re-nudge on each idle second until the banner/prompt finally arrives.
    Verify more than the single initial CR was sent and the prompt was reached."""
    proc = _FakeProc([])
    # Switch responds only after the initial nudge + at least 2 re-nudges.
    proc.stdout = _MuteUntilNudgedStdout(proc.stdin, need=3, banner="switch# ")
    conn = _FakeConn(proc)

    async def _fake_connect(*a, **k):
        return conn

    import types
    stub = types.ModuleType("asyncssh")
    stub.connect = _fake_connect
    prev = sys.modules.get("asyncssh")
    sys.modules["asyncssh"] = stub
    try:
        s = cli_io.CliSession({"address": "10.0.0.1", "username": "admin"})
        asyncio.get_event_loop().run_until_complete(s.connect())
    finally:
        if prev is not None:
            sys.modules["asyncssh"] = prev
        else:
            del sys.modules["asyncssh"]
    assert len(proc.stdin.writes) >= 3, \
        "connect() must re-nudge a mute switch, not rely on a single CR"
    assert "switch#" in s._banner


def test_read_renudge_stops_once_output_arrives():
    """on_idle must only fire BEFORE the first byte — once output flows it must
    not keep nudging (extra CRs would pollute the session)."""
    calls = {"n": 0}

    def _on_idle():
        calls["n"] += 1

    # First read yields the prompt immediately → no idle second before output.
    s = _reader_session(["switch# "])
    asyncio.get_event_loop().run_until_complete(
        s._read_until_prompt(on_idle=_on_idle))
    assert calls["n"] == 0

