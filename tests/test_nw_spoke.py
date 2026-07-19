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

# Read the repo-root VERSION dynamically so the autobump bot's last-segment bump
# (.00 → .01 → …) doesn't break these assertions (never hardcode the version).
_VERSION_FILE = os.path.join(os.path.dirname(__file__), "..", "VERSION")
with open(_VERSION_FILE) as _f:
    _NW_VERSION = _f.read().strip()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _spoke_with(devices):
    return NwSpoke("nw-spoke-1", {"devices": devices})


# ── get_version reads repo-root VERSION ──────────────────────────────────────
def test_get_version_reads_repo_root_version():
    spoke = _spoke_with([])
    # NwSpoke.get_version reads <src parent>/VERSION = the repo root VERSION.
    assert spoke.get_version() == _NW_VERSION


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


# ── Per-device commands return ERROR against an unreachable/fake host ────────
# Real drivers actually attempt IO now; with no real device at 10.0.0.3 (and no
# credentials), every datum returns an ERROR envelope — the opposite of the old
# stubbed SUCCESS. This is the whole point of the rework: silence is no longer
# mistaken for "reachable, zero rows".
def test_per_device_commands_error_against_fake_host():
    # transport=snmp with no community → SnmpSession raises in the session ctor
    # (no IO attempted), so every datum returns ERROR fast. This is the real
    # behavior change vs the old stubs: a misconfigured device is no longer
    # reported as "SUCCESS, zero rows".
    spoke = _spoke_with([
        {"id": "d1", "name": "sw1", "object_type": "cx_switch",
         "address": "10.0.0.3", "transport": "snmp"},
    ])
    for cmd in ("NW_GET_MAC_TABLE", "NW_GET_ARP", "NW_GET_INTERFACES",
                "NW_GET_DEVICE_INFO", "NW_PROBE"):
        res = _run(spoke.handle_command(cmd, {"device_id": "d1"}))
        assert res["status"] == "ERROR", f"{cmd} -> {res}"
        assert "message" in res


def test_per_device_unknown_device_errors():
    spoke = _spoke_with([])
    res = _run(spoke.handle_command("NW_GET_ARP", {"device_id": "nope"}))
    assert res["status"] == "ERROR"
    assert "not found" in res["message"]


# ── Tenant scoping (Stage 1) ───────────────────────────────────────────────
# The fleet list + per-device commands accept an optional ``tenant`` filter;
# a device is visible to a tenant if its ``tenant_id`` matches OR equals the
# shared tenant id (mirrors the hub's shared-tenant-flag invariant). Omitting
# ``tenant`` returns the whole fleet (backward-compatible with a hub that
# doesn't pass one). build_driver is stubbed to None so list_devices' 3s
# reachability probe is skipped (no real IO; reachable stays None) — these
# tests assert the tenant FILTER, not the probe.
def _no_probe(monkeypatch):
    import nw_engine as _ne
    monkeypatch.setattr(_ne, "build_driver", lambda d: None)


def _fleet():
    # acme-owned, othercorp-owned, shared, and unassigned (admin-only) devices.
    return [
        {"id": "acme-sw", "name": "acme", "object_type": "aos_switch",
         "address": "10.0.0.2", "tenant_id": "acme"},
        {"id": "other-sw", "name": "other", "object_type": "aos_switch",
         "address": "10.0.0.3", "tenant_id": "othercorp"},
        {"id": "shared-sw", "name": "shared", "object_type": "aos_switch",
         "address": "10.0.0.4", "tenant_id": "shared"},
        {"id": "unassigned-sw", "name": "unassigned", "object_type": "aos_switch",
         "address": "10.0.0.5", "tenant_id": ""},
    ]


def test_list_devices_no_tenant_returns_whole_fleet(monkeypatch):
    _no_probe(monkeypatch)
    spoke = _spoke_with(_fleet())
    spoke.engine.shared_tenant_id = "shared"
    res = _run(spoke.handle_command("NW_LIST_DEVICES", {}))
    assert res["status"] == "SUCCESS"
    ids = {r["id"] for r in res["data"]}
    assert ids == {"acme-sw", "other-sw", "shared-sw", "unassigned-sw"}


def test_list_devices_tenant_filter_returns_own_plus_shared(monkeypatch):
    _no_probe(monkeypatch)
    spoke = _spoke_with(_fleet())
    spoke.engine.shared_tenant_id = "shared"
    res = _run(spoke.handle_command("NW_LIST_DEVICES", {"tenant": "acme"}))
    ids = {r["id"] for r in res["data"]}
    assert ids == {"acme-sw", "shared-sw"}      # own + shared; not othercorp/unassigned


def test_list_devices_rows_carry_tenant_id_and_shared(monkeypatch):
    _no_probe(monkeypatch)
    spoke = _spoke_with(_fleet())
    spoke.engine.shared_tenant_id = "shared"
    res = _run(spoke.handle_command("NW_LIST_DEVICES", {"tenant": "acme"}))
    by_id = {r["id"]: r for r in res["data"]}
    assert by_id["acme-sw"]["tenant_id"] == "acme"
    assert by_id["acme-sw"]["shared"] is False
    assert by_id["shared-sw"]["tenant_id"] == "shared"
    assert by_id["shared-sw"]["shared"] is True


def test_list_devices_no_shared_tenant_excludes_shared_flag(monkeypatch):
    _no_probe(monkeypatch)
    # Without shared_tenant_id pushed, a tenant filter matches only own-tenant
    # (the shared device is NOT visible under any tenant filter) — defense
    # degrades safely; the hub filter is authoritative anyway.
    spoke = _spoke_with(_fleet())
    # shared_tenant_id stays "" (never pushed)
    res = _run(spoke.handle_command("NW_LIST_DEVICES", {"tenant": "acme"}))
    ids = {r["id"] for r in res["data"]}
    assert ids == {"acme-sw"}


def test_per_device_command_rejects_other_tenant(monkeypatch):
    _no_probe(monkeypatch)
    spoke = _spoke_with(_fleet())
    spoke.engine.shared_tenant_id = "shared"
    # acme-sw is acme-owned; resolving it as othercorp → the gate denies it
    # (no existence leak across tenants).
    assert spoke.engine._get_device("acme-sw", "othercorp") is None
    # own-tenant resolves:
    assert spoke.engine._get_device("acme-sw", "acme") is not None


def test_per_device_command_shared_visible_to_any_tenant(monkeypatch):
    _no_probe(monkeypatch)
    spoke = _spoke_with(_fleet())
    spoke.engine.shared_tenant_id = "shared"
    # shared-sw resolves under any tenant filter (shared device visible to all).
    assert spoke.engine._get_device("shared-sw", "acme") is not None
    assert spoke.engine._get_device("shared-sw", "othercorp") is not None
    # unassigned resolves only when no tenant filter is applied (admin path):
    assert spoke.engine._get_device("unassigned-sw", "acme") is None
    assert spoke.engine._get_device("unassigned-sw", None) is not None


def test_update_config_carries_shared_tenant_id():
    spoke = _spoke_with([])
    _run(spoke.handle_command("UPDATE_CONFIG", {
        "devices": [{"id": "d1", "object_type": "aos_switch", "address": "10.0.0.2",
                     "tenant_id": "acme"}],
        "shared_tenant_id": "shared",
    }))
    assert spoke.engine.shared_tenant_id == "shared"
    assert spoke.engine.devices[0]["tenant_id"] == "acme"


def test_init_reads_shared_tenant_id_from_config():
    spoke = NwSpoke("nw-1", {"devices": [], "shared_tenant_id": "shared"})
    assert spoke.engine.shared_tenant_id == "shared"


# ── NW_RUN_CONFIG returns applied/errors lists ──────────────────────────────
def test_run_config_not_implemented():
    spoke = _spoke_with([{"id": "d1", "object_type": "ex_switch", "address": "10.0.0.4"}])
    res = _run(spoke.handle_command("NW_RUN_CONFIG",
               {"device_id": "d1", "commands": ["set system name foo", "commit"]}))
    assert res["status"] == "ERROR"
    assert res["applied"] == []
    assert res["errors"] and "not implemented" in res["errors"][0]


# ── get_version / GET_VERSION ────────────────────────────────────────────────
def test_get_version_command():
    spoke = _spoke_with([])
    res = _run(spoke.handle_command("get_version", {}))
    assert res == {"status": "SUCCESS", "version": _NW_VERSION}


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