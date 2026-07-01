import logging
from pathlib import Path
from typing import Dict, Any, List

try:
    from core.src.base_spoke import BaseSpoke
except ImportError:
    from base_spoke import BaseSpoke
from nw_engine import NwEngine, _norm_mac

logger = logging.getLogger("NwSpoke")

# Credential field names — FULL mask in logs (never partial). Mirrors the
# opnsense spoke's masking precedent: leaking both ends of a credential exposes
# a meaningful fraction of a typical secret, so the whole value is replaced.
_SENSITIVE = {"password", "enable_secret", "api_token", "snmp_community",
              "secret", "hub_secret"}


class NwSpoke(BaseSpoke):
    """Network Devices Management Spoke for Lab Manager.

    Translates Hub NW_* commands into per-device SSH/CLI, REST, or SNMP actions
    via :class:`NwEngine`. Manages a **fleet** of devices (one spoke → many
    devices) pushed from ``global_config["nw_devices"]`` through UPDATE_CONFIG.
    """
    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        # The engine needs the fleet before super().__init__ so any base-class
        # background worker sees it. The hub pushes devices via UPDATE_CONFIG
        # after approval; at cold start config may carry devices from a
        # pre-provisioned config.
        devices = (config or {}).get("devices", []) if isinstance(config, dict) else []
        self.engine = NwEngine(devices)
        super().__init__(spoke_id, config)

    # ── Logging helper: mask sensitive fields in any command data ───────────
    @staticmethod
    def _mask(data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {k: ("********" if k in _SENSITIVE else v) for k, v in data.items()}

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        # Normalize command type to uppercase for case-insensitive matching.
        normalized_cmd = (command_type or "").upper()
        log_data = self._mask(data)
        logger.info(f"Handling Nw Command: {command_type} with data {log_data}")
        res = await self._dispatch_command(normalized_cmd, command_type, data)
        self._log_result(command_type, res)
        return res

    @staticmethod
    def _log_result(command_type: str, res: Dict[str, Any]) -> None:
        """Log every command's outcome in the standard module form: INFO on
        success, ERROR on failure. The ERROR line carries the word "error" so
        it surfaces in the hub's GET_ERROR_LOGS / Error Log tab (same precedent
        as the opnsense spoke's per-command result logging + the hub sync
        loops' ``[sync-error]`` marker). ``errors`` (from NW_POLL / NW_RUN_CONFIG)
        is surfaced as a sub-error count. Best-effort: never raises."""
        try:
            status = str((res or {}).get("status", "")).upper()
            msg = (res or {}).get("message", "")
            errors = (res or {}).get("errors") or []
            if status == "ERROR" or errors:
                logger.error("nw command %s result: error — %s%s", command_type,
                             msg or "failed",
                             f" ({len(errors)} sub-error(s))" if errors else "")
            else:
                logger.info("nw command %s result: %s", command_type,
                            status.lower() or "ok")
        except Exception:
            logger.debug("nw log_result failed", exc_info=True)

    async def _dispatch_command(self, normalized_cmd: str, command_type: str,
                                data: Dict[str, Any]) -> Dict[str, Any]:
        # ── Lifecycle / config ──────────────────────────────────────────────
        if normalized_cmd == "UPDATE_CONFIG":
            devices = (data or {}).get("devices", []) if isinstance(data, dict) else []
            # Mask credentials in the per-device log summary.
            summary = [{k: ("********" if k in _SENSITIVE else v)
                        for k, v in d.items()} for d in devices] if isinstance(devices, list) else []
            logger.info(f"Updating nw fleet configuration: {len(devices if isinstance(devices, list) else [])} "
                        f"device(s) -> {summary}")
            self.config = data or {}
            self.engine.set_devices(devices if isinstance(devices, list) else [])
            return {"status": "SUCCESS",
                    "message": "nw configuration updated from Hub",
                    "device_count": len(self.engine.devices)}

        if normalized_cmd in ("GET_VERSION", "GET-VERSION"):
            return {"status": "SUCCESS", "version": self.get_version()}

        # ── Fleet ───────────────────────────────────────────────────────────
        if normalized_cmd == "NW_LIST_DEVICES":
            return await self.engine.list_devices()

        # ── Per-device (data carries device_id) ─────────────────────────────
        device_id = (data or {}).get("device_id", "") if isinstance(data, dict) else ""

        if normalized_cmd == "NW_PROBE":
            return await self.engine.probe(device_id)

        if normalized_cmd == "NW_GET_DEVICE_INFO":
            return await self.engine.get_device_info(device_id)

        if normalized_cmd == "NW_GET_MAC_TABLE":
            res = await self.engine.get_mac_table(device_id)
            # Canonicalize MACs on the way out so the hub/UI/NetBox see one form.
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_GET_ARP":
            res = await self.engine.get_arp(device_id)
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_GET_INTERFACES":
            res = await self.engine.get_interfaces(device_id)
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_POLL":
            # Full poll: probe + device_info + interfaces + arp + mac_table in
            # one call. Canonicalize every MAC-bearing sub-list on the way out.
            res = await self.engine.poll(device_id)
            d = res.get("data") if isinstance(res.get("data"), dict) else None
            if d is not None:
                for key in ("arp", "mac_table", "interfaces"):
                    lst = d.get(key)
                    if isinstance(lst, list):
                        d[key] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                                  for r in lst if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_RUN_CONFIG":
            commands = (data or {}).get("commands", []) if isinstance(data, dict) else []
            return await self.engine.run_config(device_id, commands)

        # ── Unknown ─────────────────────────────────────────────────────────
        logger.warning(f"Unknown Nw command type: {command_type}")
        return {"status": "ERROR",
                "message": f"Command {command_type} not supported by nw module"}

    async def get_status(self) -> Dict[str, Any]:
        """Native LM status report for the nw fleet."""
        return {
            "spoke_id": self.spoke_id,
            "module": "nw",
            "device_count": len(self.engine.devices),
            "connection": "CONNECTED",
        }

    def get_version(self) -> str:
        """Current nw module version (repo-root VERSION).

        Reads ``<repo>/VERSION`` (one dir above ``src/``). Same path pattern as
        the opnsense spoke — avoids the cs-spoke wrong-VERSION-path gotcha
        (reading a non-existent sibling VERSION → "unknown" on the Diag page).
        """
        try:
            return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
        except Exception:
            logger.exception("Failed to read VERSION file")
            return "unknown"