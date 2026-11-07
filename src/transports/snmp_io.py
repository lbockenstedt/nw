"""SNMP v2c transport for the nw drivers (pysnmp 7.x).

Lazy-imports pysnmp inside ``SnmpSession._hlapi`` so the module imports cleanly
without pysnmp installed (the spoke venv has it; the test env doesn't).

pysnmp 7.x REMOVED the synchronous ``pysnmp.hlapi`` API this module was written
against — ``pysnmp.hlapi`` is now an empty namespace, so the old import raised
ImportError from outside the ``try`` in get()/walk() and SNMP was entirely
non-functional (nw#103). It now targets ``pysnmp.hlapi.v1arch.asyncio``:
``SnmpDispatcher`` (a context manager), an awaited
``UdpTransportTarget.create()``, ``get_cmd``, and ``walk_cmd`` (an async
generator). ``ContextData`` does not exist in v1arch and is gone.

``SnmpSession.get``/``.walk`` deliberately remain BLOCKING despite the async
library underneath: every caller reaches them via ``asyncio.to_thread`` (see
``_to_thread``), so they run on a worker thread and drive a private event loop.
Keeping the signatures blocking is what let this port stay confined to one
class.

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
# asyncio runs the blocking SNMP calls on worker threads and drives pysnmp's loop.
import asyncio
import concurrent.futures
# Standard-library logging for transport messages.
import logging
# re parses MACs, firmware revisions and model names from text.
import re
# Type hints.
from typing import Any, Dict, List, Optional, Tuple

# Named logger for SNMP transport messages.
logger = logging.getLogger("NwSnmp")

# ── OIDs ─────────────────────────────────────────────────────────────────────
# SNMPv2-MIB scalars: system description, hostname, uptime; IF-MIB interface count.
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
IF_NUMBER = "1.3.6.1.2.1.2.1.0"
# BRIDGE-MIB dot1dBaseBridgeAddress (chassis base MAC).
BRIDGE_BASE_MAC = "1.3.6.1.2.1.17.1.1.0"
# Management controllers (iLO, iDRAC, ...) have no ENTITY-MIB but serve the host
# server's identity from a vendor MIB. Tried in order; the first value wins.
# Add a vendor by appending its OIDs.
BMC_SERIAL_OIDS = (
    "1.3.6.1.4.1.232.2.2.2.1.0",        # HPE cpqSiSysSerialNum
    "1.3.6.1.4.1.674.10892.5.1.3.2.0",  # Dell systemServiceTag
)
BMC_HOST_MODEL_OIDS = (
    "1.3.6.1.4.1.232.2.2.4.2.0",        # HPE cpqSiProductName
    "1.3.6.1.4.1.674.10892.5.1.3.12.0", # Dell systemModelName
)
# ENTITY-MIB entPhysicalSerialNum / entPhysicalModelName columns.
ENT_SERIAL_PREFIX = "1.3.6.1.2.1.47.1.1.1.1.11"
ENT_MODEL_PREFIX = "1.3.6.1.2.1.47.1.1.1.1.13"
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
    or the agent doesn't respond within the timeout.
    
    Targets pysnmp 7.x v1arch asyncio API; public methods stay blocking on purpose."""

    def __init__(self, device: Dict[str, Any], timeout: float = 2.0,
                 retries: int = 1):
        """Read address, community and port from the device dict; raise SnmpError if unusable."""
        # Device address and v2c community string.
        self.host = str((device or {}).get("address") or "").strip()
        self.community = str((device or {}).get("snmp_community") or "").strip()
        # SNMP v2c cannot authenticate without a community.
        if not self.community:
            raise SnmpError("no snmp_community configured for device "
                            f"{(device or {}).get('id') or self.host!r}")
        # Nothing to query without an address.
        if not self.host:
            raise SnmpError("no device address configured")
        # UDP port (default 161), per-request timeout and retry count.
        self.port = int((device or {}).get("snmp_port") or 161)
        self.timeout = float(timeout)
        self.retries = int(retries)

    def _hlapi(self):
        """Import and return the pysnmp v1arch asyncio API pieces (lazy import)."""
        from pysnmp.hlapi.v1arch.asyncio import (SnmpDispatcher, CommunityData,
                                                 UdpTransportTarget, ObjectType,
                                                 ObjectIdentity, get_cmd, walk_cmd)
        return (SnmpDispatcher, CommunityData, UdpTransportTarget, ObjectType,
                ObjectIdentity, get_cmd, walk_cmd)

    async def _target(self, UdpTransportTarget):
        """Create the UDP transport target for this device, wrapping errors in SnmpError."""
        try:
            return await UdpTransportTarget.create((self.host, self.port),
                                                   timeout=self.timeout,
                                                   retries=self.retries)
        except SnmpError:
            # Already our error type: pass through unchanged.
            raise
        except Exception as e:
            # Any other failure (e.g. DNS) becomes an SnmpError with the host name.
            raise SnmpError(f"transport setup for {self.host}: {e}")

    async def _aget(self, oid: str) -> Optional[Any]:
        """Async GET of one OID; returns its value, or None when nothing came back."""
        # Unpack the lazily imported API.
        (SnmpDispatcher, CommunityData, UdpTransportTarget, ObjectType,
         ObjectIdentity, get_cmd, walk_cmd) = self._hlapi()
        # Dispatcher owns the UDP socket for the duration of the request.
        with SnmpDispatcher() as dispatcher:
            target = await self._target(UdpTransportTarget)
            # Send a v2c GET (mpModel=1 selects SNMPv2c).
            err, es, ei, var = await get_cmd(
                dispatcher, CommunityData(self.community, mpModel=1),
                target, ObjectType(ObjectIdentity(oid)))
            # Transport-level error (usually a timeout).
            if err:
                raise SnmpError(f"get {oid} on {self.host}: {err}")
            # Agent returned an SNMP error status.
            if es:
                raise SnmpError(f"get {oid} on {self.host}: {es} at {ei}")
            # Empty response.
            if not var:
                return None
            # Value of the single var-bind.
            return _value_of(var[0])

    async def _awalk(self, oid: str) -> List[Tuple[str, Any]]:
        """Async walk of an OID subtree; returns (oid, value) pairs."""
        # Unpack the lazily imported API.
        (SnmpDispatcher, CommunityData, UdpTransportTarget, ObjectType,
         ObjectIdentity, get_cmd, walk_cmd) = self._hlapi()
        # Dispatcher owns the UDP socket for the duration of the walk.
        with SnmpDispatcher() as dispatcher:
            target = await self._target(UdpTransportTarget)
            out: List[Tuple[str, Any]] = []
            # Walk only inside the subtree (lexicographicMode=False stops at its end).
            async for err, es, ei, var in walk_cmd(
                    dispatcher, CommunityData(self.community, mpModel=1),
                    target, ObjectType(ObjectIdentity(oid)),
                    lexicographicMode=False):
                # Any error ends the walk.
                if err:
                    # "no SNMP response" → timeout; stop what we have.
                    if "no" in str(err).lower() and "response" in str(err).lower():
                        if not out:
                            # Timed out before any data: report it as a failure.
                            raise SnmpError(f"walk {oid} on {self.host}: {err}")
                        break
                    # Other errors: keep what was collected.
                    break
                # SNMP error status: stop the walk.
                if es:
                    break
                # Record each returned OID with its value.
                for vb in (var or []):
                    full = _oid_str(vb[0])
                    out.append((full, _value_of(vb)))
        # All pairs collected.
        return out

    def _run(self, coro):
        """Drive one coroutine to completion on a private event loop.

        Both public methods stay blocking because every caller reaches them
        through ``asyncio.to_thread`` (see ``_to_thread`` below) — i.e. from a
        worker thread that has no running loop of its own. Creating, using and
        closing a loop per call is correct there, and the loop is ALWAYS closed
        so the thread pool doesn't accumulate them.
        """
        # Remember the thread's current loop (a worker thread usually has none).
        try:
            prev = asyncio.get_event_loop_policy().get_event_loop()
        except Exception:  # noqa: BLE001 — a worker thread has no loop yet
            # No loop yet in this thread.
            prev = None
        # Fresh loop just for this call.
        loop = asyncio.new_event_loop()
        try:
            # Make it current (pysnmp looks it up) and run the coroutine to completion.
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            # Always close the loop so pooled threads do not leak loops.
            loop.close()
            # Unconditional: leaving the CLOSED loop installed as this thread's
            # loop would break the next call made on the same pooled thread.
            try:
                asyncio.set_event_loop(prev)
            except Exception:  # noqa: BLE001
                # Restoring the previous loop is best-effort.
                pass

    def get(self, oid: str) -> Optional[Any]:
        """Blocking GET of one OID (call via asyncio.to_thread)."""
        return self._run(self._aget(oid))

    def walk(self, oid: str) -> List[Tuple[str, Any]]:
        """Blocking walk of an OID subtree (call via asyncio.to_thread)."""
        return self._run(self._awalk(oid))


def _oid_str(name):
    """Dotted OID string (no leading dot) from a pysnmp name, whatever its type."""
    # pysnmp ObjectIdentity: ask for its OID.
    try:
        return str(name.getOid()).lstrip(".")
    except Exception:
        # Fallback: numeric tuple form.
        try:
            return ".".join(str(x) for x in name.asNumbers()).lstrip(".")
        except Exception:
            # Last resort: plain string.
            return str(name).lstrip(".")


def _value_of(var_bind) -> Any:
    """Extract a python value from a pysnmp var-bind (the second element of an
    ObjectType tuple). Octet strings arrive as bytes → decode for text OIDs,
    keep bytes for MAC/octet fields (the parsers decide)."""
    # The value is the second element of the var-bind.
    try:
        val = var_bind[1]
    except Exception:
        # Unexpected shape: no value.
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
        # Formatting failed: return the raw value below.
        pass
    # Raw pysnmp value.
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
        # No value.
        return ""
    # Raw bytes: valid only if exactly six octets.
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
        if len(b) == 6:
            return ":".join(f"{x:02x}" for x in b)
        return ""
    # Text form: drop a "0x" prefix.
    s = str(value).strip()
    if s.startswith("0x"):
        s = s[2:]
    # Decimal-octet encoding (BRIDGE-MIB FDB OID suffix): 6 ints 0-255.
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){5}", s):
        parts = [int(p) for p in s.split(".")]
        if len(parts) == 6 and all(0 <= p <= 255 for p in parts):
            return ":".join(f"{p:02x}" for p in parts)
    # Otherwise keep only hex digits; twelve of them make a MAC.
    hexes = re.sub(r"[^0-9a-fA-F]", "", s).lower()
    if len(hexes) == 12:
        return ":".join(hexes[i:i + 2] for i in range(0, 12, 2))
    # Not a MAC.
    return ""


def parse_iftable(walk_pairs: List[Tuple[str, Any]]) -> Dict[int, dict]:
    """Walk pairs across the ifTable columns → {ifIndex: {name,mac,status,speed}}."""
    # ifIndex -> interface fields.
    out: Dict[int, dict] = {}
    # Walk pairs are "<IF_PREFIX><column>.<ifIndex>".
    for oid, val in walk_pairs or []:
        # Ignore OIDs outside the ifTable.
        if not oid.startswith(IF_PREFIX):
            continue
        # Split into column number and the ifIndex tail.
        col_idx = oid[len(IF_PREFIX):].split(".", 1)
        col, tail = col_idx[0], (col_idx[1] if len(col_idx) > 1 else "")
        # The ifIndex must be an integer.
        try:
            ifx = int(tail.split(".")[0])
        except (ValueError, IndexError):
            # Malformed OID: skip.
            continue
        # Start each interface with safe defaults.
        d = out.setdefault(ifx, {"name": "", "mac": "", "status": "down",
                                 "speed": 0})
        # Fill the field that this column holds.
        if col == IF_DESCR:
            d["name"] = str(val or "").strip()
        elif col == IF_PHYSADDR:
            # Physical address -> MAC.
            d["mac"] = _mac_from_octets(val)
        elif col == IF_OPER:
            # 1=up, 2=down, 3=testing, 4=unknown, 5=dormant, 6=notPresent, 7=lowerLayerDown
            # Only "1" counts as up.
            try:
                n = int(val)
            except (TypeError, ValueError):
                # Not a number: treat as unknown (down).
                n = 0
            d["status"] = "up" if n == 1 else "down"
        elif col == IF_SPEED:
            try:
                # Interface speed in bits per second.
                d["speed"] = int(val)
            except (TypeError, ValueError):
                # Not a number: 0.
                d["speed"] = 0
    # Interfaces by ifIndex.
    return out


def parse_ip_table(walk_pairs: List[Tuple[str, Any]]) -> Dict[int, List[str]]:
    """ipAdEntTable walk → {ifIndex: [ip, ...]}."""
    out: Dict[int, List[str]] = {}
    # ipAdEntIfIndex: <prefix>2.<ip-encoded> → ifIndex value
    # ipAdEntNetAddr: <prefix>1.<ip-encoded> → ip string value
    # The ifIndex→ip join: group by the ip-encoded suffix.
    ip_by_suffix: Dict[str, str] = {}
    ifx_by_suffix: Dict[str, int] = {}
    # Collect both columns keyed by the IP suffix.
    for oid, val in walk_pairs or []:
        # Ignore OIDs outside the table.
        if not oid.startswith(IP_PREFIX):
            continue
        # Column number and the IP-encoded suffix.
        col, tail = oid[len(IP_PREFIX):].split(".", 1)
        suffix = tail
        # Address column: the IP string.
        if col == IP_ADDR_COL:
            ip_by_suffix[suffix] = str(val or "").strip()
        elif col == IP_IFIDX_COL:
            # ifIndex column: integer interface index.
            try:
                ifx_by_suffix[suffix] = int(val)
            except (TypeError, ValueError):
                # Bad value: skip.
                pass
    # Join: each IP goes under the interface index with the same suffix.
    for suffix, ip in ip_by_suffix.items():
        ifx = ifx_by_suffix.get(suffix)
        # IP without an interface index: skip.
        if ifx is None:
            continue
        # Several IPs per interface are possible.
        out.setdefault(ifx, []).append(ip)
    # Interface index -> list of IPs.
    return out


def parse_arp(walk_pairs: List[Tuple[str, Any]]) -> List[Tuple[int, str, str]]:
    """ipNetToMediaTable walk → list of (ifIndex, ip, mac)."""
    # <prefix><col>.<ifIndex>.<ip-encoded>
    # (ifIndex, ip) -> MAC.
    mac_by_key: Dict[Tuple[int, str], str] = {}
    # Walk the ARP table entries.
    for oid, val in walk_pairs or []:
        # Ignore OIDs outside the table.
        if not oid.startswith(ARP_PREFIX):
            continue
        # Split into column, ifIndex and the IP suffix.
        parts = oid[len(ARP_PREFIX):].split(".", 2)
        # Malformed OID: skip.
        if len(parts) < 3:
            continue
        col, ifx_s, ip_suffix = parts[0], parts[1], parts[2]
        # ifIndex must be an integer.
        try:
            ifx = int(ifx_s)
        except ValueError:
            # Not a number: skip.
            continue
        # The (interface, IP) pair identifies one ARP entry.
        key = (ifx, ip_suffix)
        if col == ARP_IP:
            mac_by_key.setdefault(key, "")  # placeholder; ip is the suffix
        elif col == ARP_MAC:
            # Physical-address column: the MAC.
            mac = _mac_from_octets(val)
            if mac:
                mac_by_key[key] = mac
    # Flatten to (ifIndex, ip, mac) tuples.
    out = []
    for (ifx, ip_suffix), mac in mac_by_key.items():
        out.append((ifx, ip_suffix, mac))
    # All ARP entries.
    return out


def parse_fdb(walk_pairs: List[Tuple[str, Any]]) -> List[Tuple[str, int]]:
    """dot1dTpFdb walk → list of (mac, port). mac from FDB_ADDR, port from FDB_PORT."""
    # MAC -> bridge port number.
    port_by_mac: Dict[str, int] = {}
    # Each pair is either a port row or an address row.
    for oid, val in walk_pairs or []:
        if oid.startswith(FDB_PORT_PREFIX):
            # Port row: the MAC is encoded in the OID suffix, the value is the port.
            mac = _mac_from_octets(_mac_suffix_key(oid, FDB_PORT_PREFIX))
            try:
                port = int(val)
            except (TypeError, ValueError):
                # Bad port value: 0 (unknown).
                port = 0
            # Record the port for valid MACs.
            if mac:
                port_by_mac[mac] = port
        elif oid.startswith(FDB_ADDR_PREFIX):
            # Address row: MAC is the value; port unknown until a port row is seen.
            mac = _mac_from_octets(val)
            if mac:
                port_by_mac.setdefault(mac, 0)
    # (mac, port) pairs.
    return [(mac, port) for mac, port in port_by_mac.items()]


def _mac_suffix_key(oid: str, prefix: str) -> str:
    """The FDB entry key suffix (used to pair FDB_ADDR and FDB_PORT rows)."""
    return oid[len(prefix):].lstrip(".")


def parse_bridge_port_if(walk_pairs: List[Tuple[str, Any]]) -> Dict[int, int]:
    """dot1dBasePortIfIndex walk → {bridge_port: ifIndex}."""
    # Bridge port -> ifIndex.
    out: Dict[int, int] = {}
    # Walk pairs are "<prefix>.<port>" -> ifIndex.
    for oid, val in walk_pairs or []:
        # Ignore OIDs outside the table.
        if not oid.startswith(BRIDGE_PORT_IF_PREFIX):
            continue
        # Port number from the OID tail.
        tail = oid[len(BRIDGE_PORT_IF_PREFIX):].lstrip(".")
        # Both port and value must be integers.
        try:
            port = int(tail.split(".")[0])
            out[port] = int(val)
        except (ValueError, TypeError):
            # Malformed: skip.
            continue
    # Bridge port to interface index map.
    return out


# ── High-level async gathers (driver calls these) ────────────────────────────
# SNMP's own worker threads: a scan runs ~32 hosts at once and a silent host
# holds a thread for timeout x (retries+1), so sharing the small default pool
# (cpu+4) starved SNMP and every other role in the process.
_SNMP_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="snmp")


async def _to_thread(fn, *args):
    """Run a blocking function on an SNMP worker thread and await its result."""
    return await asyncio.get_running_loop().run_in_executor(_SNMP_POOL, fn, *args)


async def snmp_probe(session: SnmpSession) -> dict:
    """Reachability probe: GET sysUpTime and report ``{reachable, latency_ms}``.
    Raises :class:`SnmpError` on timeout/no response."""
    # time is imported lazily; used to measure latency.
    import time
    # Start the clock, then GET sysUpTime (any agent answers this).
    t0 = time.monotonic()
    try:
        await _to_thread(session.get, SYS_UPTIME)
    except SnmpError:
        # Timeout/no response: let the caller handle it.
        raise
    # Answered: report reachability and round-trip time.
    return {"reachable": True, "latency_ms": int((time.monotonic() - t0) * 1000)}


async def snmp_get_device_info(session: SnmpSession) -> dict:
    """GET sysDescr/sysName/ifNumber, plus best-effort chassis identity
    (ENTITY-MIB serial/model, BRIDGE-MIB base MAC), and return
    ``{model, serial, firmware, hostname, mac, interfaces_count}``."""
    # Fetch the three basic scalars.
    sysdescr = await _to_thread(session.get, SYS_DESCR)
    sysname = await _to_thread(session.get, SYS_NAME)
    ifnum = await _to_thread(session.get, IF_NUMBER)
    # Interface count as an int (0 when missing or not a number).
    try:
        ifnum_i = int(ifnum) if ifnum not in (None, "") else 0
    except (TypeError, ValueError):
        # Not a number.
        ifnum_i = 0
    # Description text used for model and firmware guesses.
    descr = str(sysdescr or "").strip()
    # Serial from ENTITY-MIB.
    serial = await _first_entity_value(session, ENT_SERIAL_PREFIX)
    # Model: SKU in the description, then ENTITY-MIB, then a text guess.
    model = _sku_from_descr(descr) or await _first_entity_value(
        session, ENT_MODEL_PREFIX) or _guess_model(descr)
    # Chassis base MAC from BRIDGE-MIB (optional).
    mac = ""
    try:
        mac = _mac_from_octets(await _to_thread(session.get, BRIDGE_BASE_MAC))
    except Exception:  # noqa: BLE001 — identity extras are best-effort
        # Not served: leave the MAC empty.
        pass
    # Firmware: the "revision X.Y.Z" token in sysDescr, else the whole description.
    m = re.search(r"\brevision\s+([A-Z]{0,3}\.?\d+(?:\.\w+)+)", descr, re.IGNORECASE)
    # Device info for the driver.
    return {
        "model": model,
        "serial": serial,
        "firmware": m.group(1) if m else descr,
        "hostname": str(sysname or "").strip(),  # sysName IS the configured hostname
        "mac": mac,
        "interfaces_count": ifnum_i,
    }


async def _first_entity_value(session: SnmpSession, prefix: str) -> str:
    """First non-empty value in an ENTITY-MIB column, lowest entPhysicalIndex
    first (the chassis / stack member 1). Empty when the MIB isn't served."""
    # Walk the column; any failure means "not served".
    try:
        rows = await _to_thread(session.walk, prefix)
    except Exception:  # noqa: BLE001
        # Not served: empty.
        return ""
    def _idx(oid):
        """Numeric last component of an OID (entPhysicalIndex); bad values sort last."""
        try:
            return int(str(oid).rsplit(".", 1)[-1])
        except ValueError:
            return 1 << 30
    # Lowest index first (the chassis / first stack member).
    for oid, val in sorted(rows or [], key=lambda r: _idx(r[0])):
        # Octet strings: decode to ASCII; skip undecodable values.
        if isinstance(val, bytes):
            try:
                val = val.decode("ascii")
            except UnicodeDecodeError:
                continue
        # Return the first non-empty value.
        txt = str(val or "").strip()
        if txt:
            return txt
    # Nothing useful.
    return ""


def _sku_from_descr(descr: str) -> str:
    """HPE/Aruba product SKU in sysDescr ("Aruba JL253A 2930F-24G…"), else ''."""
    # Aruba/HPE SKUs look like "JL253A" (J, optional letter, 3-4 digits, letter).
    m = re.search(r"\b(J[A-Z]?\d{3,4}[A-Z])\b", descr or "")
    return m.group(1) if m else ""


async def snmp_get_interfaces(session: SnmpSession) -> List[dict]:
    """Walk ifTable + ipAdEntTable and return
    ``[{name, ip, mac, vlan, status, speed}]`` (vlan empty — Q-BRIDGE-MIB not
    walked this pass)."""
    # Interface table and IP table.
    iftable = parse_iftable(await _to_thread(session.walk, IF_PREFIX))
    ips = parse_ip_table(await _to_thread(session.walk, IP_PREFIX))
    # One row per interface, ordered by ifIndex.
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
    # All interfaces.
    return rows


async def snmp_get_arp(session: SnmpSession,
                       ifaces: Optional[Dict[int, dict]] = None) -> List[dict]:
    """Walk ipNetToMediaTable and return ``[{ip, mac, interface}]``. ``ifaces``
    (from :func:`snmp_get_interfaces`) maps ifIndex → friendly interface name."""
    # ARP entries as (ifIndex, ip, mac).
    pairs = parse_arp(await _to_thread(session.walk, ARP_PREFIX))
    # Translate ifIndex to the interface name when known.
    names = ifaces or {}
    return [{"ip": ip, "mac": mac,
             "interface": (names.get(ifx, {}) or {}).get("name", str(ifx))}
            for (ifx, ip, mac) in pairs]


async def snmp_get_mac_table(session: SnmpSession,
                             ifaces: Optional[Dict[int, dict]] = None
                             ) -> List[dict]:
    """Walk BRIDGE-MIB FDB + base-port-ifIndex and return
    ``[{mac, vlan, interface}]`` (vlan empty — Q-BRIDGE-MIB not walked)."""
    # MAC addresses from the FDB address column.
    fdb = parse_fdb(await _to_thread(session.walk, FDB_ADDR_PREFIX))
    # also walk FDB_PORT to get ports; combine
    # Ports from the FDB port column.
    port_pairs = parse_fdb(await _to_thread(session.walk, FDB_PORT_PREFIX))
    port_by_mac = {mac: port for mac, port in port_pairs}
    # Bridge port -> ifIndex map.
    bridge_if = parse_bridge_port_if(await _to_thread(session.walk,
                                      BRIDGE_PORT_IF_PREFIX))
    # Interface names from the caller, if any.
    names = ifaces or {}
    rows = []
    # Resolve each MAC's port to an interface name (or the raw index/port).
    for mac, port in fdb:
        port = port_by_mac.get(mac, port)
        ifx = bridge_if.get(port, 0)
        rows.append({
            "mac": mac,
            "vlan": "",
            "interface": (names.get(ifx, {}) or {}).get("name", str(ifx) if ifx else str(port)),
        })
    # All MAC-table rows.
    return rows


def _guess_model(descr: str) -> str:
    """Best-effort model from sysDescr (first quoted/hardware-looking token)."""
    # No description: no guess.
    if not descr:
        return ""
    # First token that looks like a model number (letters + digits).
    m = re.search(r"\b([A-Z]{2,4}[-\s]?\d{2,4}[A-Z]*)\b", descr)
    return m.group(1) if m else descr[:24]