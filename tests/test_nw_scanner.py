"""Tests for the NW fingerprint scanner (nw_scanner).

Fully offline: the TCP-probe and SSH/SNMP identify steps are injected with
fakes, so the orchestration (concurrency, bounds, per-target flow, SSH→SNMP
fallback, classification, and the optional LLDP crawl) is exercised without a
network. Mirrors the flat-import style of the other nw tests.
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import nw_scanner  # noqa: E402
from nw_scanner import NwScanner, classify_platform  # noqa: E402


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── classify_platform ────────────────────────────────────────────────────────
def test_classify_cx_switch():
    assert classify_platform("ArubaOS-CX FL.10.09 6300M") == "cx_switch"
    assert classify_platform("Aruba JL658A CX 6300") == "cx_switch"


def test_classify_aos_switch():
    assert classify_platform("Aruba JL256A 2930F, PL.16.10") == "aos_switch"
    assert classify_platform("ProCurve J9728A 2920") == "aos_switch"
    assert classify_platform("HP Switch software") == "aos_switch"


def test_classify_gateway():
    assert classify_platform("ArubaOS (MODEL: Aruba7210), Version 8.10") == "gateway"
    assert classify_platform("Aruba Mobility Controller") == "gateway"


def test_classify_ex_switch():
    assert classify_platform("JUNOS 21.4R1 Juniper EX4300") == "ex_switch"


def test_classify_unknown():
    assert classify_platform("Ubuntu 22.04 OpenSSH_8.9") is None
    assert classify_platform("") is None
    assert classify_platform(None) is None


# ── fake probe/identify builders ─────────────────────────────────────────────
def make_probe(open_map):
    async def _probe(host, port, timeout):
        return port in open_map.get(host, set())
    return _probe


def make_ssh(banner_map):
    async def _ssh(host, port, cred, timeout):
        b = banner_map.get(host)
        if b is None:
            return None
        # Only the credential named "good" logs in, if the fixture uses names.
        if isinstance(b, dict):
            if cred.get("name") != b.get("cred"):
                return None
            return {"text": b["text"], "hostname": b.get("hostname", "")}
        return {"text": b, "hostname": ""}
    return _ssh


def make_snmp(descr_map):
    async def _snmp(host, community, timeout):
        d = descr_map.get(host)
        if d is None:
            return None
        if isinstance(d, dict):
            if community != d.get("community"):
                return None
            return {"text": d["text"], "hostname": d.get("hostname", "")}
        return {"text": d, "hostname": ""}
    return _snmp


CREDS = [{"name": "primary", "username": "admin", "password": "x", "snmp_community": "public"}]


# ── scan flow ────────────────────────────────────────────────────────────────
def test_scan_ssh_identifies_switch():
    scanner = NwScanner(
        CREDS,
        tcp_probe=make_probe({"10.0.0.1": {22, 443}}),
        ssh_identify=make_ssh({"10.0.0.1": "ArubaOS-CX FL.10.09 6300M"}),
        snmp_identify=make_snmp({}),
    )
    res = run(scanner.scan(["10.0.0.1"]))
    assert res["scanned"] == 1
    assert len(res["identified"]) == 1
    dev = res["identified"][0]
    assert dev["address"] == "10.0.0.1"
    assert dev["object_type"] == "cx_switch"
    assert dev["method"] == "ssh"
    assert dev["credential"] == "primary"
    assert 22 in dev["open_ports"]


def test_scan_falls_back_to_snmp():
    # Port 22 closed → SSH skipped; SNMP identifies it.
    scanner = NwScanner(
        CREDS,
        tcp_probe=make_probe({"10.0.0.2": {161, 80}}),
        ssh_identify=make_ssh({}),
        snmp_identify=make_snmp({"10.0.0.2": "Aruba JL256A 2930F Switch"}),
    )
    res = run(scanner.scan(["10.0.0.2"]))
    assert len(res["identified"]) == 1
    dev = res["identified"][0]
    assert dev["object_type"] == "aos_switch"
    assert dev["method"] == "snmp"


def test_scan_ssh_login_but_unclassified_is_not_identified():
    scanner = NwScanner(
        CREDS,
        tcp_probe=make_probe({"10.0.0.3": {22}}),
        ssh_identify=make_ssh({"10.0.0.3": "Ubuntu 22.04 server"}),
        snmp_identify=make_snmp({}),
    )
    res = run(scanner.scan(["10.0.0.3"]))
    assert res["identified"] == []
    assert len(res["reachable"]) == 1  # reachable but unknown


def test_scan_unreachable_target():
    scanner = NwScanner(
        CREDS,
        tcp_probe=make_probe({}),  # nothing open
        ssh_identify=make_ssh({}),
        snmp_identify=make_snmp({}),
    )
    res = run(scanner.scan(["10.0.0.9"]))
    assert res["scanned"] == 1
    assert res["reachable"] == []
    assert res["identified"] == []


def test_scan_dedupes_and_skips_non_ipv4():
    scanner = NwScanner(
        CREDS,
        tcp_probe=make_probe({"10.0.0.1": {22}}),
        ssh_identify=make_ssh({"10.0.0.1": "ArubaOS-CX 6300"}),
        snmp_identify=make_snmp({}),
    )
    res = run(scanner.scan(["10.0.0.1", "10.0.0.1", "not-an-ip", ""]))
    assert res["scanned"] == 1


def test_scan_respects_max_targets():
    opens = {f"10.0.0.{i}": {22} for i in range(1, 20)}
    scanner = NwScanner(
        CREDS,
        tcp_probe=make_probe(opens),
        ssh_identify=make_ssh({h: "ArubaOS-CX" for h in opens}),
        snmp_identify=make_snmp({}),
    )
    res = run(scanner.scan(list(opens.keys()), max_targets=5))
    assert res["scanned"] == 5


def test_scan_credential_selection():
    # Only the "good" credential logs in over SSH.
    creds = [
        {"name": "wrong", "username": "a", "password": "b"},
        {"name": "good", "username": "admin", "password": "s3cret"},
    ]
    scanner = NwScanner(
        creds,
        tcp_probe=make_probe({"10.0.0.5": {22}}),
        ssh_identify=make_ssh({"10.0.0.5": {"cred": "good", "text": "ArubaOS-CX 6300", "hostname": "core-sw"}}),
        snmp_identify=make_snmp({}),
    )
    res = run(scanner.scan(["10.0.0.5"]))
    dev = res["identified"][0]
    assert dev["credential"] == "good"
    assert dev["hostname"] == "core-sw"


def test_scan_lldp_crawl_enqueues_neighbors():
    opens = {"10.0.0.1": {22}, "10.0.0.2": {22}}
    banners = {"10.0.0.1": "ArubaOS-CX core", "10.0.0.2": "ArubaOS-CX access"}

    def neighbors(host, creds):
        # Core switch advertises the access switch as an LLDP neighbor.
        return ["10.0.0.2"] if host == "10.0.0.1" else []

    scanner = NwScanner(
        CREDS,
        tcp_probe=make_probe(opens),
        ssh_identify=make_ssh(banners),
        snmp_identify=make_snmp({}),
        lldp_neighbors=neighbors,
    )
    # Only seed the core; crawl should discover the access switch.
    res = run(scanner.scan(["10.0.0.1"], crawl=True, max_depth=2))
    addrs = {d["address"] for d in res["identified"]}
    assert addrs == {"10.0.0.1", "10.0.0.2"}
    assert res["scanned"] == 2
