# nw — Network Devices Spoke (Lab Manager Module)

The `nw` module is the authoritative network device management spoke for the Lab Manager (LM) ecosystem. It provides continuous polling, device discovery, topology mapping, port status monitoring, MAC/ARP extraction, VLAN inspection, and certificate deployment across multi-vendor switches, access points, and network gateways.

---

## Architecture

The spoke operates as a lightweight, high-performance orchestration layer between the Lab Manager hub and managed network devices:

```
                  ┌────────────────────────┐
                  │    Lab Manager Hub     │
                  │   (REST API / WebUI)   │
                  └───────────┬────────────┘
                              │ WebSocket (Port 443)
                              ▼
                  ┌────────────────────────┐
                  │        NwSpoke         │
                  │   (Spoke Coordinator)  │
                  └───────────┬────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│   NwScanner      │ │  NwPollScheduler │ │     NwEngine     │
│ (Target Discovery│ │(Autonomous Poll/ │ │ (Device Driver   │
│ & Fingerprinting)│ │ Reachability)    │ │    Dispatcher)   │
└──────────────────┘ └──────────────────┘ └────────┬─────────┘
                                                   │
         ┌───────────────────┬─────────────────────┼─────────────────────┐
         ▼                   ▼                     ▼                     ▼
┌─────────────────┐ ┌─────────────────┐  ┌──────────────────┐  ┌─────────────────┐
│ ArubaOS-CX      │ │ HP ProCurve     │  │ Cisco IOS / XE   │  │ Generic SNMP    │
│ REST API (v10)  │ │ SSH / CLI (VT)  │  │ SSH / Netmiko    │  │ v2c / v3 Poller │
└─────────────────┘ └─────────────────┘  └──────────────────┘  └─────────────────┘
```

1. **Spoke Coordinator (`nw_spoke.py`):**
   - Connects to the central Lab Manager hub over an authenticated, TLS-encrypted WebSocket channel.
   - Dispatches incoming hub commands, handles fleet reconfiguration (`UPDATE_CONFIG`), and routes telemetry and poll events.
2. **Device Engine (`nw_engine.py`):**
   - Central device registry and transport abstraction manager.
   - Normalizes hardware profiles, MAC address formatting, ARP tables, interface counters, and VLAN tables across vendors into a unified schema.
3. **Discovery Scanner (`nw_scanner.py`):**
   - Autonomous multi-source scanner utilizing NetBox prefixes, DHCP leases, and custom IP ranges.
   - Fingerprints candidate devices over SSH, HTTPS/REST, and SNMP using vault-supplied scan credentials.
4. **Polling & Reachability Scheduler (`nw_poll_scheduler.py`):**
   - Manages asynchronous, non-blocking polling loops for fleet reachability (ICMP pings) and detailed device telemetry.
   - Implements anti-stampede jitter, per-tick concurrency throttling, and automatic backoff for unresponsive hardware.
5. **Per-Vendor Transports (`transports/`):**
   - Specialized drivers for **ArubaOS-CX** (HTTPS REST API v10), **HP ProCurve / AOS-S** (SSH CLI with VT terminal emulation), **Juniper EX** (SSH CLI), and **Generic SNMP** (SNMPv2c).

---

## Features

- **Multi-Vendor Device Polling:** Unified polling engine for ArubaOS-CX, HP ProCurve (AOS-S), Juniper EX, and SNMP-enabled appliances.
- **Auto-Discovery & Fingerprinting:** Rapid scanning of CIDR blocks, automatically detecting vendor models, firmware versions, serial numbers, and management endpoints.
- **LLDP Topology Mapping:** Automated neighbor extraction across ports to map physical and logical datacenter topology.
- **VLAN Inspection:** Query and inspect configured 802.1Q VLANs and port memberships.
- **Port Operational Status & Metrics:** Real-time port speeds, duplex modes, administrative status, PoE power delivery, and traffic statistics.
- **Fused ARP & MAC Tables:** Fuses layer-2 MAC address tables with layer-3 ARP bindings into deduplicated endpoint records with standardized MAC formatting (`aa:bb:cc:dd:ee:ff`).
- **Hub-Brokered SSL Certificate Installation:** Installs Let's Encrypt certificates directly into switch web interfaces (ArubaOS-CX REST API) via `INSTALL_CERT`.
- **Tenant Isolation:** Rigorous tenant attribution across all network scans, device registries, and NetBox synchronization.

---

## Spoke Commands Reference

The spoke receives commands from the Lab Manager hub over WebSocket and executes them via `nw_spoke.py`:

| Command | Target / Arguments | Description |
| :--- | :--- | :--- |
| `GET_VERSION` | None | Returns the active running spoke code version and commit SHA. |
| `UPDATE_CONFIG` | `devices`, `shared_tenant_id` | Pushes full fleet configuration and credential mappings to the spoke engine. |
| `NW_LIST_DEVICES` | `tenant` *(optional)* | Probes all configured fleet devices and returns operational reachability status. |
| `NW_PROBE` | `device_id`, `tenant` *(optional)* | Probes a specific network device to test transport reachability. |
| `NW_GET_DEVICE_INFO` | `device_id`, `tenant` *(optional)* | Returns hardware metadata (model, serial number, OS version, uptime, sysObjectID). |
| `NW_GET_MAC_TABLE` | `device_id`, `tenant` *(optional)* | Fetches forwarding table entries (MAC, port, VLAN ID) with canonical MAC formatting. |
| `NW_GET_ARP` | `device_id`, `tenant` *(optional)* | Fetches ARP table bindings (IP address to MAC address mapping). |
| `NW_GET_INTERFACES` | `device_id`, `tenant` *(optional)* | Retrieves port states, duplex, operational speed, descriptions, and error counters. |
| `NW_GET_ENDPOINTS` | `device_id`, `tenant` *(optional)* | Returns fused ARP + MAC table of unique active IP/MAC endpoints seen on the device. |
| `NW_GET_VLANS` | `device_id`, `tenant` *(optional)* | Lists 802.1Q VLAN IDs and names configured on the switch. |
| `NW_POLL` | `device_id`, `tenant` *(optional)* | Comprehensive multi-metric poll (probe, info, interfaces, ARP, and MAC table) in one pass. |
| `NW_RUN_CONFIG` | `device_id`, `commands` | Stub handler returning not-implemented envelope (config push planned for phase 3). |
| `NW_SCAN` / `NW_DISCOVER` | `targets`, `credentials`, `options` | Probes candidate IP addresses, fingerprinting manageable devices without modifying fleet. |
| `INSTALL_CERT` | `identifier`, `fullchain`, `privkey` | Installs delivered SSL/TLS certificates onto the target device management interface. |

---

<!-- INSTALLERS:START -->
## Installation

Installers are idempotent — re-running updates code and preserves credentials.

### Network Devices Spoke — `install_nw.sh`

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/nw/main/install_nw.sh \
  | sudo bash -s -- --hub lm-hub.lrbtechnologies.com
```

| Flag | Purpose |
| :--- | :--- |
| `--hub URL` | Hub WebSocket URL. A bare host is fine — `lm-hub.example.com` becomes `wss://lm-hub.example.com:443`, `host:port` gets a `wss://` prefix, and an explicit `ws://`/`wss://` is left alone. Omit it to auto-discover the hub (DNS `lm-hub.<suffix>`, then mDNS `_lm-hub._tcp.local.`). |
| `--id`, `--name` | Pin the spoke id. Omitted, the id derives from the hostname, so a renamed clone reconnects under its new name. |
| `--secret` | Pre-shared spoke secret. |
| `--hub-secret` | Hub PSK for auto-approval. Without it the spoke lands in *pending approval* in the WebUI. |
| `--all-prereqs` | Accepted and ignored — kept so the hub's install-module call doesn't abort. |

**Environment overrides:** `HUB_URL` (same normalization as `--hub`), `SPOKE_ID`.
<!-- INSTALLERS:END -->
