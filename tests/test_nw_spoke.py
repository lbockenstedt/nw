"""Tests for the nw spoke: command dispatch, sensitive-data masking, the
get_version path, the driver registry, and the result-envelope contract.

Self-contained: inserts src/ on sys.path and uses the flat imports the spoke
uses itself, so it runs without a package install (no core.src dependency —
NwSpoke imports BaseSpoke via a try/except that falls back to a local stub
only when core is absent; here we stub BaseSpoke before importing).
"""
import os
import sys
import types
import asyncio
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Stub core.src.base_spoke so the spoke imports without the lm repo present.
# NwSpoke only needs BaseSpoke.__init__(spoke_id, config) + the two abstract
# method slots; we provide a minimal concrete base.
_core = types.ModuleType("core")
_core_src = types.ModuleType("core.src")
_core_base = types.ModuleType("core.src.base_spoke")


class _BaseSpoke:
    def __init__(self, spoke_id, config):
        self.spoke_id = spoke_id
        self.config = config

    async def handle_command(self, command_type, data):
        raise NotImplementedError

    async def get_status(self):
        raise NotImplementedError


_core_base.BaseSpoke = _BaseSpoke
sys.modules["core"] = _core
sys.modules["core.src"] = _core_src
sys.modules["core.src.base_spoke"] = _core_base

from nw_spoke import NwSpoke, _SENSITIVE  # noqa: E402
from nw_engine import (build_driver, NwEngine, SshCliDriver,  # noqa: E402
                       RestDriver, SnmpDriver, _norm_mac, _DEFAULT_TRANSPORT)

logging.disable(logging.CRITICAL)  # silence stub INFO logs during tests


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _spoke_with(devices):
    return NwSpoke("nw-spoke-1", {"devices": devices})


# ── get_version reads repo-root VERSION ──────────────────────────────────────
def test_get_version_reads_repo_root_version():
    spoke = _spoke_with([])
    # NwSpoke.get_version reads <src parent>/VERSION = the repo root VERSION.
    assert spoke.get_version() == ".00"


# ── UPDATE_CONFIG stores the fleet + reports count (creds masked in logs) ───
def test_update_config_stores_devices():
    spoke = _spoke_with([])
    res = _run(spoke.handle_command("UPDATE_CONFIG", {
        "devices": [
            {"id": "d1", "name": "core-sw", "object_type": "cx_switch",
             "address": "10.0.0.1", "password": "supersecret"},
        ],
    }))
    assert res["status"] == "SUCCESS"
    assert res["device_count"] == 1
    assert spoke.engine.devices[0]["id"] == "d1"


# ── Sensitive-data masking ──────────────────────────────────────────────────
def test_mask_redacts_sensitive_fields():
    masked = NwSpoke._mask({"device_id": "d1", "password": "hunter2",
                            "api_token": "tok", "name": "core-sw"})
    assert masked["password"] == "********"
    assert masked["api_token"] == "********"
    assert masked["name"] == "core-sw"  # non-sensitive passes through
    assert "password" in _SENSITIVE


# ── NW_LIST_DEVICES returns envelope + no credentials ───────────────────────
def test_list_devices_envelope_and_no_creds():
    spoke = _spoke_with([
        {"id": "d1", "name": "sw1", "object_type": "aos_switch",
         "address": "10.0.0.2", "password": "secret"},
    ])
    res = _run(spoke.handle_command("NW_LIST_DEVICES", {}))
    assert res["status"] == "SUCCESS"
    assert isinstance(res["data"], list)
    assert res["data"][0]["id"] == "d1"
    assert "password" not in res["data"][0]
    assert res["data"][0]["transport"] == "ssh"  # aos_switch default


# ── Per-device commands return SUCCESS envelopes (stubbed) ──────────────────
def test_per_device_commands_success_envelopes():
    spoke = _spoke_with([
        {"id": "d1", "name": "sw1", "object_type": "cx_switch", "address": "10.0.0.3"},
    ])
    for cmd, key in [("NW_GET_MAC_TABLE", "data"), ("NW_GET_ARP", "data"),
                     ("NW_GET_INTERFACES", "data"), ("NW_GET_DEVICE_INFO", "data"),
                     ("NW_PROBE", "data")]:
        res = _run(spoke.handle_command(cmd, {"device_id": "d1"}))
        assert res["status"] == "SUCCESS", f"{cmd} -> {res}"
        assert key in res, f"{cmd} missing {key}"


def test_per_device_unknown_device_errors():
    spoke = _spoke_with([])
    res = _run(spoke.handle_command("NW_GET_ARP", {"device_id": "nope"}))
    assert res["status"] == "ERROR"
    assert "not found" in res["message"]


# ── NW_RUN_CONFIG returns applied/errors lists ──────────────────────────────
def test_run_config_returns_applied():
    spoke = _spoke_with([{"id": "d1", "object_type": "ex_switch", "address": "10.0.0.4"}])
    res = _run(spoke.handle_command("NW_RUN_CONFIG",
               {"device_id": "d1", "commands": ["set system name foo", "commit"]}))
    assert res["status"] == "SUCCESS"
    assert res["applied"] == ["set system name foo", "commit"]
    assert res["errors"] == []


# ── get_version / GET_VERSION ────────────────────────────────────────────────
def test_get_version_command():
    spoke = _spoke_with([])
    res = _run(spoke.handle_command("get_version", {}))
    assert res == {"status": "SUCCESS", "version": ".00"}


# ── Unknown command ──────────────────────────────────────────────────────────
def test_unknown_command_errors():
    spoke = _spoke_with([])
    res = _run(spoke.handle_command("NW_DOES_NOT_EXIST", {}))
    assert res["status"] == "ERROR"
    assert "not supported" in res["message"]


# ── Driver registry: object_type → default transport + driver class ─────────
def test_driver_registry_default_transports():
    assert _DEFAULT_TRANSPORT == {"aos_switch": "ssh", "cx_switch": "rest",
                                  "ex_switch": "ssh", "gateway": "rest"}


def test_build_driver_picks_by_transport_override():
    d = build_driver({"id": "d", "object_type": "cx_switch", "address": "a",
                      "transport": "snmp"})
    assert isinstance(d, SnmpDriver)
    d2 = build_driver({"id": "d", "object_type": "aos_switch", "address": "a"})
    assert isinstance(d2, SshCliDriver)  # default ssh
    d3 = build_driver({"id": "d", "object_type": "cx_switch", "address": "a"})
    assert isinstance(d3, RestDriver)  # default rest


def test_build_driver_unknown_type_returns_none():
    assert build_driver({"id": "d", "object_type": "nope", "address": "a"}) is None


# ── MAC normalization ────────────────────────────────────────────────────────
def test_norm_mac():
    assert _norm_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert _norm_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
    assert _norm_mac("unknown") == ""
    assert _norm_mac("") == ""
    assert _norm_mac(None) == ""


# ── get_status ───────────────────────────────────────────────────────────────
def test_get_status():
    spoke = _spoke_with([{"id": "d1", "object_type": "cx_switch", "address": "a"}])
    st = _run(spoke.get_status())
    assert st["module"] == "nw"
    assert st["device_count"] == 1
    assert st["connection"] == "CONNECTED"