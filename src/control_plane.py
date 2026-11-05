"""Process entry point for the standalone Network Devices (nw) spoke.

Self-heals missing Python dependencies, configures logging, then runs an
NwControlPlane that connects to the Lab Manager Hub and hosts the NwSpoke
module (device polling, reachability sweeps, NW_* commands).
"""
# Dependency self-heal — MUST run before the third-party imports below. A skewed
# auto-update / partial install can leave the venv missing a declared dep, which
# would hard-crash at import and crash-loop the unit under Restart=always.
# dep_guard is stdlib-only; it find_spec-checks requirements.txt and pip-installs
# any missing. Best-effort — an unavailable dep_guard is skipped, never fatal.
# Stdlib os under a private alias, used only by the dependency self-heal below.
import os as _os
# Wrap the whole self-heal in a try so any failure in it can never stop startup.
try:
    # Prefer the package-style import (repo checkout); fall back to a flat import
    # when the spoke is installed with core/src copied next to it.
    try:
        from core.src.dep_guard import ensure_requirements as _ensure_requirements
    except ImportError:
        from dep_guard import ensure_requirements as _ensure_requirements
    # Point the guard at this spoke's requirements.txt (one directory above src/).
    _ensure_requirements(_os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "requirements.txt"))
except Exception:
    # Self-heal is best-effort: if it fails the real imports below will report the problem.
    pass

# Standard-library logging for status and error messages.
import logging
# argparse reads the --id/--secret/--hub command-line options.
import argparse
# asyncio runs the control plane's event loop.
import asyncio
# time and the typing hints below are kept for helpers/type annotations.
import time
from typing import Dict, Any
# Base class providing the Hub websocket connection and command routing;
# try the repo layout first, then the flat installed layout.
try:
    from core.src.messaging.control_plane import BaseControlPlane
except ImportError:
    from messaging.control_plane import BaseControlPlane
# The nw module that does the device polling work.
from nw_spoke import NwSpoke
# MAC normaliser from the engine (imported for re-use by callers of this module).
from nw_engine import _norm_mac
# Pure poll-scheduling helpers and their tuning constants.
from nw_poll_scheduler import (
    plan_poll_tick, POLL_MAX_CONCURRENCY, POLL_MAX_PER_TICK, POLL_JITTER_FRAC,
)

# Shared logging configuration: try the flat install path, then the repo path.
try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        # Neither copy of logging_setup is available: define a minimal stand-in so the
        # spoke still logs with the same format.
        import logging as _logging
        # Same line format / date format as the shared logging_setup module.
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            """Fallback logging setup: console (and optional file) output at the given level."""
            # Log to file + console when a log file is given; None lets basicConfig use stderr.
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            # force=True replaces any handlers a library may already have installed.
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
# Configure root logging at boot. Previously this entrypoint called no
# basicConfig at all -> root defaulted to WARNING and ALL INFO logs
# (engine _log_datum success rows, _log_result) were silently dropped at
# cold start, so a healthy spoke looked silent until a transport failed.
configure_logging()
# Named logger so control-plane messages can be filtered separately.
logger = logging.getLogger("NwControlPlane")


class NwControlPlane(BaseControlPlane):
    """Control Plane for the Network Devices (nw) module.

    Inherits core connectivity and routing from BaseControlPlane. The spoke
    advertises module_type "nw" so the hub routes NW_* commands + pushes the
    nw_devices fleet via UPDATE_CONFIG on connect/approve/reconnect.
    """
    def get_service_name(self) -> str:
        """Systemd service name the Hub restarts on self-update."""
        return "lm-nw"

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None,
                 hub_url: str = None, config: Dict[str, Any] = None):
        """Store the spoke's config and set up the Hub connection as module type "nw"."""
        # Initialize attributes before calling super().__init__ so background
        # workers started by the base class see them.
        self.config = config or {}
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        # Advertise "nw" so the Hub routes NW_* commands and fleet config to this spoke.
        self.module_type = "nw"

    async def run_hub_mode(self):
        """Native LM Spoke behavior (standalone lm-nw service)."""
        # Announce which Hub this spoke is connecting to.
        logger.info(f"Starting Network Devices Module in HUB MODE -> {self.hub_url}")

        # Build the nw module and register it so the Hub can route "nw" commands to it.
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


# Script entry point: parse the command line and run the control plane.
if __name__ == "__main__":
    # Command-line options: spoke identity, auth secrets and the Hub URL.
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", nargs='?', const="lm-secret", default="lm-secret",
                        help="Authentication secret (default: lm-secret)")
    parser.add_argument("--hub-secret", nargs='?', default="", const="",
                        help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", required=True, help="Hub WebSocket URL")
    # Parse argv into an args namespace.
    args = parser.parse_args()

    # Build the control plane, then run it until it stops.
    cp = NwControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    # Run the Hub loop; Ctrl+C exits quietly instead of printing a traceback.
    try:
        asyncio.run(cp.run_hub_mode())
    except KeyboardInterrupt:
        # Interactive stop: nothing to clean up.
        pass