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
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NwCli")


class CliError(Exception):
    """Raised on SSH connection/auth failures or command timeouts."""


# Per-family paging-disable command + prompt regex.
_PAGING_CMD = {
    "aos_switch": "no page",
    "ex_switch":  "set cli screen-length 0",
    "cx_switch":  "no page",
    "gateway":    "no page",
}
# Prompt ends in '#' (enabled) or '>' (user) for AOS-S/gateway; Junos ends in
# '>' or '#'. Match a line ending in a prompt char after optional whitespace.
_PROMPT_RE = re.compile(r"[>#]\s*$")


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

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def connect(self) -> None:
        import asyncssh
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
        await self._read_until_prompt()  # banner
        paging = _PAGING_CMD.get(self.object_type)
        if paging:
            await self._send(paging)
            await self._read_until_prompt()
        if self.enable_secret and self.object_type == "aos_switch":
            await self._send("enable")
            await self._read_until_prompt()
            await self._send(self.enable_secret)
            await self._read_until_prompt()

    async def close(self) -> None:
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
        self._proc.stdin.write(line + "\n")

    async def _read_until_prompt(self, timeout: float = 12.0) -> str:
        """Drain stdout until a prompt-looking line appears (or timeout)."""
        out: List[str] = []
        try:
            buf = ""
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
                    if buf and _PROMPT_RE.search(buf.splitlines()[-1] if buf else ""):
                        break
                    continue
                if not chunk:
                    break
                buf += chunk
                lines = buf.splitlines(keepends=True)
                if lines and _PROMPT_RE.search(lines[-1].rstrip()):
                    out.append(buf)
                    return "".join(out)
                # keep the last partial line in buf
                if "\n" in buf:
                    complete, buf = buf.rsplit("\n", 1)
                    out.append(complete + "\n")
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


# ── Pure parsers (testable) ──────────────────────────────────────────────────
_MAC_TOKEN = re.compile(r"[0-9a-fA-F]{4}[-:][0-9a-fA-F]{4}[-:][0-9a-fA-F]{4}|"
                        r"[0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5}|"
                        r"[0-9a-fA-F]{12}")
_IP_TOKEN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def parse_arp_aos_s(text: str) -> List[dict]:
    """Aruba AOS-S `show arp` → [{ip, mac, interface}]."""
    rows = []
    for line in (text or "").splitlines():
        ip = _IP_TOKEN.search(line)
        mac = _MAC_TOKEN.search(line)
        if not ip or not mac:
            continue
        # tokens after the MAC: type, port — port is the last numeric-ish token.
        tail = line[mac.end():]
        port = tail.strip().split()[-1] if tail.strip().split() else ""
        rows.append({"ip": ip.group(0), "mac": mac.group(0),
                     "interface": str(port)})
    return rows


def parse_mac_aos_s(text: str) -> List[dict]:
    """Aruba AOS-S `show mac-address` → [{mac, vlan, interface}]."""
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


# Best-effort generic parsers for the Aruba/HPE gateway CLI (output varies by
# firmware); reuse the AOS-S ARP/MAC regex which matches most Aruba formats.
def parse_arp_gateway(text: str) -> List[dict]:
    return parse_arp_aos_s(text)


def parse_mac_gateway(text: str) -> List[dict]:
    return parse_mac_aos_s(text)


def parse_interfaces_gateway(text: str) -> List[dict]:
    return parse_interfaces_aos_s(text)


# object_type → (arp, mac, interfaces) parser triple
PARSERS: Dict[str, Any] = {
    "aos_switch": (parse_arp_aos_s, parse_mac_aos_s, parse_interfaces_aos_s),
    "ex_switch":  (parse_arp_junos, parse_mac_junos, parse_interfaces_junos),
    "gateway":    (parse_arp_gateway, parse_mac_gateway, parse_interfaces_gateway),
    # AOS-CX is REST-first; if CLI is forced, the AOS-S parsers are a close
    # enough fallback for Aruba's CLI family.
    "cx_switch":  (parse_arp_aos_s, parse_mac_aos_s, parse_interfaces_aos_s),
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
                "gateway": "show system"}.get(object_type, "show version")
    try:
        text = await session.run(info_cmd)
    except Exception as e:
        raise CliError(f"info command failed: {e}")
    return {"model": _first_hw_token(text), "serial": _serial_from(text),
            "firmware": _firmware_from(text), "interfaces_count": 0}


async def cli_get_arp(session: CliSession, object_type: str) -> List[dict]:
    """Run the vendor ARP show command and return ``[{ip, mac, interface}]``."""
    arp_cmd = {"aos_switch": "show arp", "ex_switch": "show arp",
               "cx_switch": "show arp", "gateway": "show arp"}.get(object_type, "show arp")
    text = await session.run(arp_cmd)
    return PARSERS.get(object_type, PARSERS["aos_switch"])[0](text)


async def cli_get_mac_table(session: CliSession, object_type: str) -> List[dict]:
    """Run the vendor MAC-table show command and return ``[{mac, vlan, interface}]``."""
    mac_cmd = {"aos_switch": "show mac-address", "ex_switch": "show ethernet-switching table",
               "cx_switch": "show mac-address", "gateway": "show mac-address"}.get(
               object_type, "show mac-address")
    text = await session.run(mac_cmd)
    return PARSERS.get(object_type, PARSERS["aos_switch"])[1](text)


async def cli_get_interfaces(session: CliSession, object_type: str) -> List[dict]:
    """Run the vendor interface show command and return
    ``[{name, ip, mac, vlan, status, speed}]`` (IP/MAC/VLAN best-effort)."""
    if_cmd = {"aos_switch": "show interfaces brief", "ex_switch": "show interfaces descriptions",
              "cx_switch": "show interfaces brief", "gateway": "show interfaces"}.get(
              object_type, "show interfaces")
    text = await session.run(if_cmd)
    return PARSERS.get(object_type, PARSERS["aos_switch"])[2](text)


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