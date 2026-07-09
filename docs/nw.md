# nw — Network Devices

Network-devices fleet spoke. Repo: `nw`. `module_type = "nw"`. See [architecture-topology.md](architecture-topology.md).

## Role & module_type

Manages a **fleet** of switches/gateways (one spoke → many devices). Per-device discovery (probe, device info, MAC table, ARP, interfaces) over SSH/CLI, REST, or SNMP. Source for the NW→NetBox discovery sync (ARP attributed to tenants by IP-prefix containment) and the per-device POLL NOW inventory sync.

## What it does

nw manages a **fleet** of switches and gateways from a single spoke — unlike most LM modules (one spoke per target system), one nw spoke can hold many devices (AOS-Switch, AOS-CX, Juniper EX, Aruba/HPE gateway). It logs into each device to pull device info, MAC address tables, ARP tables, and interface lists, then feeds that data into NetBox so the network topology (devices, IPs, MACs, cabling) shows up automatically instead of being hand-entered.

In the WebUI this is the **Network Devices** view (Setup → Network Devices tile for adding/editing devices; the module tile itself reads "Network Devices (nw)"). Live fleet state (reachability, per-device MAC/ARP/interface data) is shown wherever the Network Devices page renders a device row.

## Entrypoints

`python3 -m src.control_plane` (`NwControlPlane`); spoke `NwSpoke(BaseSpoke)`, module name `"nw"`. systemd `lm-nw.service`. Installer `install_nw.sh` (clones to `/opt/lm/nw`, venv, `.env`, unit; equals-attached `--id=…` form so values starting with `-` don't trip argparse; clears `SPOKE_SECRET` to `""` when unset for zero-touch pending).

> **Primarily a role now.** nw runs mainly as the **`network`** role hosted by the generic agent (`agent-<hostname>`, unit `lm-agent`): the agent opens a sub-spoke `{agent}-network` (module_type `nw`, parent-auto-approved) and self-installs it via `agent/src/agent_spoke.py::_install_role` (clones `lbockenstedt/nw.git` + deps). The dedicated `lm-nw.service` / `install_nw.sh` `nw-spoke-1` path is the **legacy/standalone** alternative. The device fleet still arrives via the hub push (`global_config["nw_devices"]` over `UPDATE_CONFIG`), not a per-module `.env`.

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

## How it works

- **Fleet delivery, not per-device install.** The device list is never read from a file on the spoke — the hub pushes the whole fleet as `global_config["nw_devices"]` via `UPDATE_CONFIG` every time the spoke connects, is approved, or reconnects, and again any time an admin adds/edits/deletes a device in the WebUI. The spoke's `NwEngine.set_devices()` just replaces its in-memory list; there is no per-module `.env` involved in device data.
- **Driver selection per device.** Each device dict carries `object_type` (`aos_switch`, `cx_switch`, `ex_switch`, `gateway`) and `transport` (`ssh`, `rest`, `snmp`, or `auto`). `auto` resolves per `object_type`: `aos_switch`/`ex_switch` → SSH/CLI (asyncssh + vendor text parsers in `transports/cli_io.py`), `cx_switch`/`gateway` → REST (httpx, `transports/rest_io.py`; AOS-CX RESTv1 uses basic auth, Aruba/HPE gateway REST uses a bearer token; TLS verification is controlled by `LM_NW_VERIFY_TLS`, off by default). SNMP (`transports/snmp_io.py`, SNMPv2c) is valid as an explicit transport for any device family. An unrecognized `object_type` builds no driver at all — the device is silently skipped (logged as a WARNING), not errored.
- **Commands the hub can send:** `NW_LIST_DEVICES` (fleet summary + a concurrent 3s-timeout reachability probe per device — this is what powers the up/down dot in the UI), `NW_PROBE`, `NW_GET_DEVICE_INFO`, `NW_GET_MAC_TABLE`, `NW_GET_ARP`, `NW_GET_INTERFACES` (each a single per-device datum), and `NW_POLL` (all four data pulls plus a probe in one call — this is what "POLL NOW" in the UI triggers). `NW_RUN_CONFIG` (pushing config changes to a device) is a stub today — it always returns a not-implemented error envelope, it does not silently no-op.
- **Failure isolation.** Every driver call returns a `{"status": "SUCCESS"|"ERROR", ...}` envelope; a transport exception is caught and turned into an ERROR envelope rather than raised. `NW_POLL` treats its four sub-pulls independently, so one failing datum (say ARP times out) doesn't blank out the others — the response carries whatever succeeded plus an `errors` list, and its overall status is `PARTIAL` rather than `SUCCESS` if reachable but any sub-datum errored.
- **Data flow into NetBox** happens in two distinct paths, both hub-orchestrated (the sync logic itself lives on the hub, not the nw spoke):
  - The **discovery sync** (`nw_discovery_sync.py`) periodically calls `NW_GET_ARP` (and `NW_GET_MAC_TABLE`) against every device on every connected nw spoke, merges the results, attributes each IP↔MAC pair to a tenant by IP-prefix containment, and pushes per-tenant to the netbox spoke via `NETBOX_SYNC_DEVICES` (`source="Network Devices"`, `replace=True` — so a nw-owned tenant's record set is fully replaced each cycle, without touching records other sources like opnsense discovered). IPs matching no tenant prefix are dropped and counted, not created as orphans.
  - **POLL NOW** on a single device (`poll_nw_device`) runs `NW_POLL` against that device, attributes it to a tenant by its own management-address prefix containment, and pushes the device + its interfaces to NetBox via `NETBOX_SYNC_NW_DEVICE` — a `dcim.device` inventory upsert, distinct from the ARP-based `NETBOX_SYNC_DEVICES` flow above.
- **Caching lives on the hub, not the spoke.** The nw spoke itself is stateless between calls (aside from the in-memory fleet). The hub-side `NwCacheMixin` (`lm/core/src/nw_cache.py`) caches the last fleet list and each device's info/MAC/ARP/interfaces/poll result in memory, and atomically persists them to `cache/nw_data.json`. On hub restart this file reloads and seeds the Network Devices UI immediately (and it continues serving last-known data if the nw spoke is offline) instead of the page 503ing until the spoke reconnects.
- **Scheduling.** The discovery sync runs on its own configurable loop (`nw_netbox_device_sync` in global config: enabled/source/mode/interval — the loop starts ~75s after hub startup, staggered after the firewall-discovery sync so both don't fire simultaneously). POLL NOW is on-demand only — there is no automatic per-device polling loop; a device's info/MAC/ARP/interfaces only refresh when an admin clicks Poll Now or the discovery sync's periodic ARP pull runs.

## How to use it

- **Add a device to the fleet.** Setup → Network Devices → "+ Add Device". Required fields: a name, `object_type` (AOS Switch / CX Switch / EX Switch / Gateway), `transport` (leave `auto` unless you need to force SSH/REST/SNMP), the device's management `address`, and credentials appropriate to the transport (SSH: username/password/`enable_secret` for AOS-S enable mode; REST: username/password or API token; SNMP: community string). Saving pushes the updated fleet to the connected nw spoke immediately via `UPDATE_CONFIG` — no spoke restart needed.
- **Check whether a device is reachable.** The Network Devices list view itself triggers `NW_LIST_DEVICES`, which live-probes every device (3s timeout each) — reachable/unreachable/unknown shows per row.
- **Pull live data for one device.** Open the device's detail row and use Poll Now — this runs `NW_POLL` (probe + device info + interfaces + ARP + MAC table in one call) and immediately also pushes the device to NetBox via `NETBOX_SYNC_NW_DEVICE`.
- **View the MAC or ARP table.** From the device detail view, the MAC Table / ARP tabs pull `NW_GET_MAC_TABLE` / `NW_GET_ARP` directly (fresh, not from cache) — MACs are always shown canonicalized as lower-case colon-separated (`aa:bb:cc:dd:ee:ff`).
- **Trigger a discovery sync to NetBox on demand** instead of waiting for the scheduled interval: use the "Sync now" action on the nw discovery card in System → Sync — this pulls ARP from every device on every connected nw spoke and pushes to NetBox right away.
- **Edit or remove a device**: Setup → Network Devices → edit/delete on the device row; the change is re-pushed to the spoke the same way as an add.

## Troubleshooting / common questions

- **"A device shows unknown or unreachable in the Network Devices view — is it actually down?"** `NW_LIST_DEVICES` probes each device with a 3-second timeout; `unknown` means the probe itself errored (not necessarily that the device is down) while `false`/red generally means the connection attempt failed outright. Check: is the management `address` correct, are the credentials for the configured `transport` correct, and is the device reachable on the network from wherever the nw spoke/agent runs (SSH/443/SNMP port open, no ACL blocking it). A probe failure is logged at WARNING (not ERROR) during a fleet list, since one device being briefly unreachable in a big fleet is normal.
- **"The nw spoke/agent shows offline (red) in Diagnostics — what happens to the Network Devices page?"** Nothing breaks — the page continues to show the hub's last-known cached fleet + per-device data from `cache/nw_data.json` (see NwCacheMixin above). New probes/polls simply can't run until the spoke reconnects; expect stale (not empty) data.
- **"I added a device but it doesn't show up / doesn't get polled."** Confirm the object_type is one of the four supported values — anything else silently skips the device (logged as a WARNING: "Unknown object_type ... — skipped"), it will not appear as an error to the user, just missing from results. Also confirm a nw spoke (or an agent's `network` role) is actually connected — devices sit in `global_config["nw_devices"]` but nothing polls them without a live spoke.
- **"I tried to push a config change to a switch and nothing happened."** `NW_RUN_CONFIG` (config push) is not implemented yet — it deliberately returns an error envelope ("run_config not implemented for this transport") rather than silently doing nothing, so this is expected behavior today, not a bug.
- **"Why does NetBox show a device from nw discovery that I don't recognize, or why did a nw-discovered device disappear?"** The discovery sync is authoritative per cycle for nw-owned records: each run replaces the tenant's ARP-discovered device set with what it just saw (records not seen this cycle are deleted, but only ones previously tagged as nw-owned — other sources' records like opnsense's are untouched). A device that stopped showing an ARP entry (e.g. it aged out or the switch was unreachable that cycle) will disappear from NetBox on the next sync. A device whose management IP falls outside every tenant's NetBox prefixes is dropped from the push entirely (logged as an unattributed count), not created as an orphan.
- **"Is nw running as its own service or as part of the agent?"** In the current topology, nw normally runs as the `network` role inside the generic `lm-agent` unit (sub-spoke `{agent}-network`, auto-approved by its parent) — check Diagnostics for a spoke id like `agent-<hostname>-network`. The standalone `lm-nw.service` / `install_nw.sh` path (spoke id `nw-spoke-1`) still works but is the legacy path; either way, device config always arrives via the hub push, never a local `.env`.

## Related pages

[architecture-topology.md](architecture-topology.md), [netbox.md](netbox.md), [lm-hub.md](lm-hub.md), [install-flags.md](install-flags.md).