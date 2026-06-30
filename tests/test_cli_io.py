"""Tests for the CLI transport: pure per-vendor parsers exercised with canned
show-command text, plus CliSession config validation. No asyncssh needed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from transports import cli_io  # noqa: E402


# ── AOS-S `show arp` ──────────────────────────────────────────────────────────
def test_parse_arp_aos_s():
    text = """
  IP Address  MAC Address      Type    Port
  10.0.0.5    aa-bb-cc-dd-ee-ff dynamic 1
  10.0.0.6    001122334455     dynamic Trk2
"""
    rows = cli_io.parse_arp_aos_s(text)
    assert {"ip": "10.0.0.5", "mac": "aa-bb-cc-dd-ee-ff", "interface": "1"} in rows
    assert {"ip": "10.0.0.6", "mac": "001122334455", "interface": "Trk2"} in rows


# ── AOS-S `show mac-address` ──────────────────────────────────────────────────
def test_parse_mac_aos_s():
    text = """
  MAC Address   VLAN    Port
  aabbccddeeff  10      1
  001122334455  20      Trk2
"""
    rows = cli_io.parse_mac_aos_s(text)
    assert {"mac": "aabbccddeeff", "vlan": "10", "interface": "1"} in rows
    assert {"mac": "001122334455", "vlan": "20", "interface": "Trk2"} in rows


# ── AOS-S `show interfaces brief` ─────────────────────────────────────────────
def test_parse_interfaces_aos_s():
    text = """
  Port    Admin Status  Physical Status  Speed/Type
  1       Enabled       Up               1000
  2       Enabled       Down             100
"""
    rows = cli_io.parse_interfaces_aos_s(text)
    byname = {r["name"]: r for r in rows}
    assert byname["1"]["status"] == "up"
    assert byname["2"]["status"] == "down"


# ── Junos `show arp` + `show interfaces descriptions` ─────────────────────────
def test_parse_arp_junos():
    text = """
00:11:22:33:44:55  10.0.0.7  ge-0/0/1
aa:bb:cc:dd:ee:ff  10.0.0.8  ge-0/0/2
"""
    rows = cli_io.parse_arp_junos(text)
    assert {"ip": "10.0.0.7", "mac": "00:11:22:33:44:55",
            "interface": "ge-0/0/1"} in rows


def test_parse_interfaces_junos():
    text = """
Interface   Admin  Link
ge-0/0/0    up     up
ge-0/0/1    up     down
"""
    rows = cli_io.parse_interfaces_junos(text)
    byname = {r["name"]: r for r in rows}
    assert byname["ge-0/0/0"]["status"] == "up"
    assert byname["ge-0/0/1"]["status"] == "down"


# ── PARSERS registry triple ───────────────────────────────────────────────────
def test_parsers_registry_has_all_families():
    for ot in ("aos_switch", "cx_switch", "ex_switch", "gateway"):
        assert ot in cli_io.PARSERS
        arp, mac, ifc = cli_io.PARSERS[ot]
        assert all(callable(f) for f in (arp, mac, ifc))


# ── CliSession requires address + username ────────────────────────────────────
def test_cli_session_requires_username():
    try:
        cli_io.CliSession({"id": "d1", "address": "10.0.0.1"})
        assert False, "expected CliError"
    except cli_io.CliError as e:
        assert "username" in str(e)


def test_cli_session_requires_address():
    try:
        cli_io.CliSession({"id": "d1", "username": "admin"})
        assert False, "expected CliError"
    except cli_io.CliError as e:
        assert "address" in str(e)


# ── best-effort info-token extractors ─────────────────────────────────────────
def test_serial_and_firmware_extractors():
    text = "Aruba JL658A Serial: SG123ABCD Firmware Version 16.02.0023"
    assert cli_io._serial_from(text) == "SG123ABCD"
    assert "16.02.0023" == cli_io._firmware_from(text)