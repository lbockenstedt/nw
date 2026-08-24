"""Tests for the nw poll scheduler (nw_poll_scheduler.plan_poll_tick).

Pure scheduling math — no IO, no control-plane import. Uses an injected ``rng``
so the jitter/spread is deterministic. Covers the two anti-stampede knobs the
loop relies on: per-tick dispatch cap + jitter, plus interval resolution,
first-sight staggering, Off, and pruning.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import nw_poll_scheduler as S  # noqa: E402
from nw_poll_scheduler import plan_poll_tick, resolve_interval  # noqa: E402


def _devs(n, interval=None, prefix="d"):
    d = {}
    return [{"id": f"{prefix}{i}", **({"poll_interval": interval}
                                      if interval is not None else {})}
            for i in range(n)]


# ── interval resolution ──────────────────────────────────────────────────────

def test_resolve_interval_inherits_and_coerces():
    assert resolve_interval(None, 900) == 900        # absent → module default
    assert resolve_interval("", 900) == 900          # blank → module default
    assert resolve_interval("300", 900) == 300       # string number wins
    assert resolve_interval(0, 900) == 0             # explicit Off preserved
    assert resolve_interval("bogus", 900) == 900     # malformed → default


# ── first sight never dispatches; spread is within [0.5,1.5]×interval ─────────

def test_first_sight_schedules_not_dispatched():
    next_due = {}
    devs = _devs(3, interval=300)
    due = plan_poll_tick(devs, next_due, now=1000.0, module_default=900,
                         rng=lambda: 0.5)
    assert due == []                                  # nothing polled on first sight
    # rng=0.5 → offset = interval*(0.5+0.5)=interval → now+300
    assert all(next_due[f"d{i}"] == 1300.0 for i in range(3))


def test_first_sight_spread_uses_full_half_to_one_and_a_half():
    next_due = {}
    # rng=0.0 → 0.5×interval ; rng≈1 → 1.5×interval. Confirm bounds.
    plan_poll_tick([{"id": "a", "poll_interval": 100}], next_due, 0.0, 900,
                   rng=lambda: 0.0)
    assert next_due["a"] == 50.0                      # 0.5 × 100
    next_due2 = {}
    plan_poll_tick([{"id": "a", "poll_interval": 100}], next_due2, 0.0, 900,
                   rng=lambda: 0.999)
    assert 149.0 < next_due2["a"] <= 150.0            # ~1.5 × 100


# ── floor ────────────────────────────────────────────────────────────────────

def test_interval_floored():
    next_due = {}
    plan_poll_tick([{"id": "a", "poll_interval": 5}], next_due, 0.0, 900,
                   floor=30, rng=lambda: 0.0)
    # floored to 30 → offset 0.5×30 = 15
    assert next_due["a"] == 15.0


# ── Off (0) pruned, never polled ─────────────────────────────────────────────

def test_off_is_pruned():
    next_due = {"a": 5.0}                              # was scheduled before
    due = plan_poll_tick([{"id": "a", "poll_interval": 0}], next_due, 100.0, 900)
    assert due == []
    assert "a" not in next_due                         # pruned


# ── overdue devices dispatched, reschedule with jitter ───────────────────────

def test_overdue_dispatched_and_rescheduled_with_jitter():
    # Device already overdue (deadline in the past).
    next_due = {"a": 10.0}
    # rng=0.5 → jitter factor 1 + 0.15*(2*0.5-1) = 1.0 → now+interval exactly.
    due = plan_poll_tick([{"id": "a", "poll_interval": 300}], next_due,
                         now=1000.0, module_default=900, rng=lambda: 0.5)
    assert due == ["a"]
    assert next_due["a"] == 1300.0                     # 1000 + 300*1.0

    # rng=0.0 → factor 1-0.15 = 0.85 ; rng≈1 → 1.15. Bounds check.
    nd = {"a": 10.0}
    plan_poll_tick([{"id": "a", "poll_interval": 300}], nd, 1000.0, 900,
                   rng=lambda: 0.0)
    assert nd["a"] == 1000.0 + 300 * 0.85              # 1255.0
    nd2 = {"a": 10.0}
    plan_poll_tick([{"id": "a", "poll_interval": 300}], nd2, 1000.0, 900,
                   rng=lambda: 0.999)
    assert 1000.0 + 300 * 1.149 < nd2["a"] <= 1000.0 + 300 * 1.15


# ── per-tick dispatch cap: 100 due, only max_per_tick go this tick ────────────

def test_per_tick_cap_limits_dispatch():
    # 100 devices, all overdue (seed deadlines in the past).
    devs = _devs(100, interval=300)
    now = 10_000.0
    next_due = {f"d{i}": 1.0 for i in range(100)}      # all overdue
    due = plan_poll_tick(devs, next_due, now, 900, max_per_tick=8,
                         rng=lambda: 0.5)
    assert len(due) == 8                               # capped
    # The 8 dispatched are rescheduled into the future; the other 92 keep their
    # past deadline so they're retried next tick (no starvation of the fleet).
    dispatched = set(due)
    future = [k for k, v in next_due.items() if v > now]
    assert set(future) == dispatched
    still_overdue = [k for k, v in next_due.items() if v <= now]
    assert len(still_overdue) == 92


def test_per_tick_cap_dispatches_oldest_first():
    # Three overdue with distinct deadlines; cap=1 must pick the oldest.
    devs = _devs(3, interval=300)
    next_due = {"d0": 50.0, "d1": 10.0, "d2": 30.0}    # d1 oldest
    due = plan_poll_tick(devs, next_due, now=1000.0, module_default=900,
                         max_per_tick=1, rng=lambda: 0.5)
    assert due == ["d1"]


# ── pruning removed devices ──────────────────────────────────────────────────

def test_removed_device_pruned_from_next_due():
    next_due = {"gone": 500.0, "a": 500.0}
    plan_poll_tick([{"id": "a", "poll_interval": 300}], next_due, 100.0, 900,
                   rng=lambda: 0.5)
    assert "gone" not in next_due
    assert "a" in next_due


# ── bad/empty ids skipped ────────────────────────────────────────────────────

def test_devices_without_id_skipped():
    next_due = {}
    due = plan_poll_tick([{"poll_interval": 300}, {"id": "", "poll_interval": 300}],
                         next_due, 100.0, 900, rng=lambda: 0.5)
    assert due == []
    assert next_due == {}


def test_module_default_constants_sane():
    assert S.POLL_MAX_CONCURRENCY >= 1
    assert S.POLL_MAX_PER_TICK >= 1
    assert 0.0 < S.POLL_JITTER_FRAC < 1.0
    assert S.POLL_FLOOR_S >= 1
