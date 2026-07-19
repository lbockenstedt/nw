"""Network Devices engine + pluggable device drivers.

The engine holds the fleet of managed devices (pushed by the hub via
UPDATE_CONFIG) and, for each command, resolves the target device and delegates
to a **driver** chosen by the device's ``object_type`` and ``transport``.

Drivers implement real device IO across three transports:
  * ``SnmpDriver``    — SNMPv2c via pysnmp (standard MIBs; all four families).
  * ``SshCliDriver``  — SSH/CLI via asyncssh + per-vendor text parsers.
  * ``RestDriver``    — REST via httpx (AOS-CX RESTv1 + Aruba/HPE gateway REST).

The blocking/vendored libs live in ``transports/`` and are lazy-imported there
so this module imports cleanly without them installed. Every driver method
returns the standard result envelope ``{"status":"SUCCESS"|"ERROR",
"data":..., "message":...}``; a transport failure returns an ``ERROR`` envelope
(never raises) so one device's failure doesn't sink a batch. Credentials are
NEVER logged (the spoke masks them in handle_command before logging; the engine
only logs address + object_type + transport).

``poll(device_id)`` runs probe + device_info + interfaces + arp + mac_table in
one call (each independent — partial results on partial failure) for the
hub's POLL NOW path.
"""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NwEngine")

# ── Vendor command reference (used by the CLI driver + logged for diagnostics)─
_VENDOR_COMMANDS: Dict[str, Dict[str, str]] = {
    "aos_switch": {  # Aruba AOS-Switch (ProCurve/Aruba)
        "mac":  "show mac-address",
        "arp":  "show arp",
        "info": "show system-information",
        "if":   "show interfaces brief",
    },
    "cx_switch": {   # Aruba AOS-CX (RESTv1)
        "mac":  "GET /rest/v1/system/mac-table",
        "arp":  "GET /rest/v1/system/arp",
        "info": "GET /rest/v1/system",
        "if":   "GET /rest/v1/interfaces",
    },
    "ex_switch": {   # Juniper EX (Junos CLI)
        "mac":  "show ethernet-switching table",
        "arp":  "show arp",
        "info": "show version",
        "if":   "show interfaces descriptions",
    },
    "gateway": {     # Aruba / HPE gateway (REST + CLI)
        "mac":  "GET /api/mac-table",
        "arp":  "GET /api/arp",
        "info": "GET /api/system/info",
        "if":   "GET /api/interfaces",
    },
}

# Per object_type default transport when the device says ``transport=auto`` or
# omits it. SNMP is a valid explicit transport for any family.
_DEFAULT_TRANSPORT: Dict[str, str] = {
    "aos_switch": "ssh",
    "cx_switch":  "rest",
    "ex_switch":  "ssh",
    "gateway":    "rest",
}

_VALID_TRANSPORTS = ("ssh", "rest", "snmp", "auto")
_VALID_OBJECT_TYPES = tuple(_VENDOR_COMMANDS.keys())

# Credential field names — never logged.
_SENSITIVE = ("password", "enable_secret", "api_token", "snmp_community")


def _norm_mac(m: str) -> str:
    """Canonical lower-colon MAC ``aa:bb:cc:dd:ee:ff``; ``""`` for absent/unknown.

    Non-hex garbage is returned stripped-lower so two spellings still dedup
    upstream. Mirrors the hub-side ``norm_mac`` in access.py.
    """
    if not m:
        return ""
    s = str(m).strip().lower()
    if s in ("unknown", "none", "incomplete"):
        return ""
    hexes = re.sub(r"[^0-9a-f]", "", s)
    if len(hexes) == 12:
        return ":".join(hexes[i:i + 2] for i in range(0, 12, 2))
    return s


def merge_endpoints(arp: Any, mac_table: Any,
                    interfaces: Any = None) -> List[Dict[str, Any]]:
    """Join the ARP/user-table (IP↔MAC) with the MAC/bridge table (MAC↔VLAN) on
    MAC → a de-duplicated endpoint list ``[{mac, ip, vlan, interface}]``.

    On an ArubaOS gateway this fuses ``show user-table`` (IP + MAC) with
    ``show datapath bridge table`` (MAC + VLAN + dest) so every connected client
    shows up once with its IP, MAC and VLAN — the "unique MAC/IP" view. Works for
    any device type: a MAC seen only in ARP (no bridge entry) or only in the MAC
    table (no IP yet) is still emitted, just with the missing field blank. VLAN
    from the MAC table wins; a MAC's interface is taken from whichever table has
    a non-empty value (MAC/bridge dest preferred).
    """
    by_mac: Dict[str, Dict[str, Any]] = {}

    def _slot(mac: str) -> Dict[str, Any]:
        return by_mac.setdefault(mac, {"mac": mac, "ip": "", "vlan": "",
                                       "os": "", "interface": ""})

    for r in (mac_table or []):
        mac = _norm_mac(r.get("mac"))
        if not mac:
            continue
        s = _slot(mac)
        if r.get("vlan"):
            s["vlan"] = str(r["vlan"])
        if r.get("interface") and not s["interface"]:
            s["interface"] = str(r["interface"])

    for r in (arp or []):
        mac = _norm_mac(r.get("mac"))
        if not mac:
            continue
        s = _slot(mac)
        if r.get("ip") and not s["ip"]:
            s["ip"] = str(r["ip"])
        if r.get("vlan") and not s["vlan"]:
            s["vlan"] = str(r["vlan"])
        if r.get("os") and not s["os"]:
            s["os"] = str(r["os"])
        if r.get("interface") and not s["interface"]:
            s["interface"] = str(r["interface"])

    return sorted(by_mac.values(),
                  key=lambda e: (e["vlan"] or "~", e["ip"] or "~", e["mac"]))


def summarize_vlans(endpoints: Any, interfaces: Any = None) -> List[Dict[str, Any]]:
    """Roll the merged endpoints (+ any L3 interfaces) into a per-VLAN summary
    ``[{vlan, endpoints, macs, ips, gateway_ip}]`` for the VLANs tab."""
    vlans: Dict[str, Dict[str, Any]] = {}

    def _slot(vlan: str) -> Dict[str, Any]:
        return vlans.setdefault(vlan, {"vlan": vlan, "endpoints": 0,
                                       "macs": 0, "ips": 0, "gateway_ip": ""})

    for e in (endpoints or []):
        vlan = str(e.get("vlan") or "").strip()
        if not vlan:
            continue
        s = _slot(vlan)
        s["endpoints"] += 1
        if e.get("mac"):
            s["macs"] += 1
        if e.get("ip"):
            s["ips"] += 1

    # An SVI/L3 interface named like "vlan42" contributes the gateway IP.
    for i in (interfaces or []):
        name = str(i.get("name") or i.get("vlan") or "")
        m = re.search(r"(\d{1,4})", name)
        if m and i.get("ip"):
            s = _slot(m.group(1))
            if not s["gateway_ip"]:
                s["gateway_ip"] = str(i["ip"])

    return sorted(vlans.values(), key=lambda v: (int(v["vlan"])
                  if v["vlan"].isdigit() else 9999, v["vlan"]))


def enrich_vlans(native_rows: Any, endpoints: Any,
                 interfaces: Any = None) -> List[Dict[str, Any]]:
    """Enrich the authoritative ``show vlan`` list with live counts. Each native
    VLAN ``{vlan, name, ports}`` is annotated with endpoint/mac/ip counts (from
    the fused user-table+bridge endpoints) and a gateway IP (from an SVI-named
    L3 interface). Keeps every VLAN from ``show vlan`` (incl. empty ones), which
    a purely endpoint-derived rollup would miss."""
    counts = {v["vlan"]: v for v in summarize_vlans(endpoints, interfaces)}
    out = []
    for r in (native_rows or []):
        vid = str(r.get("vlan") or "").strip()
        if not vid:
            continue
        c = counts.get(vid, {})
        out.append({"vlan": vid, "name": r.get("name", ""),
                    "ports": r.get("ports", ""),
                    "endpoints": c.get("endpoints", 0), "macs": c.get("macs", 0),
                    "ips": c.get("ips", 0), "gateway_ip": c.get("gateway_ip", "")})
    return out


def _err(message: str, data: Any = None) -> Dict[str, Any]:
    return {"status": "ERROR", "data": data if data is not None else [],
            "message": message}


class _SharedTransportSession:
    """One live transport session (SSH PTY / httpx client) shared by all the
    datum calls of a single poll or composite fetch, with a single reconnect
    credit: if the session dies mid-poll the next datum call reconnects once;
    after that, failures degrade to per-datum ERROR envelopes."""

    def __init__(self, factory):
        self._factory = factory
        self.session = None
        self._reconnect_used = False

    async def open(self) -> None:
        s = self._factory()
        try:
            await s.connect()
        except Exception:
            try:
                await s.close()
            except Exception:
                pass
            raise
        self.session = s

    async def reconnect_once(self) -> bool:
        """One-shot reconnect after a mid-poll session death. True when a fresh
        session is live; False when the credit is spent or the reconnect itself
        failed (callers then fall back to per-datum handling)."""
        if self._reconnect_used:
            return False
        self._reconnect_used = True
        await self.close()
        try:
            await self.open()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        s, self.session = self.session, None
        if s is not None:
            try:
                await s.close()
            except Exception:
                pass


class NwDriver:
    """Abstract base for a per-device driver. Transport subclasses implement the
    IO; the base provides transport resolution + envelope helpers."""

    transport = "base"
    # Transports with a costly per-connection handshake (SSH banner/no-page/
    # enable, REST TLS+auth) set this True so poll/composite fetches open ONE
    # session per device and run their commands over it sequentially (some
    # devices also cap concurrent VTYs). SNMP stays False: sessions are cheap
    # and its datums may still run concurrently.
    shares_session = False

    def __init__(self, device: Dict[str, Any]):
        self.device = device
        self.device_id = device.get("id", "")
        self.object_type = device.get("object_type", "")
        self.address = device.get("address", "")
        self.transport = self._resolve_transport(device)
        self._shared: Optional[_SharedTransportSession] = None

    @asynccontextmanager
    async def session_scope(self):
        """Scope within which the datum methods reuse ONE transport session
        instead of opening a fresh one per call. No-op for transports whose
        sessions are cheap (``shares_session`` False); reentrant, so nested
        composite fetches reuse the outer scope's session. A failed connect
        degrades silently to the legacy per-call sessions so the per-datum
        ERROR-envelope contract is unchanged."""
        if not self.shares_session or self._shared is not None:
            yield
            return
        shared = _SharedTransportSession(self._session)
        try:
            await shared.open()
            self._shared = shared
        except Exception as e:
            logger.debug("shared %s session %s: %s (per-call fallback)",
                         self.transport, self.address, e)
        try:
            yield
        finally:
            self._shared = None
            await shared.close()

    async def _run_on_shared(self, fn):
        """Run one datum ``fn`` over the active shared session, reconnecting
        once if the session died mid-poll. Caller checks ``self._shared`` (and
        its ``.session``) first; raises on failure so the caller's existing
        except clauses build the usual ERROR envelope."""
        shared = self._shared
        try:
            return await fn(shared.session, self.object_type)
        except Exception:
            if not await shared.reconnect_once():
                raise
            return await fn(shared.session, self.object_type)

    @staticmethod
    def _resolve_transport(device: Dict[str, Any]) -> str:
        t = (device.get("transport") or "auto").strip().lower()
        if t not in _VALID_TRANSPORTS:
            t = "auto"
        if t == "auto":
            t = _DEFAULT_TRANSPORT.get(device.get("object_type", ""), "ssh")
        return t

    def _log(self, method: str, extra: str = "") -> None:
        tag = f"[{self.object_type}/{self.transport}] {method} on {self.address}"
        logger.info(f"{tag} {extra}".rstrip())

    def _ok(self, data: Any, message: str = "") -> Dict[str, Any]:
        return {"status": "SUCCESS", "data": data, "message": message}

    # ── Datum methods (transport subclasses override) ────────────────────────
    async def probe(self) -> Dict[str, Any]:
        return _err("probe not implemented for this transport",
                    {"reachable": False, "latency_ms": 0})

    async def get_device_info(self) -> Dict[str, Any]:
        return _err("device info not implemented for this transport")

    async def get_mac_table(self) -> Dict[str, Any]:
        return _err("mac table not implemented for this transport")

    async def get_arp(self) -> Dict[str, Any]:
        return _err("arp not implemented for this transport")

    async def get_interfaces(self) -> Dict[str, Any]:
        return _err("interfaces not implemented for this transport")

    async def get_vlans(self) -> Dict[str, Any]:
        # Native VLAN list (`show vlan`). Only the CLI driver implements it; other
        # transports degrade so the engine falls back to endpoint-derived VLANs.
        return _err("vlans not implemented for this transport")

    async def run_config(self, commands: List[str]) -> Dict[str, Any]:
        # TODO(phase3): push CLI/REST config changes. Out of scope for the
        # polling work — returns a clear not-implemented envelope so a caller
        # doesn't mistake silence for success.
        self._log("config", f"commands={len(commands or [])}")
        return {"status": "ERROR",
                "applied": [],
                "errors": ["run_config not implemented for this transport"],
                "message": "config apply: not implemented"}


class SnmpDriver(NwDriver):
    """SNMPv2c driver (pysnmp). Standard MIBs work across all four families."""
    transport = "snmp"

    def _session(self):
        from transports import snmp_io
        return snmp_io.SnmpSession(self.device)

    async def probe(self) -> Dict[str, Any]:
        from transports import snmp_io
        try:
            s = self._session()
            res = await snmp_io.snmp_probe(s)
            return self._ok(res)
        except Exception as e:
            return _err(f"snmp probe {self.address}: {e}",
                        {"reachable": False, "latency_ms": 0})

    async def get_device_info(self) -> Dict[str, Any]:
        from transports import snmp_io
        try:
            return self._ok(await snmp_io.snmp_get_device_info(self._session()))
        except Exception as e:
            return _err(f"snmp device info {self.address}: {e}")

    async def get_interfaces(self) -> Dict[str, Any]:
        from transports import snmp_io
        try:
            rows = await snmp_io.snmp_get_interfaces(self._session())
            return self._ok(rows)
        except Exception as e:
            return _err(f"snmp interfaces {self.address}: {e}")

    async def get_arp(self) -> Dict[str, Any]:
        from transports import snmp_io
        try:
            s = self._session()
            # Map ifIndex → name so the ARP rows carry a friendly interface.
            iftable = snmp_io.parse_iftable(
                await snmp_io._to_thread(s.walk, snmp_io.IF_PREFIX))
            rows = await snmp_io.snmp_get_arp(s, ifaces=iftable)
            return self._ok(rows)
        except Exception as e:
            return _err(f"snmp arp {self.address}: {e}")

    async def get_mac_table(self) -> Dict[str, Any]:
        from transports import snmp_io
        try:
            s = self._session()
            iftable = snmp_io.parse_iftable(
                await snmp_io._to_thread(s.walk, snmp_io.IF_PREFIX))
            rows = await snmp_io.snmp_get_mac_table(s, ifaces=iftable)
            return self._ok(rows)
        except Exception as e:
            return _err(f"snmp mac table {self.address}: {e}")


class SshCliDriver(NwDriver):
    """SSH / CLI driver (asyncssh + per-vendor text parsers). One interactive
    PTY session per call — or ONE session for a whole poll/composite fetch when
    called inside :meth:`session_scope` (connect, disable paging, then run all
    the vendor show commands over it sequentially). ``enable_secret`` enters
    enable mode on AOS-S."""
    transport = "ssh"
    shares_session = True

    def _session(self):
        from transports import cli_io
        return cli_io.CliSession(self.device)

    async def _with_session(self, fn) -> Dict[str, Any]:
        from transports import cli_io
        try:
            if self._shared is not None and self._shared.session is not None:
                return self._ok(await self._run_on_shared(fn))
            async with self._session() as s:
                rows = await fn(s, self.object_type)
                return self._ok(rows)
        except cli_io.CliError as e:
            return _err(f"cli {self.address}: {e}")
        except Exception as e:
            return _err(f"cli {self.address}: {e}")

    async def probe(self) -> Dict[str, Any]:
        from transports import cli_io
        import time
        t0 = time.monotonic()
        try:
            if self._shared is not None and self._shared.session is not None:
                # Shared-session poll: reachability == one command round-trip
                # (the connect already happened when the scope opened).
                await self._run_on_shared(
                    lambda s, _ot: s.run("show version"))
            else:
                async with self._session() as s:
                    # A successful connection + one command == reachable.
                    await s.run("show version")
            return self._ok({"reachable": True,
                             "latency_ms": int((time.monotonic() - t0) * 1000)})
        except cli_io.CliError as e:
            return _err(f"cli probe {self.address}: {e}",
                        {"reachable": False, "latency_ms": 0})
        except Exception as e:
            return _err(f"cli probe {self.address}: {e}",
                        {"reachable": False, "latency_ms": 0})

    async def get_device_info(self) -> Dict[str, Any]:
        from transports import cli_io
        return await self._with_session(cli_io.cli_get_device_info)

    async def get_arp(self) -> Dict[str, Any]:
        from transports import cli_io
        return await self._with_session(cli_io.cli_get_arp)

    async def get_mac_table(self) -> Dict[str, Any]:
        from transports import cli_io
        return await self._with_session(cli_io.cli_get_mac_table)

    async def get_interfaces(self) -> Dict[str, Any]:
        from transports import cli_io
        return await self._with_session(cli_io.cli_get_interfaces)

    async def get_vlans(self) -> Dict[str, Any]:
        from transports import cli_io
        return await self._with_session(cli_io.cli_get_vlans)

    async def install_cert(self, fullchain: str, privkey: str, chain: str,
                           domain: str) -> Dict[str, Any]:
        """Install an LE server cert over SSH. Implemented for the ArubaOS
        gateway/controller (PKCS#12 + SCP + crypto pki-import + web-server bind);
        other CLI object_types have no external-key import path."""
        from transports import cli_io
        if self.object_type != "gateway":
            return _err(f"CLI cert install not implemented for object_type "
                        f"'{self.object_type}'")
        try:
            async with self._session() as s:
                return await cli_io.cli_install_cert_gateway(
                    s, fullchain, privkey, chain, domain)
        except cli_io.CliError as e:
            return _err(f"cli {self.address}: {e}")
        except Exception as e:  # noqa: BLE001
            return _err(f"cli {self.address}: {e}")


class RestDriver(NwDriver):
    """REST driver (httpx). AOS-CX RESTv1 (basic auth) + Aruba/HPE gateway REST
    (bearer token). TLS verify controlled by ``LM_NW_VERIFY_TLS`` (default off).
    Inside :meth:`session_scope` (poll/composite fetches) one httpx client is
    reused for every GET instead of a fresh client (TLS handshake) per datum."""
    transport = "rest"
    shares_session = True

    def _session(self):
        from transports import rest_io
        return rest_io.RestSession(self.device)

    async def _with_session(self, fn) -> Dict[str, Any]:
        from transports import rest_io
        try:
            if self._shared is not None and self._shared.session is not None:
                return self._ok(await self._run_on_shared(fn))
            async with self._session() as s:
                rows = await fn(s, self.object_type)
                return self._ok(rows)
        except rest_io.RestError as e:
            return _err(f"rest {self.address}: {e}")
        except Exception as e:
            return _err(f"rest {self.address}: {e}")

    async def probe(self) -> Dict[str, Any]:
        import time
        t0 = time.monotonic()
        try:
            if self._shared is not None and self._shared.session is not None:
                await self._run_on_shared(rest_get_device_info)
            else:
                async with self._session() as s:
                    await rest_get_device_info(s, self.object_type)
            return self._ok({"reachable": True,
                             "latency_ms": int((time.monotonic() - t0) * 1000)})
        except Exception as e:
            return _err(f"rest probe {self.address}: {e}",
                        {"reachable": False, "latency_ms": 0})

    async def get_device_info(self) -> Dict[str, Any]:
        from transports import rest_io
        return await self._with_session(rest_io.rest_get_device_info)

    async def get_arp(self) -> Dict[str, Any]:
        from transports import rest_io
        return await self._with_session(rest_io.rest_get_arp)

    async def get_mac_table(self) -> Dict[str, Any]:
        from transports import rest_io
        return await self._with_session(rest_io.rest_get_mac_table)

    async def get_interfaces(self) -> Dict[str, Any]:
        from transports import rest_io
        return await self._with_session(rest_io.rest_get_interfaces)

    async def install_cert(self, fullchain: str, privkey: str, chain: str,
                           domain: str) -> Dict[str, Any]:
        """Install a CA-signed cert on this REST device (AOS-CX today) and bind
        it to the HTTPS server. Own try/except (not ``_with_session``) so the
        envelope carries a clear install message rather than a datum row
        count. The cert material + key are pushed inline (no SCP); see
        :func:`transports.rest_io.rest_install_cert` for the sequence."""
        from transports import rest_io
        try:
            async with self._session() as s:
                data = await rest_io.rest_install_cert(
                    s, self.object_type, fullchain, privkey, chain, domain)
            msg = (f"cert '{data.get('cert_name')}' installed on "
                   f"{self.address} ({data.get('service')})")
            self._log("install_cert",
                      f"cert={data.get('cert_name')} prefix={data.get('rest_prefix')}")
            return self._ok(data, message=msg)
        except rest_io.RestError as e:
            return _err(f"rest install_cert {self.address}: {e}")
        except Exception as e:
            return _err(f"rest install_cert {self.address}: {e}")


# Forward ref for RestDriver.probe (defined above in the class body via the
# rest_io helper; keep a module-level alias for clarity).
async def rest_get_device_info(session, object_type):
    from transports import rest_io
    return await rest_io.rest_get_device_info(session, object_type)


_TRANSPORT_CLASSES = {
    "ssh": SshCliDriver,
    "rest": RestDriver,
    "snmp": SnmpDriver,
}


@asynccontextmanager
async def _null_scope():
    """No-op async scope for drivers without ``session_scope`` (test fakes)."""
    yield


def build_driver(device: Dict[str, Any]) -> Optional[NwDriver]:
    """Build the right driver for a device dict (or None for unknown type)."""
    object_type = (device.get("object_type") or "").strip().lower()
    if object_type not in _VALID_OBJECT_TYPES:
        logger.warning(f"Unknown object_type {object_type!r} for device "
                       f"{device.get('id')} — skipped")
        return None
    transport = NwDriver._resolve_transport(device)
    cls = _TRANSPORT_CLASSES.get(transport, SshCliDriver)
    return cls(device)


class NwEngine:
    """Core interaction layer for the managed network-device fleet.

    Holds the device list pushed by the hub (``set_devices``) and dispatches
    per-device commands to the appropriate driver. Stateless across commands
    apart from the cached fleet — each command resolves the device + builds a
    fresh driver (drivers are cheap; real connections are per-call).
    """

    def __init__(self, devices: Optional[List[Dict[str, Any]]] = None):
        self.devices: List[Dict[str, Any]] = list(devices or [])
        # The shared tenant id (set via UPDATE_CONFIG from the hub) — a device
        # whose ``tenant_id`` equals this is visible to ALL tenants (matches the
        # hub's shared-tenant-flag invariant). Empty when the hub hasn't pushed
        # it yet (legacy) → a ``tenant`` filter then matches only own-tenant.
        self.shared_tenant_id: str = ""

    def set_devices(self, devices: List[Dict[str, Any]],
                    shared_tenant_id: str = "") -> None:
        self.devices = list(devices or [])
        self.shared_tenant_id = shared_tenant_id or ""
        types = {}
        for d in self.devices:
            ot = (d.get("object_type") or "unknown")
            types[ot] = types.get(ot, 0) + 1
        logger.info(f"NwEngine fleet updated: {len(self.devices)} device(s) "
                    f"by type={types}")

    def _tenant_matches(self, device: Dict[str, Any],
                        tenant: Optional[str]) -> bool:
        """True if ``device`` is visible to ``tenant``: own-tenant or the
        shared tenant. ``tenant`` None/empty → no filter (whole fleet, today's
        behavior — backward-compatible with a hub that doesn't pass a tenant)."""
        if not tenant:
            return True
        dt = (device or {}).get("tenant_id", "")
        return dt == tenant or (bool(self.shared_tenant_id)
                                and dt == self.shared_tenant_id)

    def _get_device(self, device_id: str,
                    tenant: Optional[str] = None) -> Optional[Dict[str, Any]]:
        for d in self.devices:
            if d.get("id") == device_id and self._tenant_matches(d, tenant):
                return d
        return None

    def _driver_for(self, device_id: str,
                    tenant: Optional[str] = None) -> Optional[NwDriver]:
        d = self._get_device(device_id, tenant)
        if not d:
            return None
        return build_driver(d)

    # ── Logging: standard per-datum outcome trail ───────────────────────────
    @staticmethod
    def _log_datum(method: str, drv: "NwDriver", res: Dict[str, Any],
                    detail: Optional[str] = None) -> None:
        """Log a per-datum outcome in the standard module form: INFO on success
        (with a row count), ERROR on failure. The ERROR line carries the word
        "error" so it surfaces in the hub's GET_ERROR_LOGS / Error Log tab —
        same precedent as the opnsense engine's ``logger.error`` on API failure
        and the hub sync loops' ``[sync-error]`` marker (one place to go, no
        spoke-log dig). Best-effort: logging never raises — a transport failure
        is still returned in the envelope regardless."""
        try:
            status = str((res or {}).get("status", "")).upper()
            msg = (res or {}).get("message", "") or "transport failure"
            if detail is None:
                data = (res or {}).get("data")
                detail = f"{len(data)} rows" if isinstance(data, list) else "ok"
            tag = (f"[{getattr(drv, 'object_type', '')}/"
                   f"{getattr(drv, 'transport', '')}] "
                   f"{method} {getattr(drv, 'address', '')}")
            if status == "SUCCESS":
                logger.info("nw %s -> %s", tag, detail)
            else:
                logger.error("nw %s -> error: %s", tag, msg)
        except Exception:
            logger.debug("nw log_datum %s failed", method, exc_info=True)

    # ── Fleet ───────────────────────────────────────────────────────────────
    async def list_devices(self, tenant: Optional[str] = None) -> Dict[str, Any]:
        """Fleet summary (no credentials) with live reachability via a
        concurrent lightweight probe per device (3s timeout each). Falls back
        to ``unknown`` on probe error so the UI never shows a stale 'up'.

        ``tenant`` (optional) scopes the returned rows to that tenant's own
        devices + the shared tenant (defense-in-depth: the hub already gates
        by the device record's ``tenant_id``). Omit → whole fleet (today's
        behavior). Each row carries ``tenant_id`` + ``shared`` so the hub can
        re-filter its cached envelope on serve."""
        fleet = [d for d in self.devices if self._tenant_matches(d, tenant)]
        rows = []
        async def _probe_row(d):
            drv = build_driver(d)
            rcell = {"reachable": None, "latency_ms": None}
            if drv:
                try:
                    pr = await asyncio.wait_for(drv.probe(), timeout=3.0)
                    if pr.get("status") == "SUCCESS":
                        rcell.update(pr.get("data") or {})
                    else:
                        # Per-device probe failure — log once at WARNING (not
                        # ERROR: a fleet list isn't a sync push, and one down
                        # device among many is normal). The probe's own datum
                        # log already fired inside drv.probe().
                        logger.warning("nw probe %s during fleet list: %s",
                                       getattr(drv, "address", ""),
                                       pr.get("message", "probe failed"))
                except (asyncio.TimeoutError, Exception) as e:
                    rcell = {"reachable": False, "latency_ms": None}
                    logger.warning("nw probe %s during fleet list: %s",
                                   getattr(drv, "address", ""), e)
            return {
                "id": d.get("id", ""),
                "name": d.get("name", ""),
                "object_type": d.get("object_type", ""),
                "address": d.get("address", ""),
                "transport": NwDriver._resolve_transport(d),
                "reachable": rcell.get("reachable"),
                "latency_ms": rcell.get("latency_ms"),
                "tenant_id": d.get("tenant_id", ""),
                "shared": bool(self.shared_tenant_id
                               and d.get("tenant_id", "") == self.shared_tenant_id),
            }
        rows = await asyncio.gather(*(_probe_row(d) for d in fleet))
        up = sum(1 for r in rows if r.get("reachable") is True)
        down = sum(1 for r in rows if r.get("reachable") is False)
        logger.info("nw list_devices -> %d device(s): %d reachable, %d unreachable, "
                    "%d unknown", len(rows), up, down, len(rows) - up - down)
        return {"status": "SUCCESS", "data": list(rows)}

    # ── Per-device passthroughs ─────────────────────────────────────────────
    async def probe(self, device_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw probe: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        res = await drv.probe()
        pd = res.get("data") if isinstance(res.get("data"), dict) else {}
        self._log_datum("probe", drv, res,
                        detail=f"reachable={pd.get('reachable')} "
                               f"latency={pd.get('latency_ms')}ms")
        return res

    async def get_device_info(self, device_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw get_device_info: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        res = await drv.get_device_info()
        self._log_datum("device_info", drv, res)
        return res

    async def get_mac_table(self, device_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw get_mac_table: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        # Augment the MAC table with the client/ARP table so each MAC carries its
        # IP: gateway = `show user-table` ⋈ `show datapath bridge table`;
        # AOS-S = `show arp` (IP+MAC) ⋈ `show mac-address` (MAC+VLAN+port).
        if getattr(drv, "object_type", "") in ("gateway", "aos_switch"):
            async with self._session_scope(drv):  # one session for both datums
                arp = await drv.get_arp()        # user-table / show arp
                mac = await drv.get_mac_table()  # datapath bridge / show mac-address
            self._log_datum("mac_table", drv, mac)
            merged = merge_endpoints(
                arp.get("data") if arp.get("status") == "SUCCESS" else [],
                mac.get("data") if mac.get("status") == "SUCCESS" else [])
            return self._ok_or_partial(merged, [arp, mac], "mac(s)")
        res = await drv.get_mac_table()
        self._log_datum("mac_table", drv, res)
        return res

    async def get_arp(self, device_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw get_arp: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        res = await drv.get_arp()
        self._log_datum("arp", drv, res)
        return res

    async def get_interfaces(self, device_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw get_interfaces: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        res = await drv.get_interfaces()
        self._log_datum("interfaces", drv, res)
        return res

    async def get_endpoints(self, device_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        """Unified endpoint list: gather ARP + MAC table (+ interfaces) and fuse
        on MAC → ``[{mac, ip, vlan, interface}]`` (the "IP Addresses" view). On a
        gateway this joins ``show user-table`` with ``show datapath bridge table``."""
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw get_endpoints: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        arp, mac, ifs = await self._gather_endpoint_datums(drv)
        self._log_datum("endpoints", drv, arp)
        eps = merge_endpoints(
            arp.get("data") if arp.get("status") == "SUCCESS" else [],
            mac.get("data") if mac.get("status") == "SUCCESS" else [],
            ifs.get("data") if ifs.get("status") == "SUCCESS" else [])
        return self._ok_or_partial(eps, [arp, mac], "endpoint(s)")

    async def get_vlans(self, device_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        """VLANs from the authoritative ``show vlan`` list, enriched with live
        endpoint/mac/ip counts → ``[{vlan, name, ports, endpoints, macs, ips,
        gateway_ip}]`` (the "VLANs" view). Falls back to an endpoint-derived
        rollup when the device/transport has no native ``show vlan``."""
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw get_vlans: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        async with self._session_scope(drv):
            native = await drv.get_vlans()
            arp, mac, ifs = await self._gather_endpoint_datums(drv)
        self._log_datum("vlans", drv, native)
        ifdata = ifs.get("data") if ifs.get("status") == "SUCCESS" else []
        eps = merge_endpoints(
            arp.get("data") if arp.get("status") == "SUCCESS" else [],
            mac.get("data") if mac.get("status") == "SUCCESS" else [], ifdata)
        native_rows = native.get("data") if native.get("status") == "SUCCESS" else None
        if native_rows:
            return self._ok_or_partial(enrich_vlans(native_rows, eps, ifdata),
                                       [native, arp, mac], "vlan(s)")
        # No native `show vlan` (non-gateway / transport w/o CLI) → derive.
        return self._ok_or_partial(summarize_vlans(eps, ifdata), [arp, mac, ifs],
                                   "vlan(s)")

    @staticmethod
    def _session_scope(drv):
        """The driver's shared-session scope (ONE connection for all the datum
        calls inside it); a no-op scope for drivers without one (test fakes) or
        with cheap sessions (SNMP)."""
        scope = getattr(drv, "session_scope", None)
        if callable(scope):
            return scope()
        return _null_scope()

    @staticmethod
    async def _gather_endpoint_datums(drv):
        """Fetch ARP + MAC + interfaces for the fused endpoint/VLAN views. On a
        session-sharing transport (SSH/REST) the three run SEQUENTIALLY over ONE
        shared session — the handshake is the expensive part, and some devices
        cap concurrent VTY sessions. On SNMP (sessions cheap) they still run
        concurrently. A datum that raises is coerced to an ERROR envelope so
        the merge still runs on whatever succeeded."""
        async def _safe(coro):
            try:
                return await coro
            except Exception as e:  # noqa: BLE001 - degrade to ERROR envelope
                return _err(str(e))
        if getattr(drv, "shares_session", False):
            async with NwEngine._session_scope(drv):
                return (await _safe(drv.get_arp()),
                        await _safe(drv.get_mac_table()),
                        await _safe(drv.get_interfaces()))
        return await asyncio.gather(_safe(drv.get_arp()),
                                    _safe(drv.get_mac_table()),
                                    _safe(drv.get_interfaces()))

    @staticmethod
    def _ok_or_partial(data, sources, noun):
        """SUCCESS envelope; downgrade to PARTIAL (still returning ``data``) when
        any source datum errored, carrying the first error message."""
        errs = [s.get("message", "failed") for s in sources
                if s.get("status") != "SUCCESS"]
        if errs and not data:
            return _err("; ".join(errs))
        return {"status": "PARTIAL" if errs else "SUCCESS", "data": data,
                "message": f"{len(data)} {noun}" + (f" ({errs[0]})" if errs else "")}

    async def run_config(self, device_id: str, commands: List[str],
                         tenant: Optional[str] = None) -> Dict[str, Any]:
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw run_config: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        return await drv.run_config(commands or [])

    async def install_cert(self, device_id: str, fullchain: str, privkey: str,
                           chain: str, domain: str,
                           tenant: Optional[str] = None) -> Dict[str, Any]:
        """Install a hub-delivered LE cert on a fleet device. Dispatches by
        ``object_type``:

        * ``cx_switch`` (AOS-CX, REST) → ``RestDriver.install_cert`` (inline
          cert+key PUT + https-server binding via REST v10).
        * ``aos_switch`` (AOS-S) → ERROR: the switch generates its keypair
          on-device during CSR creation and has no command to import an
          external private key — fundamentally incompatible with the
          ACME/certbot external-key model.
        * ``ex_switch`` / ``gateway`` → ERROR: not yet implemented (Juniper EX
          needs SFTP upload + config-mode; the gateway path is platform-
          dependent SSH/SFTP). The ERROR message names the gap so the hub's
          cert-distribution ledger surfaces it instead of a silent skip.

        The hub addresses a device by ``identifier`` (its fleet ``id``); the
        spoke maps that to ``device_id`` here. Returns the standard envelope."""
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw install_cert: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        ot = (drv.object_type or "").strip().lower()
        if ot == "cx_switch":
            if drv.transport != "rest":
                return _err(f"cx_switch cert install requires the REST transport "
                            f"(device '{device_id}' is '{drv.transport}'; "
                            f"CLI PEM-paste not yet wired)")
            res = await drv.install_cert(fullchain, privkey, chain, domain)
            self._log_datum(
                "install_cert", drv, res,
                detail=res.get("message") if res.get("status") == "SUCCESS" else None)
            return res
        if ot == "aos_switch":
            return _err("AOS-Switch cannot import an external private key "
                        "(on-switch CSR model) — ACME/certbot external key "
                        "is incompatible; generate the CSR on the switch instead")
        if ot == "ex_switch":
            return _err("Juniper EX cert install not yet implemented "
                        "(needs SFTP upload + config-mode plumbing)")
        if ot == "gateway":
            # ArubaOS mobility gateway/controller: PKCS#12 + SCP upload +
            # crypto pki-import + web-server bind (SshCliDriver.install_cert).
            if not hasattr(drv, "install_cert"):
                return _err(f"gateway '{device_id}' driver has no cert-install path "
                            f"(transport '{drv.transport}')")
            res = await drv.install_cert(fullchain, privkey, chain, domain)
            self._log_datum(
                "install_cert", drv, res,
                detail=res.get("message") if res.get("status") == "SUCCESS" else None)
            return res
        return _err(f"cert install not supported for object_type '{ot}'")

    async def install_cert_fleet(self, fullchain: str, privkey: str, chain: str,
                                 domain: str,
                                 tenant: Optional[str] = None) -> Dict[str, Any]:
        """Install a hub-delivered LE cert on the WHOLE fleet (spoke-level cert
        target). Installs on every cert-capable device (``cx_switch`` via AOS-CX
        REST, ``gateway`` via ArubaOS PKCS#12/SCP); other types are reported
        SKIPPED, not failed. Returns an aggregate envelope PLUS a per-device
        ``devices`` list so the hub/WebUI can show which devices got the cert.

        ``tenant`` (optional) scopes the fan-out to that tenant's own + shared
        devices on a multi-tenant spoke (defense-in-depth; the hub already
        resolves per-device cert targets). Omit → whole fleet."""
        results: List[Dict[str, Any]] = []
        ok = fail = 0
        for d in self.devices:
            if not self._tenant_matches(d, tenant):
                continue
            did = d.get("id", "")
            ot = (d.get("object_type") or "").strip().lower()
            name = d.get("name") or d.get("hostname") or did
            ip = d.get("address") or d.get("ip") or d.get("mgmt_ip") or ""
            if ot not in ("cx_switch", "gateway"):
                results.append({"device_id": did, "name": name, "ip": ip,
                                "object_type": ot, "status": "SKIPPED",
                                "message": f"cert install not supported for '{ot or 'unknown'}'"})
                continue
            res = await self.install_cert(did, fullchain, privkey, chain, domain)
            st = res.get("status", "ERROR")
            results.append({"device_id": did, "name": name, "ip": ip,
                            "object_type": ot, "status": st,
                            "message": res.get("message", "")})
            if st == "SUCCESS":
                ok += 1
            else:
                fail += 1
        total = ok + fail
        # The SPOKE now HOLDS the cert (valid material was received + validated by
        # the caller) — that alone is SUCCESS, like a proxmox node holding a cert
        # regardless of per-VM state. Per-switch install outcomes are reported in
        # ``devices`` for the drill-down, but a device failure does NOT fail the
        # spoke-level target (the operator only needs "the spoke has the cert").
        if total == 0:
            message = "cert received by spoke (no cert-installable devices in the fleet)"
        elif fail == 0:
            message = f"cert received; installed on all {ok} switch(es)"
        elif ok == 0:
            message = f"cert received by spoke; install failed on all {fail} switch(es) — see devices"
        else:
            message = f"cert received; installed on {ok}/{total} switch(es), {fail} failed — see devices"
        logger.info("nw install_cert_fleet: spoke holds cert (%d installed, %d failed, %d total)",
                    ok, fail, total)
        return {"status": "SUCCESS", "message": message, "devices": results,
                "installed": ok, "failed": fail, "total": total}

    async def poll(self, device_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        """Run a full poll (probe + device_info + interfaces + arp + mac_table)
        for one device. Each sub-call is independent — a failure on one datum
        doesn't sink the rest; failed datums come back as empty lists + an
        entry in ``errors``. Used by the hub's POLL NOW path."""
        drv = self._driver_for(device_id, tenant)
        if not drv:
            logger.warning("nw poll: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        errors: List[str] = []
        reachable = False
        latency_ms = None

        async def _safe(coro, label):
            r = await coro
            self._log_datum(label, drv, r)
            if r.get("status") == "SUCCESS":
                return r.get("data")
            errors.append(f"{label}: {r.get('message', 'failed')}")
            return [] if label != "device_info" else {}

        # ONE shared transport session (SSH/REST) for the whole poll — probe +
        # every datum runs over it sequentially instead of a full handshake per
        # sub-call. Each call still fails independently (ERROR envelope), and a
        # session that dies mid-poll gets one reconnect.
        async with self._session_scope(drv):
            pr = await drv.probe()
            if pr.get("status") == "SUCCESS":
                pd = pr.get("data") or {}
                reachable = bool(pd.get("reachable"))
                latency_ms = pd.get("latency_ms")
                self._log_datum("probe", drv, pr,
                                detail=f"reachable={reachable} latency={latency_ms}ms")
            else:
                self._log_datum("probe", drv, pr)
                errors.append(f"probe: {pr.get('message', 'failed')}")

            device_info = await _safe(drv.get_device_info(), "device_info")
            interfaces = await _safe(drv.get_interfaces(), "interfaces")
            arp = await _safe(drv.get_arp(), "arp")
            # ``bridge`` = the raw datapath/native MAC table; the fielded
            # ``mac_table`` for a gateway is user-table+bridge fused (matches
            # the MAC Table tab).
            bridge = await _safe(drv.get_mac_table(), "mac_table")

            is_gw = getattr(drv, "object_type", "") == "gateway"
            native_vlans = await _safe(drv.get_vlans(), "vlans") if is_gw else []

        # SUCCESS only when reachable AND no sub-datum errored; else PARTIAL — a
        # reachable device whose info/interfaces/arp/mac probes all failed must not
        # report SUCCESS (the errors[] carry the detail).
        # Fuse ARP/user-table (IP↔MAC) + bridge (MAC↔VLAN) into a unique endpoint
        # list; VLANs come from the authoritative `show vlan`, enriched with the
        # endpoint counts (endpoint-derived rollup is the no-`show vlan` fallback).
        endpoints = merge_endpoints(arp, bridge, interfaces)
        mac_table = endpoints if is_gw else bridge
        vlans = (enrich_vlans(native_vlans, endpoints, interfaces)
                 if native_vlans else summarize_vlans(endpoints, interfaces))

        status = "SUCCESS" if (reachable and not any(errors)) else "PARTIAL"
        n_if = len(interfaces) if isinstance(interfaces, list) else 0
        n_arp = len(arp) if isinstance(arp, list) else 0
        n_mac = len(mac_table) if isinstance(mac_table, list) else 0
        logger.info("nw poll %s -> status=%s reachable=%s interfaces=%d arp=%d "
                    "mac=%d errors=%d", getattr(drv, "address", ""), status,
                    reachable, n_if, n_arp, n_mac, len(errors))
        return {
            "status": status,
            "data": {
                "reachable": reachable,
                "latency_ms": latency_ms,
                "device_info": device_info,
                "interfaces": interfaces,
                "arp": arp,
                "mac_table": mac_table,
                "endpoints": endpoints,
                "vlans": vlans,
            },
            "errors": errors,
            "message": (f"reachable={reachable}, "
                        f"{n_if} interface(s), "
                        f"{n_arp} arp, "
                        f"{n_mac} mac"
                        + (f", errors={len(errors)}" if errors else "")),
        }