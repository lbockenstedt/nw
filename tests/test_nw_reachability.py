"""Tests for the nw ICMP reachability gate: icmp_ping (unprivileged system
ping), NwEngine.ping/recently_unreachable, and the poll short-circuit that
skips SSH/SNMP/REST for a box confirmed offline.

No real network IO — icmp_ping is exercised via a stubbed subprocess, and the
poll gate via a driver that fails loudly if any transport method is touched.
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import nw_engine  # noqa: E402
from nw_engine import NwEngine, icmp_ping  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── icmp_ping three-state result ─────────────────────────────────────────────

class _FakeProc:
    def __init__(self, returncode, out=b""):
        self.returncode = returncode
        self._out = out

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass


def _patch_ping(monkey_which, exec_factory):
    nw_engine.shutil.which = monkey_which
    nw_engine.asyncio.create_subprocess_exec = exec_factory


def test_icmp_ping_unknown_when_no_binary():
    orig_which = nw_engine.shutil.which
    try:
        nw_engine.shutil.which = lambda _n: None
        res = _run(icmp_ping("10.0.0.1"))
        assert res["reachable"] is None      # UNKNOWN, never gates
        assert res["latency_ms"] is None
    finally:
        nw_engine.shutil.which = orig_which


def test_icmp_ping_blank_host_unknown():
    res = _run(icmp_ping(""))
    assert res["reachable"] is None


def test_icmp_ping_up_parses_latency():
    orig_which = nw_engine.shutil.which
    orig_exec = nw_engine.asyncio.create_subprocess_exec
    try:
        async def _exec(*a, **k):
            return _FakeProc(0, b"64 bytes from 10.0.0.1: icmp_seq=0 ttl=64 time=1.23 ms")
        _patch_ping(lambda _n: "/bin/ping", _exec)
        res = _run(icmp_ping("10.0.0.1"))
        assert res["reachable"] is True
        assert res["latency_ms"] == 1.23
    finally:
        nw_engine.shutil.which = orig_which
        nw_engine.asyncio.create_subprocess_exec = orig_exec


def test_icmp_ping_down_on_nonzero_rc():
    orig_which = nw_engine.shutil.which
    orig_exec = nw_engine.asyncio.create_subprocess_exec
    try:
        async def _exec(*a, **k):
            return _FakeProc(1, b"")
        _patch_ping(lambda _n: "/bin/ping", _exec)
        res = _run(icmp_ping("10.0.0.9"))
        assert res["reachable"] is False     # CONFIRMED down → gates
        assert res["latency_ms"] is None
    finally:
        nw_engine.shutil.which = orig_which
        nw_engine.asyncio.create_subprocess_exec = orig_exec


# ── recently_unreachable gating rules ────────────────────────────────────────

def _engine():
    return NwEngine([{"id": "d1", "object_type": "gateway", "address": "10.0.0.1"}])


def test_recently_unreachable_confirmed_down_gates():
    eng = _engine()
    eng.reachability["d1"] = {"reachable": False, "latency_ms": None,
                              "checked_at": nw_engine.time.monotonic()}
    assert eng.recently_unreachable("d1") is True


def test_recently_unreachable_unknown_does_not_gate():
    eng = _engine()
    eng.reachability["d1"] = {"reachable": None, "latency_ms": None,
                              "checked_at": nw_engine.time.monotonic()}
    assert eng.recently_unreachable("d1") is False


def test_recently_unreachable_up_does_not_gate():
    eng = _engine()
    eng.reachability["d1"] = {"reachable": True, "latency_ms": 5,
                              "checked_at": nw_engine.time.monotonic()}
    assert eng.recently_unreachable("d1") is False


def test_recently_unreachable_stale_does_not_gate():
    eng = _engine()
    eng.reachability["d1"] = {"reachable": False, "latency_ms": None,
                              "checked_at": nw_engine.time.monotonic() - 10_000}
    assert eng.recently_unreachable("d1") is False


def test_recently_unreachable_absent_does_not_gate():
    assert _engine().recently_unreachable("d1") is False


# ── ping() caches the verdict ────────────────────────────────────────────────

def test_ping_updates_cache():
    orig_which = nw_engine.shutil.which
    orig_exec = nw_engine.asyncio.create_subprocess_exec
    try:
        async def _exec(*a, **k):
            return _FakeProc(0, b"time=2.0 ms")
        _patch_ping(lambda _n: "/bin/ping", _exec)
        eng = _engine()
        res = _run(eng.ping("d1"))
        assert res["status"] == "SUCCESS"
        assert res["reachable"] is True
        assert eng.reachability["d1"]["reachable"] is True
        assert "checked_at" in eng.reachability["d1"]
    finally:
        nw_engine.shutil.which = orig_which
        nw_engine.asyncio.create_subprocess_exec = orig_exec


def test_ping_unknown_device_errors():
    assert _run(_engine().ping("nope"))["status"] == "ERROR"


# ── poll gate: a confirmed-down box is NOT SSH/SNMP/REST-polled ───────────────

class _ExplodingDriver:
    """Any transport call fails the test — the gate must not touch it."""
    address = "10.0.0.1"
    object_type = "gateway"

    async def probe(self):
        raise AssertionError("probe() must NOT run for an offline device")

    async def get_device_info(self):
        raise AssertionError("get_device_info() must NOT run")

    async def get_interfaces(self):
        raise AssertionError("get_interfaces() must NOT run")

    async def get_arp(self):
        raise AssertionError("get_arp() must NOT run")

    async def get_mac_table(self):
        raise AssertionError("get_mac_table() must NOT run")


def test_poll_skips_transport_when_offline():
    eng = _engine()
    eng._driver_for = lambda device_id, tenant=None: _ExplodingDriver()  # type: ignore
    eng.reachability["d1"] = {"reachable": False, "latency_ms": None,
                              "checked_at": nw_engine.time.monotonic()}
    res = _run(eng.poll("d1"))
    assert res["status"] == "ERROR"
    assert res["data"]["reachable"] is False
    assert res["data"]["device_info"] == {}
    assert res["data"]["interfaces"] == []
    assert any("skipped" in e for e in res["errors"])


def test_poll_proceeds_when_reachability_unknown():
    """UNKNOWN (ping unavailable) must fail OPEN — the poll still runs."""
    from test_nw_poll import _FakeDriver  # reuse the canned success driver
    eng = _engine()
    eng._driver_for = lambda device_id, tenant=None: _FakeDriver()  # type: ignore
    eng.reachability["d1"] = {"reachable": None, "latency_ms": None,
                              "checked_at": nw_engine.time.monotonic()}
    res = _run(eng.poll("d1"))
    assert res["status"] == "SUCCESS"
    assert res["data"]["reachable"] is True
