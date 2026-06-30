"""Tests for NwEngine.poll (the POLL NOW aggregate) and the NW_POLL spoke
dispatch (MAC normalization on the way out).

Uses a fake driver injected via ``engine._driver_for`` so no real IO happens.
Stubs BaseSpoke before importing the spoke (same pattern as test_nw_spoke.py).
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Stub core.src.base_spoke so nw_spoke imports without the lm repo present.
_core = types.ModuleType("core")
_core_src = types.ModuleType("core.src")
_core_base = types.ModuleType("core.src.base_spoke")


class _BaseSpoke:
    def __init__(self, spoke_id, config):
        self.spoke_id = spoke_id
        self.config = config


_core_base.BaseSpoke = _BaseSpoke
sys.modules["core"] = _core
sys.modules["core.src"] = _core_src
sys.modules["core.src.base_spoke"] = _core_base

from nw_engine import NwEngine  # noqa: E402
from nw_spoke import NwSpoke  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeDriver:
    """Returns canned envelopes per method; lets us drive poll() deterministically."""

    def __init__(self, probe=None, info=None, interfaces=None, arp=None,
                 mac=None):
        self._probe = probe if probe is not None else _ok({"reachable": True,
                                                           "latency_ms": 12})
        self._info = info if info is not None else _ok({"model": "GW",
                                                        "serial": "S1",
                                                        "firmware": "1.0",
                                                        "interfaces_count": 2})
        self._interfaces = interfaces if interfaces is not None else _ok([
            {"name": "1", "ip": "10.0.0.1", "mac": "aabbccddeeff", "vlan": "",
             "status": "up", "speed": 1_000_000_000}])
        self._arp = arp if arp is not None else _ok([
            {"ip": "10.0.0.5", "mac": "aabbccddeeff", "interface": "1"}])
        self._mac = mac if mac is not None else _ok([
            {"mac": "aabbccddeeff", "vlan": "10", "interface": "1"}])

    async def probe(self): return self._probe
    async def get_device_info(self): return self._info
    async def get_interfaces(self): return self._interfaces
    async def get_arp(self): return self._arp
    async def get_mac_table(self): return self._mac


def _ok(data, message=""): return {"status": "SUCCESS", "data": data,
                                   "message": message}
def _err(message): return {"status": "ERROR", "data": [], "message": message}


def _engine_with_fake(driver):
    eng = NwEngine([{"id": "d1", "object_type": "gateway", "address": "10.0.0.1"}])
    eng._driver_for = lambda device_id: driver  # type: ignore
    return eng


# ── poll aggregates all five datums on success ────────────────────────────────
def test_poll_success_aggregates():
    eng = _engine_with_fake(_FakeDriver())
    res = _run(eng.poll("d1"))
    assert res["status"] == "SUCCESS"
    d = res["data"]
    assert d["reachable"] is True
    assert d["latency_ms"] == 12
    assert d["device_info"]["model"] == "GW"
    assert len(d["interfaces"]) == 1
    assert len(d["arp"]) == 1
    assert len(d["mac_table"]) == 1
    assert res["errors"] == []
    assert "reachable=True" in res["message"]


# ── poll tolerates partial failures (one datum ERROR → empty list + errors) ───
def test_poll_partial_failure_tolerated():
    drv = _FakeDriver(interfaces=_err("snmp interfaces: timeout"),
                      mac=_err("snmp mac table: no response"))
    eng = _engine_with_fake(drv)
    res = _run(eng.poll("d1"))
    # probe still succeeded → reachable True → SUCCESS overall
    assert res["status"] == "SUCCESS"
    assert res["data"]["reachable"] is True
    assert res["data"]["interfaces"] == []
    assert res["data"]["mac_table"] == []
    assert len(res["errors"]) == 2
    assert any("interfaces" in e for e in res["errors"])
    assert any("mac" in e for e in res["errors"])


# ── poll with unreachable probe → PARTIAL ─────────────────────────────────────
def test_poll_unreachable_is_partial():
    drv = _FakeDriver(probe=_err("no SNMP response"))
    eng = _engine_with_fake(drv)
    res = _run(eng.poll("d1"))
    assert res["data"]["reachable"] is False
    assert res["status"] == "PARTIAL"
    assert any("probe" in e for e in res["errors"])


# ── poll unknown device → ERROR ───────────────────────────────────────────────
def test_poll_unknown_device_errors():
    eng = NwEngine([{"id": "d1", "object_type": "gateway", "address": "a"}])
    res = _run(eng.poll("nope"))
    assert res["status"] == "ERROR"
    assert "not found" in res["message"]


# ── NW_POLL dispatch canonicalizes MACs across arp/mac/interfaces ──────────────
def test_nw_poll_command_normalizes_macs():
    spoke = NwSpoke("nw-1", {"devices": [
        {"id": "d1", "object_type": "gateway", "address": "10.0.0.1"}]})
    # Replace the engine's poll with a canned result carrying un-normalized MACs.
    canned = {
        "status": "SUCCESS",
        "data": {
            "reachable": True, "latency_ms": 5,
            "device_info": {}, "interfaces": [
                {"name": "1", "mac": "AA-BB-CC-DD-EE-FF", "ip": "", "vlan": "",
                 "status": "up", "speed": 0}],
            "arp": [{"ip": "10.0.0.5", "mac": "AABBCCDDEEFF", "interface": "1"}],
            "mac_table": [{"mac": "aa-bb-cc-dd-ee-ff", "vlan": "10",
                           "interface": "1"}],
        },
        "errors": [],
        "message": "ok",
    }
    spoke.engine.poll = lambda device_id: _async(canned)  # type: ignore
    res = _run(spoke.handle_command("NW_POLL", {"device_id": "d1"}))
    d = res["data"]
    assert d["arp"][0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert d["mac_table"][0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert d["interfaces"][0]["mac"] == "aa:bb:cc:dd:ee:ff"


async def _async(val):
    return val