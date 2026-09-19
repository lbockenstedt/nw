# PXMX Drive Health Monitoring Agent

## Overview

This agent provides SSD drive health monitoring for the Proxmox server, specifically designed to work with HPE storage controllers and smartctl-based wear level detection.

## Features

- **SSD Wear Level Detection**: Queries Wear_Leveling_Count values via smartctl with cciss device interface
- **Health Status Reporting**: Reports drives as healthy/warning/critical based on wear thresholds
- **Historical Trend Tracking**: Stores wear level history for trend analysis
- **HPE CLI Support**: Optional installation of HPE SSA CLI tools if not present

## Commands

### PXMX_DRIVE_HEALTH

Query drive health information for all drives on the Proxmox host.

**Request:**
```json
{
  "type": "PXMX_DRIVE_HEALTH",
  "agent_id": "<agent-id>"
}
```

**Response:**
```json
{
  "status": "SUCCESS",
  "cluster": "<cluster-name>",
  "drives": [
    {
      "physical_index": 0,
      "scsi_path": "scsi0:0:0:0",
      "block_device": "/dev/sda",
      "vendor": "Dell",
      "model": "SAFT2400SSD3",
      "serial": "ABC123",
      "wear_level": 15,
      "health_status": "healthy",
      "success": true
    }
  ],
  "summary": {
    "total_drives": 4,
    "healthy_drives": 3,
    "warning_drives": 1,
    "critical_drives": 0
  },
  "alerts": [],
  "historical_trends": {
    "drives": {},
    "summary": {"total_snapshots": 10}
  },
  "timestamp": 1234567890
}
```

### PXMX_INSTALL_SSACLI

Install HPE SSA CLI tools if not present on the system.

**Request:**
```json
{
  "type": "PXMX_INSTALL_SSACLI",
  "agent_id": "<agent-id>"
}
```

**Response:**
```json
{
  "status": "SUCCESS",
  "installed": true,
  "already_installed": false,
  "cluster": "<cluster-name>"
}
```

## Wear Level Thresholds

- **Healthy**: < 60% wear
- **Warning**: 60-79% wear
- **Critical**: >= 80% wear

## Implementation Details

### Files Modified

1. **pxmx/agent/src/drive_health.py** (NEW)
   - Core drive health monitoring logic
   - `get_scsi_devices()`: Lists SCSI/SATA devices via lsscsi
   - `get_smartctl_info()`: Queries SMART data via smartctl with cciss interface
   - `get_drive_health()`: Main health check function
   - `get_drive_health_for_ui()`: UI-ready output with alerts
   - `save_history()` / `load_history()`: Historical data persistence
   - `get_historical_trends()`: Wear trend analysis

2. **pxmx/agent/src/agent.py**
   - Added `PXMX_DRIVE_HEALTH` command handler (line ~2596)
   - Added `PXMX_INSTALL_SSACLI` command handler (line ~2610)

3. **pxmx/src/proxmox_spoke.py**
   - Added `PXMX_DRIVE_HEALTH` spoke handler (line ~349)
   - Added `PXMX_INSTALL_SSACLI` spoke handler (line ~371)

### Command Flow

```
Hub → PXMX_DRIVE_HEALTH → Spoke → Agent → drive_health.get_drive_health_for_ui()
                                    ↓
                            lsscsi -g → smartctl -d cciss,<index> -a /dev/sda
                                    ↓
                            Parse Wear_Leveling_Count → Report health status
```

### Integration with Hub-Spoke Architecture

The agent follows the existing hub-spoke pattern:
1. Hub sends `PXMX_DRIVE_HEALTH` command to the appropriate spoke
2. Spoke routes to the correct agent via `send_to_agent()`
3. Agent executes the health check via the `drive_health` module
4. Results flow back through the spoke to the hub
5. UI displays drive health, alerts, and historical trends

## Usage

### Manual Query

From the LM UI or via API:
```javascript
// Query drive health
const response = await hub.sendCommand('PXMX_DRIVE_HEALTH', {
  agent_id: 'pxmx-node-1'
});

console.log(response.drives);
console.log(response.summary);
console.log(response.alerts);
```

### Periodic Monitoring

The agent can be configured to run periodic health checks via the managed crontab:

```cron
# Run drive health check every 15 minutes
*/15 * * * * /usr/bin/python3 -c "from drive_health import run_periodic_drive_health_check; import asyncio; asyncio.run(run_periodic_drive_health_check())"
```

## Notes

- Wear level is reported on a 0-100 scale where 0 = new and 100 = end of life
- Lower wear levels are better
- Historical data is stored in `/var/lib/pxmx/drive_health_history.json`
- Only the last 100 snapshots are retained to prevent unbounded growth
