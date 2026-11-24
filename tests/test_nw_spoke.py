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

_core_messaging = types.ModuleType("core.src.messaging")
_core_messaging_cp = types.ModuleType("core.src.messaging.control_plane")


class _BaseControlPlane:
    def __init__(self, spoke_id, secret=None, hub_secret=None, hub_url=None, onboarding_psk=None, tenant_id_hint=None):
        self.spoke_id = spoke_id
        self.secret = secret
        self.hub_url = hub_url
        self.module_type = None
        self.modules = {}

    def register_module(self, name, mod):
        self.modules[name] = mod

    async def run(self):
        pass


_core_messaging_cp.BaseControlPlane = _BaseControlPlane

sys.modules["core"] = _core
sys.modules["core.src"] = _core_src
sys.modules["core.src.base_spoke"] = _core_base
sys.modules["core.src.messaging"] = _core_messaging
sys.modules["core.src.messaging.control_plane"] = _core_messaging_cp

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
                "NW_GET_DEVICE_INFO", "NW_PROBE", "NW_GET_LLDP_NEIGHBORS"):
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


# ── Fleet reachability uses the heartbeat idle-guard, not a fixed wall clock ─
# Regression for the AOS-S DIST-SW case: a switch that streams a slow login
# banner takes longer than the old fixed 3s budget to reach a prompt, but keeps
# sending bytes — so it must stay reachable. A device that goes silent still
# fails fast. These stub build_driver with fakes whose probe beats (or doesn't).
import heartbeat  # noqa: E402


class _SlowButStreamingDriver:
    """probe() runs well past its idle_timeout in total, but beats on every
    step — the guard must let it finish and report reachable."""
    address = "10.0.0.9"
    fleet_idle_timeout = 0.2
    fleet_hard_cap = 5.0

    def __init__(self, device):
        self.device = device

    async def probe(self):
        for _ in range(6):                 # ~0.6s total, 0.1s < idle_timeout gaps
            await asyncio.sleep(0.1)
            heartbeat.beat()
        return {"status": "SUCCESS",
                "data": {"reachable": True, "latency_ms": 600}}


class _StalledDriver:
    """probe() never beats — the guard must cancel it and report unreachable."""
    address = "10.0.0.10"
    fleet_idle_timeout = 0.2
    fleet_hard_cap = 5.0

    def __init__(self, device):
        self.device = device

    async def probe(self):
        await asyncio.sleep(10)            # silent → idle timeout fires
        return {"status": "SUCCESS", "data": {"reachable": True}}


def test_fleet_probe_keeps_slow_streaming_device_reachable(monkeypatch):
    import nw_engine as _ne
    monkeypatch.setattr(_ne, "build_driver", lambda d: _SlowButStreamingDriver(d))
    spoke = _spoke_with([
        {"id": "sw1", "name": "DIST-SW", "object_type": "aos_switch",
         "address": "10.0.0.9"},
    ])
    res = _run(spoke.handle_command("NW_LIST_DEVICES", {}))
    row = res["data"][0]
    assert row["reachable"] is True
    assert row["latency_ms"] == 600


def test_fleet_probe_marks_silent_device_unreachable(monkeypatch):
    import nw_engine as _ne
    monkeypatch.setattr(_ne, "build_driver", lambda d: _StalledDriver(d))
    spoke = _spoke_with([
        {"id": "sw1", "name": "DEAD-SW", "object_type": "aos_switch",
         "address": "10.0.0.10"},
    ])
    res = _run(spoke.handle_command("NW_LIST_DEVICES", {}))
    assert res["data"][0]["reachable"] is False


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
                                  "ex_switch": "ssh", "gateway": "rest", "ipmi": "snmp"}


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

# ── NW_SCAN dispatch ─────────────────────────────────────────────────────────
def test_nw_scan_requires_targets():
    spoke = _spoke_with([])
    res = _run(spoke.handle_command("NW_SCAN", {"credentials": [{"name": "c", "username": "u"}]}))
    assert res["status"] == "ERROR"
    assert "targets" in res["message"]


def test_nw_scan_without_credentials_runs_discovery(monkeypatch):
    # Credentials are OPTIONAL: a scan with none must still run as a
    # discovery-only pass (reachability + open ports) instead of erroring, so
    # "what is on this subnet?" is answerable without a device account.
    import nw_scanner

    seen = {}

    class _FakeScanner:
        def __init__(self, creds, **kw):
            seen["creds"] = creds
            seen["use_nmap"] = kw.get("use_nmap")
        async def scan(self, targets, **kw):
            return {"status": "SUCCESS", "scanned": len(targets), "identified": [],
                    "reachable": [{"address": targets[0], "open_ports": [22, 443]}]}

    monkeypatch.setattr(nw_scanner, "NwScanner", _FakeScanner)
    spoke = _spoke_with([])
    res = _run(spoke.handle_command("NW_SCAN", {"targets": ["10.0.0.1"]}))
    assert res["status"] == "SUCCESS"
    assert res["identified"] == []
    assert res["reachable"][0]["open_ports"] == [22, 443]
    assert seen["creds"] == []
    # nmap is the only classifier left without credentials → on by default.
    assert seen["use_nmap"] is True


def test_nw_scan_explicit_use_nmap_false_wins_without_credentials(monkeypatch):
    # The credential-free nmap default must never override an explicit opt-out.
    import nw_scanner

    seen = {}

    class _FakeScanner:
        def __init__(self, creds, **kw):
            seen["use_nmap"] = kw.get("use_nmap")
        async def scan(self, targets, **kw):
            return {"status": "SUCCESS", "scanned": 1, "identified": [], "reachable": []}

    monkeypatch.setattr(nw_scanner, "NwScanner", _FakeScanner)
    spoke = _spoke_with([])
    _run(spoke.handle_command("NW_SCAN", {"targets": ["10.0.0.1"],
                                          "options": {"use_nmap": False}}))
    assert seen["use_nmap"] is False


def test_nw_scan_with_credentials_leaves_nmap_off_by_default(monkeypatch):
    # Credentialed scans keep the cheap default: SSH/SNMP identify, no nmap.
    import nw_scanner

    seen = {}

    class _FakeScanner:
        def __init__(self, creds, **kw):
            seen["use_nmap"] = kw.get("use_nmap")
        async def scan(self, targets, **kw):
            return {"status": "SUCCESS", "scanned": 1, "identified": [], "reachable": []}

    monkeypatch.setattr(nw_scanner, "NwScanner", _FakeScanner)
    spoke = _spoke_with([])
    _run(spoke.handle_command("NW_SCAN", {
        "targets": ["10.0.0.1"],
        "credentials": [{"name": "c", "username": "u", "password": "p"}]}))
    assert seen["use_nmap"] is False


def test_nw_scan_masks_nested_credentials():
    # Nested per-credential secrets must be masked in the log-data view.
    masked = NwSpoke._mask({
        "targets": ["10.0.0.1"],
        "credentials": [{"name": "c", "username": "admin", "password": "hunter2",
                         "snmp_community": "public"}],
    })
    cred = masked["credentials"][0]
    assert cred["password"] == "********"
    assert cred["snmp_community"] == "********"
    assert cred["username"] == "admin"  # non-secret passes through


def test_nw_scan_runs_with_injected_scanner(monkeypatch):
    # Patch NwScanner so no real network is touched; assert the envelope flows.
    import nw_scanner

    class _FakeScanner:
        def __init__(self, creds, **kw):
            self.creds = creds
            self.kw = kw
        async def scan(self, targets, **kw):
            return {"status": "SUCCESS", "scanned": len(targets),
                    "identified": [{"address": targets[0], "object_type": "cx_switch"}],
                    "reachable": []}

    monkeypatch.setattr(nw_scanner, "NwScanner", _FakeScanner)
    spoke = _spoke_with([])
    res = _run(spoke.handle_command("NW_SCAN", {
        "targets": ["10.0.0.1"],
        "credentials": [{"name": "c", "username": "admin", "password": "x"}],
        "options": {"try_snmp": False},
    }))
    assert res["status"] == "SUCCESS"
    assert res["identified"][0]["object_type"] == "cx_switch"


# ── LLDP neighbor parsing (crawl) ───────────────────────────────────────────
def test_parse_lldp_neighbors():
    from transports.cli_io import parse_lldp_neighbors
    text = """
    Local Port : 1/1/1
      Management Address : 10.0.0.2
    Local Port : 1/1/2
      Management Address : 10.0.0.3
      Neighbor Address   : 999.1.1.1
    """
    ips = parse_lldp_neighbors(text)
    assert ips == ["10.0.0.2", "10.0.0.3"]


# ── NW_GET_LLDP_NEIGHBORS: structured topology edges ────────────────────────
# The chassis id is canonicalised so an LLDP edge and a MAC-table row describe
# the SAME node when the hub stitches them together. It must be canonicalised
# only when it really is a MAC: LLDP chassis subtypes also carry interface
# names and locally-assigned strings, and lower-casing one of those invents a
# second identity for the device.

def _lldp_spoke(rows):
    spoke = _spoke_with([{"id": "d1", "name": "sw", "object_type": "aos_switch",
                          "address": "10.0.0.1", "transport": "ssh"}])

    async def _fake(device_id, tenant=None):
        return {"status": "SUCCESS", "data": list(rows), "message": ""}

    spoke.engine.get_lldp_neighbors = _fake
    return spoke


def _edge(**kw):
    base = {"local_port": "1", "remote_chassis": "", "remote_port": "2",
            "remote_name": "", "remote_mgmt_ip": "", "remote_descr": ""}
    base.update(kw)
    return base


def test_lldp_command_is_dispatched():
    spoke = _lldp_spoke([_edge(remote_chassis="00:0b:86:bc:49:87",
                               remote_name="OLKS-EDGE-1")])
    res = _run(spoke.handle_command("NW_GET_LLDP_NEIGHBORS", {"device_id": "d1"}))
    assert res["status"] == "SUCCESS"
    assert res["data"][0]["remote_name"] == "OLKS-EDGE-1"


def test_lldp_command_is_case_insensitive():
    spoke = _lldp_spoke([_edge(remote_chassis="000b.86bc.4987")])
    res = _run(spoke.handle_command("nw_get_lldp_neighbors", {"device_id": "d1"}))
    assert res["status"] == "SUCCESS"


def test_mac_chassis_id_is_canonicalised():
    spoke = _lldp_spoke([_edge(remote_chassis="000b.86bc.4987"),
                         _edge(local_port="2", remote_chassis="00 0b 86 bc 49 87"),
                         _edge(local_port="3", remote_chassis="00-0B-86-BC-49-87")])
    res = _run(spoke.handle_command("NW_GET_LLDP_NEIGHBORS", {"device_id": "d1"}))
    assert {r["remote_chassis"] for r in res["data"]} == {"00:0b:86:bc:49:87"}


def test_non_mac_chassis_id_is_left_alone():
    """"OLKS-CORE" lower-cased is a DIFFERENT node key than every other view of
    that switch uses — the bug this guard exists to prevent."""
    spoke = _lldp_spoke([_edge(remote_chassis="OLKS-CORE"),
                         _edge(local_port="2", remote_chassis="GigabitEthernet0/1")])
    res = _run(spoke.handle_command("NW_GET_LLDP_NEIGHBORS", {"device_id": "d1"}))
    assert [r["remote_chassis"] for r in res["data"]] == ["OLKS-CORE",
                                                          "GigabitEthernet0/1"]


def test_blank_chassis_id_survives():
    spoke = _lldp_spoke([_edge(remote_chassis="", remote_name="no-chassis")])
    res = _run(spoke.handle_command("NW_GET_LLDP_NEIGHBORS", {"device_id": "d1"}))
    assert res["data"][0]["remote_chassis"] == ""


# ── In-session LLDP failure yields ERROR, not a clean empty SUCCESS ─────────
# test_per_device_commands_error_against_fake_host above only covers a
# CONNECT-time failure: it never reaches cli_get_lldp_detail, so it could not
# see the state conflation the PR #119 panel found. This covers the in-session
# path — connected fine, then the command itself fails — which used to return
# `[]` and be wrapped as status SUCCESS with zero rows.
def test_lldp_in_session_failure_is_an_error_envelope():
    import nw_engine
    from transports import cli_io

    class _BrokenSession:
        async def run(self, cmd):
            raise cli_io.CliError("session dropped mid-command")

    class _CM:
        async def __aenter__(self):
            return _BrokenSession()

        async def __aexit__(self, *a):
            return False

    drv = nw_engine.SshCliDriver.__new__(nw_engine.SshCliDriver)
    drv.device = {"id": "d1", "address": "10.0.0.9", "object_type": "aos_switch"}
    drv.address = "10.0.0.9"
    drv.object_type = "aos_switch"
    drv._shared = None
    drv._session = lambda: _CM()

    res = _run(drv.get_lldp_neighbors())
    # The envelope, not the empty list, is what carries the distinction: _err
    # legitimately returns data=[] too, so the hub must read `status`.
    assert res["status"] == "ERROR", res
    assert "session dropped" in res.get("message", ""), res


def test_lldp_in_session_failure_matches_sibling_datums():
    """LLDP was the ONLY datum that swallowed in-session errors; arp/mac/
    interfaces already surfaced them. Pin the parity so it can't drift back."""
    import nw_engine
    from transports import cli_io

    class _BrokenSession:
        async def run(self, cmd):
            raise cli_io.CliError("session dropped mid-command")

    class _CM:
        async def __aenter__(self):
            return _BrokenSession()

        async def __aexit__(self, *a):
            return False

    def _drv():
        d = nw_engine.SshCliDriver.__new__(nw_engine.SshCliDriver)
        d.device = {"id": "d1", "address": "10.0.0.9", "object_type": "aos_switch"}
        d.address = "10.0.0.9"
        d.object_type = "aos_switch"
        d._shared = None
        d._session = lambda: _CM()
        return d

    statuses = {name: _run(getattr(_drv(), name)())["status"]
                for name in ("get_arp", "get_mac_table", "get_interfaces",
                             "get_lldp_neighbors")}
    assert set(statuses.values()) == {"ERROR"}, statuses


def test_ping_sweep_returns_only_answering_ips():
    async def ping(ip, timeout):
        return {"reachable": True if ip == "10.1.1.1" else False}
    res = _run(_spoke_with([])._ping_sweep(
        {"targets": ["10.1.1.1", "10.1.1.254", ""]}, ping=ping))
    assert res == {"status": "SUCCESS", "probed": 2, "alive": ["10.1.1.1"]}


def test_ipmi_snmp_driver_falls_back_when_the_controller_serves_no_iftable(monkeypatch):
    from transports import snmp_io

    class _S:
        def get(self, oid):
            return {snmp_io.BMC_SERIAL_OIDS[0]: "No Such Object currently exists at this OID",
                    snmp_io.BMC_SERIAL_OIDS[1]: "SN123",
                    snmp_io.BMC_HOST_MODEL_OIDS[1]: "PowerEdge R640"}.get(oid, "")

    async def info(_s):
        return {"model": "iLO 5", "serial": "", "firmware": "", "hostname": "h",
                "mac": "", "interfaces_count": 0}

    async def no_ifaces(_s):
        return []

    monkeypatch.setattr(snmp_io, "snmp_get_device_info", info)
    monkeypatch.setattr(snmp_io, "snmp_get_interfaces", no_ifaces)
    d = build_driver({"id": "d", "object_type": "ipmi", "address": "10.1.1.5"})
    monkeypatch.setattr(d, "_session", lambda: _S())
    data = _run(d.get_device_info())["data"]
    assert data["serial"] == "SN123" and data["host_model"] == "PowerEdge R640"
    rows = _run(d.get_interfaces())["data"]
    assert rows[0]["ip"] == "10.1.1.5" and rows[0]["status"] == "up"


def test_device_info_ttl_cache_reduces_driver_calls(monkeypatch):
    """nw#293 / nw#294: verify get_device_info caches SUCCESS and ERROR responses."""
    from nw_engine import NwEngine

    calls = {"count": 0}

    class _MockDriver:
        async def get_device_info(self):
            calls["count"] += 1
            return {"status": "SUCCESS", "data": {"model": "AOS-S", "serial": "CN123"}}

    eng = NwEngine([{"id": "sw-1", "object_type": "aos_switch", "address": "10.0.0.1"}])
    monkeypatch.setattr(eng, "_driver_for", lambda dev_id, tenant=None: _MockDriver())

    # Call 1: misses cache, queries driver
    res1 = _run(eng.get_device_info("sw-1"))
    assert res1["status"] == "SUCCESS"
    assert calls["count"] == 1

    # Call 2: hits cache, driver call count remains 1
    res2 = _run(eng.get_device_info("sw-1"))
    assert res2["status"] == "SUCCESS"
    assert res2["data"]["serial"] == "CN123"
    assert calls["count"] == 1

    # set_devices invalidates cache
    eng.set_devices([{"id": "sw-1", "object_type": "aos_switch", "address": "10.0.0.1"}])

    # Call 3: queries driver again after cache invalidation
    res3 = _run(eng.get_device_info("sw-1"))
    assert res3["status"] == "SUCCESS"
    assert calls["count"] == 2


def test_device_info_icmp_reachability_gate(monkeypatch):
    """nw#293 / nw#297: verify get_device_info short-circuits when device is ICMP offline."""
    import time
    from nw_engine import NwEngine

    calls = {"count": 0}

    class _MockDriver:
        async def get_device_info(self):
            calls["count"] += 1
            return {"status": "SUCCESS", "data": {"model": "AOS-S", "serial": "CN123"}}

    eng = NwEngine([{"id": "sw-down", "object_type": "aos_switch", "address": "10.0.0.99"}])
    monkeypatch.setattr(eng, "_driver_for", lambda dev_id, tenant=None: _MockDriver())

    # Mark device as confirmed unreachable via ICMP
    eng.reachability["sw-down"] = {
        "reachable": False,
        "latency_ms": None,
        "checked_at": time.monotonic(),
    }
    assert eng.recently_unreachable("sw-down") is True

    # Calling get_device_info should short-circuit with ERROR without invoking driver
    res = _run(eng.get_device_info("sw-down"))
    assert res["status"] == "ERROR"
    assert "offline (ICMP confirmed)" in res["message"]
    assert calls["count"] == 0

    # Device recovery: when reachability flips to True, next call reaches driver immediately
    eng.reachability["sw-down"]["reachable"] = True
    assert eng.recently_unreachable("sw-down") is False
    res_recovered = _run(eng.get_device_info("sw-down"))
    assert res_recovered["status"] == "SUCCESS"
    assert calls["count"] == 1


def test_device_info_tenant_isolation_before_gate(monkeypatch):
    """Defect A fix: cross-tenant lookup must return 'not found', never 'offline'."""
    import time
    from nw_engine import NwEngine

    # sw-tenant-a belongs to tenant 't-a'
    eng = NwEngine([{"id": "sw-a", "object_type": "aos_switch", "address": "10.0.0.1", "tenant_id": "t-a"}])
    # Mark it down in reachability
    eng.reachability["sw-a"] = {
        "reachable": False,
        "latency_ms": None,
        "checked_at": time.monotonic(),
    }

    # Query from tenant 't-b' must return 'not found', NOT leak offline status
    res = _run(eng.get_device_info("sw-a", tenant="t-b"))
    assert res["status"] == "ERROR"
    assert "not found" in res["message"]
    assert "offline" not in res["message"]


def test_device_info_address_bypass_gate(monkeypatch):
    """Stub probes passing address= must bypass the device-level ICMP gate."""
    import time
    from nw_engine import NwEngine

    calls = {"count": 0}

    class _MockDriver:
        async def get_device_info(self):
            calls["count"] += 1
            return {"status": "SUCCESS", "data": {"model": "AOS-S", "serial": "CN-STUB"}}

    import nw_engine
    monkeypatch.setattr(nw_engine, "build_driver", lambda d: _MockDriver())

    eng = NwEngine([{"id": "sw-down", "object_type": "aos_switch", "address": "10.0.0.99"}])
    eng.reachability["sw-down"] = {
        "reachable": False,
        "latency_ms": None,
        "checked_at": time.monotonic(),
    }
    assert eng.recently_unreachable("sw-down") is True

    # Passing address= targets an alternate IP/stub and must reach driver
    res = _run(eng.get_device_info("sw-down", address="10.0.0.100"))
    assert res["status"] == "SUCCESS"
    assert calls["count"] == 1


def test_device_info_concurrency_coalescing(monkeypatch):
    """Defect B fix: concurrent in-flight requests coalesce into a single driver call."""
    import asyncio
    from nw_engine import NwEngine

    calls = {"count": 0}

    class _SlowDriver:
        async def get_device_info(self):
            calls["count"] += 1
            await asyncio.sleep(0.05)
            return {"status": "SUCCESS", "data": {"model": "AOS-S", "serial": "CN-COALESCE"}}

    eng = NwEngine([{"id": "sw-slow", "object_type": "aos_switch", "address": "10.0.0.5"}])
    monkeypatch.setattr(eng, "_driver_for", lambda dev_id, tenant=None: _SlowDriver())

    async def _concurrent_calls():
        # Launch 5 concurrent calls
        tasks = [eng.get_device_info("sw-slow") for _ in range(5)]
        results = await asyncio.gather(*tasks)
        return results

    results = _run(_concurrent_calls())
    assert len(results) == 5
    for r in results:
        assert r["status"] == "SUCCESS"
        assert r["data"]["serial"] == "CN-COALESCE"
    # Exactly 1 driver call occurred despite 5 concurrent callers
    assert calls["count"] == 1


def test_device_info_leader_cancellation(monkeypatch):
    """H1 fix: cancelling the leader resolves the future with an error so waiting callers never hang."""
    import asyncio
    from nw_engine import NwEngine

    started = asyncio.Event()

    class _HangingDriver:
        async def get_device_info(self):
            started.set()
            await asyncio.sleep(10.0)
            return {"status": "SUCCESS", "data": {"model": "AOS-S"}}

    eng = NwEngine([{"id": "sw-lead", "object_type": "aos_switch", "address": "10.0.0.6"}])
    monkeypatch.setattr(eng, "_driver_for", lambda dev_id, tenant=None: _HangingDriver())

    async def _test():
        leader_task = asyncio.create_task(eng.get_device_info("sw-lead"))
        await started.wait()

        # Launch 2 waiting callers
        waiter1 = asyncio.create_task(eng.get_device_info("sw-lead"))
        waiter2 = asyncio.create_task(eng.get_device_info("sw-lead"))
        await asyncio.sleep(0.01)

        # Cancel leader
        leader_task.cancel()
        try:
            await leader_task
        except asyncio.CancelledError:
            pass

        # Waiters must not hang and must receive an ERROR envelope
        res1 = await asyncio.wait_for(waiter1, timeout=1.0)
        res2 = await asyncio.wait_for(waiter2, timeout=1.0)
        assert res1["status"] == "ERROR"
        assert res2["status"] == "ERROR"
        assert "aborted" in res1["message"]
        assert "aborted" in res2["message"]

    _run(_test())


def test_device_info_waiting_caller_cancellation(monkeypatch):
    """H1 fix: cancelling a waiting caller does not cancel the shared flight for leader or others."""
    import asyncio
    from nw_engine import NwEngine

    started = asyncio.Event()

    class _SlowDriver:
        async def get_device_info(self):
            started.set()
            await asyncio.sleep(0.05)
            return {"status": "SUCCESS", "data": {"model": "AOS-S", "serial": "CN-SHIELD"}}

    eng = NwEngine([{"id": "sw-shield", "object_type": "aos_switch", "address": "10.0.0.7"}])
    monkeypatch.setattr(eng, "_driver_for", lambda dev_id, tenant=None: _SlowDriver())

    async def _test():
        leader = asyncio.create_task(eng.get_device_info("sw-shield"))
        await started.wait()

        waiter1 = asyncio.create_task(eng.get_device_info("sw-shield"))
        waiter2 = asyncio.create_task(eng.get_device_info("sw-shield"))
        await asyncio.sleep(0.01)

        # Cancel waiter 1
        waiter1.cancel()
        try:
            await waiter1
        except asyncio.CancelledError:
            pass

        # Leader and waiter2 must still complete with SUCCESS
        res_lead = await leader
        res_wait2 = await waiter2
        assert res_lead["status"] == "SUCCESS"
        assert res_lead["data"]["serial"] == "CN-SHIELD"
        assert res_wait2["status"] == "SUCCESS"
        assert res_wait2["data"]["serial"] == "CN-SHIELD"

    _run(_test())


def test_device_info_cache_hard_bound():
    """H2 fix: cache enforces a hard limit of 500 entries across all write paths."""
    from nw_engine import NwEngine

    eng = NwEngine([])
    # Query 600 unknown devices (writes to cache on 'not found' path)
    for i in range(600):
        _run(eng.get_device_info(f"dev-{i}"))

    assert len(eng._device_info_cache) <= 500


def test_nwspoke_stop_background_loops():
    """Verify stop_background_loops cancels all running background tasks and empties the list."""
    async def _test():
        spoke = NwSpoke("nw-spoke-bg", {})
        spoke.start_background_loops()
        assert len(spoke._bg_tasks) == 2
        tasks = list(spoke._bg_tasks)
        spoke.stop_background_loops()
        assert len(spoke._bg_tasks) == 0
        await asyncio.sleep(0)
        assert all(t.cancelled() or t.done() for t in tasks)

    _run(_test())


def test_control_plane_lifecycle_teardown(monkeypatch):
    """Verify NwControlPlane initializes module_type properly and run_hub_mode cleans up in finally."""
    from control_plane import NwControlPlane

    cp = NwControlPlane(
        "spoke-test",
        "secret-123",
        hub_url="wss://user:pass@hub.local:443",
        config={"key": "val"},
    )
    # Finding 2 & 6 contract check
    assert cp.module_type == "nw"
    assert cp.config == {"key": "val"}

    # Track whether log masking occurred
    logged_messages = []
    import control_plane
    monkeypatch.setattr(control_plane.logger, "info", lambda msg: logged_messages.append(msg))

    async def _test():
        await cp.run_hub_mode()
        # Verify unregistration and log masking
        assert "nw" not in cp.modules
        masked_logs = [m for m in logged_messages if "wss://user:***@hub.local:443" in m]
        assert len(masked_logs) == 1
        assert not any("pass@" in m for m in logged_messages)

    _run(_test())


def test_mac_table_icmp_reachability_gate(monkeypatch):
    """nw#235: verify get_mac_table short-circuits in 0.0s when device is ICMP offline."""
    import time
    from nw_engine import NwEngine

    calls = {"count": 0}

    class _MockDriver:
        object_type = "generic_switch"
        async def get_mac_table(self):
            calls["count"] += 1
            return {"status": "SUCCESS", "data": [{"mac": "aa:bb:cc:dd:ee:ff", "port": "1"}]}

    eng = NwEngine([{"id": "sw-down", "object_type": "generic_switch", "address": "10.0.0.99"}])
    monkeypatch.setattr(eng, "_driver_for", lambda dev_id, tenant=None: _MockDriver() if dev_id == "sw-down" else None)

    # Mark device as confirmed unreachable via ICMP
    eng.reachability["sw-down"] = {
        "reachable": False,
        "latency_ms": None,
        "checked_at": time.monotonic(),
    }
    assert eng.recently_unreachable("sw-down") is True

    # get_mac_table must short-circuit without invoking driver
    res = _run(eng.get_mac_table("sw-down"))
    assert res["status"] == "ERROR"
    assert "offline (ICMP confirmed)" in res["message"]
    assert calls["count"] == 0

    # Device recovery: when reachability flips to True, call reaches driver immediately
    eng.reachability["sw-down"]["reachable"] = True
    assert eng.recently_unreachable("sw-down") is False
    res_recovered = _run(eng.get_mac_table("sw-down"))
    assert res_recovered["status"] == "SUCCESS"
    assert calls["count"] == 1


def test_datum_queries_icmp_reachability_gate(monkeypatch):
    """nw#235: verify all individual datum queries short-circuit when device is offline."""
    import time
    from nw_engine import NwEngine

    calls = []

    class _MockDriver:
        object_type = "generic_switch"
        async def get_arp(self):
            calls.append("arp")
            return {"status": "SUCCESS", "data": []}
        async def get_interfaces(self):
            calls.append("interfaces")
            return {"status": "SUCCESS", "data": []}
        async def get_lldp_neighbors(self):
            calls.append("lldp")
            return {"status": "SUCCESS", "data": []}
        async def get_endpoints(self):
            calls.append("endpoints")
            return {"status": "SUCCESS", "data": []}
        async def get_vlans(self):
            calls.append("vlans")
            return {"status": "SUCCESS", "data": []}

    eng = NwEngine([{"id": "sw-down", "object_type": "generic_switch", "address": "10.0.0.99"}])
    monkeypatch.setattr(eng, "_driver_for", lambda dev_id, tenant=None: _MockDriver() if dev_id == "sw-down" else None)

    eng.reachability["sw-down"] = {
        "reachable": False,
        "latency_ms": None,
        "checked_at": time.monotonic(),
    }

    for method_name in ("get_arp", "get_interfaces", "get_lldp_neighbors", "get_endpoints", "get_vlans"):
        method = getattr(eng, method_name)
        res = _run(method("sw-down"))
        assert res["status"] == "ERROR", f"{method_name} should have failed"
        assert "offline (ICMP confirmed)" in res["message"], f"{method_name} message unexpected: {res}"

    assert len(calls) == 0, "No driver calls should have been made for offline device"


def test_mac_table_tenant_isolation_before_gate(monkeypatch):
    """nw#235 tenant isolation: cross-tenant lookup must return 'not found', never 'offline'."""
    import time
    from nw_engine import NwEngine

    # sw-a belongs to tenant 't-a'
    eng = NwEngine([{"id": "sw-a", "object_type": "generic_switch", "address": "10.0.0.1", "tenant_id": "t-a"}])
    eng.reachability["sw-a"] = {
        "reachable": False,
        "latency_ms": None,
        "checked_at": time.monotonic(),
    }

    # Query with wrong tenant 't-b' must return 'not found', NOT leak offline status
    res = _run(eng.get_mac_table("sw-a", tenant="t-b"))
    assert res["status"] == "ERROR"
    assert "not found" in res["message"]
    assert "offline" not in res["message"]






