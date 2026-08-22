"""Network fingerprinting scanner for the NW module.

Given a set of target IPs + candidate admin credentials, the scanner probes
each target and tries to IDENTIFY it as a manageable network device:

  1. TCP-connect port probe (async, no root, no external binary) for the common
     management ports (SSH 22, HTTPS/REST 443, HTTP 80, Telnet 23). SNMP (UDP
     161) can't be TCP-probed, so it is attempted directly.
  2. Identify via SSH — log in with each candidate credential, run a version
     command, and match the banner to a vendor family.
  3. Identify via SNMP — GET sysDescr/sysName with each candidate community and
     match the description to a vendor family.

Each reachable target is classified into one of the NW ``object_type`` families
(``aos_switch`` / ``cx_switch`` / ``ex_switch`` / ``gateway``) or left
``None`` (unknown → not manageable hardware, reported but never auto-added).

Pure-stdlib + asyncio by default, matching the rest of the NW module's
transports (no root, no external binary). If an ``nmap`` binary is present and
``use_nmap`` is set, an nmap ``-sV`` pass augments the banner text used for
classification — otherwise it is silently skipped.

Runs ON the NW spoke (it has reachability to the fleet subnets + the SSH/SNMP
transports). The hub aggregates the tenant's candidate IPs + the selected scan
credentials and sends them via ``NW_SCAN``; identified, not-already-known
devices are auto-added to the tenant device list on the hub.

The probe/identify steps are injected (``_tcp_probe`` / ``_ssh_identify`` /
``_snmp_identify``) so the orchestrator is unit-testable without a network.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import shutil
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("NwScanner")

# Common management ports we TCP-probe. 161 (SNMP) is UDP — attempted directly,
# never TCP-probed. Order is the classification preference (SSH first).
DEFAULT_TCP_PORTS: Tuple[int, ...] = (22, 443, 80, 23)

# Bounds (defense-in-depth; the hub also caps the target list it sends).
DEFAULT_CONCURRENCY = 32
DEFAULT_TCP_TIMEOUT = 1.5
DEFAULT_TARGET_TIMEOUT = 20.0
MAX_TARGETS = 4096
MAX_CRAWL_DEPTH = 3

# object_type families the scanner can identify (the manageable ones).
_MANAGEABLE = ("aos_switch", "cx_switch", "ex_switch", "gateway")


# ── Vendor classification ────────────────────────────────────────────────────
def classify_platform(text: str) -> Optional[str]:
    """Best-effort map a version banner / sysDescr / nmap service string to one
    of the NW ``object_type`` families, or ``None`` when it doesn't look like a
    manageable Aruba/HPE/Juniper network device.

    Heuristic + order-sensitive (AOS-CX before generic Aruba; mobility/gateway
    before the switch families). Kept tolerant on purpose — the same signatures
    also identify LLDP-crawled neighbors."""
    t = (text or "").lower()
    if not t:
        return None
    # Aruba AOS-CX (REST-first switch) — check before generic "aruba".
    if ("aos-cx" in t or "arubaos-cx" in t
            or ("aruba" in t and re.search(r"\bcx\b", t))):
        return "cx_switch"
    # Juniper EX (Junos).
    if "junos" in t or "juniper" in t:
        return "ex_switch"
    # Aruba mobility controller / gateway (ArubaOS, NOT CX).
    if ("mobilitycontroller" in t or "mobility controller" in t
            or "mobility conductor" in t
            or ("aruba" in t and ("controller" in t or "gateway" in t))
            or re.search(r"aruba\s?(70\d\d|72\d\d|90\d\d|91\d\d|92\d\d)", t)):
        return "gateway"
    if "arubaos" in t and "cx" not in t:
        return "gateway"
    # Aruba/HPE ProCurve/AOS-S switch (incl. JL#/J9# model tokens).
    if ("procurve" in t or "provision" in t or "aos-s" in t
            or re.search(r"\b(jl\d{2,4}[a-z]?|j9\d{3}[a-z]?)\b", t)
            or "hewlett" in t or re.search(r"\bhp\b", t) or "aruba" in t):
        return "aos_switch"
    return None


def _norm_ip(addr: str) -> str:
    try:
        return str(ipaddress.ip_address(str(addr).strip()))
    except ValueError:
        return str(addr or "").strip()


def _is_ipv4(addr: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(str(addr).strip()),
                          ipaddress.IPv4Address)
    except ValueError:
        return False


# ── Default probe/identify implementations (overridable for tests) ───────────
async def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    """True if a TCP connection to ``host:port`` completes within ``timeout``."""
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except (asyncio.TimeoutError, OSError):
        return False
    except Exception:  # noqa: BLE001
        return False


async def _ssh_identify(host: str, port: int, cred: Dict[str, Any],
                        timeout: float) -> Optional[Dict[str, Any]]:
    """Log in over SSH with ``cred`` and collect a version banner. Returns
    ``{"text", "hostname"}`` on a successful login+command, else ``None``. The
    device family is unknown pre-login, so a neutral session is used (paging
    disable is skipped) and generic ``show version``-style commands are run."""
    from transports import cli_io
    dev = {
        "address": host, "port": port,
        "username": cred.get("username") or cred.get("user") or "",
        "password": cred.get("password") or "",
        "enable_secret": cred.get("enable_secret") or "",
        "object_type": "",  # neutral — identify from the banner
    }
    if not dev["username"]:
        return None
    try:
        session = cli_io.CliSession(dev, command_timeout=timeout)
    except cli_io.CliError:
        return None
    text = ""
    try:
        await asyncio.wait_for(session.connect(), timeout=timeout)
        for cmd in ("show version", "show system-information", "show system"):
            try:
                out = await asyncio.wait_for(session.run(cmd), timeout=timeout)
            except Exception:  # noqa: BLE001
                continue
            if out:
                text += "\n" + out
            if classify_platform(text):
                break
    except Exception:  # noqa: BLE001 — any failure == not identified via SSH
        return None
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
    if not text.strip():
        return None
    return {"text": text, "hostname": _hostname_from_banner(text)}


async def _snmp_identify(host: str, community: str,
                         timeout: float) -> Optional[Dict[str, Any]]:
    """GET sysDescr/sysName over SNMPv2c. Returns ``{"text", "hostname"}`` or
    ``None``. Runs the blocking pysnmp calls in a thread."""
    from transports import snmp_io
    if not community:
        return None
    try:
        session = snmp_io.SnmpSession({"address": host, "snmp_community": community},
                                      timeout=min(timeout, 3.0))
        descr = await asyncio.wait_for(
            snmp_io._to_thread(session.get, snmp_io.SYS_DESCR), timeout=timeout)
        if not descr:
            return None
        name = None
        try:
            name = await asyncio.wait_for(
                snmp_io._to_thread(session.get, snmp_io.SYS_NAME), timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        return {"text": str(descr), "hostname": str(name or "").strip()}
    except Exception:  # noqa: BLE001
        return None


def _hostname_from_banner(text: str) -> str:
    """Best-effort hostname from a CLI banner (the prompt token or a
    'System Name'/'Hostname' line)."""
    m = re.search(r"(?im)^\s*(?:system name|hostname|name)\s*[:=]\s*(\S+)", text or "")
    if m:
        return m.group(1).strip()
    return ""


async def _nmap_augment(host: str, timeout: float) -> str:
    """Optional: run ``nmap -sV`` and return its service text for classification.
    Silently returns '' if nmap isn't installed or the run fails/times out."""
    if not shutil.which("nmap"):
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmap", "-sV", "-Pn", "-T4", "--host-timeout", f"{int(timeout)}s",
            host, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
        return (out or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


# ── Scanner ──────────────────────────────────────────────────────────────────
class NwScanner:
    """Fingerprint a set of targets against a set of candidate credentials.

    Probe/identify functions are injected so the orchestration (concurrency,
    bounds, per-target flow, optional crawl) is testable without a network."""

    def __init__(self, credentials: List[Dict[str, Any]], *,
                 tcp_ports: Tuple[int, ...] = DEFAULT_TCP_PORTS,
                 try_snmp: bool = True,
                 use_nmap: bool = False,
                 concurrency: int = DEFAULT_CONCURRENCY,
                 tcp_timeout: float = DEFAULT_TCP_TIMEOUT,
                 target_timeout: float = DEFAULT_TARGET_TIMEOUT,
                 tcp_probe: Callable = _tcp_probe,
                 ssh_identify: Callable = _ssh_identify,
                 snmp_identify: Callable = _snmp_identify,
                 lldp_neighbors: Optional[Callable] = None):
        self.credentials = [c for c in (credentials or []) if isinstance(c, dict)]
        self.tcp_ports = tuple(tcp_ports or DEFAULT_TCP_PORTS)
        self.try_snmp = bool(try_snmp)
        self.use_nmap = bool(use_nmap)
        self.concurrency = max(1, int(concurrency or DEFAULT_CONCURRENCY))
        self.tcp_timeout = float(tcp_timeout or DEFAULT_TCP_TIMEOUT)
        self.target_timeout = float(target_timeout or DEFAULT_TARGET_TIMEOUT)
        self._tcp_probe = tcp_probe
        self._ssh_identify = ssh_identify
        self._snmp_identify = snmp_identify
        self._lldp_neighbors = lldp_neighbors  # callable(host, cred) -> [ips]

    async def _probe_ports(self, host: str) -> List[int]:
        results = await asyncio.gather(
            *(self._tcp_probe(host, p, self.tcp_timeout) for p in self.tcp_ports),
            return_exceptions=True)
        return [p for p, ok in zip(self.tcp_ports, results) if ok is True]

    async def _fingerprint(self, host: str) -> Dict[str, Any]:
        """Probe + identify a single target. Never raises — returns a result
        dict with ``object_type`` set when identified, else ``None``."""
        host = _norm_ip(host)
        result: Dict[str, Any] = {
            "address": host, "object_type": None, "hostname": "", "os": "",
            "open_ports": [], "method": None, "credential": None,
            "credential_id": None, "reachable": False,
        }
        try:
            open_ports = await self._probe_ports(host)
        except Exception:  # noqa: BLE001
            open_ports = []
        result["open_ports"] = open_ports
        result["reachable"] = bool(open_ports)

        banner = ""
        # SSH identify — try each credential until one logs in + classifies.
        if 22 in open_ports:
            for cred in self.credentials:
                info = await self._ssh_identify(host, 22, cred, self.target_timeout)
                if not info:
                    continue
                banner = info.get("text") or ""
                ot = classify_platform(banner)
                result["reachable"] = True
                if ot:
                    result.update(object_type=ot, method="ssh",
                                  credential=cred.get("name") or cred.get("id"),
                                  credential_id=cred.get("id"),
                                  hostname=info.get("hostname") or result["hostname"],
                                  os=_os_from_banner(banner))
                    return result
                # Logged in but couldn't classify — keep the banner + hostname.
                result["hostname"] = info.get("hostname") or result["hostname"]
                break

        # SNMP identify — try each candidate community.
        if self.try_snmp and result["object_type"] is None:
            for cred in self.credentials:
                community = cred.get("snmp_community") or cred.get("community")
                if not community:
                    continue
                info = await self._snmp_identify(host, community, self.target_timeout)
                if not info:
                    continue
                descr = info.get("text") or ""
                banner = banner or descr
                result["reachable"] = True
                ot = classify_platform(descr)
                if ot:
                    result.update(object_type=ot, method="snmp",
                                  credential=cred.get("name") or cred.get("id"),
                                  credential_id=cred.get("id"),
                                  hostname=info.get("hostname") or result["hostname"],
                                  os=_os_from_banner(descr))
                    return result

        # Optional nmap augmentation for the still-unknown-but-reachable ones.
        if self.use_nmap and result["object_type"] is None and open_ports:
            svc = await _nmap_augment(host, self.target_timeout)
            ot = classify_platform(svc)
            if ot:
                result.update(object_type=ot, method="nmap",
                              os=_os_from_banner(svc))
        return result

    async def scan(self, targets: List[str], *, crawl: bool = False,
                   max_targets: int = MAX_TARGETS,
                   max_depth: int = 1) -> Dict[str, Any]:
        """Fingerprint ``targets`` with bounded concurrency. When ``crawl`` is
        set, LLDP neighbors of each SSH-identified device are enqueued (bounded
        by ``max_depth`` / ``max_targets``). Returns
        ``{"identified": [...], "reachable": [...], "scanned": N, ...}``."""
        max_targets = max(1, min(int(max_targets or MAX_TARGETS), MAX_TARGETS))
        max_depth = max(1, min(int(max_depth or 1), MAX_CRAWL_DEPTH))
        sem = asyncio.Semaphore(self.concurrency)

        seen: set = set()
        queue: List[Tuple[str, int]] = []
        for t in targets or []:
            ip = _norm_ip(t)
            if ip and ip not in seen and _is_ipv4(ip):
                seen.add(ip)
                queue.append((ip, 0))
                if len(seen) >= max_targets:
                    break

        identified: List[Dict[str, Any]] = []
        reachable: List[Dict[str, Any]] = []
        scanned = 0

        async def _one(host: str, depth: int) -> Tuple[Dict[str, Any], int]:
            async with sem:
                try:
                    res = await asyncio.wait_for(self._fingerprint(host),
                                                 timeout=self.target_timeout + 5)
                except Exception as e:  # noqa: BLE001
                    logger.debug("scan %s failed: %s", host, e)
                    res = {"address": host, "object_type": None, "reachable": False,
                           "open_ports": [], "method": None}
                return res, depth

        while queue:
            batch, queue = queue, []
            results = await asyncio.gather(*(_one(h, d) for h, d in batch))
            for res, depth in results:
                scanned += 1
                if res.get("reachable"):
                    reachable.append(res)
                if res.get("object_type"):
                    identified.append(res)
                if (crawl and res.get("method") == "ssh"
                        and self._lldp_neighbors and depth + 1 < max_depth):
                    for nip in await self._safe_neighbors(res["address"]):
                        nip = _norm_ip(nip)
                        if (nip and nip not in seen and _is_ipv4(nip)
                                and len(seen) < max_targets):
                            seen.add(nip)
                            queue.append((nip, depth + 1))

        logger.info("nw scan complete: %d scanned, %d reachable, %d identified",
                    scanned, len(reachable), len(identified))
        return {
            "status": "SUCCESS",
            "scanned": scanned,
            "identified": identified,
            "reachable": reachable,
        }

    async def _safe_neighbors(self, host: str) -> List[str]:
        try:
            neigh = self._lldp_neighbors(host, self.credentials)
            if asyncio.iscoroutine(neigh):
                neigh = await neigh
            return list(neigh or [])
        except Exception:  # noqa: BLE001
            return []


def _os_from_banner(text: str) -> str:
    """Extract a friendly OS/platform name from a banner for the device record."""
    m = re.search(r"\b(ArubaOS-CX|ArubaOS|AOS-CX|AOS-S|JUNOS|Junos|ProCurve)\b",
                  text or "", re.IGNORECASE)
    return m.group(1) if m else ""
