"""HPE iLO hardware health over SNMP (overall, power, memory, fans, temperatures,
CPUs and storage).

The OIDs, column numbers and value enums live in ``hw_health_oids.json`` next
to this file, each one checked against the HPE MIBs. ``parse_hw_health`` is
pure (walk pairs in, JSON-safe dict out) so every table is testable without
pysnmp; ``snmp_get_hw_health`` does the walks over the shared safe walk path
in ``snmp_io``.

Payload::

    {"overall": "ok|degraded|failed|other", "source": "snmp", "polled_at": 1.7e9,
     "components": {"power": [{"name", "location"?, "condition", "detail"?}], ...}}

A table the controller doesn't implement walks empty and its group is left
out, which means "not reported", not an error. Only a controller that doesn't
answer at all raises.
"""
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from transports import snmp_io

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hw_health_oids.json"),
          encoding="utf-8") as _fh:
    HW_HEALTH_MAP: Dict[str, Any] = json.load(_fh)

# Worst wins when rolling conditions up; "other" (unknown) never hides a real state.
_RANK = {"other": 0, "ok": 1, "degraded": 2, "failed": 3}


def condition_name(raw: Any, mapping: Optional[Dict[str, str]] = None) -> str:
    """Map a raw SNMP condition integer (as text) to other/ok/degraded/failed."""
    return (mapping or HW_HEALTH_MAP["condition"]).get(snmp_io._lldp_text(raw), "other")


def worst(conditions) -> str:
    """The worst of several conditions; "other" only when nothing better is known."""
    best = "other"
    for c in conditions:
        if _RANK.get(c, 0) > _RANK[best]:
            best = c
    return best


def _scalar(pairs) -> str:
    """The value of a walked scalar (its single ``.0`` instance), or ""."""
    for _oid, value in pairs or []:
        return snmp_io._lldp_text(value)
    return ""


def _rows(spec: Dict[str, Any], column_pairs: Dict[str, list]) -> Dict[str, Dict[str, str]]:
    """Group walked column values by row index: ``{index: {field: text}}``."""
    rows: Dict[str, Dict[str, str]] = {}
    for field, col in spec["columns"].items():
        prefix = f"{spec['entry']}.{col}."
        for oid, value in column_pairs.get(field) or []:
            if oid.startswith(prefix):
                rows.setdefault(oid[len(prefix):], {})[field] = snmp_io._lldp_text(value)
    return rows


def _component(spec: Dict[str, Any], index: str, row: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """One table row as ``{name, location?, condition, detail?}``, or None to skip it."""
    # Rows the MIB marks absent (empty bay, unpopulated DIMM slot, unread sensor).
    for field, values in (spec.get("skip_if") or {}).items():
        if row.get(field) in values:
            return None
    # Condition from the table's own column and enum when it differs from the standard one.
    raw = row.get(spec.get("condition_column", "condition"))
    if raw is None:
        return None
    condition = condition_name(raw, spec.get("condition_map"))
    # Name from the template, falling back to the row index for a missing field.
    fields = {k: row.get(k) or index for k in spec["columns"]}
    out: Dict[str, Any] = {"name": spec["name"].format(**fields), "condition": condition}
    if row.get("location"):
        out["location"] = row["location"]
    # Short detail: non-OK status words, plus the reading for sensors.
    detail = [labels[row[f]] for f, labels in (spec.get("detail") or {}).items()
              if row.get(f) in labels]
    reading = spec.get("reading")
    if reading and row.get(reading["value"], "").lstrip("-").isdigit():
        text = f"{row[reading['value']]}°C"
        if row.get(reading["threshold"], "").isdigit() and int(row[reading["threshold"]]) > 0:
            text += f" (limit {row[reading['threshold']]}°C)"
        detail.append(text)
    if detail:
        out["detail"] = ", ".join(detail)
    return out


def parse_hw_health(walks: Dict[str, Any], polled_at: Optional[float] = None) -> Dict[str, Any]:
    """Build the health payload from walk results.

    ``walks`` maps the overall OID, each scalar OID and each table entry OID to
    what was walked: a list of ``(oid, value)`` pairs for the overall/scalar
    OIDs, and ``{field: pairs}`` for a table. Missing keys and empty walks are
    tables the controller doesn't report; their group is left out."""
    components: Dict[str, List[Dict[str, Any]]] = {}
    for group, specs in HW_HEALTH_MAP["groups"].items():
        items: List[Dict[str, Any]] = []
        for spec in specs:
            if "scalar" in spec:
                # A group-level condition (power redundancy) is listed only when not OK.
                raw = _scalar(walks.get(spec["scalar"]))
                cond = condition_name(raw) if raw else ""
                if cond in ("degraded", "failed"):
                    items.append({"name": spec["name"], "condition": cond})
                continue
            rows = _rows(spec, walks.get(spec["entry"]) or {})
            for index in sorted(rows, key=lambda i: [int(p) if p.isdigit() else p for p in i.split(".")]):
                comp = _component(spec, index, rows[index])
                if comp:
                    items.append(comp)
        if items:
            components[group] = items
    # Overall: the MIB's own roll-up, made at least as bad as any component.
    raw = _scalar(walks.get(HW_HEALTH_MAP["overall"]["oid"]))
    mib_overall = condition_name(raw) if raw else "other"
    overall = worst([mib_overall] + [c["condition"] for cs in components.values() for c in cs])
    return {"overall": overall, "source": "snmp",
            "polled_at": time.time() if polled_at is None else polled_at,
            "components": components}


def _walk_plan() -> List[Tuple[str, str, str]]:
    """Every walk to run, as ``(key, field, oid)``; field is "" for scalars."""
    plan = [(HW_HEALTH_MAP["overall"]["oid"], "", HW_HEALTH_MAP["overall"]["oid"])]
    for specs in HW_HEALTH_MAP["groups"].values():
        for spec in specs:
            if "scalar" in spec:
                plan.append((spec["scalar"], "", spec["scalar"]))
                continue
            # The condition column first: when it is empty the table is absent
            # and the other columns are skipped.
            cond = spec.get("condition_column", "condition")
            fields = [cond] + [f for f in spec["columns"] if f != cond]
            plan.extend((spec["entry"], f, f"{spec['entry']}.{spec['columns'][f]}") for f in fields)
    return plan


async def snmp_get_hw_health(session: "snmp_io.SnmpSession") -> Optional[Dict[str, Any]]:
    """Walk the health tables one column at a time over the safe walk path and
    parse them. Any failed walk raises ``SnmpError`` (``walk_complete`` also
    raises on a walk cut short after some rows, or on an SNMP error status): a
    read that broke part-way must not look like a short or missing table, and stopping at the
    first failure keeps a dead agent from timing out on every column. Returns
    None when nothing under the HPE health MIBs answered (a non-HPE controller),
    so no reading is sent at all."""
    walks: Dict[str, Any] = {}
    absent = set()
    for key, field, oid in _walk_plan():
        if key in absent:
            continue
        pairs = await snmp_io._to_thread(session.walk_complete, oid)
        if not field:
            walks[key] = pairs
            continue
        # An empty condition column means the whole table is absent.
        if not pairs and not walks.get(key):
            absent.add(key)
            continue
        walks.setdefault(key, {})[field] = pairs
    if not any(walks.values()):
        return None
    return parse_hw_health(walks)
