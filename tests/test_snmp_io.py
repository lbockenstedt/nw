"""Tests for the SNMP transport: pure parsers + a fake SnmpSession driving the
high-level gathers. No pysnmp needed — the session's get/walk are stubbed, and
blocking runs via asyncio.to_thread against the stub (fast, no real IO).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from transports import snmp_io  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── _mac_from_octets ──────────────────────────────────────────────────────────
def test_mac_from_bytes():
    assert snmp_io._mac_from_octets(b"\xaa\xbb\xcc\xdd\xee\xff") == \
        "aa:bb:cc:dd:ee:ff"


def test_mac_from_hex_string():
    assert snmp_io._mac_from_octets("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
    assert snmp_io._mac_from_octets("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"


def test_mac_from_decimal_oid_suffix():
    # BRIDGE-MIB FDB OID tail encodes MAC as six decimal octets.
    assert snmp_io._mac_from_octets("0.10.20.30.40.50") == "00:0a:14:1e:28:32"


def test_mac_from_bad_length_is_empty():
    assert snmp_io._mac_from_octets(b"\xaa\xbb") == ""
    assert snmp_io._mac_from_octets("nothex") == ""
    assert snmp_io._mac_from_octets(None) == ""


# ── parse_iftable ─────────────────────────────────────────────────────────────
def test_parse_iftable():
    P = snmp_io.IF_PREFIX  # 1.3.6.1.2.1.2.2.1.
    pairs = [
        (P + "2.1", "GigabitEthernet1/0/1"),       # ifDescr ifx1
        (P + "6.1", b"\x00\x0a\x14\x1e\x28\x32"),  # ifPhysAddress ifx1
        (P + "8.1", 1),                            # ifOperStatus up
        (P + "5.1", 1_000_000_000),                # ifSpeed 1Gbps
        (P + "2.2", "GigabitEthernet1/0/2"),
        (P + "8.2", 2),                            # down
    ]
    ift = snmp_io.parse_iftable(pairs)
    assert ift[1]["name"] == "GigabitEthernet1/0/1"
    assert ift[1]["mac"] == "00:0a:14:1e:28:32"
    assert ift[1]["status"] == "up"
    assert ift[1]["speed"] == 1_000_000_000
    assert ift[2]["status"] == "down"


# ── parse_ip_table + parse_arp ────────────────────────────────────────────────
def test_parse_ip_table():
    P = snmp_io.IP_PREFIX  # 1.3.6.1.2.1.4.20.1.
    pairs = [
        (P + "1.10.0.0.1", "10.0.0.1"),  # ipAdEntNetAddr
        (P + "2.10.0.0.1", 1),           # ipAdEntIfIndex → ifIndex 1
    ]
    ips = snmp_io.parse_ip_table(pairs)
    assert ips == {1: ["10.0.0.1"]}


def test_parse_arp():
    P = snmp_io.ARP_PREFIX  # 1.3.6.1.2.1.4.22.1.
    pairs = [
        (P + "3.1.10.0.0.5", "10.0.0.5"),           # ipNetToMediaNetAddress
        (P + "2.1.10.0.0.5", b"\xaa\xbb\xcc\xdd\xee\xff"),  # physAddress
    ]
    rows = snmp_io.parse_arp(pairs)
    assert rows == [(1, "10.0.0.5", "aa:bb:cc:dd:ee:ff")]


# ── parse_fdb + parse_bridge_port_if (decimal-octet MAC pairing) ──────────────
def test_parse_fdb_pairs_port_to_mac():
    addr_pairs = [(snmp_io.FDB_ADDR_PREFIX + ".0.10.20.30.40.50",
                   b"\x00\x0a\x14\x1e\x28\x32")]
    port_pairs = [(snmp_io.FDB_PORT_PREFIX + ".0.10.20.30.40.50", 4)]
    fdb = snmp_io.parse_fdb(addr_pairs)
    ports = {mac: port for mac, port in snmp_io.parse_fdb(port_pairs)}
    assert fdb == [("00:0a:14:1e:28:32", 0)]
    assert ports["00:0a:14:1e:28:32"] == 4


def test_parse_bridge_port_if():
    pairs = [(snmp_io.BRIDGE_PORT_IF_PREFIX + ".4", 1)]  # bridge port 4 → ifx1
    assert snmp_io.parse_bridge_port_if(pairs) == {4: 1}


# ── SnmpSession config errors ─────────────────────────────────────────────────
def test_snmp_session_requires_community():
    try:
        snmp_io.SnmpSession({"id": "d1", "address": "10.0.0.1"})
        assert False, "expected SnmpError"
    except snmp_io.SnmpError as e:
        assert "snmp_community" in str(e)


# ── High-level gathers via a fake session ─────────────────────────────────────
class _FakeSnmp:
    """Drop-in for SnmpSession: canned get/walk. Raises SnmpError if flagged."""

    def __init__(self, gets=None, walks=None, fail=False):
        self._gets = gets or {}
        self._walks = walks or {}
        self.fail = fail

    def get(self, oid):
        if self.fail:
            raise snmp_io.SnmpError("no SNMP response")
        return self._gets.get(oid)

    def walk(self, oid):
        if self.fail:
            raise snmp_io.SnmpError("no SNMP response")
        return self._walks.get(oid, [])


def test_snmp_probe_reachable():
    s = _FakeSnmp(gets={snmp_io.SYS_UPTIME: 12345})
    res = _run(snmp_io.snmp_probe(s))
    assert res["reachable"] is True
    assert isinstance(res["latency_ms"], int)


def test_snmp_probe_timeout_raises():
    s = _FakeSnmp(fail=True)
    try:
        _run(snmp_io.snmp_probe(s))
        assert False, "expected SnmpError"
    except snmp_io.SnmpError:
        pass


def test_snmp_get_device_info():
    s = _FakeSnmp(gets={snmp_io.SYS_DESCR: "Aruba JL658A 2530-48G",
                        snmp_io.SYS_NAME: "core-sw",
                        snmp_io.IF_NUMBER: 52})
    info = _run(snmp_io.snmp_get_device_info(s))
    assert info["model"] == "core-sw"
    assert info["interfaces_count"] == 52
    assert "Aruba" in info["firmware"]


def test_snmp_get_interfaces():
    P = snmp_io.IF_PREFIX
    IP = snmp_io.IP_PREFIX
    s = _FakeSnmp(walks={
        snmp_io.IF_PREFIX: [
            (P + "2.1", "Gi1/0/1"), (P + "6.1", b"\x00\x0a\x14\x1e\x28\x32"),
            (P + "8.1", 1), (P + "5.1", 1_000_000_000),
        ],
        snmp_io.IP_PREFIX: [
            (IP + "1.10.0.0.1", "10.0.0.1"), (IP + "2.10.0.0.1", 1),
        ],
    })
    rows = _run(snmp_io.snmp_get_interfaces(s))
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Gi1/0/1"
    assert r["mac"] == "00:0a:14:1e:28:32"
    assert r["ip"] == "10.0.0.1"
    assert r["status"] == "up"
    assert r["speed"] == 1_000_000_000


def test_snmp_get_arp_maps_interface_name():
    P = snmp_io.IF_PREFIX
    A = snmp_io.ARP_PREFIX
    s = _FakeSnmp(walks={
        snmp_io.IF_PREFIX: [(P + "2.1", "Gi1/0/1")],
        snmp_io.ARP_PREFIX: [
            (A + "3.1.10.0.0.5", "10.0.0.5"),
            (A + "2.1.10.0.0.5", b"\xaa\xbb\xcc\xdd\xee\xff"),
        ],
    })
    ift = snmp_io.parse_iftable(_run(asyncio.to_thread(s.walk, snmp_io.IF_PREFIX)))
    rows = _run(snmp_io.snmp_get_arp(s, ifaces=ift))
    assert rows == [{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff",
                     "interface": "Gi1/0/1"}]