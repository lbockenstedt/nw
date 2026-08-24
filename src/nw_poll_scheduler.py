"""Pure scheduling logic for the nw spoke's autonomous per-device poll loop.

Kept dependency-free (stdlib only) so it can be unit-tested without importing
the control-plane / messaging stack. ``NwControlPlane._nw_poll_loop`` calls
``plan_poll_tick`` once per tick; all the cadence math lives here — interval
resolution, first-sight spread, anti-resonance jitter, and the per-tick
dispatch cap that stops a 100-device fleet from stampeding a single cycle.

Two anti-stampede mechanisms this module provides (the caller enforces the
third — a concurrency ceiling via ``asyncio.Semaphore(POLL_MAX_CONCURRENCY)``):

1. **Jitter.** A device is never scheduled at exactly ``now + interval``. On
   first sight its initial poll lands at a random point in
   ``[0.5, 1.5] × interval`` (so a fleet that loads together never aligns), and
   every reschedule adds ``± POLL_JITTER_FRAC`` so devices that happen to align
   drift back apart instead of resonating.
2. **Per-tick dispatch cap.** At most ``POLL_MAX_PER_TICK`` devices are
   dispatched per tick (oldest-deadline first); the rest keep their past
   deadline and are retried on the next tick — smooth back-pressure rather than
   one giant burst.
"""
import random
from typing import Any, Dict, List

# Scheduler bounds (seconds / counts). The cadence itself stays per-device;
# these only bound the scheduler so a large fleet degrades gracefully.
POLL_FLOOR_S = 30           # never poll a single device faster than this
POLL_MAX_PER_TICK = 8       # dispatch at most this many devices per tick
POLL_MAX_CONCURRENCY = 5    # ceiling on simultaneous polls (enforced by caller)
POLL_JITTER_FRAC = 0.15     # ±15% cadence jitter (anti-resonance)


def resolve_interval(raw: Any, module_default: int) -> int:
    """Effective poll interval for a device: its own ``poll_interval`` when set
    (``0`` = Off is preserved and handled by the caller), else the module
    default. A malformed value falls back to the module default."""
    if raw is None or raw == "":
        return module_default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return module_default


def plan_poll_tick(devices: List[Dict[str, Any]], next_due: Dict[str, float],
                   now: float, module_default: int, *,
                   floor: int = POLL_FLOOR_S,
                   max_per_tick: int = POLL_MAX_PER_TICK,
                   jitter_frac: float = POLL_JITTER_FRAC,
                   rng=random.random) -> List[str]:
    """Decide which device ids to poll this tick and update ``next_due`` in
    place. Returns the (capped) list of device ids to poll now.

    - Effective interval per device via ``resolve_interval``; ``≤ 0`` (Off) is
      pruned from ``next_due`` and never polled. Positive intervals are floored
      to ``floor``.
    - **First sight** (no deadline yet): schedule the initial poll at a jittered
      offset in ``[0.5, 1.5] × interval`` so a fleet loaded together de-syncs.
      Never dispatched on the tick it is first seen.
    - **Overdue** devices are dispatched oldest-first, capped at
      ``max_per_tick``; only the dispatched ones are rescheduled to
      ``now + interval × (1 ± jitter_frac)``. Undispatched-but-overdue devices
      keep their deadline and are retried next tick.
    - Devices no longer present are pruned from ``next_due``.

    ``rng`` returns a float in ``[0, 1)`` (injected for deterministic tests).
    """
    seen = set()
    interval_by: Dict[str, int] = {}
    ready: List[tuple] = []  # (deadline, did) for overdue devices
    for d in devices:
        did = d.get("id")
        if not did:
            continue
        seen.add(did)
        interval = resolve_interval(d.get("poll_interval"), module_default)
        if interval <= 0:                       # explicit Off → never poll
            next_due.pop(did, None)
            continue
        interval = max(int(interval), floor)
        interval_by[did] = interval
        deadline = next_due.get(did)
        if deadline is None:                    # first sight → jittered spread
            next_due[did] = now + interval * (0.5 + rng())
        elif now >= deadline:
            ready.append((deadline, did))
    for gone in set(next_due) - seen:           # prune removed devices
        next_due.pop(gone, None)
    ready.sort()                                # oldest deadline first
    batch = [did for _, did in ready[:max_per_tick]]
    for did in batch:                           # reschedule ONLY the dispatched
        iv = interval_by[did]
        factor = 1.0 + jitter_frac * (2.0 * rng() - 1.0)
        next_due[did] = now + iv * factor
    return batch
