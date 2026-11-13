"""iLO hardware health over SNMP: table parsing, the condition enum, absent
tables, the fail-fast walk, and the ipmi-only poll datum. Fake values only."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from transports import hw_health, snmp_io  # noqa: E402

E = "1.3.6.1.4.1.232"
OVERALL = f"{E}.6.1.3"
PSU = f"{E}.6.2.9.3.1"
PSU_ALL = f"{E}.6.2.9.1"
MEM = f"{E}.6.2.14.13.1"
FAN = f"{E}.6.2.6.7.1"
TEMP = f"{E}.6.2.6.8.1"
CPU = f"{E}.1.2.2.1.1"
CTL = f"{E}.3.2.2.1.1"
DRV = f"{E}.3.2.5.1.1"


def _col(entry, col, rows):
    """Walk pairs for one column: ``{index: value}`` → ``[(oid, value)]``."""
    return [(f"{entry}.{col}.{idx}", val) for idx, val in rows.items()]


def _run(coro):
    """Run a coroutine to completion on a fresh event loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


def test_condition_enum_maps_each_value():
    assert [hw_health.condition_name(v) for v in ("1", "2", "3", "4", "9", b"2")] == [
        "other", "ok", "degraded", "failed", "other", "ok"]
    assert hw_health.worst(["ok", "other", "degraded", "ok"]) == "degraded"
    assert hw_health.worst(["failed", "degraded"]) == "failed"
    assert hw_health.worst(["other"]) == "other"


def test_power_table_skips_absent_bay_and_details_failure():
    walks = {PSU: {
        "condition": _col(PSU, 4, {"0.1": "2", "0.2": "4", "0.3": "1"}),
        "bay": _col(PSU, 2, {"0.1": "1", "0.2": "2", "0.3": "3"}),
        "present": _col(PSU, 3, {"0.1": "3", "0.2": "3", "0.3": "2"}),
        "status": _col(PSU, 5, {"0.1": "1", "0.2": "17", "0.3": "1"}),
        "redundant": _col(PSU, 9, {"0.1": "3", "0.2": "2", "0.3": "1"}),
    }}
    out = hw_health.parse_hw_health(walks, polled_at=1.0)
    assert out["components"]["power"] == [
        {"name": "Power supply 1", "condition": "ok"},
        {"name": "Power supply 2", "condition": "failed", "detail": "no power input, not redundant"},
    ]
    assert out["overall"] == "failed"


def test_power_redundancy_scalar_listed_only_when_not_ok():
    assert "power" not in hw_health.parse_hw_health({PSU_ALL: [(PSU_ALL + ".0", "2")]})["components"]
    out = hw_health.parse_hw_health({PSU_ALL: [(PSU_ALL + ".0", "3")]})
    assert out["components"]["power"] == [{"name": "Power supplies (redundancy)", "condition": "degraded"}]


def test_memory_table_skips_empty_slot_and_maps_index_unknown_to_degraded():
    walks = {MEM: {
        "condition": _col(MEM, 20, {"1": "2", "2": "4", "3": "1"}),
        "module": _col(MEM, 5, {"1": "1", "2": "2", "3": "3"}),
        "location": _col(MEM, 13, {"1": b"PROC 1 DIMM 1", "2": b"PROC 1 DIMM 2", "3": b""}),
        "status": _col(MEM, 19, {"1": "4", "2": "11", "3": "2"}),
    }}
    out = hw_health.parse_hw_health(walks)
    assert out["components"]["memory"] == [
        {"name": "DIMM 1", "condition": "ok", "location": "PROC 1 DIMM 1"},
        {"name": "DIMM 2", "condition": "degraded", "location": "PROC 1 DIMM 2", "detail": "degraded"},
    ]
    assert out["overall"] == "degraded"


def test_fans_temperatures_cpus_and_storage():
    walks = {
        FAN: {"condition": _col(FAN, 9, {"0.1": "2", "0.2": "3"}),
              "index": _col(FAN, 2, {"0.1": "1", "0.2": "2"}),
              "present": _col(FAN, 4, {"0.1": "3", "0.2": "3"})},
        TEMP: {"condition": _col(TEMP, 6, {"0.1": "2", "0.2": "1"}),
               "index": _col(TEMP, 2, {"0.1": "1", "0.2": "2"}),
               "celsius": _col(TEMP, 4, {"0.1": "24", "0.2": "0"}),
               "threshold": _col(TEMP, 5, {"0.1": "42", "0.2": "0"}),
               "location": _col(TEMP, 8, {"0.1": b"01-Inlet Ambient"})},
        CPU: {"status": _col(CPU, 6, {"0": "2", "1": "5"}),
              "socket": _col(CPU, 9, {"0": "1", "1": "2"})},
        CTL: {"condition": _col(CTL, 6, {"0": "2"}), "slot": _col(CTL, 5, {"0": "0"})},
        DRV: {"condition": _col(DRV, 37, {"0.1": "2", "0.2": "3"}),
              "bay": _col(DRV, 5, {"0.1": "1", "0.2": "2"}),
              "status": _col(DRV, 6, {"0.1": "2", "0.2": "4"}),
              "location": _col(DRV, 64, {"0.2": b"Port 1I Box 1 Bay 2"})},
    }
    c = hw_health.parse_hw_health(walks)["components"]
    assert c["fans"] == [{"name": "Fan 1", "condition": "ok"}, {"name": "Fan 2", "condition": "degraded"}]
    # The unread sensor (condition other) is left out; the reading is the detail.
    assert c["temperatures"] == [{"name": "Sensor 1", "condition": "ok", "location": "01-Inlet Ambient",
                                  "detail": "24°C (limit 42°C)"}]
    assert c["cpus"] == [{"name": "CPU 1", "condition": "ok"},
                         {"name": "CPU 2", "condition": "other", "detail": "disabled"}]
    assert c["storage"] == [
        {"name": "Controller slot 0", "condition": "ok"},
        {"name": "Drive bay 1", "condition": "ok"},
        {"name": "Drive bay 2", "condition": "degraded", "location": "Port 1I Box 1 Bay 2",
         "detail": "predictive failure"},
    ]


def test_absent_tables_are_not_reported():
    out = hw_health.parse_hw_health({OVERALL: [(OVERALL + ".0", "2")], MEM: {}})
    assert out["overall"] == "ok" and out["components"] == {}
    assert hw_health.parse_hw_health({})["overall"] == "other"


class _Session:
    """Fake SNMP session: canned walks per OID; records what was walked."""

    def __init__(self, data, fail=()):
        self.data, self.fail, self.walked = data, set(fail), []

    def walk_complete(self, oid):
        """Canned rows for ``oid``, or SnmpError when it is set to fail."""
        self.walked.append(oid)
        if oid in self.fail:
            raise snmp_io.SnmpError("timeout")
        return self.data.get(oid, [])


def test_walk_skips_columns_of_absent_table_and_parses():
    data = {OVERALL: [(OVERALL + ".0", "3")],
            f"{FAN}.9": _col(FAN, 9, {"0.1": "3"}), f"{FAN}.2": _col(FAN, 2, {"0.1": "4"})}
    s = _Session(data)
    out = _run(hw_health.snmp_get_hw_health(s))
    assert out["overall"] == "degraded"
    assert out["components"] == {"fans": [{"name": "Fan 4", "condition": "degraded"}]}
    # Memory's condition column was empty, so its other columns were never walked.
    assert f"{MEM}.20" in s.walked and f"{MEM}.13" not in s.walked


def test_any_failed_walk_fails_the_read_instead_of_looking_absent():
    s = _Session({}, fail={OVERALL})
    try:
        _run(hw_health.snmp_get_hw_health(s))
        raise AssertionError("expected SnmpError")
    except snmp_io.SnmpError:
        pass
    assert s.walked == [OVERALL]
    # A timeout on a later table (here a condition column) is a failed read,
    # not a table the controller lacks.
    s = _Session({OVERALL: [(OVERALL + ".0", "2")]}, fail={f"{TEMP}.6"})
    try:
        _run(hw_health.snmp_get_hw_health(s))
        raise AssertionError("expected SnmpError")
    except snmp_io.SnmpError:
        pass
    assert s.walked[-1] == f"{TEMP}.6"


def test_controller_without_hpe_health_mibs_sends_nothing():
    assert _run(hw_health.snmp_get_hw_health(_Session({}))) is None
