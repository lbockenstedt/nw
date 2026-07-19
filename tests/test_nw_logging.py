"""Tests for the nw module's logging standard: every per-datum outcome and
every command result is logged (INFO on success, ERROR on failure), matching
the opnsense spoke / hub sync-loop precedent where ERROR lines surface in the
hub's GET_ERROR_LOGS / Error Log tab.

Uses a fake driver injected via ``engine._driver_for`` so no real IO happens.
Re-enables logging (``test_nw_spoke`` calls ``logging.disable(CRITICAL)`` at
import, which is global state) so ``caplog`` can capture the records.
"""
import asyncio
import logging
import os
import sys
import types

import pytest

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

# Undo the global logging.disable(CRITICAL) that test_nw_spoke sets at import
# (it's process-global state, not per-module) so caplog can capture INFO/ERROR.
logging.disable(logging.NOTSET)

from nw_engine import NwEngine  # noqa: E402
from nw_spoke import NwSpoke  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _ok(data, message=""): return {"status": "SUCCESS", "data": data,
                                    "message": message}
def _err(message): return {"status": "ERROR", "data": [], "message": message}


class _FakeDriver:
    """Returns canned envelopes per method AND carries the tag attributes the
    engine's _log_datum reads (object_type/transport/address) so the log line
    can be asserted on."""

    def __init__(self, probe=None, info=None, interfaces=None, arp=None,
                 mac=None):
        self.object_type = "gateway"
        self.transport = "rest"
        self.address = "10.0.0.1"
        self._probe = probe if probe is not None else _ok(
            {"reachable": True, "latency_ms": 12})
        self._info = info if info is not None else _ok({"model": "GW"})
        self._interfaces = interfaces if interfaces is not None else _ok(
            [{"name": "1"}])
        self._arp = arp if arp is not None else _ok(
            [{"ip": "10.0.0.5", "mac": "aabbccddeeff", "interface": "1"}])
        self._mac = mac if mac is not None else _ok(
            [{"mac": "aabbccddeeff", "vlan": "10", "interface": "1"}])

    async def probe(self): return self._probe
    async def get_device_info(self): return self._info
    async def get_interfaces(self): return self._interfaces
    async def get_arp(self): return self._arp
    async def get_mac_table(self): return self._mac


def _engine_with_fake(driver):
    eng = NwEngine([{"id": "d1", "object_type": "gateway", "address": "10.0.0.1"}])
    eng._driver_for = lambda device_id, tenant=None: driver  # type: ignore
    return eng


def _records(caplog, name):
    return [r for r in caplog.records if r.name == name]


# ── per-datum success → INFO with a row count ─────────────────────────────────
def test_engine_logs_success_datum(caplog):
    caplog.set_level(logging.INFO)
    eng = _engine_with_fake(_FakeDriver())
    with caplog.at_level(logging.INFO, logger="NwEngine"):
        res = _run(eng.get_mac_table("d1"))
    assert res["status"] == "SUCCESS"
    msgs = [r.getMessage() for r in _records(caplog, "NwEngine")
            if r.levelno == logging.INFO]
    assert any("mac_table" in m and "-> 1 rows" in m for m in msgs), msgs


# ── per-datum failure → ERROR (carries "error" so GET_ERROR_LOGS captures) ───
def test_engine_logs_error_datum(caplog):
    eng = _engine_with_fake(_FakeDriver(arp=_err("snmp arp: no response")))
    with caplog.at_level(logging.DEBUG, logger="NwEngine"):
        _run(eng.get_arp("d1"))
    errs = [r for r in _records(caplog, "NwEngine") if r.levelno == logging.ERROR]
    assert errs, "expected an ERROR log for the failed datum"
    assert "error" in errs[0].getMessage().lower()
    assert "arp" in errs[0].getMessage()


# ── poll logs a summary line + a sub-datum ERROR per failing datum ────────────
def test_engine_poll_logs_summary_and_sub_errors(caplog):
    drv = _FakeDriver(interfaces=_err("snmp interfaces: timeout"),
                      mac=_err("snmp mac table: no response"))
    eng = _engine_with_fake(drv)
    with caplog.at_level(logging.INFO, logger="NwEngine"):
        res = _run(eng.poll("d1"))
    assert res["status"] == "SUCCESS"  # probe still ok → reachable
    recs = _records(caplog, "NwEngine")
    # one ERROR per failing sub-datum
    err_msgs = [r.getMessage() for r in recs if r.levelno == logging.ERROR]
    assert any("interfaces" in m for m in err_msgs), err_msgs
    assert any("mac_table" in m for m in err_msgs), err_msgs
    # a final INFO summary line
    summary = [r.getMessage() for r in recs if r.levelno == logging.INFO
               and "nw poll" in r.getMessage() and "status=SUCCESS" in r.getMessage()]
    assert summary, [r.getMessage() for r in recs]


# ── unknown device → WARNING (not a transport ERROR) ─────────────────────────
def test_engine_unknown_device_logs_warning(caplog):
    eng = NwEngine([{"id": "d1", "object_type": "gateway", "address": "a"}])
    with caplog.at_level(logging.DEBUG, logger="NwEngine"):
        res = _run(eng.get_arp("nope"))
    assert res["status"] == "ERROR"
    warns = [r for r in _records(caplog, "NwEngine") if r.levelno == logging.WARNING]
    assert warns and "not in fleet" in warns[0].getMessage()


# ── spoke logs every command result: INFO on success ─────────────────────────
def test_spoke_logs_command_result_success(caplog):
    spoke = NwSpoke("nw-1", {})
    with caplog.at_level(logging.INFO, logger="NwSpoke"):
        res = _run(spoke.handle_command("get_version", {}))
    assert res["status"] == "SUCCESS"
    msgs = [r.getMessage() for r in _records(caplog, "NwSpoke")
            if r.levelno == logging.INFO and "result" in r.getMessage()]
    assert msgs and "get_version" in msgs[0], msgs


# ── spoke logs ERROR on a failed command (surfaces in GET_ERROR_LOGS) ─────────
def test_spoke_logs_command_result_error(caplog):
    spoke = NwSpoke("nw-1", {})
    with caplog.at_level(logging.DEBUG, logger="NwSpoke"):
        res = _run(spoke.handle_command("NW_DOES_NOT_EXIST", {}))
    assert res["status"] == "ERROR"
    errs = [r for r in _records(caplog, "NwSpoke") if r.levelno == logging.ERROR]
    assert errs and "error" in errs[0].getMessage().lower()
    assert "NW_DOES_NOT_EXIST" in errs[0].getMessage()


# ── spoke surfaces a poll's sub-error count in the result ERROR line ─────────
def test_spoke_logs_poll_suberrors(caplog):
    spoke = NwSpoke("nw-1", [{"id": "d1", "object_type": "gateway",
                              "address": "10.0.0.1"}])
    # Canned poll result with two sub-errors (no real IO).
    canned = {"status": "SUCCESS", "errors": ["arp: x", "mac: y"],
              "data": {"reachable": True, "latency_ms": 1, "device_info": {},
                       "interfaces": [], "arp": [], "mac_table": []},
              "message": "reachable=True, 0 interface(s), 0 arp, 0 mac"}

    async def _canned_poll(device_id, tenant=None):
        return canned
    spoke.engine.poll = _canned_poll  # type: ignore
    with caplog.at_level(logging.DEBUG, logger="NwSpoke"):
        _run(spoke.handle_command("NW_POLL", {"device_id": "d1"}))
    errs = [r.getMessage() for r in _records(caplog, "NwSpoke")
            if r.levelno == logging.ERROR]
    assert errs and "sub-error" in errs[0], errs