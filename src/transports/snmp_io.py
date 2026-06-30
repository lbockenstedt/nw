"""SNMP v2c transport for the nw drivers (pysnmp-lextudio).

Lazy-imports pysnmp inside the session methods so the module imports cleanly
without pysnmp installed (the spoke venv has it; the test env doesn't). All
pysnmp hlapi calls are blocking, so the driver runs them via
``asyncio.to_thread`` to keep the spoke's event loop free.

Standard MIBs only (numeric OIDs, no MIB-file dependency) — these work across
all four nw families (AOS-S, AOS-CX, Juniper EX, Aruba/HPE gateway) because
IF-MIB / IP-MIB / BRIDGE-MIB are vendor-neutral:

  * device info — sysDescr/sysName/ifNumber (SNMPv2-MIB + IF-MIB)
  * interfaces  — ifTable (ifDescr/ifPhysAddress/ifOperStatus/ifSpeed) +
                  ipAdEntTable (IP→ifIndex)
  * arp         — ipNetToMediaTable (IP-MIB)
  * mac table   — dot1dTpFdbTable + dot1dBasePortIfIndex (BRIDGE-MIB)

Row shapes match API_SPEC.md: arp ``{ip,mac,interface}``, mac
``{mac,vlan,interface}``, interfaces ``{name,ip,mac,vlan,status,speed}``,
device_info ``{model,serial,firmware,interfaces_count}``.
"""
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("NwSnmp")

# ── OIDs ─────────────────────────────────────────────────────────────────────
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
IF_NUMBER = "1.3.6.1.2.1.2.1.0"
# ifTable columns: 1 ifIndex, 2 ifDescr, 3 ifType, 5 ifSpeed, 6 ifPhysAddress,
# 7 ifAdminStatus, 8 ifOperStatus. Full OID = <prefix><col>.<ifIndex>.
IF_PREFIX = "1.3.6.1.2.1.2.2.1."
IF_DESCR, IF_SPEED, IF_PHYSADDR, IF_OPER = "2", "5", "6", "8"
# ipAdEntTable: 1 ipAdEntNetAddr, 2 ipAdEntIfIndex. Full = <prefix><col>.<ip>
IP_PREFIX = "1.3.6.1.2.1.4.20.1."
IP_ADDR_COL, IP_IFIDX_COL = "1", "2"
# ipNetToMediaTable: 1 ifIndex, 2 physAddress, 3 netAddress.
# Full = <prefix><col>.<ifIndex>.<ip>
ARP_PREFIX = "1.3.6.1.2.1.4.22.1."
ARP_IFIDX, ARP_MAC, ARP_IP = "1", "2", "3"
# BRIDGE-MIB: dot1dTpFdbAddress (.1.<fdbId>), dot1dTpFdbPort (.2.<fdbId>),
# dot1dBasePortIfIndex (.1.4.1.2.<port> → ifIndex).
FDB_ADDR_PREFIX = "1.3.6.1.2.1.17.4.3.1.1"
FDB_PORT_PREFIX = "1.3.6.1.2.1.17.4.3.1.2"
BRIDGE_PORT_IF_PREFIX = "1.3.6.1.2.1.17.1.4.1.2"


class SnmpError(Exception):
    """Raised on configuration errors, timeouts, or SNMP error responses."""


# ── SnmpSession: blocking pysnmp get/walk (run via to_thread) ────────────────
class SnmpSession:
    """SNMPv2c community session. Raises SnmpError if no community is configured
    or the agent doesn't respond within the timeout."""

    def __init__(self, device: Dict[str, Any], timeout: float = 2.0,
                 retries: int = 1):
        self.host = str((device or {}).get("address") or "").strip()
        self.community = str((device or {}).get("snmp_community") or "").strip()
        if not self.community:
            raise SnmpError("no snmp_community configured for device "
                            f"{(device or {}).get('id') or self.host!r}")
        if not self.host:
            raise SnmpError("no device address configured")
        self.port = int((device or {}).get("snmp_port") or 161)
        self.timeout = float(timeout)
        self.retries = int(retries)

    def _hlapi(self):
        from pysnmp.hlapi import (SnmpEngine, CommunityData, UdpTransportTarget,
                                  ContextData, ObjectType, ObjectIdentity,
                                  getCmd, nextCmd)
        return (SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
                ObjectType, ObjectIdentity, getCmd, nextCmd)

    def _target(self, UdpTransportTarget):
        return UdpTransportTarget((self.host, self.port),
                                  timeout=self.timeout, retries=self.retries)

    def get(self, oid: str) -> Optional[Any]:
        (SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
         ObjectType, ObjectIdentity, getCmd, nextCmd) = self._hlapi()
        try:
            transport = self._target(UdpTransportTarget)
        except Exception as e:
            raise SnmpError(f"transport setup for {self.host}: {e}")
        err, es, ei, var = next(getCmd(
            SnmpEngine(), CommunityData(self.community, mpModel=1),
            transport, ContextData(), ObjectType(ObjectIdentity(oid))))
        if err:
            raise SnmpError(f"get {oid} on {self.host}: {err}")
        if es:
            raise SnmpError(f"get {oid} on {self.host}: {es} at {ei}")
        if not var:
            return None
        # var[0] is an ObjectType; .prettyPrint() yields the value.
        return _value_of(var[0])

    def walk(self, oid: str) -> List[Tuple[str, Any]]:
        """Walk the subtree under ``oid`` → list of (full_oid_str, value)."""
        (SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
         ObjectType, ObjectIdentity, getCmd, nextCmd) = self._hlapi()
        try:
            transport = self._target(UdpTransportTarget)
        except Exception as e:
            raise SnmpError(f"transport setup for {self.host}: {e}")
        out: List[Tuple[str, Any]] = []
        it = nextCmd(
            SnmpEngine(), CommunityData(self.community, mpModel=1),
            transport, ContextData(), ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False)
        for err, es, ei, var in it:
            if err:
                # "no SNMP response" → timeout; stop what we have.
                if "no" in str(err).lower() and "response" in str(err).lower():
                    if not out:
                        raise SnmpError(f"walk {oid} on {self.host}: {err}")
                    break
                break
            if es:
                break
            for vb in (var or []):
                full = ".".join(str(x) for x in vb[0].asNumbers())
                out.append((full, _value_of(vb)))
        return out


def _value_of(var_bind) -> Any:
    """Extract a python value from a pysnmp var-bind (the second element of an
    ObjectType tuple). Octet strings arrive as bytes → decode for text OIDs,
    keep bytes for MAC/octet fields (the parsers decide)."""
    try:
        val = var_bind[1]
    except Exception:
        return None
    # Integer types → int; OctetString → bytes; prettyPrint fallback.
    try:
        if hasattr(val, "prettyPrint"):
            txt = val.prettyPrint()
            # Hex-octet strings print as "0xAAAA..." — preserve as bytes for MAC.
            if isinstance(txt, str) and txt.startswith("0x") and len(txt) > 2:
                return bytes.fromhex(txt[2:])
            return txt
    except Exception:
        pass
    return val


# ── Pure parsers (testable without pysnmp) ───────────────────────────────────
def _mac_from_octets(value: Any) -> str:
    """Colon-form MAC from bytes/0x-string/colon-or-hex string; '' if not 6
    octets. Mirrors nw_engine._norm_mac's canonical form.

    Also handles the BRIDGE-MIB FDB OID-suffix encoding, where the MAC is the
    OID tail as six dot-separated **decimal** octets (e.g. ``0.10.20.30.40.50``)
    — that's how ``dot1dTpFdbPort`` keys its entries, and we pair them with
    ``dot1dTpFdbAddress`` by that key.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
        if len(b) == 6:
            return ":".join(f"{x:02x}" for x in b)
        return ""
    s = str(value).strip()
    if s.startswith("0x"):
        s = s[2:]
    # Decimal-octet encoding (BRIDGE-MIB FDB OID suffix): 6 ints 0-255.
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){5}", s):
        parts = [int(p) for p in s.split(".")]
        if len(parts) == 6 and all(0 <= p <= 255 for p in parts):
            return ":".join(f"{p:02x}" for p in parts)
    hexes = re.sub(r"[^0-9a-fA-F]", "", s).lower()
    if len(hexes) == 12:
        return ":".join(hexes[i:i + 2] for i in range(0, 12, 2))
    return ""


def _suffix_int(oid: str, prefix: str) -> Optional[int]:
    """Return the integer suffix of ``oid`` after ``prefix`` (e.g. the ifIndex
    for an ifTable column), or None if it doesn't match."""
    if not oid.startswith(prefix):
        return None
    rest = oid[len(prefix):]
    rest = rest.lstrip(".")
    if not rest:
        return None
    head = rest.split(".")[0]
    try:
        return int(head)
    except ValueError:
        return None


def parse_iftable(walk_pairs: List[Tuple[str, Any]]) -> Dict[int, dict]:
    """Walk pairs across the ifTable columns → {ifIndex: {name,mac,status,speed}}."""
    out: Dict[int, dict] = {}
    for oid, val in walk_pairs or []:
        if not oid.startswith(IF_PREFIX):
            continue
        col_idx = oid[len(IF_PREFIX):].split(".", 1)
        col, tail = col_idx[0], (col_idx[1] if len(col_idx) > 1 else "")
        try:
            ifx = int(tail.split(".")[0])
        except (ValueError, IndexError):
            continue
        d = out.setdefault(ifx, {"name": "", "mac": "", "status": "down",
                                 "speed": 0})
        if col == IF_DESCR:
            d["name"] = str(val or "").strip()
        elif col == IF_PHYSADDR:
            d["mac"] = _mac_from_octets(val)
        elif col == IF_OPER:
            # 1=up, 2=down, 3=testing, 4=unknown, 5=dormant, 6=notPresent, 7=lowerLayerDown
            try:
                n = int(val)
            except (TypeError, ValueError):
                n = 0
            d["status"] = "up" if n == 1 else "down"
        elif col == IF_SPEED:
            try:
                d["speed"] = int(val)
            except (TypeError, ValueError):
                d["speed"] = 0
    return out


def parse_ip_table(walk_pairs: List[Tuple[str, Any]]) -> Dict[int, List[str]]:
    """ipAdEntTable walk → {ifIndex: [ip, ...]}."""
    out: Dict[int, List[str]] = {}
    # ipAdEntIfIndex: <prefix>2.<ip-encoded> → ifIndex value
    # ipAdEntNetAddr: <prefix>1.<ip-encoded> → ip string value
    # The ifIndex→ip join: group by the ip-encoded suffix.
    ip_by_suffix: Dict[str, str] = {}
    ifx_by_suffix: Dict[str, int] = {}
    for oid, val in walk_pairs or []:
        if not oid.startswith(IP_PREFIX):
            continue
        col, tail = oid[len(IP_PREFIX):].split(".", 1)
        suffix = tail
        if col == IP_ADDR_COL:
            ip_by_suffix[suffix] = str(val or "").strip()
        elif col == IP_IFIDX_COL:
            try:
                ifx_by_suffix[suffix] = int(val)
            except (TypeError, ValueError):
                pass
    for suffix, ip in ip_by_suffix.items():
        ifx = ifx_by_suffix.get(suffix)
        if ifx is None:
            continue
        out.setdefault(ifx, []).append(ip)
    return out


def parse_arp(walk_pairs: List[Tuple[str, Any]]) -> List[Tuple[int, str, str]]:
    """ipNetToMediaTable walk → list of (ifIndex, ip, mac)."""
    # <prefix><col>.<ifIndex>.<ip-encoded>
    mac_by_key: Dict[Tuple[int, str], str] = {}
    for oid, val in walk_pairs or []:
        if not oid.startswith(ARP_PREFIX):
            continue
        parts = oid[len(ARP_PREFIX):].split(".", 2)
        if len(parts) < 3:
            continue
        col, ifx_s, ip_suffix = parts[0], parts[1], parts[2]
        try:
            ifx = int(ifx_s)
        except ValueError:
            continue
        key = (ifx, ip_suffix)
        if col == ARP_IP:
            mac_by_key.setdefault(key, "")  # placeholder; ip is the suffix
        elif col == ARP_MAC:
            mac = _mac_from_octets(val)
            if mac:
                mac_by_key[key] = mac
    out = []
    for (ifx, ip_suffix), mac in mac_by_key.items():
        out.append((ifx, ip_suffix, mac))
    return out


def parse_fdb(walk_pairs: List[Tuple[str, Any]]) -> List[Tuple[str, int]]:
    """dot1dTpFdb walk → list of (mac, port). mac from FDB_ADDR, port from FDB_PORT."""
    port_by_mac: Dict[str, int] = {}
    for oid, val in walk_pairs or []:
        if oid.startswith(FDB_PORT_PREFIX):
            mac = _mac_from_octets(_mac_suffix_key(oid, FDB_PORT_PREFIX))
            try:
                port = int(val)
            except (TypeError, ValueError):
                port = 0
            if mac:
                port_by_mac[mac] = port
        elif oid.startswith(FDB_ADDR_PREFIX):
            mac = _mac_from_octets(val)
            if mac:
                port_by_mac.setdefault(mac, 0)
    return [(mac, port) for mac, port in port_by_mac.items()]


def _mac_suffix_key(oid: str, prefix: str) -> str:
    """The FDB entry key suffix (used to pair FDB_ADDR and FDB_PORT rows)."""
    return oid[len(prefix):].lstrip(".")


def parse_bridge_port_if(walk_pairs: List[Tuple[str, Any]]) -> Dict[int, int]:
    """dot1dBasePortIfIndex walk → {bridge_port: ifIndex}."""
    out: Dict[int, int] = {}
    for oid, val in walk_pairs or []:
        if not oid.startswith(BRIDGE_PORT_IF_PREFIX):
            continue
        tail = oid[len(BRIDGE_PORT_IF_PREFIX):].lstrip(".")
        try:
            port = int(tail.split(".")[0])
            out[port] = int(val)
        except (ValueError, TypeError):
            continue
    return out


# ── High-level async gathers (driver calls these) ────────────────────────────
async def _to_thread(fn, *args):
    return await asyncio.to_thread(fn, *args)


async def snmp_probe(session: SnmpSession) -> dict:
    import time
    t0 = time.monotonic()
    try:
        await _to_thread(session.get, SYS_UPTIME)
    except SnmpError:
        raise
    return {"reachable": True, "latency_ms": int((time.monotonic() - t0) * 1000)}


async def snmp_get_device_info(session: SnmpSession) -> dict:
    sysdescr = await _to_thread(session.get, SYS_DESCR)
    sysname = await _to_thread(session.get, SYS_NAME)
    ifnum = await _to_thread(session.get, IF_NUMBER)
    try:
        ifnum_i = int(ifnum) if ifnum not in (None, "") else 0
    except (TypeError, ValueError):
        ifnum_i = 0
    descr = str(sysdescr or "").strip()
    model = str(sysname or "").strip() or _guess_model(descr)
    return {
        "model": model,
        "serial": "",  # serial via ENTITY-MIB entPhysicalSerialNum — not walked
        "firmware": descr,
        "interfaces_count": ifnum_i,
    }


async def snmp_get_interfaces(session: SnmpSession) -> List[dict]:
    iftable = parse_iftable(await _to_thread(session.walk, IF_PREFIX))
    ips = parse_ip_table(await _to_thread(session.walk, IP_PREFIX))
    rows = []
    for ifx, d in sorted(iftable.items()):
        rows.append({
            "name": d["name"] or f"if{ifx}",
            "ip": (ips.get(ifx, [""])[0] if ips.get(ifx) else ""),
            "mac": d["mac"],
            "vlan": "",  # VLAN via Q-BRIDGE-MIB — not walked this pass
            "status": d["status"],
            "speed": d["speed"],
        })
    return rows


async def snmp_get_arp(session: SnmpSession,
                       ifaces: Optional[Dict[int, dict]] = None) -> List[dict]:
    pairs = parse_arp(await _to_thread(session.walk, ARP_PREFIX))
    names = ifaces or {}
    return [{"ip": ip, "mac": mac,
             "interface": (names.get(ifx, {}) or {}).get("name", str(ifx))}
            for (ifx, ip, mac) in pairs]


async def snmp_get_mac_table(session: SnmpSession,
                             ifaces: Optional[Dict[int, dict]] = None
                             ) -> List[dict]:
    fdb = parse_fdb(await _to_thread(session.walk, FDB_ADDR_PREFIX))
    # also walk FDB_PORT to get ports; combine
    port_pairs = parse_fdb(await _to_thread(session.walk, FDB_PORT_PREFIX))
    port_by_mac = {mac: port for mac, port in port_pairs}
    bridge_if = parse_bridge_port_if(await _to_thread(session.walk,
                                      BRIDGE_PORT_IF_PREFIX))
    names = ifaces or {}
    rows = []
    for mac, port in fdb:
        port = port_by_mac.get(mac, port)
        ifx = bridge_if.get(port, 0)
        rows.append({
            "mac": mac,
            "vlan": "",
            "interface": (names.get(ifx, {}) or {}).get("name", str(ifx) if ifx else str(port)),
        })
    return rows


def _guess_model(descr: str) -> str:
    """Best-effort model from sysDescr (first quoted/hardware-looking token)."""
    if not descr:
        return ""
    m = re.search(r"\b([A-Z]{2,4}[-\s]?\d{2,4}[A-Z]*)\b", descr)
    return m.group(1) if m else descr[:24]