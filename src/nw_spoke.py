import logging
from pathlib import Path
from typing import Dict, Any

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
        shared_tid = (config or {}).get("shared_tenant_id", "") if isinstance(config, dict) else ""
        if shared_tid:
            self.engine.shared_tenant_id = shared_tid
        super().__init__(spoke_id, config)

    # ── Logging helper: mask sensitive fields in any command data ───────────
    @staticmethod
    def _mask(data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {k: ("********" if k in _SENSITIVE else v) for k, v in data.items()}

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a hub NW_* command to the engine.

        Command types (case-insensitive): ``UPDATE_CONFIG`` (store the fleet,
        credentials masked in logs), ``GET_VERSION``, ``NW_LIST_DEVICES`` (fleet
        summary + concurrent 3s reachability probe), ``NW_PROBE``,
        ``NW_GET_DEVICE_INFO``, ``NW_GET_MAC_TABLE``, ``NW_GET_ARP``,
        ``NW_GET_INTERFACES``, ``NW_GET_ENDPOINTS`` (fused ARP+MAC unique IP/MAC
        list), ``NW_GET_VLANS`` (each per-device via ``data["device_id"]``),
        ``NW_POLL`` (probe + all datums in one call, partial results on
        partial failure), and ``NW_RUN_CONFIG`` (not-implemented envelope). MACs
        are canonicalized to lower-colon form on the way out. Unknown commands
        return an ERROR envelope. Every outcome is logged via ``_log_result``.
        ``INSTALL_CERT`` (hub-brokered cert distribution) installs a delivered
        LE cert on the device named by ``identifier`` (cx_switch via AOS-CX REST
        v10; other families return a clear ERROR naming the gap).
        """
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
            shared_tid = (data or {}).get("shared_tenant_id", "") if isinstance(data, dict) else ""
            # Mask credentials in the per-device log summary.
            summary = [{k: ("********" if k in _SENSITIVE else v)
                        for k, v in d.items()} for d in devices] if isinstance(devices, list) else []
            logger.info(f"Updating nw fleet configuration: {len(devices if isinstance(devices, list) else [])} "
                        f"device(s) -> {summary}")
            self.config = data or {}
            self.engine.set_devices(devices if isinstance(devices, list) else [],
                                    shared_tenant_id=shared_tid)
            return {"status": "SUCCESS",
                    "message": "nw configuration updated from Hub",
                    "device_count": len(self.engine.devices)}

        if normalized_cmd in ("GET_VERSION", "GET-VERSION"):
            return {"status": "SUCCESS", "version": self.get_version()}

        # ── Fleet ───────────────────────────────────────────────────────────
        if normalized_cmd == "NW_LIST_DEVICES":
            tenant = (data or {}).get("tenant") if isinstance(data, dict) else None
            return await self.engine.list_devices(tenant)

        # ── Per-device (data carries device_id) ─────────────────────────────
        device_id = (data or {}).get("device_id", "") if isinstance(data, dict) else ""
        tenant = (data or {}).get("tenant") if isinstance(data, dict) else None

        if normalized_cmd == "NW_PROBE":
            return await self.engine.probe(device_id, tenant)

        if normalized_cmd == "NW_GET_DEVICE_INFO":
            return await self.engine.get_device_info(device_id, tenant)

        if normalized_cmd == "NW_GET_MAC_TABLE":
            res = await self.engine.get_mac_table(device_id, tenant)
            # Canonicalize MACs on the way out so the hub/UI/NetBox see one form.
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_GET_ARP":
            res = await self.engine.get_arp(device_id, tenant)
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_GET_INTERFACES":
            res = await self.engine.get_interfaces(device_id, tenant)
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_GET_ENDPOINTS":
            # Fused ARP+MAC "unique IP/MAC" list. MACs already canonical (merge
            # normalizes), but re-apply for safety/parity with the other datums.
            res = await self.engine.get_endpoints(device_id, tenant)
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_GET_VLANS":
            return await self.engine.get_vlans(device_id, tenant)

        if normalized_cmd == "NW_POLL":
            # Full poll: probe + device_info + interfaces + arp + mac_table in
            # one call. Canonicalize every MAC-bearing sub-list on the way out.
            res = await self.engine.poll(device_id, tenant)
            d = res.get("data") if isinstance(res.get("data"), dict) else None
            if d is not None:
                for key in ("arp", "mac_table", "interfaces", "endpoints"):
                    lst = d.get(key)
                    if isinstance(lst, list):
                        d[key] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                                  for r in lst if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_RUN_CONFIG":
            commands = (data or {}).get("commands", []) if isinstance(data, dict) else []
            return await self.engine.run_config(device_id, commands, tenant)

        if normalized_cmd == "INSTALL_CERT":
            # Hub-brokered cert distribution: install the delivered LE cert on
            # the target fleet device. The hub addresses a device by
            # ``identifier`` (its fleet id); accept ``device_id`` as a fallback
            # (parity with the other per-device commands). The engine
            # dispatches by object_type — cx_switch via AOS-CX REST v10 today;
            # aos_switch / ex_switch / gateway return a clear ERROR naming the
            # gap so the hub ledger surfaces it.
            d = data or {}
            identifier = (d.get("identifier") or d.get("device_id") or "").strip()
            fullchain = d.get("fullchain", "")
            privkey = d.get("privkey", "")
            chain = d.get("chain", "")
            domain = d.get("domain", "")
            if not fullchain or not privkey:
                return {"status": "ERROR",
                        "message": "INSTALL_CERT requires fullchain + privkey"}
            # Spoke-level cert target: an empty/"*"/"all" identifier means "the nw
            # spoke" — fan the cert out to every cert-capable switch in the fleet
            # and return a per-device report. A specific identifier that resolves
            # to a fleet device still installs on that ONE device (targeted
            # re-push). The hub's wildcard fan-out sends ``identifier = <this
            # spoke's hub-registered id>``; on a generic-agent-hosted nw role that
            # registered id is the BASE agent UUID while the role subspoke's
            # ``self.spoke_id`` is ``{base}-nw``, so the ``self.spoke_id`` check
            # alone mis-routes a wildcard cert to a per-device "not found." Fan
            # out whenever the identifier isn't a real fleet device too — a
            # targeted install always names an existing device, so this only
            # catches the wildcard fan-out (a typo'd device id would also fan out,
            # which is recoverable and beats a broken wildcard deploy).
            if not identifier or identifier.lower() in ("*", "all", "fleet", self.spoke_id.lower()):
                return await self.engine.install_cert_fleet(
                    fullchain, privkey, chain, domain, tenant)
            if self.engine._get_device(identifier, tenant) is None:
                logger.info("nw INSTALL_CERT: identifier %r not in fleet — "
                            "treating as wildcard fan-out", identifier)
                return await self.engine.install_cert_fleet(
                    fullchain, privkey, chain, domain, tenant)
            return await self.engine.install_cert(
                identifier, fullchain, privkey, chain, domain, tenant)

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