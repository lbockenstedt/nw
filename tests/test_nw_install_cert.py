"""nw INSTALL_CERT — install a hub-delivered LE cert on a fleet device.

AOS-CX (``cx_switch``) is the cert-capable family today: the cert+key are
pushed inline over REST v10 (no SCP) and bound to the https-server via the
system configuration. ``aos_switch`` is a hard blocker (on-switch CSR model,
no external-key import); ``ex_switch`` / ``gateway`` return a clear ERROR
naming the missing plumbing. These tests cover the REST sequence (with a fake
session), the v10-prefix + cert-name helpers, the engine's per-family
dispatch, and the spoke's INSTALL_CERT routing by ``identifier``.
"""
import os
import sys
import types
import asyncio
import logging
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Stub core.src.base_spoke so the spoke imports without the lm repo present
# (same pattern as test_nw_spoke.py).
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

from transports import rest_io  # noqa: E402
from transports.rest_io import RestError, _cx_cert_name, _cx_v10_prefix  # noqa: E402
from nw_spoke import NwSpoke  # noqa: E402
from nw_engine import NwEngine  # noqa: E402

logging.disable(logging.CRITICAL)  # silence stub INFO/ERROR logs during tests

_PEM = "-----BEGIN CERTIFICATE-----\nMIIfake\n-----END CERTIFICATE-----\n"
_KEY = "-----BEGIN PRIVATE KEY-----\nMIIkey\n-----END PRIVATE KEY-----\n"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── _cx_cert_name sanitization ───────────────────────────────────────────────
def test_cx_cert_name_sanitization():
    assert _cx_cert_name("a.example.com") == "lm-le-a-example-com"
    # Wildcard leading label → 'wild'.
    assert _cx_cert_name("*.lab.example.com") == "lm-le-wild-lab-example-com"
    # Empty domain → fallback.
    assert _cx_cert_name("") == "lm-le-cert"
    # Disallowed chars → hyphen, runs collapsed, stripped.
    assert _cx_cert_name("weird_ host!@#.com") == "lm-le-weird-host-com"
    # Capped at 63 chars.
    assert len(_cx_cert_name("x" * 200)) == 63
    assert _cx_cert_name("x" * 200).startswith("lm-le-")


# ── _cx_v10_prefix ───────────────────────────────────────────────────────────
class _FwSession:
    """Fake session whose GET returns a firmware_version (or raises)."""
    def __init__(self, fw="RL.10.13.0001", raise_=False):
        self._fw = fw
        self._raise = raise_

    async def get(self, path):
        if self._raise:
            raise RuntimeError("no v1 system endpoint")
        return {"firmware_version": self._fw}


def test_cx_v10_prefix_env_override(monkeypatch):
    monkeypatch.setenv("LM_NW_CX_REST_VER", "v10_11")
    assert _run(_cx_v10_prefix(_FwSession(fw="RL.10.13.0001"))) == "v10_11"


def test_cx_v10_prefix_strips_leading_slash(monkeypatch):
    monkeypatch.setenv("LM_NW_CX_REST_VER", "/v10_09")
    assert _run(_cx_v10_prefix(_FwSession())) == "v10_09"


def test_cx_v10_prefix_firmware_parse():
    assert _run(_cx_v10_prefix(_FwSession(fw="FL.10.08.0001"))) == "v10_08"
    assert _run(_cx_v10_prefix(_FwSession(fw="XL.10.4.0001"))) == "v10_04"


def test_cx_v10_prefix_default_when_unavailable(monkeypatch):
    monkeypatch.delenv("LM_NW_CX_REST_VER", raising=False)
    # firmware GET raises → default v10_09.
    assert _run(_cx_v10_prefix(_FwSession(raise_=True))) == "v10_09"
    # firmware string has no 10.XX → default v10_09.
    assert _run(_cx_v10_prefix(_FwSession(fw="unknown"))) == "v10_09"


# ── rest_install_cert sequence (fake session — no HTTP) ──────────────────────
class _FakeSession:
    """Records GETs/PUTs; returns a firmware_version for /rest/v1/system and a
    system-configuration object for the selector=configuration GET."""
    def __init__(self, fw="RL.10.13.0001",
                 syscfg=None, no_assoc=False):
        self._fw = fw
        self._syscfg = syscfg if syscfg is not None else {
            "certificate_association": {"https-server": "self-signed"},
            "hostname": "edge",
        }
        self._no_assoc = no_assoc
        self.puts = []
        self.gets = []

    async def get(self, path):
        self.gets.append(path)
        if path == "/rest/v1/system":
            return {"firmware_version": self._fw}
        if "system?selector=configuration" in path:
            return dict(self._syscfg)
        return {}

    async def put(self, path, json=None):
        self.puts.append((path, json))
        return None


def test_rest_install_cert_cx_sequence():
    s = _FakeSession()
    data = _run(rest_io.rest_install_cert(
        s, "cx_switch", _PEM, _KEY, "", "a.example.com"))
    assert data == {"cert_name": "lm-le-a-example-com",
                    "service": "https-server", "rest_prefix": "v10_13"}
    # 1. cert+key PUT to the versioned certificates path.
    cert_puts = [(p, b) for p, b in s.puts if "certificates" in p]
    assert len(cert_puts) == 1
    p, body = cert_puts[0]
    assert p == "/rest/v10_13/certificates/lm-le-a-example-com"
    assert "BEGIN CERTIFICATE" in body["certificate"]
    assert "PRIVATE KEY" in body["certificate"]
    assert body["password"] == ""
    # 2. system GET (selector=configuration) then PUT with the https-server
    #    association updated to the new cert name.
    sys_puts = [(p, b) for p, b in s.puts
                if p == "/rest/v10_13/system" and "certificates" not in p]
    assert len(sys_puts) == 1
    sent = sys_puts[0][1]
    assert sent["certificate_association"]["https-server"] == "lm-le-a-example-com"
    # The rest of the system config round-trips unchanged (hostname preserved).
    assert sent["hostname"] == "edge"


def test_rest_install_cert_non_cx_raises():
    s = _FakeSession()
    with pytest.raises(RestError):
        _run(rest_io.rest_install_cert(
            s, "gateway", _PEM, _KEY, "", "a.example.com"))


def test_rest_install_cert_invalid_pem_raises():
    s = _FakeSession()
    with pytest.raises(RestError):
        _run(rest_io.rest_install_cert(
            s, "cx_switch", "not a cert", _KEY, "", "a.example.com"))


def test_rest_install_cert_no_association_object_raises():
    s = _FakeSession(syscfg={"hostname": "edge"}, no_assoc=True)
    with pytest.raises(RestError):
        _run(rest_io.rest_install_cert(
            s, "cx_switch", _PEM, _KEY, "", "a.example.com"))


# ── NwEngine.install_cert dispatch ───────────────────────────────────────────
_CX = {"id": "edge-sw-1", "name": "edge", "object_type": "cx_switch",
       "address": "10.0.0.1", "username": "admin", "password": "pw",
       "transport": "rest"}
_CX_SSH = {"id": "edge-sw-1", "object_type": "cx_switch",
           "address": "10.0.0.1", "username": "admin", "password": "pw",
           "transport": "ssh"}
_AOS = {"id": "sw-1", "object_type": "aos_switch", "address": "10.0.0.2",
        "username": "admin", "password": "pw"}
_EX = {"id": "ex-1", "object_type": "ex_switch", "address": "10.0.0.3",
       "username": "admin", "password": "pw"}
_GW = {"id": "gw-1", "object_type": "gateway", "address": "10.0.0.4",
       "api_token": "tok"}


async def _fake_install(session, object_type, fullchain, privkey, chain, domain):
    assert object_type == "cx_switch"
    return {"cert_name": "lm-le-fake", "service": "https-server",
            "rest_prefix": "v10_13"}


def test_engine_install_cert_cx_switch_success(monkeypatch):
    monkeypatch.setattr(rest_io, "rest_install_cert", _fake_install)
    eng = NwEngine([_CX])
    res = _run(eng.install_cert("edge-sw-1", _PEM, _KEY, "", "a.example.com"))
    assert res["status"] == "SUCCESS"
    assert "lm-le-fake" in res["message"]
    assert "10.0.0.1" in res["message"]


def test_engine_install_cert_cx_switch_ssh_transport_is_error():
    # A cx_switch device forced to SSH transport can't use the REST cert path
    # (CLI PEM-paste isn't wired) → clear ERROR naming the gap.
    eng = NwEngine([_CX_SSH])
    res = _run(eng.install_cert("edge-sw-1", _PEM, _KEY, "", "a.example.com"))
    assert res["status"] == "ERROR"
    assert "REST transport" in res["message"]


def test_engine_install_cert_aos_switch_is_error():
    eng = NwEngine([_AOS])
    res = _run(eng.install_cert("sw-1", _PEM, _KEY, "", "a.example.com"))
    assert res["status"] == "ERROR"
    assert "external private key" in res["message"]


def test_engine_install_cert_ex_switch_is_error():
    eng = NwEngine([_EX])
    res = _run(eng.install_cert("ex-1", _PEM, _KEY, "", "a.example.com"))
    assert res["status"] == "ERROR"
    assert "not yet implemented" in res["message"]


def test_engine_install_cert_gateway_is_error():
    eng = NwEngine([_GW])
    res = _run(eng.install_cert("gw-1", _PEM, _KEY, "", "a.example.com"))
    assert res["status"] == "ERROR"
    assert "not yet implemented" in res["message"]


def test_engine_install_cert_unknown_device_is_error():
    eng = NwEngine([_CX])
    res = _run(eng.install_cert("nope", _PEM, _KEY, "", "a.example.com"))
    assert res["status"] == "ERROR"
    assert "not found" in res["message"]


# ── NwSpoke INSTALL_CERT routing ─────────────────────────────────────────────
def _spoke_with(devices):
    return NwSpoke("nw-spoke-1", {"devices": devices})


def test_spoke_install_cert_routes_by_identifier():
    spoke = _spoke_with([_CX])
    captured = {}

    async def _engine_install(device_id, fullchain, privkey, chain, domain, tenant=None):
        captured.update(device_id=device_id, fullchain=fullchain,
                        privkey=privkey, chain=chain, domain=domain)
        return {"status": "SUCCESS", "message": f"installed on {device_id}"}

    spoke.engine.install_cert = _engine_install
    res = _run(spoke.handle_command("INSTALL_CERT", {
        "identifier": "edge-sw-1", "fullchain": _PEM, "privkey": _KEY,
        "chain": "", "domain": "a.example.com", "module_type": "nw"}))
    assert res["status"] == "SUCCESS"
    assert captured["device_id"] == "edge-sw-1"
    assert captured["fullchain"] == _PEM
    assert captured["domain"] == "a.example.com"


def test_spoke_install_cert_accepts_device_id_fallback():
    spoke = _spoke_with([_CX])

    async def _engine_install(device_id, *a, **k):
        return {"status": "SUCCESS", "message": "ok"}
    spoke.engine.install_cert = _engine_install
    # No identifier — falls back to device_id (parity with other nw commands).
    res = _run(spoke.handle_command("INSTALL_CERT", {
        "device_id": "edge-sw-1", "fullchain": _PEM, "privkey": _KEY}))
    assert res["status"] == "SUCCESS"


def test_spoke_install_cert_missing_identifier_is_error():
    spoke = _spoke_with([_CX])
    res = _run(spoke.handle_command("INSTALL_CERT", {
        "fullchain": _PEM, "privkey": _KEY, "domain": "a.example.com"}))
    assert res["status"] == "ERROR"
    assert "identifier" in res["message"]


def test_spoke_install_cert_missing_material_is_error():
    spoke = _spoke_with([_CX])
    res = _run(spoke.handle_command("INSTALL_CERT", {
        "identifier": "edge-sw-1", "fullchain": "", "privkey": ""}))
    assert res["status"] == "ERROR"
    assert "fullchain" in res["message"]