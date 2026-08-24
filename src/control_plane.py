# Dependency self-heal — MUST run before the third-party imports below. A skewed
# auto-update / partial install can leave the venv missing a declared dep, which
# would hard-crash at import and crash-loop the unit under Restart=always.
# dep_guard is stdlib-only; it find_spec-checks requirements.txt and pip-installs
# any missing. Best-effort — an unavailable dep_guard is skipped, never fatal.
import os as _os
try:
    try:
        from core.src.dep_guard import ensure_requirements as _ensure_requirements
    except ImportError:
        from dep_guard import ensure_requirements as _ensure_requirements
    _ensure_requirements(_os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "requirements.txt"))
except Exception:
    pass

import logging
import argparse
import asyncio
import time
from typing import Dict, Any
try:
    from core.src.messaging.control_plane import BaseControlPlane
except ImportError:
    from messaging.control_plane import BaseControlPlane
from nw_spoke import NwSpoke
from nw_engine import _norm_mac
from nw_poll_scheduler import (
    plan_poll_tick, POLL_MAX_CONCURRENCY, POLL_MAX_PER_TICK, POLL_JITTER_FRAC,
)

try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        import logging as _logging
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
# Configure root logging at boot. Previously this entrypoint called no
# basicConfig at all -> root defaulted to WARNING and ALL INFO logs
# (engine _log_datum success rows, _log_result) were silently dropped at
# cold start, so a healthy spoke looked silent until a transport failed.
configure_logging()
logger = logging.getLogger("NwControlPlane")


class NwControlPlane(BaseControlPlane):
    """Control Plane for the Network Devices (nw) module.

    Inherits core connectivity and routing from BaseControlPlane. The spoke
    advertises module_type "nw" so the hub routes NW_* commands + pushes the
    nw_devices fleet via UPDATE_CONFIG on connect/approve/reconnect.
    """
    def get_service_name(self) -> str:
        return "lm-nw"

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None,
                 hub_url: str = None, config: Dict[str, Any] = None):
        # Initialize attributes before calling super().__init__ so background
        # workers started by the base class see them.
        self.config = config or {}
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "nw"

    # Poll-loop tick granularity + default cadence. The per-device cadence lives
    # on each nw_devices entry (``poll_interval``); the scheduler bounds
    # (floor / per-tick cap / jitter / concurrency) live in nw_poll_scheduler.
    _NW_POLL_TICK = 10
    # Default cadence when a device has no poll_interval set at all. An explicit
    # 0 (the UI "Off" choice) disables; only an absent/blank value defaults.
    _NW_POLL_DEFAULT = 21600  # 6 hours

    async def run_hub_mode(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting Network Devices Module in HUB MODE -> {self.hub_url}")

        nw_spoke = NwSpoke(self.spoke_id, self.config)
        self.register_module("nw", nw_spoke)

        # Autonomous per-device polling (spoke-driven). Started before the main
        # loop; it idles until connected + a device sets poll_interval.
        asyncio.create_task(self._nw_poll_loop())

        # Delegate to BaseControlPlane's main loop
        await self.run()

    async def _nw_poll_loop(self):
        """Per-device autonomous polling done **by the spoke**.

        Each nw device may set ``poll_interval`` (seconds) in its config; this
        ticks every ``_NW_POLL_TICK`` and polls any device whose interval has
        elapsed, pushing the result to the hub (``NW_POLL_RESULT``) so the hub
        warms its per-device cache — every sub-view (info/arp/macs/interfaces/
        endpoints/vlans) then loads instantly instead of blocking on a live
        SSH round-trip. ``poll_interval`` ≤ 0 / absent = disabled.

        Anti-stampede (so a 100-device fleet never polls all at once): the
        scheduling in ``nw_poll_scheduler.plan_poll_tick`` jitters every
        (re)schedule and caps how many devices dispatch per tick, and this loop
        caps how many run **concurrently** via a semaphore (``max_poll_concurrency``
        from module config, else ``POLL_MAX_CONCURRENCY``)."""
        next_due: Dict[str, float] = {}
        while True:
            await asyncio.sleep(self._NW_POLL_TICK)
            try:
                module = self.modules.get("nw")
                engine = getattr(module, "engine", None)
                if engine is None or getattr(self, "_hub_ws", None) is None:
                    continue
                mod_cfg = getattr(module, "config", {}) or {}
                # Module-level default cadence (pushed via UPDATE_CONFIG); a
                # device with no poll_interval inherits it, else the 15m built-in.
                mod_raw = mod_cfg.get("default_poll_interval")
                try:
                    module_default = (self._NW_POLL_DEFAULT
                                      if mod_raw in (None, "") else int(mod_raw))
                except (TypeError, ValueError):
                    module_default = self._NW_POLL_DEFAULT
                now = time.monotonic()
                # Anti-stampede knobs — operator-tunable via module config
                # (Setup → Module Management), each clamped to a safe range so a
                # bad value can't defeat the protection. Jitter fraction spreads
                # (re)schedules; per-tick cap bounds how many dispatch at once.
                try:
                    jf = float(mod_cfg.get("poll_jitter_frac"))
                except (TypeError, ValueError):
                    jf = POLL_JITTER_FRAC
                jf = max(0.0, min(jf, 0.9))
                try:
                    mpt = int(mod_cfg.get("max_poll_per_tick"))
                except (TypeError, ValueError):
                    mpt = POLL_MAX_PER_TICK
                mpt = max(1, min(mpt, 100))
                due = plan_poll_tick(list(engine.devices), next_due, now,
                                     module_default,
                                     max_per_tick=mpt, jitter_frac=jf)
                if due:
                    # Concurrency ceiling — how many devices may poll at the same
                    # time. Operator-tunable via module config; clamped to sane
                    # bounds so a bad value can't unleash or stall the fleet.
                    try:
                        conc = int(mod_cfg.get("max_poll_concurrency"))
                    except (TypeError, ValueError):
                        conc = POLL_MAX_CONCURRENCY
                    conc = max(1, min(conc, 50))
                    sem = asyncio.Semaphore(conc)

                    async def _one(device_id):
                        async with sem:
                            await self._nw_poll_and_push(device_id)
                    await asyncio.gather(*(_one(x) for x in due))
            except Exception as e:  # noqa: BLE001 - loop must never die
                logger.debug("nw poll loop tick error: %s", e)

    async def _nw_poll_and_push(self, device_id: str):
        """Run one full engine poll + push it to the hub as NW_POLL_RESULT."""
        module = self.modules.get("nw")
        engine = getattr(module, "engine", None)
        if engine is None:
            return
        try:
            res = await engine.poll(device_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("nw auto-poll %s failed: %s", device_id, e)
            return
        data = res.get("data") if isinstance(res, dict) else None
        if not isinstance(data, dict):
            return
        # A PARTIAL poll returns EMPTY lists for datums whose SSH sub-call
        # failed (engine.poll → errors[]). The hub folds a poll into its warm
        # per-device cache PER-KEY on presence (nw_cache_set_poll), so pushing
        # those []s would blank arp/mac/endpoints in the UI while it still
        # badges LIVE. Drop the failed datums from the push — the hub keeps its
        # last-good value for each, and only the datums that succeeded this
        # cycle update. errors[] entries are "<label>: <msg>".
        failed = set()
        for msg in (res.get("errors") or []):
            if isinstance(msg, str) and ":" in msg:
                failed.add(msg.split(":", 1)[0].strip())
        for label in ("device_info", "interfaces", "arp", "mac_table", "vlans"):
            if label in failed:
                data.pop(label, None)
        # ``endpoints`` is a fusion of arp + mac_table + interfaces, so any of
        # those failing taints it too — don't let a partial fusion overwrite.
        if failed & {"arp", "mac_table", "interfaces"}:
            data.pop("endpoints", None)
        for key in ("arp", "mac_table", "interfaces", "endpoints"):
            lst = data.get(key)
            if isinstance(lst, list):
                data[key] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                             for r in lst if isinstance(r, dict)]
        await self.send_to_hub("NW_POLL_RESULT",
                               {"device_id": device_id, "data": data})
        logger.info("nw auto-poll %s -> pushed (status=%s, dropped=%s)",
                    device_id, res.get("status"), sorted(failed) or None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", nargs='?', const="lm-secret", default="lm-secret",
                        help="Authentication secret (default: lm-secret)")
    parser.add_argument("--hub-secret", nargs='?', default="", const="",
                        help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", required=True, help="Hub WebSocket URL")
    args = parser.parse_args()

    cp = NwControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    try:
        asyncio.run(cp.run_hub_mode())
    except KeyboardInterrupt:
        pass