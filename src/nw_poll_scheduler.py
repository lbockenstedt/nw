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
# random supplies the jitter used to spread device poll times apart.
import random
# Type hints for device dicts and id lists.
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
    # Missing or blank per-device value: use the module-wide default.
    if raw is None or raw == "":
        return module_default
    # Accept ints and numeric strings.
    try:
        return int(raw)
    except (TypeError, ValueError):
        # Anything non-numeric is treated as "not set".
        return module_default


def plan_poll_tick(devices: List[Dict[str, Any]], next_due: Dict[str, float],
                   now: float, module_default: int, *,
                   floor: int = POLL_FLOOR_S,
                   max_per_tick: int = POLL_MAX_PER_TICK,
                   jitter_frac: float = POLL_JITTER_FRAC,
                   rng=random.random,
                   first_max: int = 0) -> List[str]:
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
    ``first_max`` (> 0) caps the first-sight spread to ``[0.5, 1.5] × min(
    interval, first_max)`` so a freshly (re)started spoke polls within minutes
    instead of waiting up to 1.5 × a multi-hour interval.
    """
    # Ids present in this tick's device list, each device's interval, and the overdue set.
    seen = set()
    interval_by: Dict[str, int] = {}
    ready: List[tuple] = []  # (deadline, did) for overdue devices
    # Walk the fleet: work out each device's interval and whether it is due.
    for d in devices:
        did = d.get("id")
        # Devices without an id cannot be scheduled.
        if not did:
            continue
        # Remember it as present, then resolve its effective interval.
        seen.add(did)
        interval = resolve_interval(d.get("poll_interval"), module_default)
        if interval <= 0:                       # explicit Off → never poll
            next_due.pop(did, None)
            continue
        # Enforce the minimum poll interval and remember it for rescheduling.
        interval = max(int(interval), floor)
        interval_by[did] = interval
        # None means this device has never been scheduled.
        deadline = next_due.get(did)
        if deadline is None:                    # first sight → jittered spread
            # Optionally cap the spread so a restart does not delay the first poll by hours.
            span = min(interval, first_max) if first_max and first_max > 0 else interval
            next_due[did] = now + span * (0.5 + rng())
        # Deadline has passed: candidate for dispatch this tick.
        elif now >= deadline:
            ready.append((deadline, did))
    for gone in set(next_due) - seen:           # prune removed devices
        next_due.pop(gone, None)
    ready.sort()                                # oldest deadline first
    # Take at most max_per_tick devices this tick; the rest stay overdue.
    batch = [did for _, did in ready[:max_per_tick]]
    for did in batch:                           # reschedule ONLY the dispatched
        # Pick a factor in [1 - jitter_frac, 1 + jitter_frac] to break alignment.
        iv = interval_by[did]
        factor = 1.0 + jitter_frac * (2.0 * rng() - 1.0)
        next_due[did] = now + iv * factor
    # Ids the caller should poll now.
    return batch
