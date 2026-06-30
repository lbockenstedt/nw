"""Network Devices engine + pluggable device drivers.

The engine holds the fleet of managed devices (pushed by the hub via
UPDATE_CONFIG) and, for each command, resolves the target device and delegates
to a **driver** chosen by the device's ``object_type`` and ``transport``.

PHASE 1: device IO is **stubbed**. Every driver method logs its intent and
returns a structured ``SUCCESS`` placeholder so the full hub → spoke → UI →
NetBox pipeline is exercisable end-to-end without real devices. Real
SSH/REST/SNMP implementations land in phase 2 — see the ``# TODO(phase2)``
markers. Credentials are NEVER logged (the spoke masks them in handle_command
before logging; the engine itself only logs address + object_type + transport).

Conforms to the standard result envelope used across LM spokes:
``{"status": "SUCCESS"|"ERROR", "data": [...], "message": "..."}``.
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NwEngine")

# ── Vendor command reference (logged by the stubs; used by phase-2 drivers) ──
# Per object_type: the canonical CLI/REST queries for each datum. Keeping these
# in one table makes the phase-2 implementation a lookup, not a rewrite.
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
# omits it.
_DEFAULT_TRANSPORT: Dict[str, str] = {
    "aos_switch": "ssh",
    "cx_switch":  "rest",
    "ex_switch":  "ssh",
    "gateway":    "rest",
}

_VALID_TRANSPORTS = ("ssh", "rest", "snmp", "auto")
_VALID_OBJECT_TYPES = tuple(_VENDOR_COMMANDS.keys())

# Credential field names — never logged. (handle_command masks these too; this
# is the engine-side guarantee for any future direct logging.)
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


class NwDriver:
    """Abstract base for a per-device driver.

    Phase-1 default implementations are stubs that log + return ``SUCCESS``
    placeholders. Transport subclasses (SSH/CLI, REST, SNMP) override the
    transport-specific mechanics in phase 2; until then they inherit the stubs
    and only customize the logged transport label.
    """

    transport = "base"

    def __init__(self, device: Dict[str, Any]):
        self.device = device
        self.device_id = device.get("id", "")
        self.object_type = device.get("object_type", "")
        self.address = device.get("address", "")
        # Resolved transport (auto → per-type default).
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
        cmds = _VENDOR_COMMANDS.get(self.object_type, {}).get(method, "")
        if cmds:
            logger.info(f"  phase2 query: {cmds}")

    def _ok(self, data: Any, message: str = "") -> Dict[str, Any]:
        return {"status": "SUCCESS", "data": data, "message": message}

    # ── Datum methods (stubbed) ─────────────────────────────────────────────
    async def probe(self) -> Dict[str, Any]:
        # TODO(phase2): open the transport and verify reachability.
        self._log("probe")
        return self._ok({"reachable": True, "latency_ms": 0})

    async def get_device_info(self) -> Dict[str, Any]:
        # TODO(phase2): fetch model/serial/firmware via the vendor query.
        self._log("info")
        return self._ok({
            "model": f"{self.object_type}-stub",
            "serial": "stub-serial",
            "firmware": "stub-fw",
            "interfaces_count": 0,
        })

    async def get_mac_table(self) -> Dict[str, Any]:
        # TODO(phase2): run the vendor mac-table query + parse.
        self._log("mac")
        return self._ok([], "mac table: stub (phase 2 will populate)")

    async def get_arp(self) -> Dict[str, Any]:
        # TODO(phase2): run the vendor arp query + parse into {ip, mac, interface}.
        self._log("arp")
        return self._ok([], "arp: stub (phase 2 will populate)")

    async def get_interfaces(self) -> Dict[str, Any]:
        # TODO(phase2): enumerate interfaces + IPs + VLANs.
        self._log("if")
        return self._ok([], "interfaces: stub (phase 2 will populate)")

    async def run_config(self, commands: List[str]) -> Dict[str, Any]:
        # TODO(phase2): push the CLI/REST config changes.
        self._log("config", f"commands={len(commands or [])}")
        return {"status": "SUCCESS",
                "applied": list(commands or []),
                "errors": [],
                "message": "config apply: stub (phase 2 will push)"}


class SshCliDriver(NwDriver):
    """SSH / CLI driver (asyncssh). Phase-1: stubbed.

    Phase-2 will use asyncssh to open an interactive shell, page-through paging
    ('no page' on AOS-S / 'set screen-length 0' on Junos), run the vendor
    commands, and parse the text output. enable_secret is used for privileged
    mode where the vendor requires it.
    """
    transport = "ssh"


class RestDriver(NwDriver):
    """REST API driver (httpx). Phase-1: stubbed.

    Phase-2 will use httpx against the vendor REST endpoints (AOS-CX RESTv1,
    Aruba/HPE gateway REST) with api_token bearer auth, TLS verify controlled
    by an LM_NW_VERIFY_TLS env knob (lab devices often self-signed).
    """
    transport = "rest"


class SnmpDriver(NwDriver):
    """SNMP driver (pysnmp). Phase-1: stubbed.

    Phase-2 will walk BRIDGE-MIB (dot1dTpFdbPort → MAC table) + IP-MIB
    (ipNetToPhysicalPhysAddress → ARP) using snmp_community / v3 creds.
    """
    transport = "snmp"


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
    fresh driver (drivers are cheap; real connections are per-call in phase 2).
    """

    def __init__(self, devices: Optional[List[Dict[str, Any]]] = None):
        self.devices: List[Dict[str, Any]] = list(devices or [])

    def set_devices(self, devices: List[Dict[str, Any]]) -> None:
        self.devices = list(devices or [])
        # Log device count + types WITHOUT credentials.
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

    # ── Fleet ───────────────────────────────────────────────────────────────
    async def list_devices(self) -> Dict[str, Any]:
        """Fleet summary (no credentials). Reachability is best-effort probed
        per device in phase 2; phase 1 reports the configured fleet as-is."""
        rows = []
        for d in self.devices:
            rows.append({
                "id": d.get("id", ""),
                "name": d.get("name", ""),
                "object_type": d.get("object_type", ""),
                "address": d.get("address", ""),
                "transport": NwDriver._resolve_transport(d),
                "reachable": True,  # phase 2: probe each device
            })
        return {"status": "SUCCESS", "data": rows}

    # ── Per-device passthroughs ─────────────────────────────────────────────
    async def probe(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            return {"status": "ERROR", "message": f"Device {device_id} not found"}
        return await drv.probe()

    async def get_device_info(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            return {"status": "ERROR", "message": f"Device {device_id} not found"}
        return await drv.get_device_info()

    async def get_mac_table(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            return {"status": "ERROR", "message": f"Device {device_id} not found"}
        return await drv.get_mac_table()

    async def get_arp(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            return {"status": "ERROR", "message": f"Device {device_id} not found"}
        return await drv.get_arp()

    async def get_interfaces(self, device_id: str) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            return {"status": "ERROR", "message": f"Device {device_id} not found"}
        return await drv.get_interfaces()

    async def run_config(self, device_id: str, commands: List[str]) -> Dict[str, Any]:
        drv = self._driver_for(device_id)
        if not drv:
            return {"status": "ERROR", "message": f"Device {device_id} not found"}
        return await drv.run_config(commands or [])