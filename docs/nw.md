# nw — Network Devices

Network-devices fleet spoke. Repo: `nw`. `module_type = "nw"`. See [architecture-topology.md](architecture-topology.md).

## Role & module_type

Manages a **fleet** of switches/gateways (one spoke → many devices). Per-device discovery (probe, device info, MAC table, ARP, interfaces) over SSH/CLI, REST, or SNMP. Source for the NW→NetBox discovery sync (ARP attributed to tenants by IP-prefix containment) and the per-device POLL NOW inventory sync.

## Entrypoints

`python3 -m src.control_plane` (`NwControlPlane`); spoke `NwSpoke(BaseSpoke)`, module name `"nw"`. systemd `lm-nw.service`. Installer `install_nw.sh` (clones to `/opt/lm/nw`, venv, `.env`, unit; equals-attached `--id=…` form so values starting with `-` don't trip argparse; clears `SPOKE_SECRET` to `""` when unset for zero-touch pending).

## Ports / backends

No port served. Per-device transports (chosen by `object_type` + `transport`):
- **SSH/CLI** (`SshCliDriver`, `transports/cli_io.py`, asyncssh + per-vendor text parsers): AOS-Switch (`aos_switch`, default ssh), Junos EX (`ex_switch`, ssh). `enable_secret` enters enable mode on AOS-S.
- **REST** (`RestDriver`, `transports/rest_io.py`, httpx): AOS-CX RESTv1 basic auth (`cx_switch`, default rest), Aruba/HPE gateway REST bearer (`gateway`, default rest). TLS verify controlled by `LM_NW_VERIFY_TLS` (default off).
- **SNMP** (`SnmpDriver`, `transports/snmp_io.py`, pysnmp-lextudio, SNMPv2c): valid for any family.
- Vendor command map in `_VENDOR_COMMANDS` (`aos_switch`, `cx_switch`, `ex_switch`, `gateway`).

## Environment variables

`SPOKE_ID`, `SPOKE_SECRET`, `HUB_SECRET`, `HUB_URL`; `LM_NW_VERIFY_TLS` (rest_io). The fleet itself is **not** env-driven — the hub pushes `global_config["nw_devices"]` via `UPDATE_CONFIG` on connect/approve/reconnect.

## Install flags

`install_nw.sh`: `--hub`, `--id`/`--name`, `--secret`, `--hub-secret`, `--all-prereqs` (no-op).

## Key commands / handlers (`nw_spoke.handle_command`)

`UPDATE_CONFIG` (store fleet, masked, returns `device_count`), `GET_VERSION`, `NW_LIST_DEVICES` (fleet summary + concurrent 3s probe per device for live reachability), `NW_PROBE`, `NW_GET_DEVICE_INFO`, `NW_GET_MAC_TABLE`, `NW_GET_ARP`, `NW_GET_INTERFACES`, `NW_POLL` (probe + device_info + interfaces + arp + mac_table in one call; each sub-call independent → partial results + `errors`), `NW_RUN_CONFIG` (TODO/not-implemented envelope — config push out of scope). MACs canonicalized to lower-colon `aa:bb:cc:dd:ee:ff` on the way out.

## Key files

`src/nw_engine.py` (~558 lines — `NwEngine`, `NwDriver` base + `SnmpDriver`/`SshCliDriver`/`RestDriver`, `build_driver`, `_norm_mac`, `_VENDOR_COMMANDS`, `_DEFAULT_TRANSPORT`), `src/transports/{cli_io,rest_io,snmp_io}.py` (lazy-imported so the engine imports cleanly without asyncssh/pysnmp), `src/nw_spoke.py`, `src/control_plane.py`, `install_nw.sh`, `API_SPEC.md`.

## Notable behaviors & gotchas

- **Driver selection** — `transport=auto` resolves per `object_type` (aos_switch→ssh, cx_switch→rest, ex_switch→ssh, gateway→rest). Unknown `object_type` → driver None, device skipped.
- **Failure isolation** — every driver method returns a `{"status":"ERROR",…}` envelope (never raises); `poll()` continues on partial failure; `list_devices` probes concurrently with `asyncio.gather` + 3s `wait_for` and falls back to `unknown` reachability so the UI never shows a stale "up".
- **Logging** — `_log_datum`/`_log_result` emit INFO on success (row count) and ERROR (carrying "error" so it surfaces in the hub `GET_ERROR_LOGS`/Error Log tab). Credentials (`password, enable_secret, api_token, snmp_community, secret, hub_secret`) never logged.
- **ARP is the discovery feed** — `NW_GET_ARP` data is what the hub's NW→NetBox sync attributes to tenants by IP-prefix containment and pushes via `NETBOX_SYNC_DEVICES`; `NW_POLL`'s `interfaces` feed `NETBOX_SYNC_NW_DEVICE`.
- **Cache lives on the hub, not here.** The spoke is explicitly stateless across commands apart from the in-memory fleet (`self.devices`); there is no JSON cache file on this spoke. Hub-side `NwCacheMixin` (`lm/core/src/nw_cache.py`) caches the nw fleet + per-device info/macs/arp/interfaces in memory AND atomically persists `cache/nw_data.json` (loaded on startup to seed the Network Devices UI on restart/spoke-outage). Staleness sweep lives on the **netbox** spoke (`NETBOX_STALENESS_SWEEP`).

## Related pages

[architecture-topology.md](architecture-topology.md), [netbox.md](netbox.md), [lm-hub.md](lm-hub.md), [install-flags.md](install-flags.md).