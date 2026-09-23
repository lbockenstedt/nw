"""Structured LLDP neighbour parsing — the raw material for the topology view.

``parse_lldp_neighbors`` (used by the scanner's optional crawl) reduces LLDP
output to a list of management IPs and throws away everything else. A topology
edge needs the four fields it discards: which LOCAL port, and the chassis id,
port and system name on the far end. ``parse_lldp_detail`` keeps them.

The four layouts below are the ones the fleet actually presents: AOS-S's piped
column table, AOS-CX's key/value blocks, Junos' plain columns and the ArubaOS
gateway's AP-oriented table.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402
from transports import cli_io  # noqa: E402


_KEYS = {"local_port", "remote_chassis", "remote_port", "remote_name",
         "remote_mgmt_ip", "remote_descr"}

AOS_S = """ LLDP Remote Devices Information

  LocalPort | ChassisId         PortId PortDescr SysName
  --------- + ----------------- ------ --------- --------------------
  1         | 00 0b 86 bc 49 87 2      Port 2    OLKS-EDGE-1
  24        | 3c a8 2a 11 22 33 Trk1   Uplink    OLKS-CORE
"""

CX = """Port                          : 1/1/1
Neighbor Entries              : 1
Chassis-id                    : 00:0b:86:bc:49:87
Port-id                       : 2
Port Description              : Port 2
System Name                   : OLKS-EDGE-1
System Description            : Aruba JL256A 2930F
Management Address            : 172.16.1.91

Port                          : 1/1/24
Chassis-id                    : 3c:a8:2a:11:22:33
Port-id                       : Trk1
System Name                   : OLKS-CORE
Management Address            : 172.16.1.92
"""

JUNOS = """Local Interface    Parent Interface    Chassis Id          Port info     System Name
ge-0/0/1           -                   00:0b:86:bc:49:87   2             OLKS-EDGE-1
ge-0/0/24          ae0                 3c:a8:2a:11:22:33   Trk1          OLKS-CORE
"""

GATEWAY = """AP Name        Local Port   Remote Chassis     Remote Port  Remote Name
ap-105-lab     eth0         000b.86bc.4987     2            OLKS-EDGE-1
"""


def _first(text, ot):
    rows = cli_io.parse_lldp_detail(text, ot)
    assert rows, "parser returned no records"
    return rows


@pytest.mark.parametrize("text,ot,count", [
    (AOS_S, "aos_switch", 2), (CX, "cx_switch", 2),
    (JUNOS, "ex_switch", 2), (GATEWAY, "gateway", 1),
])
def test_every_record_carries_the_full_edge_shape(text, ot, count):
    """The hub builds edges by key lookup, so a missing key is a KeyError at
    render time rather than a merely incomplete edge."""
    rows = cli_io.parse_lldp_detail(text, ot)
    assert len(rows) == count
    for r in rows:
        assert set(r) == _KEYS


def test_aos_s_pipe_table():
    rows = _first(AOS_S, "aos_switch")
    assert rows[0]["local_port"] == "1"
    assert rows[0]["remote_chassis"] == "00:0b:86:bc:49:87"
    assert rows[0]["remote_port"] == "2"
    assert rows[0]["remote_name"] == "OLKS-EDGE-1"
    assert rows[1]["local_port"] == "24"
    assert rows[1]["remote_name"] == "OLKS-CORE"


def test_aos_s_header_and_rule_are_not_parsed_as_neighbours():
    """The AOS-S interface parser already leaks a header row as a device named
    "Port" (visible in live hub cache data), which would become a phantom node
    on the topology map. Guard against the same mistake here."""
    names = {r["remote_name"] for r in cli_io.parse_lldp_detail(AOS_S, "aos_switch")}
    assert names == {"OLKS-EDGE-1", "OLKS-CORE"}
    assert not any(r["local_port"].startswith("-") for r
                   in cli_io.parse_lldp_detail(AOS_S, "aos_switch"))


def test_cx_key_value_blocks():
    rows = _first(CX, "cx_switch")
    assert rows[0]["local_port"] == "1/1/1"
    assert rows[0]["remote_mgmt_ip"] == "172.16.1.91"
    assert rows[0]["remote_descr"] == "Aruba JL256A 2930F"
    assert rows[1]["local_port"] == "1/1/24"
    assert rows[1]["remote_mgmt_ip"] == "172.16.1.92"
    # No System/Port Description in the second block.
    assert rows[1]["remote_descr"] == ""


def test_junos_columns_skip_the_parent_interface():
    """``ae0`` in column 2 is the aggregate the port belongs to, not the local
    port — taking it would collapse every LAG member onto one node."""
    rows = _first(JUNOS, "ex_switch")
    assert rows[0]["local_port"] == "ge-0/0/1"
    assert rows[1]["local_port"] == "ge-0/0/24"


def test_gateway_dotted_mac_is_canonicalised():
    rows = _first(GATEWAY, "gateway")
    assert rows[0]["remote_chassis"] == "00:0b:86:bc:49:87"
    assert rows[0]["local_port"] == "eth0"


@pytest.mark.parametrize("raw", [
    "00 0b 86 bc 49 87", "000b.86bc.4987", "000b86bc4987",
    "00-0B-86-BC-49-87", "00:0B:86:BC:49:87",
])
def test_every_vendor_mac_spelling_reaches_one_canonical_form(raw):
    """A chassis id must compare equal to the same MAC seen in a MAC table,
    or LLDP edges and MAC-inferred edges describe two different nodes."""
    text = ("Port : 1\nChassis-id : %s\nPort-id : 2\nSystem Name : X\n" % raw)
    assert cli_io.parse_lldp_detail(text, "cx_switch")[0]["remote_chassis"] \
        == "00:0b:86:bc:49:87"


@pytest.mark.parametrize("junk", ["", None, "   ", "\n\n", "garbage\nlines\n",
                                  "% Invalid input detected at '^' marker."])
def test_devices_without_lldp_contribute_nothing_and_never_raise(junk):
    """A device with LLDP disabled must not fail the whole topology build — it
    is precisely the case the operator-declared-link feature exists to cover."""
    for ot in ("aos_switch", "cx_switch", "ex_switch", "gateway", ""):
        assert cli_io.parse_lldp_detail(junk, ot) == []


def test_duplicate_edges_are_collapsed():
    doubled = AOS_S + "  1         | 00 0b 86 bc 49 87 2      Port 2    OLKS-EDGE-1\n"
    assert len(cli_io.parse_lldp_detail(doubled, "aos_switch")) == 2


def test_detail_command_is_defined_for_every_known_family():
    for ot in ("aos_switch", "cx_switch", "ex_switch", "gateway"):
        assert cli_io._LLDP_DETAIL_CMDS.get(ot)


# ── State separation (PR #119 state-logic panel finding) ────────────────────
# cli_get_lldp_detail used to wrap session.run AND the parse in
# `except Exception: return []`, so four distinct states came back identical:
#   (a) device asked, genuinely zero neighbours
#   (b) device rejected the command / LLDP disabled
#   (c) output arrived but matched no parser (vendor format change)
#   (d) the session itself broke mid-command (timeout, dropped PTY)
# _with_session wraps [] as status SUCCESS, so (b)/(c)/(d) were all reported as
# a healthy device with no edges — and the hub could not tell "no edges here,
# infer from MAC tables" from "we never got an answer". Every other datum on
# this driver (arp, mac, interfaces) lets the error propagate to an ERROR
# envelope; LLDP was the sole exception.

class _FakeSession:
    def __init__(self, text=None, exc=None):
        self.text, self.exc, self.cmds = text, exc, []

    async def run(self, cmd):
        self.cmds.append(cmd)
        if self.exc:
            raise self.exc
        return self.text


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def test_transport_failure_propagates_instead_of_looking_empty():
    """(d) A broken session must NOT read as 'this device has no neighbours'.

    Propagating lets NwDriver._with_session build the ERROR envelope, which is
    what arp/mac/interfaces already do.
    """
    sess = _FakeSession(exc=TimeoutError("paging stall"))
    with pytest.raises(TimeoutError):
        _run(cli_io.cli_get_lldp_detail(sess, "aos_switch"))


def test_parser_crash_propagates_instead_of_looking_empty():
    """(c, worst case) A parser blowing up must not become a silent zero-edge
    SUCCESS that persists across a whole firmware generation."""
    import transports.cli_io as mod
    original = mod._parse_lldp_detail_raw
    mod._parse_lldp_detail_raw = lambda *a, **k: (_ for _ in ()).throw(
        ValueError("unrecognised layout"))
    try:
        with pytest.raises(ValueError):
            mod.parse_lldp_detail(CX, "cx_switch")
    finally:
        mod._parse_lldp_detail_raw = original


@pytest.mark.parametrize("rejection", [
    "% Invalid input: lldp",
    "Invalid command.",
    "                 ^\n% Ambiguous input at '^' marker.",
    "LLDP is not supported on this platform",
])
def test_rejected_command_is_no_edges_not_an_error(rejection):
    """(b) A device that cannot answer still contributes no edges rather than
    failing the topology build — that original intent is preserved — but it is
    now recognised explicitly rather than inferred from an empty parse."""
    sess = _FakeSession(text=rejection)
    assert _run(cli_io.cli_get_lldp_detail(sess, "aos_switch")) == []


def test_rejection_is_logged_so_it_is_not_silent(caplog):
    sess = _FakeSession(text="% Invalid input: lldp")
    with caplog.at_level("WARNING"):
        _run(cli_io.cli_get_lldp_detail(sess, "aos_switch"))
    assert any("rejected" in r.message.lower() or "rejected" in r.getMessage().lower()
               for r in caplog.records), "a refused command logged nothing"


def test_unparseable_output_warns_about_the_parser_gap(caplog):
    """(c) Real output that no layout understands is a parser gap. It still
    yields no edges, but it must be visible — this is what would otherwise
    hide a vendor changing its output format."""
    sess = _FakeSession(text="Neighbour table\nsome entirely novel layout\n")
    with caplog.at_level("WARNING"):
        rows = _run(cli_io.cli_get_lldp_detail(sess, "aos_switch"))
    assert rows == []
    assert any("parser gap" in r.getMessage() for r in caplog.records)


def test_genuinely_empty_output_is_quiet():
    """(a) The one state that SHOULD be a silent empty list."""
    sess = _FakeSession(text="")
    assert _run(cli_io.cli_get_lldp_detail(sess, "aos_switch")) == []


def test_real_output_still_parses_through_the_new_path():
    """The guard rails must not cost us the happy path."""
    sess = _FakeSession(text=CX)
    rows = _run(cli_io.cli_get_lldp_detail(sess, "cx_switch"))
    assert [r["remote_name"] for r in rows] == ["OLKS-EDGE-1", "OLKS-CORE"]


# ── FORMAT B phantom edges (second panel finding) ───────────────────────────

CX_WITH_EMPTY_PORT = """Port                          : 1/1/1
Neighbor Entries              : 1
Chassis-id                    : 00:0b:86:bc:49:87
Port-id                       : 2
System Name                   : OLKS-EDGE-1

Port                          : 1/1/2
Neighbor Entries              : 0

Port                          : 1/1/3
Neighbor Entries              : 0
"""


def test_ports_with_zero_neighbours_do_not_become_edges():
    """AOS-CX prints a block per PORT, including ports with nobody attached.
    Every 'Port :' line used to start a record, so an empty port became an
    edge to a node with no identity — and the dedup key (local_port, '', '')
    could not collapse them because each had a different local_port.
    """
    rows = cli_io.parse_lldp_detail(CX_WITH_EMPTY_PORT, "cx_switch")
    assert len(rows) == 1, f"phantom edges survived: {rows}"
    assert rows[0]["local_port"] == "1/1/1"
    assert rows[0]["remote_name"] == "OLKS-EDGE-1"


def test_no_edge_is_ever_emitted_without_some_remote_identity():
    """The invariant behind the fix: an edge needs a far end. A record with no
    chassis, port, name or management address identifies nothing and can only
    draw a link to a phantom node."""
    for text in (CX_WITH_EMPTY_PORT, CX, AOS_S, JUNOS, GATEWAY):
        for row in cli_io.parse_lldp_detail(text, "cx_switch"):
            assert any(row.get(f) for f in ("remote_chassis", "remote_port",
                                            "remote_name", "remote_mgmt_ip")), row


def test_a_port_with_no_neighbour_count_but_real_data_still_counts():
    """Not every vendor prints 'Neighbor Entries'. Absence of the counter must
    not drop a record that plainly has a neighbour (the second CX block in the
    main fixture has no counter and must survive)."""
    rows = cli_io.parse_lldp_detail(CX, "cx_switch")
    assert len(rows) == 2
    assert rows[1]["local_port"] == "1/1/24"
