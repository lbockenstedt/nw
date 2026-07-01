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


def _err(message: str, data: Any = None) -> Dict[str, Any]:
    return {"status": "ERROR", "data": data if data is not None else [],
            "message": message}


class NwDriver:
    """Abstract base for a per-device driver. Transport subclasses implement the
    IO; the base provides transport resolution + envelope helpers."""

    transport = "base"

    def __init__(self, device: Dict[str, Any]):
        self.device = device
        self.device_id = device.get("id", "")
        self.object_type = device.get("object_type", "")
        self.address = device.get("address", "")
        self.transport = self._resolve_transport(device)

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
    PTY session per call: connect, disable paging, run the vendor show commands,
    parse the text. ``enable_secret`` enters enable mode on AOS-S."""
    transport = "ssh"

    def _session(self):
        from transports import cli_io
        return cli_io.CliSession(self.device)

    async def _with_session(self, fn) -> Dict[str, Any]:
        from transports import cli_io
        try:
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


class RestDriver(NwDriver):
    """REST driver (httpx). AOS-CX RESTv1 (basic auth) + Aruba/HPE gateway REST
    (bearer token). TLS verify controlled by ``LM_NW_VERIFY_TLS`` (default off)."""
    transport = "rest"

    def _session(self):
        from transports import rest_io
        return rest_io.RestSession(self.device)

    async def _with_session(self, fn) -> Dict[str, Any]:
        from transports import rest_io
        try:
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

    def set_devices(self, devices: List[Dict[str, Any]]) -> None:
        self.devices = list(devices or [])
        types = {}
        for d in self.devices:
            ot = (d.get("object_type") or "unknown")
            types[ot] = types.get(ot, 0) + 1
        logger.info(f"NwEngine fleet updated: {len(self.devices)} device(s) "
                    f"by type={types}")

    def _get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        for d in self.devices:
            if d.get("id") == device_id:
                return d
        return None

    def _driver_for(self, device_id: str) -> Optional[NwDriver]:
        d = self._get_device(device_id)
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
    async def list_devices(self) -> Dict[str, Any]:
        """Fleet summary (no credentials) with live reachability via a
        concurrent lightweight probe per device (2s timeout each). Falls back
        to ``unknown`` on probe error so the UI never shows a stale 'up'."""
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
            }
        rows = await asyncio.gather(*(_probe_row(d) for d in self.devices))
        up = sum(1 for r in rows if r.get("reachable") is True)
        down = sum(1 for r in rows if r.get("reachable") is False)
        logger.info("nw list_devices -> %d device(s): %d reachable, %d unreachable, "
                    "%d unknown", len(rows), up, down, len(rows) - up - down)
        return {"status": "SUCCESS", "data": list(rows)}

    # ── Per-device passthroughs ─────────────────────────────────────────────
    async def probe(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            logger.warning("nw probe: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        res = await drv.probe()
        pd = res.get("data") if isinstance(res.get("data"), dict) else {}
        self._log_datum("probe", drv, res,
                        detail=f"reachable={pd.get('reachable')} "
                               f"latency={pd.get('latency_ms')}ms")
        return res

    async def get_device_info(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            logger.warning("nw get_device_info: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        res = await drv.get_device_info()
        self._log_datum("device_info", drv, res)
        return res

    async def get_mac_table(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            logger.warning("nw get_mac_table: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        res = await drv.get_mac_table()
        self._log_datum("mac_table", drv, res)
        return res

    async def get_arp(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            logger.warning("nw get_arp: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        res = await drv.get_arp()
        self._log_datum("arp", drv, res)
        return res

    async def get_interfaces(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            logger.warning("nw get_interfaces: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        res = await drv.get_interfaces()
        self._log_datum("interfaces", drv, res)
        return res

    async def run_config(self, device_id: str, commands: List[str]) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            logger.warning("nw run_config: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        return await drv.run_config(commands or [])

    async def poll(self, device_id: str) -> Dict[str, Any]:
        """Run a full poll (probe + device_info + interfaces + arp + mac_table)
        for one device. Each sub-call is independent — a failure on one datum
        doesn't sink the rest; failed datums come back as empty lists + an
        entry in ``errors``. Used by the hub's POLL NOW path."""
        drv = self._driver_for(device_id)
        if not drv:
            logger.warning("nw poll: device %s not in fleet", device_id)
            return _err(f"Device {device_id} not found")
        errors: List[str] = []
        reachable = False
        latency_ms = None

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

        async def _safe(coro, label):
            r = await coro
            self._log_datum(label, drv, r)
            if r.get("status") == "SUCCESS":
                return r.get("data")
            errors.append(f"{label}: {r.get('message', 'failed')}")
            return [] if label != "device_info" else {}

        device_info = await _safe(drv.get_device_info(), "device_info")
        interfaces = await _safe(drv.get_interfaces(), "interfaces")
        arp = await _safe(drv.get_arp(), "arp")
        mac_table = await _safe(drv.get_mac_table(), "mac_table")

        status = "SUCCESS" if reachable else ("PARTIAL" if any(errors) else "SUCCESS")
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
            },
            "errors": errors,
            "message": (f"reachable={reachable}, "
                        f"{n_if} interface(s), "
                        f"{n_arp} arp, "
                        f"{n_mac} mac"
                        + (f", errors={len(errors)}" if errors else "")),
        }