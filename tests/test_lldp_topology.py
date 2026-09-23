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
