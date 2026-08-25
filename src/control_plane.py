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

    async def run_hub_mode(self):
        """Native LM Spoke behavior (standalone lm-nw service)."""
        logger.info(f"Starting Network Devices Module in HUB MODE -> {self.hub_url}")

        nw_spoke = NwSpoke(self.spoke_id, self.config)
        self.register_module("nw", nw_spoke)

        # The autonomous poll loop + ICMP reachability sweep live on the MODULE
        # (NwSpoke.start_background_loops) so they run identically whether nw is
        # standalone (here) or hosted as the "network" role on the generic agent
        # (RoleConnection._start_role_local_services calls the same hook). Wire
        # the control-plane back-ref first so the loops can reach the hub.
        nw_spoke.control_plane = self
        nw_spoke.start_background_loops()

        # Delegate to BaseControlPlane's main loop
        await self.run()


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