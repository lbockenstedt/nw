"""SSH / CLI transport for the nw drivers (asyncssh).

Lazy-imports asyncssh so the module imports without it installed. One
interactive PTY session per device: connect, disable paging, enter enable mode
where the vendor requires it, then run the vendor ``show`` commands and parse
the text output into the API row shapes.

The per-vendor parsers are pure functions (no asyncssh, no device) so they're
unit-tested directly with canned CLI text. The session mechanics are
best-effort real IO — a parse failure on a line skips that line, never the
whole poll.
"""
import asyncio
import logging
import os
import re
from typing import Any, Dict, List

logger = logging.getLogger("NwCli")


class CliError(Exception):
    """Raised on SSH connection/auth failures or command timeouts."""


# Per-family paging-disable command + prompt regex.
_PAGING_CMD = {
    "aos_switch": "no page",
    "ex_switch":  "set cli screen-length 0",
    "cx_switch":  "no page",
    # ArubaOS gateway/controller: `no page` disables the pager (confirmed on the
    # live device). If paging is ever still on, the `--More--` fallback in
    # _read_until_prompt advances + strips it so output isn't truncated.
    "gateway":    "no page",
}
# Pager continuation marker (belt-and-suspenders if paging is still on).
_PAGER_RE = re.compile(r"--\s*More\s*--", re.IGNORECASE)
# AOS-S (ArubaOS-Switch) prints a "Press any key to continue" gate after the
# login banner, BEFORE the CLI prompt. If the client never answers it the
# session stalls at the banner until the switch closes it — which surfaces
# downstream as an opaque "Channel not open for sending" on the first command.
# Detect it and answer with a CR so the session reaches the real prompt.
_CONTINUE_RE = re.compile(r"press any key to continue", re.IGNORECASE)
# Prompt ends in '#' (enabled) or '>' (user) for AOS-S/gateway; Junos ends in
# '>' or '#'. Match a line ending in a prompt char after optional whitespace.
_PROMPT_RE = re.compile(r"[>#]\s*$")
# Tail windows so per-chunk detection cost is O(chunk), never O(total output):
# pager markers are short ("--  More  --"-ish), so 64 chars of overlap catches
# one split across a chunk boundary; prompt lines are short, so 512 chars is
# ample to isolate the text after the last newline (the prompt regex is
# end-anchored, so a bounded tail gives identical results).
_PAGER_OVERLAP = 64
_PROMPT_TAIL = 512


class CliSession:
    """One interactive SSH session against a device. Use as an async context
    manager, or call connect()/close() directly."""

    def __init__(self, device: Dict[str, Any], command_timeout: float = 15.0):
        d = device or {}
        self.host = str(d.get("address") or "").strip()
        self.port = int(d.get("port") or 22)
        self.username = str(d.get("username") or "").strip()
        self.password = str(d.get("password") or "")
        self.enable_secret = str(d.get("enable_secret") or "")
        self.object_type = str(d.get("object_type") or "").strip()
        if not self.host or not self.username:
            raise CliError("device address/username not configured")
        self.command_timeout = command_timeout
        self._conn = None
        self._proc = None
        self._banner = ""      # login banner / first output (captured for debug)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    _SESSION_CLOSED_HINT = (
        "device closed the SSH session right after login — the account may "
        "lack CLI/exec privilege, the device may require different "
        "credentials, or it may not grant an interactive PTY")

    def _channel_closed(self) -> bool:
        """True if the device has already torn the interactive channel down
        (e.g. an exit status arrived during the banner read). Best-effort:
        any attribute error means we can't tell, so assume still open."""
        proc = self._proc
        if proc is None:
            return True
        try:
            if proc.exit_status is not None or proc.exit_signal is not None:
                return True
        except Exception:
            pass
        try:
            chan = proc.channel
            if chan is not None and chan.is_closing():
                return True
        except Exception:
            pass
        return False

    async def connect(self) -> None:
        import asyncssh
        # Opt-in wire-level debug: set LM_NW_SSH_DEBUG=1 (or 2 for packet dumps)
        # on the spoke to turn on asyncssh's own protocol logging, so an auth /
        # channel-open / channel-close failure is visible in the spoke log.
        _dbg = os.environ.get("LM_NW_SSH_DEBUG", "").strip()
        if _dbg:
            try:
                logging.getLogger("asyncssh").setLevel(logging.DEBUG)
                asyncssh.set_log_level(logging.DEBUG)
                asyncssh.set_debug_level(2 if _dbg == "2" else 1)
            except Exception:  # noqa: BLE001 — best-effort debug toggle
                pass
        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(self.host, port=self.port, username=self.username,
                                 password=self.password, known_hosts=None,
                                 login_timeout=10),
                timeout=15.0)
        except Exception as e:
            raise CliError(f"SSH connect to {self.host}: {e}")
        self._proc = await self._conn.create_process(term_type="vt100",
                                                      encoding="utf-8")
        try:
            # Nudge with a bare CR before reading the banner. AOS-S prints a
            # multi-line copyright / RESTRICTED-RIGHTS legend after login and
            # only renders the CLI prompt once it receives an Enter, so a
            # prompt-anchored read would otherwise stall the FULL timeout on
            # every connect — wasting ~12s per session and failing the 3s fleet
            # reachability probe (the device shows "unreachable" even though its
            # datums fetch fine). A CR is harmless on devices that already show
            # a prompt (it just reprints it) and answers any "Press any key to
            # continue" gate. Best-effort: a closed channel is caught below.
            try:
                self._proc.stdin.write("\n")
            except Exception:  # noqa: BLE001 — channel may already be closed
                pass
            self._banner = await self._read_until_prompt()  # banner / first output
            if self._banner.strip():
                logger.info("cli %s: login banner/first output (%d bytes): %r",
                            self.host, len(self._banner), self._banner[-500:])
            # A device that accepts the login but tears the session down right
            # after — restricted role, no CLI-exec privilege, no PTY, or (AOS-S)
            # "maximum number of sessions are active" — shows up as an already-
            # closed channel here, or as a bare "Channel not open for sending"
            # on the first write. Detect it and raise a banner-enriched,
            # actionable error instead of the opaque asyncssh text.
            if self._channel_closed():
                raise CliError(self._session_closed_error())
            paging = _PAGING_CMD.get(self.object_type)
            if paging:
                await self._send(paging)
                await self._read_until_prompt()
            if self.enable_secret and self.object_type == "aos_switch":
                await self._send("enable")
                await self._read_until_prompt()
                await self._send(self.enable_secret)
                await self._read_until_prompt()
        except BaseException:
            # CRITICAL: connect() runs inside __aenter__; if it raises here,
            # __aexit__ (hence close()) never fires — so without this the
            # authenticated asyncssh connection, and the device's session/vty
            # slot, would LEAK. On small-session-cap switches (AOS-S) leaked
            # sessions pile up until the device refuses ALL logins ("maximum
            # number of sessions are active"), locking out polling AND humans.
            # Always tear the half-open session down before propagating.
            await self.close()
            raise

    def _session_closed_error(self, exc=None) -> str:
        """Actionable message for a session the device tore down right after
        login, enriched with a tail of the banner/first output it sent (so the
        poll error — cached + shown in the UI — carries a debuggable clue).
        Special-cases the AOS-S session-pool-exhausted disconnect."""
        banner = (self._banner or "").strip()
        if re.search(r"maximum number of sessions", banner, re.IGNORECASE):
            msg = ('device refused the login: its SSH/console session pool is '
                   'full ("maximum number of sessions are active") — free stale '
                   'sessions or raise the cap on the device (AOS-S: '
                   '"show ip ssh" to list, "ip ssh" / "console" session limits '
                   'to raise)')
        else:
            msg = self._SESSION_CLOSED_HINT
        if banner:
            msg += f"; device sent before closing: {banner[-300:]!r}"
        if exc is not None:
            msg += f" ({exc})"
        return msg

    async def close(self) -> None:
        # Best-effort clean logout so the device frees its vty/session slot
        # IMMEDIATELY rather than holding it until idle-timeout. On small-cap
        # switches (AOS-S) a TCP-drop-only close lingers and, across repeated
        # polls, exhausts the session pool. Guarded: a closed channel skips.
        try:
            if self._proc is not None and not self._channel_closed():
                self._proc.stdin.write("exit\n")
        except Exception:
            pass
        try:
            if self._proc:
                self._proc.close()
        except Exception:
            pass
        try:
            if self._conn:
                self._conn.close()
                await self._conn.wait_closed()
        except Exception:
            pass

    async def _send(self, line: str) -> None:
        try:
            self._proc.stdin.write(line + "\n")
        except (BrokenPipeError, ConnectionError, OSError) as e:
            # asyncssh raises BrokenPipeError("Channel not open for sending")
            # once the peer has closed the channel. Translate it into an
            # operator-actionable message (same root cause as the connect-time
            # check, but for a mid-session teardown).
            raise CliError(self._session_closed_error(exc=e))

    async def _read_until_prompt(self, timeout: float = 12.0) -> str:
        """Drain stdout until a prompt-looking line appears (or timeout).

        Linear cost: complete lines are drained into ``out`` every iteration,
        so ``buf`` only ever holds the trailing partial line + the newest
        chunk, and both detections examine bounded tails — the prompt check
        looks only at the last line (``_PROMPT_TAIL`` window), the pager check
        only at the newest chunk plus a ``_PAGER_OVERLAP`` overlap for a
        marker split across the boundary. A multi-thousand-line MAC/user
        table is never rescanned whole per 4KB read."""
        out: List[str] = []
        # Invariant: between iterations buf holds only the trailing partial
        # line (no newline) — complete lines are drained below.
        buf = ""
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    chunk = await asyncio.wait_for(
                        self._proc.stdout.read(4096), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    # buf is the current (only) partial line; the end-anchored
                    # prompt regex on its tail == the old whole-last-line check.
                    if buf and _PROMPT_RE.search(buf[-_PROMPT_TAIL:]):
                        break
                    continue
                if not chunk:
                    break
                # If paging is still on despite `no page`, advance the pager (send
                # a space) and strip the marker so output isn't truncated/polluted.
                # Scan only the newest chunk + a small overlap — older text was
                # already scanned (and scrubbed) when its own chunk arrived.
                keep = max(0, len(buf) - _PAGER_OVERLAP)
                window = buf[keep:] + chunk
                # Two stream-blocking gates need a key sent before more output
                # arrives: the "-- More --" pager AND the AOS-S login banner's
                # "Press any key to continue" (the JL-series 2540/2930 legend
                # blocks here and only renders the CLI prompt once a key is
                # pressed). Answer either the moment it actually appears in the
                # stream — a fixed pre-read nudge races the banner and is lost,
                # leaving the switch blocked so a prompt-anchored read stalls the
                # full timeout and the device shows "unreachable". Strip the
                # marker so it can't truncate/pollute parsed output, then keep
                # reading toward the prompt.
                if _PAGER_RE.search(window) or _CONTINUE_RE.search(window):
                    try:
                        self._proc.stdin.write(" ")
                    except Exception:
                        pass
                    buf = buf[:keep] + _CONTINUE_RE.sub("", _PAGER_RE.sub("", window))
                    deadline = loop.time() + timeout  # keep reading further pages
                else:
                    buf += chunk
                # Prompt check on the LAST line only (same semantics as the old
                # splitlines()[-1].rstrip() over the whole buffer, but bounded).
                tail = buf[-_PROMPT_TAIL:]
                if tail.endswith("\n"):
                    tail = tail[:-1]
                    if tail.endswith("\r"):
                        tail = tail[:-1]
                last_line = tail[tail.rfind("\n") + 1:]
                if last_line and _PROMPT_RE.search(last_line.rstrip()):
                    out.append(buf)
                    return "".join(out)
                # Drain complete lines; keep only the trailing partial line so
                # the next iteration's scans stay O(chunk). Any newline is at
                # index >= keep (the pre-chunk buf held none).
                nl = buf.rfind("\n", keep)
                if nl != -1:
                    out.append(buf[:nl + 1])
                    buf = buf[nl + 1:]
        except Exception as e:
            logger.debug("cli read %s: %s", self.host, e)
        return "".join(out)

    async def run(self, command: str) -> str:
        """Run one show command, return its text output (echoed command line
        stripped)."""
        await self._send(command)
        raw = await self._read_until_prompt(timeout=self.command_timeout)
        # Strip the echoed command line (first line) if present.
        text = raw
        if command in text:
            _, _, text = text.partition(command)
            text = text.lstrip("\n")
        return text

    async def config(self, commands: List[str], timeout: float = 20.0) -> str:
        """Enter config mode, run each command, exit (``end``). Returns the
        concatenated output. Used for the ArubaOS cert bind (crypto-local pki +
        web-server profile). ``web-server profile`` etc. enter sub-contexts;
        ``end`` at the finish pops all the way out regardless of nesting."""
        out = []
        await self._send("configure terminal")
        out.append(await self._read_until_prompt(timeout=timeout))
        for c in commands:
            await self._send(c)
            out.append(await self._read_until_prompt(timeout=timeout))
        await self._send("end")
        out.append(await self._read_until_prompt(timeout=timeout))
        return "".join(out)

    async def scp_put_bytes(self, data: bytes, remote_path: str) -> None:
        """SCP-upload ``data`` to ``remote_path`` on the device over the SAME SSH
        connection (ArubaOS accepts a file into flash: via SCP). Writes a 0600
        temp file locally and streams it with asyncssh.scp."""
        import asyncssh, os, tempfile
        if self._conn is None:
            raise CliError("not connected")
        fd, tmp = tempfile.mkstemp(suffix=".pfx")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o600)
            await asyncio.wait_for(asyncssh.scp(tmp, (self._conn, remote_path)),
                                   timeout=60.0)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ── Pure parsers (testable) ──────────────────────────────────────────────────
_MAC_TOKEN = re.compile(
    r"[0-9a-fA-F]{6}-[0-9a-fA-F]{6}|"                          # Aruba aabbcc-ddeeff
    r"[0-9a-fA-F]{4}[-.:][0-9a-fA-F]{4}[-.:][0-9a-fA-F]{4}|"   # Cisco aabb.ccdd.eeff
    r"[0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5}|"                # aa:bb:cc:dd:ee:ff
    r"[0-9a-fA-F]{12}")                                        # bare 12 hex


def _is_null_mac(mac: str) -> bool:
    """True for an all-zero / incomplete MAC (e.g. ``000000-000000`` in an ARP
    table with no resolved hardware address)."""
    return set(re.sub(r"[^0-9a-fA-F]", "", mac or "")) <= {"0"}
_IP_TOKEN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def parse_arp_aos_s(text: str) -> List[dict]:
    """Aruba AOS-S ``show arp`` → ``[{ip, mac, interface}]``. Columns:
    ``IP | MAC(aabbcc-ddeeff) | Type(dynamic) | Port``. The Port is optional
    (an unresolved ``000000-000000`` entry has none); such null-MAC rows are
    dropped. Port is taken only when it's a real port token (not the Type word)."""
    rows = []
    for line in (text or "").splitlines():
        ip = _IP_TOKEN.search(line)
        mac = _MAC_TOKEN.search(line)
        if not ip or not mac or _is_null_mac(mac.group(0)):
            continue
        # tail = [Type, Port?]. Port present only when the last token is port-like
        # (digit, module/slash form like A1 or 1/2, or a named trunk like Trk2)
        # — never the Type word ("dynamic").
        tail = line[mac.end():].split()
        port = ""
        if len(tail) >= 2 and re.match(r"^[A-Za-z]*\d+(?:/\d+)*$", tail[-1]):
            port = tail[-1]
        rows.append({"ip": ip.group(0), "mac": mac.group(0).lower(),
                     "interface": str(port)})
    return rows


def parse_mac_aos_s(text: str) -> List[dict]:
    """Aruba AOS-S ``show mac-address`` → ``[{mac, vlan, interface}]``. Columns:
    ``MAC(aabbcc-ddeeff) | VLAN | Port`` — VLAN precedes Port."""
    rows = []
    for line in (text or "").splitlines():
        mac = _MAC_TOKEN.search(line)
        if not mac or _is_null_mac(mac.group(0)):
            continue
        rest = line[mac.end():].split()
        vlan = rest[0] if rest else ""
        port = rest[1] if len(rest) > 1 else ""
        rows.append({"mac": mac.group(0).lower(), "vlan": str(vlan),
                     "interface": str(port)})
    return rows


def parse_port_access_clients_aos_s(text: str) -> List[dict]:
    """Aruba AOS-S ``show port-access clients`` → ``[{ip, mac, vlan, interface}]``.
    Columns: ``Port | Client Name | MAC(aabbcc-ddeeff) | IP | User Role | Type |
    VLAN(list)``. IP may be ``n/a``; VLAN may be a comma-separated list or empty.
    Anchored on the hyphenated MAC (col 3); the port is column 1; the trailing
    comma-separated integers are the VLAN list."""
    rows = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\d+(?:/\d+)*)\s+\S+\s+"
                     r"([0-9a-fA-F]{6}-[0-9a-fA-F]{6})\s+(\S+)", line)
        if not m or _is_null_mac(m.group(2)):
            continue
        port, mac, ip = m.group(1), m.group(2), m.group(3)
        ip = "" if ip.lower() in ("n/a", "na", "-", "") else ip
        vm = re.search(r"((?:\d{1,4})(?:\s*,\s*\d{1,4})*)\s*$", line)
        vlan = re.sub(r"\s+", "", vm.group(1)) if vm else ""
        rows.append({"ip": ip, "mac": mac.lower(), "vlan": vlan,
                     "interface": str(port)})
    return rows


def parse_interfaces_aos_s(text: str) -> List[dict]:
    """Aruba AOS-S `show interfaces brief` → [{name,ip,mac,vlan,status,speed}].
    Best-effort: name=port, status from Physical Status, speed from speed token;
    IP/MAC/VLAN not in this output (SNMP path gives the full interface detail)."""
    rows = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(("Status", "  Port", "---", "Admin")):
            continue
        toks = s.split()
        if len(toks) < 3 or not toks[0][0].isalnum():
            continue
        name = toks[0]
        # Admin Status | Physical Status | Speed/Type ... columns vary; find
        # an explicit up/down token for status and a speed token.
        st = "down"
        for t in toks[1:]:
            if t.lower() in ("up", "down"):
                st = t.lower()
                break
        speed = 0
        for t in toks[1:]:
            m = re.match(r"^(\d+)([GMK]?)", t)
            if m:
                speed = int(m.group(1))
                break
        rows.append({"name": name, "ip": "", "mac": "", "vlan": "",
                     "status": st, "speed": speed})
    return rows


def parse_arp_junos(text: str) -> List[dict]:
    """Junos `show arp` → [{ip, mac, interface}].

    Junos output order is ``MAC  IP  interface`` (not IP-then-MAC like AOS-S),
    so the interface is the trailing token after the MAC that isn't the IP."""
    rows = []
    for line in (text or "").splitlines():
        ip = _IP_TOKEN.search(line)
        mac = _MAC_TOKEN.search(line)
        if not ip or not mac:
            continue
        iface = ""
        for tok in reversed(line[mac.end():].split()):
            if tok != ip.group(0):
                iface = tok
                break
        rows.append({"ip": ip.group(0), "mac": mac.group(0),
                     "interface": str(iface)})
    return rows


def parse_mac_junos(text: str) -> List[dict]:
    """Junos `show ethernet-switching table` → [{mac, vlan, interface}]."""
    rows = []
    for line in (text or "").splitlines():
        mac = _MAC_TOKEN.search(line)
        if not mac:
            continue
        rest = line[mac.end():].split()
        vlan = rest[0] if rest else ""
        iface = rest[1] if len(rest) > 1 else ""
        rows.append({"mac": mac.group(0), "vlan": str(vlan),
                     "interface": str(iface)})
    return rows


def parse_interfaces_junos(text: str) -> List[dict]:
    """Junos `show interfaces descriptions` → [{name,ip,mac,vlan,status,speed}]."""
    rows = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(("Interface", "Admin")) or set(s) <= {"-"}:
            continue
        toks = s.split()
        if len(toks) < 2:
            continue
        name = toks[0]
        admin, link = toks[1], (toks[2] if len(toks) > 2 else "")
        status = "up" if link.lower() == "up" else "down"
        rows.append({"name": name, "ip": "", "mac": "", "vlan": "",
                     "status": status, "speed": 0})
    return rows


# ── ArubaOS gateway/controller parsers (NOT AOS-S — different CLI + commands) ──
# The gateway is an ArubaOS 8 mobility gateway/controller, not an AOS-S switch, so
# `show mac-address` / `show interfaces` (AOS-S) return nothing useful. Correct
# sources:
#   ARP/IP+MAC  ← `show user-table`          (connected clients: IP col1, MAC col2)
#   MAC table   ← `show user-table` augmented by `show datapath bridge table`
#                 (user-table = client MAC+IP; datapath adds bridged MACs + VLAN)
#   VLANs       ← `show vlan`                 (authoritative list, incl. empty VLANs)
#   Interfaces  ← `show port status`          (physical-port status/speed/mode)
# Parsers ported from ntc-templates (aruba_os) + real device output.

# ArubaOS device-fingerprint "Type" column vocabulary → client OS. Longest-first
# so "Windows Mobile" wins over "Windows". Matched only in the row tail (after the
# forward-mode token) to avoid a false hit in an SSID/name.
_OS_TYPES = ["Windows Mobile", "Windows Phone", "Windows", "macOS", "OS X",
             "iPhone", "iPad", "iPod", "iOS", "Android", "Chrome OS",
             "Chromebook", "Apple TV", "AppleTV", "tvOS", "watchOS", "Ubuntu",
             "Debian", "Fedora", "Linux", "Roku", "PlayStation", "Xbox",
             "Nintendo", "Kindle", "BlackBerry", "Symbian"]
_OS_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in _OS_TYPES)
                    + r")(?![\w-])", re.IGNORECASE)
_FWD_MODE_RE = re.compile(r"\b(?:tunnel|bridge|decrypt-tunnel|split-tunnel)\b")


def parse_user_table_gateway(text: str) -> List[dict]:
    """ArubaOS ``show user-table`` → ``[{ip, mac, os, interface}]``. IP is always
    column 1 and MAC column 2 (later columns — Name/Auth/Host Name — are
    frequently EMPTY, so whitespace-column parsing misaligns; anchoring on IP+MAC
    is robust). ``os`` is the device-fingerprint "Type" column (macOS/iPhone/…),
    read from the row tail after the forward-mode token so an SSID/name can't
    false-match. ``interface`` carries the AP name / wired port when present."""
    rows = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+"
                     r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b", line)
        if not m:
            continue
        # OS from the "Type" column: search only the tail after forward-mode
        # (tunnel/bridge) where Type|Host Name|User Type live.
        fwd = _FWD_MODE_RE.search(line, m.end())
        tail = line[fwd.end():] if fwd else line[m.end():]
        om = _OS_RE.search(tail)
        os_val = ""
        if om:
            hit = om.group(1)
            os_val = next((t for t in _OS_TYPES if t.lower() == hit.lower()), hit)
        rows.append({"ip": m.group(1), "mac": m.group(2).lower(),
                     "os": os_val, "interface": ""})
    return rows


# Retain the old name as an alias so callers/tests referencing parse_arp_gateway
# keep working — the gateway ARP source is now the user-table.
parse_arp_gateway = parse_user_table_gateway


def parse_datapath_bridge_gateway(text: str) -> List[dict]:
    """ArubaOS ``show datapath bridge table`` → ``[{mac, vlan, interface}]``.
    Columns vary by version (MAC, VLAN, Assigned-VLAN, Destination, Flags), so
    parse tolerantly: MAC token + first VLAN-like integer + a destination token
    (tunnel/local/port)."""
    rows = []
    for line in (text or "").splitlines():
        mac = _MAC_TOKEN.search(line)
        if not mac:
            continue
        rest = line[mac.end():].split()
        vlan = next((t for t in rest if t.isdigit() and 1 <= int(t) <= 4094), "")
        dest = next((t for t in rest if ("/" in t or t.lower() in ("tunnel", "local"))), "")
        rows.append({"mac": mac.group(0).lower(), "vlan": str(vlan), "interface": str(dest)})
    return rows


# Alias: the gateway MAC-table source is the datapath bridge table.
parse_mac_gateway = parse_datapath_bridge_gateway


def parse_interfaces_gateway(text: str) -> List[dict]:
    """ArubaOS ``show ip interface brief`` → ``[{name, ip, mac, vlan, status,
    speed}]``. Ported from ntc-templates ``aruba_os_show_ip_interface_brief``:
    ``<iface (two tokens)>  <ip> / <netmask>  <admin>  <protocol>``."""
    rows = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\S+\s\S+)\s+(\S+)\s+/\s+(\S+)\s+(\S+)\s+(\S+)\s*$", line)
        if not m:
            continue
        name, ip, _netmask, _admin, proto = m.groups()
        if name.lower().startswith(("interface", "----")):  # header/separator
            continue
        rows.append({"name": name.strip(),
                     "ip": "" if ip.lower() in ("unassigned", "n/a") else ip,
                     "mac": "", "vlan": "", "status": proto.lower(), "speed": 0})
    return rows


def parse_port_status_gateway(text: str) -> List[dict]:
    """ArubaOS gateway ``show port status`` → ``[{name, ip, mac, vlan, status,
    speed, mode}]``. Columns: ``Slot-Port | PortType | AdminState | OperState |
    PoE | Trusted | SpanningTree | PortMode | Speed | Duplex | PortError``.

    Anchor on a ``slot/port`` first token; status = OperState (col 4); speed is
    parsed from the ``<n> Gbps|Mbps`` token → Mbps int (Auto → 0); mode = PortMode
    (Access/Trunk). No IP/MAC/VLAN in this output (those come from the user-table
    / bridge). Header/separator lines have no slot-port token so they drop."""
    rows = []
    for line in (text or "").splitlines():
        toks = line.split()
        if len(toks) < 4 or not re.match(r"^\d+/\d+(?:/\d+)?$", toks[0]):
            continue
        oper = toks[3].lower()
        status = "up" if oper == "up" else ("down" if oper == "down" else oper)
        sm = re.search(r"\b(\d+)\s*(Gbps|Mbps)\b", line, re.IGNORECASE)
        speed = (int(sm.group(1)) * (1000 if sm.group(2).lower().startswith("g")
                 else 1)) if sm else 0
        mode = toks[7] if len(toks) > 7 else ""
        rows.append({"name": toks[0], "ip": "", "mac": "", "vlan": "",
                     "status": status, "speed": speed, "mode": mode})
    return rows


def parse_vlans_gateway(text: str) -> List[dict]:
    """ArubaOS ``show vlan`` → ``[{vlan, name, ports}]`` — the authoritative VLAN
    list (includes empty VLANs, unlike a MAC/user-table rollup). Tolerant of the
    column layout (VLAN | Name | Ports | AAA Profile): anchor on a leading VLAN
    id 1-4094, take the next token as the name, and keep the remainder as ports/
    description. Header/separator lines have no leading integer so they drop."""
    rows = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\d{1,4})\s+(\S.*?)\s*$", line)
        if not m:
            continue
        vid = int(m.group(1))
        if not (1 <= vid <= 4094):
            continue
        rest = m.group(2).split()
        name = rest[0] if rest else ""
        ports = " ".join(rest[1:]) if len(rest) > 1 else ""
        rows.append({"vlan": str(vid), "name": name, "ports": ports})
    return rows


def parse_lldp_neighbors(text: str) -> List[str]:
    """Best-effort extract neighbor management IPv4 addresses from an LLDP
    neighbor listing across the Aruba/Junos families. Different platforms format
    LLDP output very differently, so rather than column-parse, scan for any
    'management address' / 'mgmt' line carrying an IPv4, plus a fallback of any
    dotted-quad on a line mentioning 'address'. Deduped, order-preserving.
    Used by the scanner's optional LLDP crawl to enqueue neighbors."""
    ips: List[str] = []
    seen = set()
    ipv4 = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
    for line in (text or "").splitlines():
        low = line.lower()
        if not ("address" in low or "mgmt" in low or "management" in low):
            continue
        for m in ipv4.finditer(line):
            ip = m.group(1)
            octs = ip.split(".")
            if any(int(o) > 255 for o in octs):
                continue
            if ip in ("0.0.0.0", "255.255.255.255") or ip.startswith("127."):
                continue
            if ip not in seen:
                seen.add(ip)
                ips.append(ip)
    return ips


async def cli_get_lldp_neighbors(session: CliSession, object_type: str) -> List[str]:
    """Run the vendor LLDP-neighbor command and return neighbor management IPs.
    Best-effort — returns ``[]`` on any failure (a device without LLDP simply
    doesn't extend the crawl)."""
    cmd = {"aos_switch": "show lldp info remote-device",
           "cx_switch": "show lldp neighbor-info detail",
           "ex_switch": "show lldp neighbors",
           "gateway": "show ap lldp neighbors"}.get(object_type, "show lldp neighbors")
    try:
        text = await session.run(cmd)
    except Exception:
        return []
    return parse_lldp_neighbors(text)


# object_type → (arp, mac, interfaces) parser triple
PARSERS: Dict[str, Any] = {
    "aos_switch": (parse_arp_aos_s, parse_mac_aos_s, parse_interfaces_aos_s),
    "ex_switch":  (parse_arp_junos, parse_mac_junos, parse_interfaces_junos),
    "gateway":    (parse_arp_gateway, parse_mac_gateway, parse_port_status_gateway),
    # AOS-CX is REST-first; if CLI is forced, the AOS-S parsers are a close
    # enough fallback for Aruba's CLI family.
    "cx_switch":  (parse_arp_aos_s, parse_mac_aos_s, parse_interfaces_aos_s),
}

# object_type → VLAN parser. The ``show vlan`` layout is close enough across the
# Aruba/generic families that the tolerant gateway parser covers all of them.
VLAN_PARSERS: Dict[str, Any] = {
    "gateway": parse_vlans_gateway, "aos_switch": parse_vlans_gateway,
    "cx_switch": parse_vlans_gateway, "ex_switch": parse_vlans_gateway,
}


# ── High-level async gathers (the SshCliDriver calls these) ──────────────────
async def cli_get_device_info(session: CliSession, object_type: str) -> dict:
    """Run the vendor ``info`` show command and parse model/serial/firmware from
    its text (best-effort). Raises :class:`CliError` on command failure."""
    # Reuse the vendor "info" command via run(); model/firmware parsed from the
    # raw text best-effort.
    info_cmd = {"aos_switch": "show system-information",
                "ex_switch": "show version",
                "cx_switch": "show version",
                # ArubaOS gateway: `show version` carries the OS name + version
                # (wanted for NetBox platform / software_version).
                "gateway": "show version"}.get(object_type, "show version")
    try:
        text = await session.run(info_cmd)
    except Exception as e:
        raise CliError(f"info command failed: {e}")
    # OS name for NetBox (platform). Detect the family from the version banner;
    # fall back to a sensible default per object_type.
    _os_m = re.search(r"\b(ArubaOS-CX|ArubaOS|AOS-CX|AOS-S|JUNOS|Junos)\b", text or "", re.IGNORECASE)
    os_name = _os_m.group(1) if _os_m else {"gateway": "ArubaOS", "cx_switch": "ArubaOS-CX",
                                            "aos_switch": "AOS-S", "ex_switch": "Junos"}.get(object_type, "")
    return {"model": _first_hw_token(text), "serial": _serial_from(text),
            "firmware": _firmware_from(text), "os": os_name, "interfaces_count": 0}


async def cli_get_arp(session: CliSession, object_type: str) -> List[dict]:
    """Run the vendor ARP show command and return ``[{ip, mac, interface}]``."""
    arp_cmd = {"aos_switch": "show arp", "ex_switch": "show arp",
               "cx_switch": "show arp",
               # ArubaOS gateway: the user-table holds the client IP↔MAC bindings
               # (there is no useful `show arp` for wireless clients).
               "gateway": "show user-table"}.get(object_type, "show arp")
    text = await session.run(arp_cmd)
    return PARSERS.get(object_type, PARSERS["aos_switch"])[0](text)


async def cli_get_mac_table(session: CliSession, object_type: str) -> List[dict]:
    """Run the vendor MAC-table show command and return ``[{mac, vlan, interface}]``."""
    mac_cmd = {"aos_switch": "show mac-address", "ex_switch": "show ethernet-switching table",
               "cx_switch": "show mac-address",
               # ArubaOS gateway: `show mac-address` is a switch command; the
               # bridge/MAC table lives in the datapath.
               "gateway": "show datapath bridge table"}.get(
               object_type, "show mac-address")
    text = await session.run(mac_cmd)
    return PARSERS.get(object_type, PARSERS["aos_switch"])[1](text)


def _err(message: str, data: Any = None) -> dict:
    """Standard ERROR envelope (mirrors nw_engine._err) for the cert-install
    helpers, which return an envelope rather than a parsed list."""
    out = {"status": "ERROR", "message": message}
    if data is not None:
        out["data"] = data
    return out


def _build_pkcs12(fullchain: str, privkey: str, name: str, password: str) -> bytes:
    """Bundle the LE fullchain + private key into a PKCS#12 (.pfx) — the format
    ArubaOS imports for a server cert (cert + key in one file). Uses the
    ``cryptography`` lib (an asyncssh dependency, so always present)."""
    import re as _re
    from cryptography.hazmat.primitives.serialization import pkcs12, BestAvailableEncryption
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.x509 import load_pem_x509_certificate
    key = load_pem_private_key(privkey.encode(), password=None)
    blocks = _re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                         fullchain, _re.DOTALL)
    certs = [load_pem_x509_certificate(b.encode()) for b in blocks]
    if not certs:
        raise CliError("no certificate found in fullchain")
    leaf, cas = certs[0], (certs[1:] or None)
    return pkcs12.serialize_key_and_certificates(
        name=name.encode(), key=key, cert=leaf, cas=cas,
        encryption_algorithm=BestAvailableEncryption(password.encode()))


async def cli_install_cert_gateway(session: CliSession, fullchain: str, privkey: str,
                                   chain: str, domain: str) -> dict:
    """Install an LE server cert on an ArubaOS mobility gateway/controller over
    SSH. Workflow (ArubaOS 8): build a PKCS#12, SCP it into flash:, import it
    (``crypto pki-import pfx serverCert``), then bind it to the web server
    (``crypto-local pki SERVERCERT`` + ``web-server profile / switch-cert``) and
    save. Captures every command's output so a failure is diagnosable. Returns
    the standard ``{status, message}`` envelope."""
    import re as _re
    import secrets as _secrets
    name = _re.sub(r"[^A-Za-z0-9._-]", "_", domain) or "lm-cert"
    fname = f"{name}.pfx"
    # ArubaOS pfx passphrase must avoid ' $ & ( ) | \ " ; < > ? — hex is safe.
    passphrase = _secrets.token_hex(12)
    try:
        pfx = _build_pkcs12(fullchain, privkey, name, passphrase)
    except Exception as e:  # noqa: BLE001
        return _err(f"gateway: PKCS#12 build failed: {e}")

    log = []
    try:
        # 1. Upload the .pfx into flash: over SCP (same SSH connection).
        await session.scp_put_bytes(pfx, f"flash/{fname}")
        log.append(f"uploaded flash/{fname} ({len(pfx)} bytes)")
        # 2. Import the server cert (exec/privileged mode).
        imp = await session.run(
            f"crypto pki-import pfx serverCert {name} {fname} {passphrase}")
        log.append("import: " + " ".join(imp.split())[:300])
        # 3. Bind: register the server cert + point the web server at it, save.
        cfg = await session.config([
            f"crypto-local pki SERVERCERT {name} {fname}",
            "web-server profile",
            f"switch-cert {name}",
        ])
        log.append("bind: " + " ".join(cfg.split())[:300])
        save = await session.run("write memory")
        log.append("save: " + " ".join(save.split())[:120])
    except CliError as e:
        return _err(f"gateway cert install: {e}", {"log": log})
    except Exception as e:  # noqa: BLE001
        return _err(f"gateway cert install: {e}", {"log": log})

    # Best-effort error detection in the captured output.
    joined = " ".join(log).lower()
    for marker in ("error", "invalid", "% ", "failed", "cannot"):
        if marker in joined:
            return _err(f"gateway cert install may have failed (device said: "
                        f"{'; '.join(log)})", {"log": log})
    return {"status": "SUCCESS",
            "message": f"imported {name} + bound to web-server on {session.host}",
            "log": log}


async def cli_get_interfaces(session: CliSession, object_type: str) -> List[dict]:
    """Run the vendor interface show command and return
    ``[{name, ip, mac, vlan, status, speed}]`` (IP/MAC/VLAN best-effort)."""
    if_cmd = {"aos_switch": "show interfaces brief", "ex_switch": "show interfaces descriptions",
              "cx_switch": "show interfaces brief",
              # ArubaOS gateway: physical-port status via `show port status`.
              "gateway": "show port status"}.get(
              object_type, "show interfaces")
    text = await session.run(if_cmd)
    return PARSERS.get(object_type, PARSERS["aos_switch"])[2](text)


async def cli_get_vlans(session: CliSession, object_type: str) -> List[dict]:
    """Run the vendor VLAN show command and return ``[{vlan, name, ports}]`` —
    the authoritative VLAN list (``show vlan`` on the ArubaOS gateway)."""
    vlan_cmd = {"aos_switch": "show vlans", "ex_switch": "show vlans",
                "cx_switch": "show vlan",
                "gateway": "show vlan"}.get(object_type, "show vlan")
    text = await session.run(vlan_cmd)
    return VLAN_PARSERS.get(object_type, parse_vlans_gateway)(text)


def _first_hw_token(text: str) -> str:
    m = re.search(r"\b([A-Z]{2,4}[-\s]?\d{2,4}[A-Z]*)\b", text or "")
    return m.group(1) if m else ""


def _serial_from(text: str) -> str:
    m = re.search(r"(?:Serial|S/N|Serial Number)\s*[:#]\s*(\S+)", text or "",
                  re.IGNORECASE)
    return m.group(1) if m else ""


def _firmware_from(text: str) -> str:
    m = re.search(r"(?:Version|SW|Firmware|Software)[^\n]*?(\d+\.\d+(?:\.\w+)*)",
                  text or "", re.IGNORECASE)
    return m.group(1) if m else ""