import logging
import argparse
import asyncio
from typing import Dict, Any
try:
    from core.src.messaging.control_plane import BaseControlPlane
except ImportError:
    from messaging.control_plane import BaseControlPlane
from nw_spoke import NwSpoke

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
        """Native LM Spoke behavior."""
        logger.info(f"Starting Network Devices Module in HUB MODE -> {self.hub_url}")

        nw_spoke = NwSpoke(self.spoke_id, self.config)
        self.register_module("nw", nw_spoke)

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