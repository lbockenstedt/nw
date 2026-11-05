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

# asyncio drives all probes concurrently.
import asyncio
# ipaddress validates and normalises target addresses.
import ipaddress
# Standard-library logging.
import logging
# re for banner/HTTP parsing and classification.
import re
# shutil finds the optional nmap binary.
import shutil
# Type hints.
from typing import Any, Callable, Dict, List, Optional, Tuple

# Named logger for scanner messages.
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
    # Lower-case once; empty text cannot be classified.
    t = (text or "").lower()
    if not t:
        return None
    # Aruba access points (Instant / AOS-10) run an ArubaOS CLI ("ArubaOS (MODEL:
    # 515)", "AP uptime is …") but are managed by Central, not the NW fleet.
    # Gateways report "(MODEL: Aruba7210-US)", so a bare numeric model is an AP.
    if (re.search(r"\bap uptime\b", t)
            or re.search(r"\(model:\s*(?:ap-)?\d{3}", t)
            or "instant access point" in t):
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
    # ArubaOS without CX is also a gateway.
    if "arubaos" in t and "cx" not in t:
        return "gateway"
    # Aruba/HPE ProCurve/AOS-S switch (incl. JL#/J9# model tokens).
    if ("procurve" in t or "provision" in t or "aos-s" in t
            or re.search(r"\b(jl\d{2,4}[a-z]?|j9\d{3}[a-z]?)\b", t)
            or "hewlett" in t or re.search(r"\bhp\b", t) or "aruba" in t):
        return "aos_switch"
    # Not a recognised manageable device.
    return None


def _norm_ip(addr: str) -> str:
    """Normalize IP address string, stripping leading/trailing whitespace."""
    # Canonical form when it parses as an IP.
    try:
        return str(ipaddress.ip_address(str(addr).strip()))
    except ValueError:
        # Not an IP: return the trimmed text.
        return str(addr or "").strip()


def _is_ipv4(addr: str) -> bool:
    """Check if address string parses as a valid IPv4 address."""
    # Parse and check the address family.
    try:
        return isinstance(ipaddress.ip_address(str(addr).strip()),
                          ipaddress.IPv4Address)
    except ValueError:
        # Not an IP at all.
        return False


# ── Default probe/identify implementations (overridable for tests) ───────────
async def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    """True if a TCP connection to ``host:port`` completes within ``timeout``."""
    # Try to open the connection within the timeout.
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        # Connected: close it again; a failing close does not matter.
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except (asyncio.TimeoutError, OSError):
        # Refused, unreachable or timed out.
        return False
    except Exception:  # noqa: BLE001
        # Any other error also means "not open".
        return False


# Web ports to profile, with whether each uses TLS; first one that answers wins.
_HTTP_PORTS: Tuple[Tuple[int, bool], ...] = ((443, True), (8443, True), (80, False), (8080, False))


async def _read_ssh_banner(host: str, timeout: float) -> str:
    """The server's SSH identification line (``SSH-2.0-…``), or ''."""
    # Connect to port 22 and read the first line the server sends.
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 22), timeout)
        try:
            line = await asyncio.wait_for(reader.readline(), timeout)
        finally:
            writer.close()
        # The identification line, trimmed and capped.
        return line.decode("utf-8", "replace").strip()[:200]
    except Exception:  # noqa: BLE001
        # No banner.
        return ""


async def _http_get(host: str, port: int, tls: bool, path: str, timeout: float) -> str:
    """Raw response head + first 16 KB of ``GET path`` (TLS unverified), or ''."""
    # ssl imported lazily; only needed here.
    import ssl
    # For HTTPS, accept any certificate (we only want the banner/title).
    ctx = None
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # Connect, send a minimal HTTP/1.0 GET and read up to 16 KB.
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx), timeout)
        try:
            writer.write(f"GET {path} HTTP/1.0\r\nHost: {host}\r\n"
                         "User-Agent: lm-nw-scan\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            data = await asyncio.wait_for(reader.read(16384), timeout)
        finally:
            writer.close()
        # Decoded response text.
        return data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        # Any failure: empty response.
        return ""


def _parse_http(raw: str) -> Dict[str, str]:
    """``http_server`` / ``http_realm`` / ``http_title`` / ``location`` of a raw response."""
    # Split headers from body.
    head, _, body = raw.partition("\r\n\r\n")
    out: Dict[str, str] = {}
    # Pull Server, auth realm and Location from the headers.
    for key, pattern in (("http_server", r"(?im)^server:\s*(.+)$"),
                         ("http_realm", r'(?im)^www-authenticate:.*realm="?([^"\r\n]+)'),
                         ("location", r"(?im)^location:\s*(\S+)")):
        m = re.search(pattern, head)
        if m:
            out[key] = m.group(1).strip()[:200]
    # Page title from the body, whitespace collapsed.
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    if m:
        out["http_title"] = " ".join(m.group(1).split())[:200]
    # Extracted fields.
    return out


async def _profile_probe(host: str, open_ports: List[int], timeout: float) -> Dict[str, str]:
    """What an unidentified host answers with: SSH banner + the first web
    port's Server / title / auth realm (one same-host redirect followed).
    The hub forwards this to NetBox, which guesses a role from it."""
    # Keep each probe short.
    timeout = min(timeout, 3.0)
    profile: Dict[str, str] = {}
    # SSH banner when port 22 is open.
    if 22 in open_ports:
        banner = await _read_ssh_banner(host, timeout)
        if banner:
            profile["ssh_banner"] = banner
    # First open web port that yields any info.
    for port, tls in _HTTP_PORTS:
        if port not in open_ports:
            continue
        # Fetch "/" and parse the response.
        info = _parse_http(await _http_get(host, port, tls, "/", timeout))
        loc = info.pop("location", "")
        # Follow one same-host redirect when the first page had no title.
        if loc and "http_title" not in info:
            path = re.sub(r"^https?://[^/]*", "", loc) or "/"
            if path.startswith("/"):
                more = _parse_http(await _http_get(host, port, tls, path, timeout))
                more.pop("location", None)
                info = {**more, **info}
        # Stop at the first port that answered with something.
        if info:
            profile.update(info)
            break
    # Collected host profile.
    return profile


async def _ssh_identify(host: str, port: int, cred: Dict[str, Any],
                        timeout: float) -> Optional[Dict[str, Any]]:
    """Log in over SSH with ``cred`` and collect a version banner. Returns
    ``{"text", "hostname"}`` on a successful login+command, else ``None``. The
    device family is unknown pre-login, so a neutral session is used (paging
    disable is skipped) and generic ``show version``-style commands are run."""
    # Imported lazily to avoid a circular import at module load.
    from transports import cli_io
    # Device dict for a neutral CLI session.
    dev = {
        "address": host, "port": port,
        "username": cred.get("username") or cred.get("user") or "",
        "password": cred.get("password") or "",
        "enable_secret": cred.get("enable_secret") or "",
        "object_type": "",  # neutral — identify from the banner
    }
    # No username: cannot log in.
    if not dev["username"]:
        return None
    # Build the session; config errors mean "not identified".
    try:
        session = cli_io.CliSession(dev, command_timeout=timeout)
    except cli_io.CliError:
        return None
    # Collected command output.
    text = ""
    try:
        # Log in, then try version commands until the text classifies.
        await asyncio.wait_for(session.connect(), timeout=timeout)
        for cmd in ("show version", "show system-information", "show system"):
            # A failing command is skipped.
            try:
                out = await asyncio.wait_for(session.run(cmd), timeout=timeout)
            except Exception:  # noqa: BLE001
                continue
            # Append any output.
            if out:
                text += "\n" + out
            # Stop once the vendor is known.
            if classify_platform(text):
                break
    except Exception:  # noqa: BLE001 — any failure == not identified via SSH
        # Any failure means "not identified via SSH".
        return None
    finally:
        # Always log out.
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
    # No output at all: not identified.
    if not text.strip():
        return None
    # Output plus a hostname from the banner or the prompt.
    return {"text": text, "hostname": _hostname_from_banner(text)
            or cli_io._hostname_from_prompt(getattr(session, "_last_prompt", ""))}


async def _snmp_identify(host: str, community: str,
                         timeout: float) -> Optional[Dict[str, Any]]:
    """GET sysDescr/sysName over SNMPv2c. Returns ``{"text", "hostname"}`` or
    ``None``. Runs the blocking pysnmp calls in a thread."""
    # Imported lazily; pysnmp may be absent.
    from transports import snmp_io
    # No community: cannot query.
    if not community:
        return None
    # Any failure means "not identified via SNMP".
    try:
        # One quick SNMP session for this host.
        session = snmp_io.SnmpSession({"address": host, "snmp_community": community},
                                      timeout=min(timeout, 3.0))
        # sysDescr identifies the vendor; no answer means not identified.
        descr = await asyncio.wait_for(
            snmp_io._to_thread(session.get, snmp_io.SYS_DESCR), timeout=timeout)
        if not descr:
            return None
        # sysName is optional.
        name = None
        try:
            name = await asyncio.wait_for(
                snmp_io._to_thread(session.get, snmp_io.SYS_NAME), timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        # sysDescr text and the hostname.
        return {"text": str(descr), "hostname": str(name or "").strip()}
    except Exception:  # noqa: BLE001
        # Any failure: not identified.
        return None


def _hostname_from_banner(text: str) -> str:
    """Best-effort hostname from a CLI banner (the prompt token or a
    'System Name'/'Hostname' line)."""
    # First "System Name"/"Hostname"/"Name" field.
    m = re.search(r"(?im)^\s*(?:system name|hostname|name)\s*[:=]\s*(\S+)", text or "")
    if m:
        return m.group(1).strip()
    # Not found.
    return ""


async def _nmap_augment(host: str, timeout: float) -> str:
    """Optional: run ``nmap -sV`` and return its service text for classification.
    Silently returns '' if nmap isn't installed or the run fails/times out."""
    # nmap not installed: skip.
    if not shutil.which("nmap"):
        return ""
    # Any failure or timeout: no extra text.
    try:
        # Service/version scan of this host only, without a ping check.
        proc = await asyncio.create_subprocess_exec(
            "nmap", "-sV", "-Pn", "-T4", "--host-timeout", f"{int(timeout)}s",
            host, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        # Wait a little past nmap's own host timeout.
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
        return (out or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        # Failed or timed out.
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
                 lldp_neighbors: Optional[Callable] = None,
                 profile_probe: Optional[Callable] = None,
                 pinged: bool = False):
        """Initialize the network scanner with credentials, ports, timeouts, and probe hooks.
        ``pinged`` means every target already answered a ping, so it counts as
        reachable and gets the SNMP attempt even with no open TCP port."""
        # Keep only dict credentials.
        self.credentials = [c for c in (credentials or []) if isinstance(c, dict)]
        # Probe settings, clamped to sane values.
        self.tcp_ports = tuple(tcp_ports or DEFAULT_TCP_PORTS)
        self.try_snmp = bool(try_snmp)
        self.use_nmap = bool(use_nmap)
        self.concurrency = max(1, int(concurrency or DEFAULT_CONCURRENCY))
        self.tcp_timeout = float(tcp_timeout or DEFAULT_TCP_TIMEOUT)
        self.target_timeout = float(target_timeout or DEFAULT_TARGET_TIMEOUT)
        # Injected probe/identify functions (real ones by default).
        self._tcp_probe = tcp_probe
        self._ssh_identify = ssh_identify
        self._snmp_identify = snmp_identify
        self._lldp_neighbors = lldp_neighbors  # callable(host, cred) -> [ips]
        self._profile_probe = profile_probe or _profile_probe
        # Whether targets already answered a ping.
        self.pinged = bool(pinged)

    async def _probe_ports(self, host: str) -> List[int]:
        """Probe configured TCP ports concurrently on the host and return list of open ports."""
        # Probe every port at once; exceptions count as closed.
        results = await asyncio.gather(
            *(self._tcp_probe(host, p, self.tcp_timeout) for p in self.tcp_ports),
            return_exceptions=True)
        # Ports that answered.
        return [p for p, ok in zip(self.tcp_ports, results) if ok is True]

    async def _fingerprint(self, host: str) -> Dict[str, Any]:
        """Probe + identify a single target. Never raises — returns a result
        dict with ``object_type`` set when identified, else ``None``."""
        # Canonical address and an empty result.
        host = _norm_ip(host)
        result: Dict[str, Any] = {
            "address": host, "object_type": None, "hostname": "", "os": "",
            "open_ports": [], "method": None, "credential": None,
            "credential_id": None, "reachable": False,
            "snmp_ok": None, "snmp_credential_id": None,
        }
        # Open TCP ports; a probe failure means none.
        try:
            open_ports = await self._probe_ports(host)
        except Exception:  # noqa: BLE001
            open_ports = []
        # Reachable if any port answered or the host was pinged.
        result["open_ports"] = open_ports
        result["reachable"] = bool(open_ports) or self.pinged

        # Text used for classification.
        banner = ""
        # SSH identify — try each credential until one logs in + classifies.
        if 22 in open_ports:
            # First credential that logs in decides.
            for cred in self.credentials:
                info = await self._ssh_identify(host, 22, cred, self.target_timeout)
                # Login failed: try the next credential.
                if not info:
                    continue
                # Classify the banner text.
                banner = info.get("text") or ""
                ot = classify_platform(banner)
                logger.info("nw scan %s: ssh banner classified %s — %r", host, ot,
                            " ".join(banner.split())[:400])
                # A login proves the host is reachable.
                result["reachable"] = True
                # Identified: record it, check SNMP too, and finish.
                if ot:
                    result.update(object_type=ot, method="ssh",
                                  credential=cred.get("name") or cred.get("id"),
                                  credential_id=cred.get("id"),
                                  hostname=info.get("hostname") or result["hostname"],
                                  os=_os_from_banner(banner))
                    await self._probe_snmp(host, result)
                    return result
                # Logged in but couldn't classify — keep the banner + hostname.
                result["hostname"] = info.get("hostname") or result["hostname"]
                break

        # SNMP identify — try each candidate community. Only on hosts that answered
        # the TCP probe: UDP gives no cheap liveness signal, and timing out on every
        # dead address of a large sweep would take far longer than the sweep itself.
        if self.try_snmp and result["object_type"] is None and (open_ports or self.pinged):
            # First community that answers decides.
            for cred in self.credentials:
                community = cred.get("snmp_community") or cred.get("community")
                # Credential without a community: skip.
                if not community:
                    continue
                # Query sysDescr/sysName.
                info = await self._snmp_identify(host, community, self.target_timeout)
                # No answer: try the next community.
                if not info:
                    continue
                # Classify the description.
                descr = info.get("text") or ""
                banner = banner or descr
                result["reachable"] = True
                ot = classify_platform(descr)
                # Identified via SNMP: record it and finish.
                if ot:
                    result.update(object_type=ot, method="snmp", snmp_ok=True,
                                  snmp_credential_id=cred.get("id"),
                                  credential=cred.get("name") or cred.get("id"),
                                  credential_id=cred.get("id"),
                                  hostname=info.get("hostname") or result["hostname"],
                                  os=_os_from_banner(descr))
                    return result
                # Answered SNMP but isn't a platform we auto-add: still monitorable.
                result["profile"] = {"sys_descr": " ".join(descr.split())[:300]}
                result.update(snmp_ok=True, snmp_credential_id=cred.get("id"),
                              hostname=info.get("hostname") or result["hostname"],
                              os=result["os"] or _os_from_banner(descr))
                break

        # Optional nmap augmentation for the still-unknown-but-reachable ones.
        if self.use_nmap and result["object_type"] is None and open_ports:
            # Run nmap and classify its service text.
            svc = await _nmap_augment(host, self.target_timeout)
            ot = classify_platform(svc)
            # Identified via nmap.
            if ot:
                result.update(object_type=ot, method="nmap",
                              os=_os_from_banner(svc))
        # Still unknown: gather a profile (banner/title) for the hub, capped at 10s.
        if result["object_type"] is None and open_ports:
            try:
                # Profile is best-effort.
                extra = await asyncio.wait_for(
                    self._profile_probe(host, open_ports, self.target_timeout), 10)
            except Exception:  # noqa: BLE001
                # Profile failed: empty.
                extra = {}
            # Merge with any SNMP profile already present.
            if extra:
                result["profile"] = {**result.get("profile", {}), **extra}
        # Result for this target.
        return result

    async def _probe_snmp(self, host: str, result: Dict[str, Any]) -> None:
        """Record which community (if any) answers, independently of SSH, so the
        monitoring sync knows which credential to replicate. ``snmp_ok`` stays
        None when no community was offered (nothing was tested)."""
        # SNMP disabled: nothing to test.
        if not self.try_snmp:
            return
        # Try each community until one answers.
        for cred in self.credentials:
            community = cred.get("snmp_community") or cred.get("community")
            # Credential without a community: skip.
            if not community:
                continue
            # At least one community was tested.
            result["snmp_ok"] = False
            # Record the first community that answers.
            if await self._snmp_identify(host, community, self.target_timeout):
                result.update(snmp_ok=True, snmp_credential_id=cred.get("id"))
                return

    async def scan(self, targets: List[str], *, crawl: bool = False,
                   max_targets: int = MAX_TARGETS,
                   max_depth: int = 1) -> Dict[str, Any]:
        """Fingerprint ``targets`` with bounded concurrency. When ``crawl`` is
        set, LLDP neighbors of each SSH-identified device are enqueued (bounded
        by ``max_depth`` / ``max_targets``). Returns
        ``{"identified": [...], "reachable": [...], "scanned": N, ...}``."""
        # Clamp the target count and crawl depth.
        max_targets = max(1, min(int(max_targets or MAX_TARGETS), MAX_TARGETS))
        max_depth = max(1, min(int(max_depth or 1), MAX_CRAWL_DEPTH))
        # Limit how many targets are fingerprinted at once.
        sem = asyncio.Semaphore(self.concurrency)

        # Unique IPv4 targets queued at depth 0, up to the target cap.
        seen: set = set()
        queue: List[Tuple[str, int]] = []
        for t in targets or []:
            ip = _norm_ip(t)
            if ip and ip not in seen and _is_ipv4(ip):
                seen.add(ip)
                queue.append((ip, 0))
                if len(seen) >= max_targets:
                    break

        # Results and counters.
        identified: List[Dict[str, Any]] = []
        reachable: List[Dict[str, Any]] = []
        scanned = 0

        async def _one(host: str, depth: int) -> Tuple[Dict[str, Any], int]:
            """Fingerprint a single target host within concurrency limits."""
            # Wait for a free slot, then fingerprint with a hard timeout.
            async with sem:
                try:
                    res = await asyncio.wait_for(self._fingerprint(host),
                                                 timeout=self.target_timeout + 5)
                except Exception as e:  # noqa: BLE001
                    # Timed out or failed: record as unreachable.
                    logger.debug("scan %s failed: %s", host, e)
                    res = {"address": host, "object_type": None, "reachable": False,
                           "open_ports": [], "method": None}
                # Result and the crawl depth it came from.
                return res, depth

        # Process the queue in waves; crawled neighbours form the next wave.
        while queue:
            batch, queue = queue, []
            # Fingerprint the whole wave concurrently.
            results = await asyncio.gather(*(_one(h, d) for h, d in batch))
            # Collect each result.
            for res, depth in results:
                scanned += 1
                if res.get("reachable"):
                    reachable.append(res)
                if res.get("object_type"):
                    identified.append(res)
                # Crawl: queue new LLDP neighbours of SSH-identified devices.
                if (crawl and res.get("method") == "ssh"
                        and self._lldp_neighbors and depth + 1 < max_depth):
                    for nip in await self._safe_neighbors(res["address"]):
                        nip = _norm_ip(nip)
                        if (nip and nip not in seen and _is_ipv4(nip)
                                and len(seen) < max_targets):
                            seen.add(nip)
                            queue.append((nip, depth + 1))

        # Log a summary and return the scan result.
        logger.info("nw scan complete: %d scanned, %d reachable, %d identified",
                    scanned, len(reachable), len(identified))
        return {
            "status": "SUCCESS",
            "scanned": scanned,
            # Credentials are optional. Without them the pass is discovery-only:
            # every identification came from nmap service detection, not an
            # SSH/SNMP login, and carries no credential or vault reference. Say
            # so explicitly — "identified nothing because nothing was offered"
            # and "the credentials failed on every host" are different outcomes
            # and the hub/UI could not previously tell them apart.
            "discovery_only": not self.credentials,
            "identified": identified,
            "reachable": reachable,
        }

    async def _safe_neighbors(self, host: str) -> List[str]:
        """Safely fetch LLDP neighbors for a host, swallowing any transport exceptions."""
        # The neighbour hook may be sync or async.
        try:
            neigh = self._lldp_neighbors(host, self.credentials)
            if asyncio.iscoroutine(neigh):
                neigh = await neigh
            # List of neighbour IPs.
            return list(neigh or [])
        except Exception:  # noqa: BLE001
            # Any failure: no neighbours.
            return []


def _os_from_banner(text: str) -> str:
    """Extract a friendly OS/platform name from a banner for the device record."""
    # First OS/platform name in the text.
    m = re.search(r"\b(ArubaOS-CX|ArubaOS|AOS-CX|AOS-S|JUNOS|Junos|ProCurve)\b",
                  text or "", re.IGNORECASE)
    return m.group(1) if m else ""
