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

# ── SnmpSession against a fake pysnmp 7.x v1arch asyncio API — nw#103 ────────
# pysnmp 7.x removed the synchronous hlapi this module used to import, so SNMP
# was dead in the field while every test here still passed: the tests stubbed
# SnmpSession itself and never exercised the pysnmp plumbing. These drive the
# REAL SnmpSession.get/walk against a fake that enforces the 7.x contract —
# SnmpDispatcher is a sync context manager, UdpTransportTarget.create() must be
# awaited, walk_cmd is an async generator — so a regression to the 6.x shapes,
# or using the dispatcher after its `with` block closed it, fails loudly.
class _FakeOid:
    def __init__(self, dotted):
        self._d = dotted

    def getOid(self):
        return self._d


class _FakeVal:
    def __init__(self, txt):
        self._t = txt

    def prettyPrint(self):
        return self._t


class _FakeDispatcher:
    instances = []

    def __init__(self):
        self.closed = False
        _FakeDispatcher.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        return False


class _FakeTarget:
    def __init__(self, addr, timeout, retries):
        self.addr, self.timeout, self.retries = addr, timeout, retries


class _FakeTransportTarget:
    created = []

    @classmethod
    async def create(cls, address, timeout=None, retries=None):
        t = _FakeTarget(address, timeout, retries)
        cls.created.append(t)
        return t


def _install_fake(monkeypatch, *, get_result=None, walk_rows=None, get_raises=None):
    """Point SnmpSession._hlapi at the fake API and assert 7.x call shapes."""
    _FakeDispatcher.instances.clear()
    _FakeTransportTarget.created.clear()
    seen = {}

    async def _get_cmd(dispatcher, auth, target, *varbinds, **kw):
        assert isinstance(dispatcher, _FakeDispatcher)
        # The dispatcher must still be OPEN: the command has to run INSIDE the
        # `with SnmpDispatcher()` block, not after it.
        assert dispatcher.closed is False, "dispatcher used after close"
        assert isinstance(target, _FakeTarget), "transport target must be awaited"
        seen["auth"] = auth
        if get_raises:
            raise get_raises
        return get_result

    async def _walk_cmd(dispatcher, auth, target, varbind, **kw):
        assert dispatcher.closed is False, "dispatcher used after close"
        assert isinstance(target, _FakeTarget), "transport target must be awaited"
        # Without this the walk runs past the requested subtree to end-of-MIB.
        assert kw.get("lexicographicMode") is False, "walk must be subtree-bounded"
        seen["walk_kw"] = kw
        for row in (walk_rows or []):
            yield row

    def _hlapi(self):
        return (_FakeDispatcher, lambda c, mpModel=None: ("community", c, mpModel),
                _FakeTransportTarget, lambda x: x, lambda o: o, _get_cmd, _walk_cmd)

    monkeypatch.setattr(snmp_io.SnmpSession, "_hlapi", _hlapi, raising=True)
    return seen


_DEV = {"address": "10.0.0.1", "snmp_community": "public"}


def test_get_uses_v1arch_asyncio_and_returns_the_value(monkeypatch):
    seen = _install_fake(monkeypatch,
                         get_result=(None, 0, 0, [(_FakeOid("1.3.6.1.2.1.1.1.0"),
                                                   _FakeVal("ArubaOS-CX"))]))
    s = snmp_io.SnmpSession(_DEV, timeout=3.0, retries=2)
    assert s.get(snmp_io.SYS_DESCR) == "ArubaOS-CX"
    # mpModel=1 is what selects SNMPv2c; losing it silently downgrades to v1.
    assert seen["auth"] == ("community", "public", 1)
    t = _FakeTransportTarget.created[0]
    assert t.addr == ("10.0.0.1", 161) and t.timeout == 3.0 and t.retries == 2


def test_dispatcher_is_closed_after_the_call(monkeypatch):
    _install_fake(monkeypatch, get_result=(None, 0, 0, []))
    snmp_io.SnmpSession(_DEV).get(snmp_io.SYS_DESCR)
    assert _FakeDispatcher.instances and all(d.closed for d in _FakeDispatcher.instances)


def test_get_with_no_varbinds_is_none(monkeypatch):
    _install_fake(monkeypatch, get_result=(None, 0, 0, []))
    assert snmp_io.SnmpSession(_DEV).get(snmp_io.SYS_DESCR) is None


def test_get_error_indication_raises_snmp_error(monkeypatch):
    _install_fake(monkeypatch, get_result=("no SNMP response received", 0, 0, []))
    try:
        snmp_io.SnmpSession(_DEV).get(snmp_io.SYS_DESCR)
    except snmp_io.SnmpError as e:
        assert "10.0.0.1" in str(e)
    else:
        raise AssertionError("expected SnmpError")


def test_get_error_status_raises_snmp_error(monkeypatch):
    _install_fake(monkeypatch, get_result=(None, 2, 1, []))
    try:
        snmp_io.SnmpSession(_DEV).get(snmp_io.SYS_DESCR)
    except snmp_io.SnmpError as e:
        assert "at 1" in str(e)
    else:
        raise AssertionError("expected SnmpError")


def test_walk_collects_rows_as_oid_value_pairs(monkeypatch):
    rows = [(None, 0, 0, [(_FakeOid("1.3.6.1.2.1.2.2.1.2.1"), _FakeVal("eth0"))]),
            (None, 0, 0, [(_FakeOid("1.3.6.1.2.1.2.2.1.2.2"), _FakeVal("eth1"))])]
    _install_fake(monkeypatch, walk_rows=rows)
    got = snmp_io.SnmpSession(_DEV).walk(snmp_io.IF_PREFIX + snmp_io.IF_DESCR)
    assert got == [("1.3.6.1.2.1.2.2.1.2.1", "eth0"),
                   ("1.3.6.1.2.1.2.2.1.2.2", "eth1")]


def test_walk_timeout_on_first_row_raises(monkeypatch):
    _install_fake(monkeypatch, walk_rows=[("No SNMP response received", 0, 0, [])])
    try:
        snmp_io.SnmpSession(_DEV).walk(snmp_io.ARP_PREFIX)
    except snmp_io.SnmpError:
        pass
    else:
        raise AssertionError("expected SnmpError")


def test_walk_timeout_after_some_rows_keeps_what_it_has(monkeypatch):
    """A partial table beats an exception: the gathers upstream merge whatever
    rows they get, and a large ifTable often times out mid-walk."""
    rows = [(None, 0, 0, [(_FakeOid("1.3.6.1.2.1.4.22.1.3.1.1"), _FakeVal("10.0.0.5"))]),
            ("No SNMP response received", 0, 0, [])]
    _install_fake(monkeypatch, walk_rows=rows)
    got = snmp_io.SnmpSession(_DEV).walk(snmp_io.ARP_PREFIX)
    assert got == [("1.3.6.1.2.1.4.22.1.3.1.1", "10.0.0.5")]


def test_walk_stops_on_error_status(monkeypatch):
    rows = [(None, 5, 0, [(_FakeOid("1.2.3"), _FakeVal("x"))])]
    _install_fake(monkeypatch, walk_rows=rows)
    assert snmp_io.SnmpSession(_DEV).walk(snmp_io.ARP_PREFIX) == []


def test_repeated_calls_on_one_thread_still_work(monkeypatch):
    """_run closes its private loop each time; leaving the closed loop installed
    would break every later call made on the same pooled thread."""
    _install_fake(monkeypatch,
                  get_result=(None, 0, 0, [(_FakeOid("1.1"), _FakeVal("ok"))]))
    s = snmp_io.SnmpSession(_DEV)
    assert [s.get(snmp_io.SYS_DESCR) for _ in range(3)] == ["ok", "ok", "ok"]


def test_missing_community_still_rejected_before_any_io():
    try:
        snmp_io.SnmpSession({"address": "10.0.0.1"})
    except snmp_io.SnmpError as e:
        assert "snmp_community" in str(e)
    else:
        raise AssertionError("expected SnmpError")


# ── _oid_str ─────────────────────────────────────────────────────────────────
def test_oid_str_prefers_get_oid():
    assert snmp_io._oid_str(_FakeOid("1.3.6.1.2.1.1.1.0")) == "1.3.6.1.2.1.1.1.0"


def test_oid_str_strips_a_leading_dot():
    assert snmp_io._oid_str(_FakeOid(".1.3.6.1")) == "1.3.6.1"


def test_oid_str_falls_back_to_as_numbers():
    class _Old:
        def asNumbers(self):
            return (1, 3, 6, 1)
    assert snmp_io._oid_str(_Old()) == "1.3.6.1"


def test_oid_str_last_resort_is_str():
    class _Bare:
        def __str__(self):
            return "1.2.3"
    assert snmp_io._oid_str(_Bare()) == "1.2.3"
