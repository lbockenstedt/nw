"""Network devices spoke implementation for Lab Manager.

Connects to the Lab Manager hub over WebSocket and coordinates fleet polling,
device telemetry, VLAN/port operations, and network discovery scans.
"""

# asyncio for background loops and concurrent polls.
import asyncio
# json persists the poll schedule.
import json
# Standard-library logging.
import logging
# os for paths, env knobs and atomic file replace.
import os
# re for MAC detection in chassis ids.
import re
# time for poll deadlines.
import time
# Path helper.
from pathlib import Path
# Type hints.
from typing import Dict, Any

# BaseSpoke lives in core.src in the repo layout, or at top level when deployed.
try:
    from core.src.base_spoke import BaseSpoke
except ImportError:
    from base_spoke import BaseSpoke
# The device engine and its MAC normaliser.
from nw_engine import NwEngine, _norm_mac
# Poll scheduling helper and its default anti-stampede knobs.
from nw_poll_scheduler import (
    plan_poll_tick, POLL_MAX_CONCURRENCY, POLL_MAX_PER_TICK, POLL_JITTER_FRAC,
)

# Named logger for spoke messages.
logger = logging.getLogger("NwSpoke")

# Credential field names — FULL mask in logs (never partial). Mirrors the
# opnsense spoke's masking precedent: leaking both ends of a credential exposes
# a meaningful fraction of a typical secret, so the whole value is replaced.
_SENSITIVE = {"password", "enable_secret", "api_token", "snmp_community",
              "secret", "hub_secret"}



def _norm_chassis(value: str) -> str:
    """Canonicalize an LLDP chassis id ONLY when it is actually a MAC.

    ``chassis subtype`` may be macAddress (4), but it may equally be
    interfaceName, local or networkAddress — in which case the id is a
    human-assigned string. Passing those through ``_norm_mac`` lower-cases
    them, silently turning "OLKS-CORE" into a different identifier than every
    other view of that device uses.
    """
    # Trimmed text; empty means no id.
    raw = "" if value is None else str(value).strip()
    if not raw:
        return ""
    # Exactly 12 hex digits means it is a MAC: normalise it.
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(hex_only) == 12:
        return _norm_mac(raw)
    # Not a MAC: keep the id exactly as given.
    return raw

class NwSpoke(BaseSpoke):
    """Network Devices Management Spoke for Lab Manager.

    Translates Hub NW_* commands into per-device SSH/CLI, REST, or SNMP actions
    via :class:`NwEngine`. Manages a **fleet** of devices (one spoke → many
    devices) pushed from ``global_config["nw_devices"]`` through UPDATE_CONFIG.
    """
    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        """Initialize the network devices spoke with ID, config, and engine."""
        # The engine needs the fleet before super().__init__ so any base-class
        # background worker sees it. The hub pushes devices via UPDATE_CONFIG
        # after approval; at cold start config may carry devices from a
        # pre-provisioned config.
        devices = (config or {}).get("devices", []) if isinstance(config, dict) else []
        self.engine = NwEngine(devices)
        shared_tid = (config or {}).get("shared_tenant_id", "") if isinstance(config, dict) else ""
        if shared_tid:
            self.engine.shared_tenant_id = shared_tid
        # Control-plane back-reference. Set by the standalone NwControlPlane AND
        # by the generic agent's RoleConnection (_start_role_local_services) —
        # the autonomous loops reach the hub through it (send_to_hub) and read
        # its live-connection state. Must init to None so RoleConnection's
        # "only set if None" back-ref assignment fires.
        self.control_plane = None
        # Background loop tasks, started once per process.
        self._bg_tasks: list = []
        # Base-class setup (hub connection plumbing, config).
        super().__init__(spoke_id, config)

    # ── Autonomous background loops (poll + reachability sweep) ──────────────
    # Cadence granularity + defaults. The per-device data-poll cadence lives on
    # each device (``poll_interval``); the fleet-wide reachability sweep uses
    # ``ping_interval``. Both share the anti-stampede knobs in nw_poll_scheduler.
    _NW_POLL_TICK = 10
    _NW_POLL_DEFAULT = 21600   # 6h — data-poll cadence when a device sets none
    _NW_PING_TICK = 10
    _NW_PING_DEFAULT = 300     # 5m — reachability sweep cadence default

    def start_background_loops(self) -> None:
        """Start the spoke's autonomous data-poll loop + ICMP reachability
        sweep. Idempotent, PROCESS-scoped (survives hub reconnects). Called from
        BOTH the standalone ``NwControlPlane.run_hub_mode`` AND the generic
        agent's ``RoleConnection._start_role_local_services`` — so a role-HOSTED
        nw spoke actually polls + pings its fleet (previously these loops lived
        only on NwControlPlane, so a role-hosted nw ran neither: devices never
        got the ping sweep and so never flipped to offline)."""
        # Already running: do nothing.
        if self._bg_tasks:
            return
        # Start the poll loop and the reachability sweep.
        self._bg_tasks = [
            asyncio.create_task(self._nw_poll_loop()),
            asyncio.create_task(self._nw_reachability_loop()),
        ]
        logger.info("nw background loops started (poll + reachability sweep)")

    _NW_POLL_FIRST_MAX = 600   # first poll after a (re)start: 5–15 min, not 3–9h

    @staticmethod
    def _poll_state_path() -> str:
        """Where the poll schedule survives restarts: /var/lib/lm/nw, else a
        repo-local .lm-state/nw (dev / unprivileged boxes)."""
        # Explicit env override wins.
        env = os.environ.get("NW_POLL_STATE_FILE")
        if env:
            return env
        # Use the first writable state directory, creating it if needed.
        for base in ("/var/lib/lm/nw",
                     os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  ".lm-state", "nw")):
            try:
                os.makedirs(base, exist_ok=True)
                if os.access(base, os.W_OK):
                    return os.path.join(base, "poll_due.json")
            except OSError:
                # Cannot create it: try the next one.
                continue
        # Nowhere writable: persistence disabled.
        return ""

    def _load_poll_due(self) -> Dict[str, float]:
        """Load the saved per-device next-poll times; {} if missing or unreadable."""
        path = self._poll_state_path()
        # Read the JSON file and coerce keys/values.
        try:
            with open(path) as f:
                raw = json.load(f)
            return {str(k): float(v) for k, v in (raw or {}).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            # Missing or corrupt: start fresh.
            return {}

    def _save_poll_due(self, next_due: Dict[str, float]) -> None:
        """Save the per-device next-poll times atomically (best-effort)."""
        path = self._poll_state_path()
        # Persistence disabled.
        if not path:
            return
        # Write a temp file, then rename it over the real one.
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(next_due, f)
            os.replace(tmp, path)
        except OSError as e:
            # Saving is best-effort.
            logger.debug("nw poll schedule save failed: %s", e)

    def _hub_ready(self):
        """The control-plane back-ref, iff it's live-connected to the hub; else
        None (loops idle until connected)."""
        # Need a control plane with a live hub socket.
        cp = self.control_plane
        if cp is None or getattr(cp, "_hub_ws", None) is None:
            return None
        # Connected control plane.
        return cp

    async def _nw_poll_loop(self):
        """Per-device autonomous polling. Each device may set ``poll_interval``
        (seconds); this ticks every ``_NW_POLL_TICK`` and polls any device whose
        interval elapsed, pushing ``NW_POLL_RESULT`` so the hub warms its cache.
        Anti-stampede via ``plan_poll_tick`` (jitter + per-tick cap) plus a
        concurrency semaphore."""
        # Deadlines are wall-clock and persisted, so the frequent self-update
        # restarts resume the schedule instead of pushing every device's first
        # poll out again (with a 6h default, a box restarting every few hours
        # never polled at all).
        # Restore the saved schedule.
        next_due: Dict[str, float] = self._load_poll_due()
        if next_due:
            logger.info("nw poll schedule restored for %d device(s)", len(next_due))
        # Tick forever.
        while True:
            await asyncio.sleep(self._NW_POLL_TICK)
            # Errors in one tick must not stop the loop.
            try:
                # Idle until connected to the hub.
                cp = self._hub_ready()
                if cp is None:
                    continue
                # Module-wide default poll interval (blank -> built-in default).
                engine = self.engine
                mod_cfg = self.config or {}
                mod_raw = mod_cfg.get("default_poll_interval")
                try:
                    module_default = (self._NW_POLL_DEFAULT
                                      if mod_raw in (None, "") else int(mod_raw))
                except (TypeError, ValueError):
                    module_default = self._NW_POLL_DEFAULT
                # Wall-clock time and a copy of the schedule to detect changes.
                now = time.time()
                before = dict(next_due)
                # Jitter fraction, clamped to 0-0.9.
                try:
                    jf = float(mod_cfg.get("poll_jitter_frac"))
                except (TypeError, ValueError):
                    jf = POLL_JITTER_FRAC
                jf = max(0.0, min(jf, 0.9))
                # Max devices started per tick, clamped to 1-100.
                try:
                    mpt = int(mod_cfg.get("max_poll_per_tick"))
                except (TypeError, ValueError):
                    mpt = POLL_MAX_PER_TICK
                mpt = max(1, min(mpt, 100))
                # Pick the devices due this tick (also updates next_due).
                due = plan_poll_tick(list(engine.devices), next_due, now,
                                     module_default,
                                     max_per_tick=mpt, jitter_frac=jf,
                                     first_max=self._NW_POLL_FIRST_MAX)
                # Persist the schedule when it changed.
                if next_due != before:
                    self._save_poll_due(next_due)
                # Poll the due devices with bounded concurrency.
                if due:
                    try:
                        conc = int(mod_cfg.get("max_poll_concurrency"))
                    except (TypeError, ValueError):
                        conc = POLL_MAX_CONCURRENCY
                    conc = max(1, min(conc, 50))
                    sem = asyncio.Semaphore(conc)

                    async def _one(device_id):
                        """Poll a single device and push results to hub."""
                        async with sem:
                            await self._nw_poll_and_push(device_id)
                    # Wait for this tick's polls to finish.
                    await asyncio.gather(*(_one(x) for x in due))
            except Exception as e:  # noqa: BLE001 - loop must never die
                # Log and continue on the next tick.
                logger.debug("nw poll loop tick error: %s", e)

    async def _nw_poll_and_push(self, device_id: str):
        """Run one full engine poll + push it to the hub as NW_POLL_RESULT."""
        # Not connected: nothing to push to.
        cp = self.control_plane
        if cp is None:
            return
        engine = self.engine
        # Full poll; a failure is logged and skipped.
        try:
            res = await engine.poll(device_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("nw auto-poll %s failed: %s", device_id, e)
            return
        # Only dict data can be pushed.
        data = res.get("data") if isinstance(res, dict) else None
        if not isinstance(data, dict):
            return
        # A PARTIAL poll returns EMPTY lists for datums whose SSH sub-call
        # failed; the hub folds a poll per-key on presence, so pushing those []s
        # would blank arp/mac/endpoints while it still badges LIVE. Drop the
        # failed datums so the hub keeps its last-good for each.
        # Datum names that failed, from "datum: message" errors.
        failed = set()
        for msg in (res.get("errors") or []):
            if isinstance(msg, str) and ":" in msg:
                failed.add(msg.split(":", 1)[0].strip())
        # Drop the failed datums.
        for label in ("device_info", "interfaces", "arp", "mac_table", "vlans"):
            if label in failed:
                data.pop(label, None)
        # Endpoints are derived from arp/mac/interfaces: drop them if any of those failed.
        if failed & {"arp", "mac_table", "interfaces"}:
            data.pop("endpoints", None)
        # Normalise MACs in every list row.
        for key in ("arp", "mac_table", "interfaces", "endpoints"):
            lst = data.get(key)
            if isinstance(lst, list):
                data[key] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                             for r in lst if isinstance(r, dict)]
        # Push to the hub and log what was dropped.
        await cp.send_to_hub("NW_POLL_RESULT",
                             {"device_id": device_id, "data": data})
        logger.info("nw auto-poll %s -> pushed (status=%s, dropped=%s)",
                    device_id, res.get("status"), sorted(failed) or None)

    async def _nw_reachability_loop(self):
        """Lightweight ICMP reachability sweep — independent of the (slow) data
        poll. Every device is ICMP-pinged on a ~5-min cadence (operator-tunable
        ``ping_interval``; ``0`` disables) and the result is (a) cached on the
        engine so a box confirmed DOWN is NOT hammered with a full SSH/SNMP/REST
        poll, and (b) pushed to the hub (``NW_REACHABILITY``) so the fleet
        up/down/unknown badge stays fresh between the hours-apart data polls.
        Same anti-stampede machinery + knobs as the poll loop; the cadence is
        fleet-wide (``ping_interval``), so every device is shadowed to id-only."""
        # Per-device next-ping time (monotonic).
        next_ping_due: Dict[str, float] = {}
        # Tick forever.
        while True:
            await asyncio.sleep(self._NW_PING_TICK)
            # Errors in one tick must not stop the loop.
            try:
                # Idle until connected to the hub.
                cp = self._hub_ready()
                if cp is None:
                    continue
                engine = self.engine
                mod_cfg = self.config or {}
                # Sweep interval (blank -> default; 0 or less disables).
                raw = mod_cfg.get("ping_interval")
                try:
                    ping_interval = (self._NW_PING_DEFAULT
                                     if raw in (None, "") else int(raw))
                except (TypeError, ValueError):
                    ping_interval = self._NW_PING_DEFAULT
                if ping_interval <= 0:            # operator disabled the sweep
                    continue
                # Monotonic time for this tick.
                now = time.monotonic()
                # Jitter fraction, clamped to 0-0.9.
                try:
                    jf = float(mod_cfg.get("poll_jitter_frac"))
                except (TypeError, ValueError):
                    jf = POLL_JITTER_FRAC
                jf = max(0.0, min(jf, 0.9))
                # Max devices started per tick, clamped to 1-100.
                try:
                    mpt = int(mod_cfg.get("max_poll_per_tick"))
                except (TypeError, ValueError):
                    mpt = POLL_MAX_PER_TICK
                mpt = max(1, min(mpt, 100))
                # Same interval for every device: plan with id-only shadows.
                shadow = [{"id": d.get("id")} for d in engine.devices
                          if d.get("id")]
                due = plan_poll_tick(shadow, next_ping_due, now, ping_interval,
                                     max_per_tick=mpt, jitter_frac=jf)
                # Ping the due devices with bounded concurrency.
                if due:
                    try:
                        conc = int(mod_cfg.get("max_poll_concurrency"))
                    except (TypeError, ValueError):
                        conc = POLL_MAX_CONCURRENCY
                    conc = max(1, min(conc, 50))
                    sem = asyncio.Semaphore(conc)

                    async def _one(device_id):
                        """Ping a single device and push reachability status to hub."""
                        async with sem:
                            await self._nw_ping_and_push(device_id)
                    # Wait for this tick's pings to finish.
                    await asyncio.gather(*(_one(x) for x in due))
            except Exception as e:  # noqa: BLE001 - loop must never die
                # Log and continue on the next tick.
                logger.debug("nw reachability loop tick error: %s", e)

    async def _nw_ping_and_push(self, device_id: str):
        """ICMP-ping one device (engine caches the verdict to gate polling) and
        push it to the hub as ``NW_REACHABILITY`` so the fleet badge flips
        promptly. Best-effort; never raises into the loop."""
        # Not connected: nothing to push to.
        cp = self.control_plane
        if cp is None:
            return
        # Ping; a failure is logged and skipped.
        try:
            res = await self.engine.ping(device_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("nw reachability ping %s failed: %s", device_id, e)
            return
        # Push only successful pings.
        if not isinstance(res, dict) or res.get("status") != "SUCCESS":
            return
        # Report reachability and latency to the hub.
        await cp.send_to_hub("NW_REACHABILITY", {
            "device_id": device_id,
            "reachable": res.get("reachable"),
            "latency_ms": res.get("latency_ms"),
        })


    # ── Logging helper: mask sensitive fields in any command data ───────────
    @staticmethod
    def _mask(data: Any) -> Any:
        """Recursively redact sensitive credential keys from dictionary data."""
        # Non-dicts pass through.
        if not isinstance(data, dict):
            return data
        # Copy each key, masking sensitive ones.
        out = {}
        for k, v in data.items():
            # Credential fields are fully masked.
            if k in _SENSITIVE:
                out[k] = "********"
            elif k == "credentials" and isinstance(v, list):
                # NW_SCAN carries a list of credential dicts — mask each nested
                # secret so a scan never leaks candidate passwords/communities.
                out[k] = [NwSpoke._mask(c) if isinstance(c, dict) else c for c in v]
            else:
                # Other values are copied as is.
                out[k] = v
        # Masked copy.
        return out

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a hub NW_* command to the engine.

        Command types (case-insensitive): ``UPDATE_CONFIG`` (store the fleet,
        credentials masked in logs), ``GET_VERSION``, ``NW_LIST_DEVICES`` (fleet
        summary + concurrent 3s reachability probe), ``NW_PROBE``,
        ``NW_GET_DEVICE_INFO``, ``NW_GET_MAC_TABLE``, ``NW_GET_ARP``,
        ``NW_GET_LLDP_NEIGHBORS`` (structured topology edges),
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
        # Log the command with secrets masked.
        log_data = self._mask(data)
        logger.info(f"Handling Nw Command: {command_type} with data {log_data}")
        # Run it and log the outcome.
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
            # Status, message and any sub-errors from the result.
            status = str((res or {}).get("status", "")).upper()
            msg = (res or {}).get("message", "")
            errors = (res or {}).get("errors") or []
            # Failures (or sub-errors) log at ERROR so the hub's error view picks them up.
            if status == "ERROR" or errors:
                logger.error("nw command %s result: error — %s%s", command_type,
                             msg or "failed",
                             f" ({len(errors)} sub-error(s))" if errors else "")
            else:
                # Success logs at INFO.
                logger.info("nw command %s result: %s", command_type,
                            status.lower() or "ok")
        except Exception:
            # Logging must never fail the command.
            logger.debug("nw log_result failed", exc_info=True)

    async def _dispatch_command(self, normalized_cmd: str, command_type: str,
                                data: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a normalized command string to the appropriate engine or scanner handler."""
        # ── Lifecycle / config ──────────────────────────────────────────────
        if normalized_cmd == "UPDATE_CONFIG":
            # New fleet list and shared tenant id from the hub.
            devices = (data or {}).get("devices", []) if isinstance(data, dict) else []
            shared_tid = (data or {}).get("shared_tenant_id", "") if isinstance(data, dict) else ""
            # Mask credentials in the per-device log summary.
            summary = [{k: ("********" if k in _SENSITIVE else v)
                        for k, v in d.items()} for d in devices] if isinstance(devices, list) else []
            # Log the masked summary, store the config and reload the engine's fleet.
            logger.info(f"Updating nw fleet configuration: {len(devices if isinstance(devices, list) else [])} "
                        f"device(s) -> {summary}")
            self.config = data or {}
            self.engine.set_devices(devices if isinstance(devices, list) else [],
                                    shared_tenant_id=shared_tid)
            # Report how many devices are now configured.
            return {"status": "SUCCESS",
                    "message": "nw configuration updated from Hub",
                    "device_count": len(self.engine.devices)}

        if normalized_cmd in ("GET_VERSION", "GET-VERSION"):
            # Report the spoke version.
            return {"status": "SUCCESS", "version": self.get_version()}

        # ── Fleet ───────────────────────────────────────────────────────────
        if normalized_cmd == "NW_LIST_DEVICES":
            # Fleet summary for the optional tenant.
            tenant = (data or {}).get("tenant") if isinstance(data, dict) else None
            return await self.engine.list_devices(tenant)

        # ── Per-device (data carries device_id) ─────────────────────────────
        # Target device and optional tenant for the per-device commands.
        device_id = (data or {}).get("device_id", "") if isinstance(data, dict) else ""
        tenant = (data or {}).get("tenant") if isinstance(data, dict) else None

        if normalized_cmd == "NW_PROBE":
            # Reachability probe.
            return await self.engine.probe(device_id, tenant)

        if normalized_cmd == "NW_GET_DEVICE_INFO":
            # Model/serial/firmware.
            return await self.engine.get_device_info(device_id, tenant)

        if normalized_cmd == "NW_GET_MAC_TABLE":
            res = await self.engine.get_mac_table(device_id, tenant)
            # Canonicalize MACs on the way out so the hub/UI/NetBox see one form.
            # Normalise each row's MAC.
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_GET_LLDP_NEIGHBORS":
            res = await self.engine.get_lldp_neighbors(device_id, tenant)
            # Canonicalize the remote chassis id the same way MAC/ARP rows are,
            # so a chassis id and a MAC-table MAC compare equal when the hub
            # stitches LLDP edges together with MAC-inferred ones. Guarded:
            # LLDP chassis subtypes 4/5/7 carry a NAME, not a MAC, and
            # _norm_mac would lower-case it into a different identifier.
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "remote_chassis": _norm_chassis(r.get("remote_chassis", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_GET_ARP":
            res = await self.engine.get_arp(device_id, tenant)
            # Normalise each row's MAC.
            if isinstance(res.get("data"), list):
                res["data"] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                               for r in res["data"] if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_GET_INTERFACES":
            res = await self.engine.get_interfaces(device_id, tenant)
            # Normalise each row's MAC.
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
            # VLAN list.
            return await self.engine.get_vlans(device_id, tenant)

        if normalized_cmd == "NW_POLL":
            # Full poll: probe + device_info + interfaces + arp + mac_table in
            # one call. Canonicalize every MAC-bearing sub-list on the way out.
            res = await self.engine.poll(device_id, tenant)
            # Normalise MACs in each list datum.
            d = res.get("data") if isinstance(res.get("data"), dict) else None
            if d is not None:
                for key in ("arp", "mac_table", "interfaces", "endpoints"):
                    lst = d.get(key)
                    if isinstance(lst, list):
                        d[key] = [{**r, "mac": _norm_mac(r.get("mac", ""))}
                                  for r in lst if isinstance(r, dict)]
            return res

        if normalized_cmd == "NW_RUN_CONFIG":
            # Config commands (currently returns a not-implemented envelope).
            commands = (data or {}).get("commands", []) if isinstance(data, dict) else []
            return await self.engine.run_config(device_id, commands, tenant)

        if normalized_cmd == "NW_PING_SWEEP":
            # ICMP sweep over a list of targets.
            return await self._ping_sweep(data or {})

        if normalized_cmd in ("NW_SCAN", "NW_DISCOVER"):
            # Fingerprint scan: the hub sends candidate target IPs + candidate
            # scan credentials (already overlaid from the vault) + options. The
            # spoke probes each target and returns identified manageable devices
            # for the hub to auto-add. No fleet mutation happens here.
            return await self._run_scan(data or {})

        if normalized_cmd == "INSTALL_CERT":
            # Hub-brokered cert distribution: install the delivered LE cert on
            # the target fleet device. The hub addresses a device by
            # ``identifier`` (its fleet id); accept ``device_id`` as a fallback
            # (parity with the other per-device commands). The engine
            # dispatches by object_type — cx_switch via AOS-CX REST v10 today;
            # aos_switch / ex_switch / gateway return a clear ERROR naming the
            # gap so the hub ledger surfaces it.
            # Target device and the cert material.
            d = data or {}
            identifier = (d.get("identifier") or d.get("device_id") or "").strip()
            fullchain = d.get("fullchain", "")
            privkey = d.get("privkey", "")
            chain = d.get("chain", "")
            domain = d.get("domain", "")
            # Both the chain and the key are required.
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
            # Identifier not in the fleet: treat it as a wildcard as well.
            if self.engine._get_device(identifier, tenant) is None:
                logger.info("nw INSTALL_CERT: identifier %r not in fleet — "
                            "treating as wildcard fan-out", identifier)
                return await self.engine.install_cert_fleet(
                    fullchain, privkey, chain, domain, tenant)
            # A specific fleet device: install on that device only.
            return await self.engine.install_cert(
                identifier, fullchain, privkey, chain, domain, tenant)

        # ── Unknown ─────────────────────────────────────────────────────────
        # Anything else is not supported.
        logger.warning(f"Unknown Nw command type: {command_type}")
        return {"status": "ERROR",
                "message": f"Command {command_type} not supported by nw module"}

    async def _ping_sweep(self, data: Dict[str, Any], ping=None) -> Dict[str, Any]:
        """NW_PING_SWEEP: one ICMP echo to each target IP (at most 2048, 64 at a
        time). Returns ``{"status": "SUCCESS", "alive": [ips that answered]}``.
        The hub uses it to find subnet gateways (.1 / .254)."""
        # Default to the engine's ICMP ping (injectable for tests).
        from nw_engine import icmp_ping
        ping = ping or icmp_ping
        # Clean, capped target list; at most 64 pings at once.
        targets = [str(t).strip() for t in (data.get("targets") or []) if str(t).strip()][:2048]
        sem = asyncio.Semaphore(64)

        async def _one(ip):
            """Ping one IP within the concurrency limit; return it if it answered."""
            async with sem:
                res = await ping(ip, timeout=1.0)
                return ip if res.get("reachable") is True else None

        # Ping all targets and keep the ones that answered.
        alive = [ip for ip in await asyncio.gather(*(_one(t) for t in targets)) if ip]
        logger.info("NW_PING_SWEEP: %d/%d answered", len(alive), len(targets))
        return {"status": "SUCCESS", "probed": len(targets), "alive": alive}

    async def _run_scan(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an NW_SCAN: build an :class:`NwScanner` from the pushed
        credentials + options and fingerprint the target IP list. Returns an
        envelope carrying identified/reachable device fingerprints. Never
        raises — a scan failure is reported as an ERROR envelope so the hub
        ledger surfaces it."""
        # Imported lazily to keep spoke startup light.
        from nw_scanner import NwScanner, DEFAULT_TCP_PORTS
        # Targets, credentials and options from the hub, with type checks.
        targets = data.get("targets") if isinstance(data.get("targets"), list) else []
        credentials = data.get("credentials") if isinstance(data.get("credentials"), list) else []
        opts = data.get("options") if isinstance(data.get("options"), dict) else {}
        # Nothing to scan.
        if not targets:
            return {"status": "ERROR", "message": "NW_SCAN requires a non-empty targets list"}
        if not credentials:
            # Discovery-only pass. Credentials are OPTIONAL: without them the
            # scanner still TCP-probes every target for reachability + open
            # management ports (and can still classify via nmap when enabled),
            # it just cannot SSH/SNMP-identify. That makes "what is on this
            # subnet?" answerable without first creating a device account.
            logger.info("NW_SCAN with no credentials — discovery-only pass "
                        "(reachability + open ports, no SSH/SNMP identify)")
        # TCP ports to probe; bad values fall back to the defaults.
        ports = opts.get("tcp_ports") or DEFAULT_TCP_PORTS
        try:
            ports = tuple(int(p) for p in ports)
        except (TypeError, ValueError):
            ports = DEFAULT_TCP_PORTS
        # Whether to follow LLDP neighbours.
        crawl = bool(opts.get("crawl", False))
        # Without credentials, nmap service detection is the ONLY way left to
        # classify a host, so opt into it by default for a discovery-only pass
        # (an explicit use_nmap in the options always wins). _nmap_augment
        # silently no-ops when nmap isn't installed, so this can't break a scan.
        use_nmap = opts.get("use_nmap")
        if use_nmap is None:
            use_nmap = not credentials
        # Any scan failure becomes an ERROR envelope.
        try:
            # Build the scanner from the options (with defaults).
            scanner = NwScanner(
                credentials,
                tcp_ports=ports,
                try_snmp=bool(opts.get("try_snmp", True)),
                use_nmap=bool(use_nmap),
                concurrency=int(opts.get("concurrency") or 32),
                tcp_timeout=float(opts.get("tcp_timeout") or 1.5),
                target_timeout=float(opts.get("target_timeout") or 20.0),
                lldp_neighbors=self._lldp_neighbors if crawl else None,
                pinged=bool(opts.get("pinged", False)),
            )
            # Run the scan.
            result = await scanner.scan(
                targets,
                crawl=crawl,
                max_targets=int(opts.get("max_targets") or 4096),
                max_depth=int(opts.get("max_depth") or 1),
            )
            return result
        except Exception as e:  # noqa: BLE001
            # Log the traceback and report the failure.
            logger.exception("NW_SCAN failed")
            return {"status": "ERROR", "message": f"scan failed: {e}"}

    @staticmethod
    async def _lldp_neighbors(host: str, credentials: list) -> list:
        """LLDP-crawl callable: SSH into ``host`` with each candidate credential,
        identify the family from the banner, and return neighbor management IPs.
        Best-effort — any failure yields ``[]`` so the crawl simply stops here."""
        # Imported lazily; only needed for the crawl.
        from transports import cli_io
        from nw_scanner import classify_platform
        # Try each credential until one logs in.
        for cred in credentials or []:
            # Neutral CLI device dict for this host.
            dev = {"address": host, "port": 22,
                   "username": cred.get("username") or cred.get("user") or "",
                   "password": cred.get("password") or "",
                   "enable_secret": cred.get("enable_secret") or "",
                   "object_type": ""}
            # No username: skip.
            if not dev["username"]:
                continue
            # Bad device config: skip this credential.
            try:
                session = cli_io.CliSession(dev, command_timeout=15.0)
            except cli_io.CliError:
                continue
            try:
                # Log in and grab the version banner (optional).
                await session.connect()
                banner = ""
                try:
                    banner = await session.run("show version")
                except Exception:  # noqa: BLE001
                    pass
                # Family from the banner (AOS-S default), then fetch neighbours.
                ot = classify_platform(banner) or "aos_switch"
                return await cli_io.cli_get_lldp_neighbors(session, ot)
            except Exception:  # noqa: BLE001
                # Failed: try the next credential.
                continue
            finally:
                # Always log out.
                try:
                    await session.close()
                except Exception:  # noqa: BLE001
                    pass
        # No credential worked.
        return []

    async def get_status(self) -> Dict[str, Any]:
        """Native LM status report for the nw fleet."""
        # Basic status fields.
        status = {
            "spoke_id": self.spoke_id,
            "module": "nw",
            "device_count": len(self.engine.devices),
            "connection": "CONNECTED",
        }
        # Surface the learned per-device connection profiles (prompt / paging /
        # login gates / banner fingerprint) so the record of how we reach each
        # device is visible without a code/log dig. Best-effort.
        try:
            from transports import device_profile
            profiles = device_profile.store().all()
            if profiles:
                status["device_profiles"] = profiles
        except Exception:  # noqa: BLE001 — status must not fail on profiling
            pass
        # Status report.
        return status

    def get_version(self) -> str:
        """Current nw module version (repo-root VERSION).

        Reads ``<repo>/VERSION`` (one dir above ``src/``). Same path pattern as
        the opnsense spoke — avoids the cs-spoke wrong-VERSION-path gotcha
        (reading a non-existent sibling VERSION → "unknown" on the Diag page).
        """
        # VERSION file one level above src/.
        try:
            return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
        except Exception:
            logger.exception("Failed to read VERSION file")
            return "unknown"