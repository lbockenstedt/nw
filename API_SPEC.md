# Network Devices (`nw`) Spoke — API / Command Contract

The `nw` spoke manages a **fleet** of network devices (Aruba AOS-S switches,
Aruba AOS-CX switches, Juniper EX switches, and Aruba/HPE gateways). It
connects to the LM hub as `module_type="nw"` (service `lm-nw`) and translates
hub commands into per-device SSH/CLI, REST, or SNMP actions.

All command responses use the standard envelope:

```json
{"status": "SUCCESS" | "ERROR", "data": [...], "message": "..."}
```

The hub's `unwrap_spoke` / discovery-sync extraction reads `payload.data`, so
every list response must carry `data` as a list.

## Device model

The hub persists `global_config["nw_devices"]` (a list) and pushes the subset
bound to this spoke via `UPDATE_CONFIG`. Each device dict:

| field          | type | notes                                                       |
|----------------|------|-------------------------------------------------------------|
| `id`           | str  | uuid (hub-assigned)                                         |
| `name`         | str  | display name                                                |
| `object_type`  | str  | `aos_switch` \| `cx_switch` \| `ex_switch` \| `gateway`     |
| `address`      | str  | management IP / hostname                                    |
| `transport`    | str  | `ssh` \| `rest` \| `snmp` \| `auto` (auto = per-type default)|
| `port`         | int  | SSH/REST port (optional)                                    |
| `username`     | str  | SSH/REST username                                           |
| `password`     | str  | SSH/REST password (secret — never logged)                   |
| `enable_secret`| str  | enable/privileged password (secret)                         |
| `api_token`    | str  | REST API token (secret)                                     |
| `snmp_community`| str | SNMP community (secret)                                     |
| `spoke_id`     | str  | hub binding (optional; unbound → this spoke)                |

## Commands

### Lifecycle
- `get_version` / `GET_VERSION` → `{"status":"SUCCESS","version": "<.NN>"}`
  (reads repo-root `VERSION`).
- `UPDATE_CONFIG` `{"devices": [...]}` → store the fleet; returns
  `{"status":"SUCCESS","message":"nw configuration updated from Hub",
  "device_count": N}`. Credentials are masked in logs.
- `*_GET_STATUS` → `get_status()` fleet summary.

### Fleet
- `NW_LIST_DEVICES` → `{"status":"SUCCESS","data":[{id,name,object_type,
  address,transport,reachable}]}`.

### Per-device (data carries `{"device_id": "<id>"}`)
- `NW_PROBE` → `{"status":"SUCCESS","data":{reachable: bool, latency_ms: int}}`.
- `NW_GET_DEVICE_INFO` → `{"status":"SUCCESS","data":{model,serial,firmware,
  interfaces_count}}`.
- `NW_GET_MAC_TABLE` → `{"status":"SUCCESS","data":[{mac, vlan, interface}]}`.
- `NW_GET_ARP` → `{"status":"SUCCESS","data":[{ip, mac, interface}]}`.
  (This is the record set the hub's NW→NetBox discovery sync attributes to
  tenants by IP-prefix containment and pushes to NetBox.)
- `NW_GET_INTERFACES` → `{"status":"SUCCESS","data":[{name, ip, mac, vlan,
  status, speed}]}`.
- `NW_POLL` → one-shot full poll (probe + device_info + interfaces + arp +
  mac_table), each datum independent so a single failure doesn't sink the rest:
  `{"status":"SUCCESS"|"PARTIAL","data":{reachable, latency_ms, device_info,
  interfaces, arp, mac_table}, "errors":[...], "message":"..."}`. MACs are
  canonicalized on the way out. The hub's POLL NOW button sends this and pushes
  the device + interfaces to NetBox via `NETBOX_SYNC_NW_DEVICE`.

> Every datum method returns an `ERROR` envelope (`{"status":"ERROR","data":[],
> "message":...}`) on transport failure (timeout, no community, auth refused,
> bad JSON) — never raises — so one device's failure doesn't sink a batch. This
> is the fix for the "SNMP scan returned nothing" symptom: the old stubs
> returned `SUCCESS` with empty data; real drivers now surface the error.

### Configure (admin-only at the hub route)
- `NW_RUN_CONFIG` `{"device_id","commands":[...]}` → not implemented this pass
  (`{"status":"ERROR","applied":[],"errors":["run_config not implemented ..."]}`).

## Driver / transport matrix

| object_type  | default transport | vendor CLI / API                             |
|--------------|-------------------|----------------------------------------------|
| `aos_switch` | ssh               | Aruba AOS-S (`show mac-address-table`, `show arp`, `show system`) |
| `cx_switch`  | rest              | Aruba AOS-CX RESTv1 (`/rest/v1/...`)         |
| `ex_switch`  | ssh               | Junos (`show ethernet-switching table`, `show arp`, `show version`) |
| `gateway`    | rest              | Aruba/HPE gateway REST                       |

All four object types also support SNMP (v2c) as an alternate/fallback
transport (`transport=snmp`). `transport=auto` selects the per-type default.

Drivers are real (not stubbed):
- **SnmpDriver** — SNMPv2c via pysnmp-lextudio, standard MIBs only (IF-MIB,
  IP-MIB, BRIDGE-MIB) so the same OIDs work across all four families. Requires
  `snmp_community` on the device. Blocking pysnmp calls run via
  `asyncio.to_thread`.
- **SshCliDriver** — asyncssh interactive PTY, per-vendor text parsers
  (`transports/cli_io.py`); `enable_secret` enters enable mode on AOS-S.
- **RestDriver** — httpx async, AOS-CX RESTv1 (basic auth) + gateway REST
  (bearer `api_token`); `LM_NW_VERIFY_TLS` env knob (default off for lab
  self-signed).

IO lives in `src/transports/` (lazy-imported heavy libs) so `nw_engine.py`
imports cleanly without them.