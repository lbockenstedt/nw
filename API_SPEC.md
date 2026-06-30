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
- `NW_PROBE` → `{"status":"SUCCESS","reachable": bool, "latency_ms": int}`.
- `NW_GET_DEVICE_INFO` → `{"status":"SUCCESS","data":{model,serial,firmware,
  interfaces_count}}`.
- `NW_GET_MAC_TABLE` → `{"status":"SUCCESS","data":[{mac, vlan, interface}]}`.
- `NW_GET_ARP` → `{"status":"SUCCESS","data":[{ip, mac, interface}]}`.
  (This is the record set the hub's NW→NetBox discovery sync attributes to
  tenants by IP-prefix containment and pushes to NetBox.)
- `NW_GET_INTERFACES` → `{"status":"SUCCESS","data":[{name, ip, mac, vlan,
  status, speed}]}`.

### Configure (admin-only at the hub route)
- `NW_RUN_CONFIG` `{"device_id","commands":[...]}` →
  `{"status":"SUCCESS","applied":[...],"errors":[...]}`.

## Driver / transport matrix

| object_type  | default transport | vendor CLI / API                             |
|--------------|-------------------|----------------------------------------------|
| `aos_switch` | ssh               | Aruba AOS-S (`show mac-address-table`, `show arp`, `show system`) |
| `cx_switch`  | rest              | Aruba AOS-CX RESTv1 (`/rest/v1/...`)         |
| `ex_switch`  | ssh               | Junos (`show ethernet-switching table`, `show arp`, `show version`) |
| `gateway`    | rest              | Aruba/HPE gateway REST                       |

All four object types also support SNMP (v2c/v3) as an alternate/fallback
transport. `transport=auto` selects the per-type default.

> **Phase 1 (this drop):** device IO is **stubbed** — each driver method logs
> the intent and returns a structured `SUCCESS` placeholder so the full
> hub→spoke→UI→NetBox pipeline is exercisable without real devices. Real
> driver implementations land in phase 2.