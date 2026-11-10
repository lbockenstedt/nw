# LM ↔ Client-Sim UI Port — Plan & Status

Handoff document for porting the solutions-hpe Client-Sim UI into the Lab Manager
hub's **Simulations** module. Full approved plan: `/Users/lbockenstedt/.claude/plans/agile-finding-sun.md`.
Project memory: `~/.claude/projects/-Users-lbockenstedt-vscode/memory/lm-cs-sim-port.md`.

Last updated: 2026-06-25.

---

## Goal (consolidated architecture)

The LM WebUI **Simulations** module should mimic the solutions-hpe hub UI, fed by **all** the
spoke's API data, with **actions**. The standalone spoke UI (part 1) stays untouched; only its
relay destination changes from `cs/webui-hub` to `lm/core` over the LM signed websocket.
Auth/permissions/multi-tenancy handled by LM; cs auth/superadmin/workspaces dropped. Interactive
VNC/shell/live-log features out of scope this pass. The spoke is tied to a tenant (admin assigns
at approval). Module type **`Client-Sim`**.

```
combined spoke (one process)
  cs/webui-spoke/server.py      ── standalone UI (unchanged) + data collector
  + cs/webui-spoke/lm_relay.py  ── LMControlPlane(module_type="Client-Sim"),
                                    pushes CS_TELEMETRY from _build_relay_telemetry_payload,
                                    handles CS_* commands (dispatch in Phase 4)
                 │  LM signed websocket (ws://hub:8765)
                 ▼
lm/core
  hub.simulations_cache[spoke_id]    ── ingest CS_TELEMETRY
  /sim/api/*  ── read (from cache) + action (request_response → spoke)
  /sim        ── serves ported cs frontend (verbatim) + fetch/WS URL-rewriting shim
  /sim/ws     ── browser broadcast on telemetry (tenant-scoped)
  spoke→tenant binding (new) at approval → module_metadata[spoke_id]["tenant_id"]
  auth adapters /sim/api/init, /sim/api/auth/me map LM session → cs frontend shapes
```

Key reuse: `hub.request_response`, `spoke_telemetry` in-memory pattern → `simulations_cache`,
`_session_user`/`_resolve_tenant`/`access_control_middleware`, `get_spoke_by_type`,
`server._build_relay_telemetry_payload`, `server._apply_relay_command_batch`.

---

## Phase status

| Phase | Status | Notes |
|---|---|---|
| 1 — relay + ingest + tenant binding | ✅ Code-complete, compiles | Files below. Runtime e2e verify pending user env. |
| 2 — read API + /sim/ws + slim store | ✅ Code-complete, compiles + smoke-tested | New `lm/core/src/simulations/` package; see "Phase 2 — DONE" below. |
| 3 — frontend port + shim + auth adapters + nav wiring | ✅ Code-complete, dry-run validated | Frontend copied verbatim; shim + /sim serving + iframe nav wiring done. See "Phase 3 — DONE" below. |
| 4 — action/config endpoints via request_response | ✅ Code-complete, committed lm a7ab7b9 / 0.27.23 + cs 3ff1f87 / 3.1.9 | Reads + config writes wired; smoke-tested. See "Phase 4 — DONE" below. |
| 5 — native views + admin/config adapters + exclude interactive | ✅ Code-complete, committed lm 8c6dfa6 / 0.27.19 | PIVOTED off the iframe; see "Phase 5 — DONE" below. Phase 4 actions still unwired (graceful 404s). |

User decision (2026-06-24): proceed straight into Phase 2+3 without waiting for runtime test.

---

## Phase 1 — DONE (code-complete)

All files `python3 -m py_compile` / `bash -n` clean.

### lm/core (hub)
- **`lm/core/src/main.py`**
  - `__init__`: added `self.simulations_cache: Dict[str, dict] = {}` (after `response_cache`).
  - message loop: added `CS_TELEMETRY` branch after "Handle other messages" / before
    AGENT_RELAY_UP — `self.simulations_cache[spoke_id] = payload["data"]` (approved spokes only;
    unapproved dropped earlier in loop).
  - added `get_client_sim_spoke(self, tenant_id=None)`: returns approved+connected Client-Sim
    spoke for a tenant (binding from `module_metadata[sid]["tenant_id"]`), falls back to first
    available for admins/unassigned. Checks `Client-Sim` then legacy `simulation` type.
- **`lm/core/src/state/manager.py`**
  - added `set_spoke_tenant(module_id, tenant_id)` and `get_spoke_tenant(module_id)` — binding
    stored in `module_metadata` (persisted with system_state).
- **`lm/core/src/api.py`**
  - `/setup/approve_spoke` now accepts optional `tenant_id` → `hub.state.set_spoke_tenant(...)`.
  - `/setup/diagnostics` per-spoke dict now includes `module_type`, `tenant_id`,
    `cs_telemetry_cached`, `cs_telemetry_ts`.
- **`lm/core/src/messaging/control_plane.py`**
  - added backward-compatible `_create_spoke_tasks(websocket) -> list` hook (default `[]`);
  - wired into `_connect_and_serve` (create after heartbeat/log-relay tasks; cancel+gather in
    `finally`). Lets subclasses add per-connection tasks (e.g. telemetry loop) without overriding
    the whole method.

### cs (combined spoke)
- **`cs/webui-spoke/lm_relay.py`** (NEW)
  - `LMControlPlane(BaseControlPlane)`: `module_type = "Client-Sim"`; registers `CSBridge`
    module; `_create_spoke_tasks` returns a telemetry task.
  - `_telemetry_loop(websocket)`: every `telemetry_interval` s, builds payload via
    `server._build_relay_telemetry_payload(spoke_id)` (lazy `import server` to avoid circular
    import), sends signed `CS_TELEMETRY` frame. Only sends once `self.signer` set (post-approval).
  - `CSBridge.handle_command`: `CS_GET_STATUS`/`CS_GET_TELEMETRY` → payload; `CS_GET_VERSION`;
    other `CS_*` → returns not-implemented (Phase 4 fills action map).
  - `build_lm_control_plane()`: factory reading `settings["lm_hub_*"]`; returns None if disabled.
  - `sys.path` fallback to `../../lm/core/src` + `../../lm` so `core.src.*` imports resolve in dev.
- **`cs/webui-spoke/server.py`**
  - added `lm_hub_*` settings keys: `lm_hub_enabled`, `lm_hub_url`, `lm_hub_secret`,
    `lm_spoke_id`, `lm_spoke_secret`, `lm_hub_poll_interval` (defaults from legacy relay_* / env).
  - `lifespan`: when `lm_hub_enabled`, runs `build_lm_control_plane()` as
    `background_tasks["lm_relay"]` and SKIPS legacy `relay_loop` (relay to exactly one hub);
    falls back to `relay_loop` on build failure or when disabled.
- **`cs/installers/install-lxc.sh`**
  - new flags `--lm-hub-url`, `--lm-hub-secret`, `--lm-core-path` (default `/opt/lm`);
  - writes `lm_hub_*` to `settings.json` (merge, preserves existing keys) when `--lm-hub-url` set;
  - systemd unit gets `Environment="PYTHONPATH=$INSTALL_DIR:$LM_CORE_PATH:$LM_CORE_PATH/core/src"`
    so `core.src.*` resolves in the combined spoke.

### Phase 1 verification (run when ready)
1. Run combined spoke (`webui-spoke` with `lm_hub_enabled=on`, `lm_hub_url=ws://hub:8765`)
   against `lm/core`.
2. Standalone spoke UI still loads at the spoke port (part 1 unchanged).
3. Approve the Client-Sim spoke in LM Setup with a `tenant_id`.
4. `curl /setup/diagnostics` shows the spoke with `module_type="Client-Sim"`,
   `tenant_id` set, `cs_telemetry_cached: true`, `cs_telemetry_ts` updating.
5. Logs show `[spoke-event] <id> registered module_type=Client-Sim`.

---

## Phase 2 — DONE (code-complete, smoke-tested)

New package `lm/core/src/simulations/`:
- `__init__.py` — module docstring.
- `broadcaster.py` — `SimulationsBroadcaster`: tenant-scoped `/sim/ws` fan-out on
  `CS_TELEMETRY`; admins see all tenants. Emits `{"type":"telemetry","spoke_id","data"}`;
  event-name reconciliation with `app.js` deferred to Phase 3.
- `store.py` — `SimulationsStore`: JSON-backed (`simulations_store.json` in hub data dir)
  per-tenant cs config (sim_conf/user_conf overrides, client_sim_overrides, usb_vidpids,
  processing_mode, aruba/notifications/github, backup, monitored_items, site_mappings).
  Getters return defaults; setters ready for Phase 4 actions.
- `service.py` — pure read shapers of `hub.simulations_cache[spoke_id]` into the cs webui-hub
  contract: `get_dashboard/clients/simulations/proxmox/central/spoke_config/api_server`,
  `serialize_spoke`, plus `_checks_summary` (uses exact PASS/FAIL_STATUSES from aggregate.py),
  `_is_online` (300s), `_hardware_type`, `_client_has_usb/t3_pci`. Smoke-tested.
- `routes.py` — `register_simulations_routes(app, hub, session_user, resolve_tenant, is_admin,
  check_tenant_access, sessions)` defines `@app.*` closures under `/sim/api` + `@app.websocket
  /sim/ws`, reusing lm/core's closure-route + auth-helper convention.

Endpoints: `/sim/api/{init,health,auth/providers,auth/me}`,
`/sim/api/aggregate/{dashboard,clients,simulations,proxmox,central,central-status,api-server,qa/system-health}`,
`/sim/api/{tenant}/spokes[/{id}[/config|/t3/devices|/t3/mac-profile]]`,
`/sim/api/{tenant}/config/{simulation-conf,sim-conf-override,user-conf-override,user-overrides-conf}`,
`/sim/api/{tenant}/{clients/sim-overrides,usb-vidpids,usb-config,settings,settings/processing-mode,processing-summary}`,
`/sim/api/{tenant}/aggregate/{monitored-items,fleet-reclone-status,usb-provisioning-status}`,
`/sim/api/{tenant}/qa/provisioning-check`, `/sim/api/{sites,spokes/diag,checks,oui-pool}`,
`/sim/api/backup/{config,status,templates}`, WS `/sim/ws`.

Wiring:
- `main.py`: imports `SimulationsBroadcaster`/`SimulationsStore`; Hub `__init__` creates
  `self.simulations_broadcaster` + `self.simulations_store`; `CS_TELEMETRY` ingest now calls
  `await self.simulations_broadcaster.broadcast(spoke_id, cs_data, get_spoke_tenant(spoke_id))`.
- `api.py`: imports `register_simulations_routes`; middleware `_GATED_PREFIXES` adds `/sim/api/`,
  `_PUBLIC` adds `/sim/api/init` + `/sim/api/health`; `create_app` calls
  `register_simulations_routes(app, hub, _session_user, _resolve_tenant, _is_admin,
  _check_tenant_access, _sessions)` after the auth helpers are defined.

Auth: each handler resolves tenant via `_resolve_tenant` (+ `?tenant_id=`/`?tenant=`), enforces
`_check_tenant_access` for path/query tenants, resolves the spoke via
`hub.get_client_sim_spoke(tenant_id)`. `/sim/ws` auths via the `lm_session` cookie against
`_sessions` (HTTP middleware doesn't gate WS). Tenant isolation: non-admins only see their
tenant's spoke; admins see all.

Verified: `python3 -m py_compile` clean across all files; `import api` succeeds; service shapers
produce correct shapes against a fake hub (dashboard counts, client rows, sim rows, proxmox
enriched prov_status, central, store override round-trip).

Phase 2 runtime verify (when ready): login to LM → `curl -b lm_session /sim/api/aggregate/dashboard`
returns shaped data; `/sim/ws` pushes telemetry on spoke update.

---

## Phase 3 — DONE (code-complete, dry-run validated)

Ported the cs webui-hub frontend into LM, served verbatim under `/sim` with a URL-rewriting
shim; the LM WebUI Simulations nav loads it in an iframe.

**Files:**
- `lm/WebUI/simulations/templates/index.html` — verbatim copy of `cs/webui-hub/frontend/templates/index.html`.
- `lm/WebUI/simulations/static/app.js` (781 KB, byte-identical) + `style.css` — verbatim. **Never
  edit app.js**; future cs updates drop in unchanged.
- `lm/WebUI/simulations/static/sim_shim.js` — NEW shim, injected before app.js. Wraps `window.fetch`
  (rewrites same-origin `/api/`→`/sim/api/`, `/static/`→`/sim/static/`, `/shared/`→`/sim/shared/`;
  leaves cross-origin like the jsdelivr noVNC import) and `window.WebSocket` (rewrites
  `ws(s)://host/ws[?…]`→`/sim/ws`; leaves `/ws/console/*` VNC and `/api/*` shell WS untouched —
  excluded features). Handles string, full-origin (`${location.origin}/api/…`), and `Request` inputs.
- `lm/core/src/api.py` — added `HTMLResponse` import; before the catch-all `serve_ui`:
  `app.mount("/sim/static", StaticFiles(...))`; `GET /sim` + `GET /sim/{full_path:path}` (SPA
  fallback) serve `templates/index.html` with substitutions mirroring `cs/webui-hub/app/main.py:218-236`:
  `window.WEBUI_MODE='{{WEBUI_MODE}}'`→`'hub'`, `href="/static/style.css"`→`/sim/static/style.css?v=<lmver>`,
  inject `<script src="/sim/static/sim_shim.js"></script>` before the deferred `app.js` (cache-busted).
  Registered before `serve_ui` so `/sim/*` wins; `/sim/api/*` + `/sim/ws` registered even earlier
  via `register_simulations_routes`.
- `lm/WebUI/main.js` — `_viewTemplate` `case 'cs'` now returns an `<iframe src="/sim">` filling the
  viewport (`height:calc(100vh - 120px)`) instead of the thin native status card; `renderTopNav`
  clears the top-nav for `cs` (the iframe carries its own topbar); `initView` has no `cs` case
  (no-op — the iframe loads its own data). The `lm_session` cookie (SameSite=Lax, httponly) is sent
  same-origin in the iframe, so `/sim/api/auth/me` resolves the LM session.
- `lm/WebUI/index.html` — bumped `main.js?v=0.2544`→`0.2545` cache-buster.

**Auth adapters** — shipped 2026-06-24 (the Phase 2 stub only registered read/aggregate routes;
the adapters were missing, which is why the cs login overlay appeared). Implemented in
`lm/core/src/simulations/routes.py` `register_simulations_routes`, reusing the passed auth helpers:
- `GET /sim/api/init` (public) → `{mode:"hub", app_version, installer_version}`.
- `GET /sim/api/health` (public) → `{status:"ok", version, branch, sha}`.
- `GET /sim/api/auth/providers` → `{providers:[...], active:["password"]}` (cosmetic; LM owns auth).
- `GET /sim/api/auth/me` (cookie-gated) → cs `UserResponse` `{id, username, is_superadmin,
  tenant_roles:[{tenant_id, role, tenant_name}]}`. `id`/`username` = LM `user_id`;
  `is_superadmin` = `is_admin_fn(sess)`; `tenant_roles` = the session user's tenants (admins get
  all tenants from `hub.state.tenant_state["tenants"]`), `tenant_name` from `hub.state.get_tenant`.
  No session → 401 `{"authenticated": False}` (frontend `apiFetch` 401 → `logout`).
- `GET /sim/api/superadmin/tenants` (admin-only) → `[{id, name, ...}]` from
  `hub.state.tenant_state["tenants"]`. Feeds the superadmin dashboard tenant rows.
- `GET /sim/api/superadmin/users` (admin-only) → `[{id, tenant_roles:[{tenant_id,...}]}]` from
  `hub.state.system_state["users"]` for `buildTenantUserCounts`.
Admin guard (`_require_admin`) returns 403 for non-admins; middleware still cookie-gates all
`/sim/api/*`. No `api.py`/middleware changes needed — `/sim/api/init` + `/sim/api/health` were
already in `_PUBLIC`, `/sim/api/` already in `_GATED_PREFIXES`.

**hub_token gate** — the cs frontend bails to its login overlay before ever calling `/api/auth/me`
because `authToken = sessionStorage.getItem("hub_token")` (app.js:7603) is null on a fresh iframe
and `loadUserContext()` early-returns (app.js:9896). `sim_shim.js` now seeds a vestigial
`sessionStorage["hub_token"]="lm-session"` at parse time (shim is non-defer, injected before the
deferred app.js) so the frontend clears its login gate and reaches `/sim/api/auth/me`. The Bearer
value is ignored by the LM handlers (cookie auth); if the LM session is invalid, `/sim/api/auth/me`
→ 401 → cs `logout()` clears it — self-correcting.

The cs login screen is now unreachable from the LM WebUI: users auth via LM first, then the
Simulations nav loads `/sim` and the dashboard renders directly. Known edge case: if the LM
session expires mid-session, `/sim/api/auth/me` → 401 → the cs `logout()` briefly shows the cs
login overlay (cannot suppress without editing app.js); re-login to LM clears it.

**Verified:** `import api` clean; `register_simulations_routes` exercised in isolation with
TestClient — all 6 routes register; `init`/`health`/`providers` return JSON; `auth/me` returns 401
with no session and the correct `UserResponse` shape for admin (all tenants, `is_superadmin:true`)
and tenant user (own tenant, `is_superadmin:false`); `superadmin/tenants`/`users` return 403 for
non-admins and the expected list shapes for admins.

**Phase 3 runtime verify (when ready):** log into LM WebUI → click **Simulations** → the
solutions-hpe dashboard renders in the iframe with live data from the assigned tenant's spoke,
updating via `/sim/ws`; no 404s except the excluded VNC/shell buttons.

**Excluded this pass (Phase 5):** VNC console (`/ws/console/*`), browser shell (`/api/*` WS),
remote-logs, command-trace — shim leaves their URLs untouched; their UI buttons get hidden in Phase 5.

---

## Phase 2 contract (reference — gathered, do not re-derive)
lm/core has NO APIRouter convention — all routes are `@app.*` closures inside `create_app`
sharing closure access to `_session_user`, `_resolve_tenant`, `_is_admin`, `_sessions`, `hub`.
To match surrounding style AND keep auth-helper access, do **not** use APIRouter. Instead:
- `lm/core/src/simulations/` package with pure-logic modules: `service.py` (read functions
  taking `hub`/`spoke_id`), `broadcaster.py` (`SimulationsBroadcaster`), `store.py`
  (cs-specific config persistence).
- A `register_simulations_routes(app, hub, session_user_fn, resolve_tenant_fn, is_admin_fn)`
  function (in `simulations/routes.py`) that defines `@app.*` closures under `/sim/api` and
  `/sim/ws`, capturing the auth helpers. Called from `create_app` in `api.py`.

### /sim/ws broadcast
- `hub.simulations_broadcaster = SimulationsBroadcaster()` in Hub `__init__`.
- main.py CS_TELEMETRY ingest calls `await hub.simulations_broadcaster.broadcast(spoke_id, cs_data)`.
- broadcaster fans out to subscribed browsers whose tenant matches the spoke's tenant
  (`hub.state.get_spoke_tenant(spoke_id)`); admins get all. Browser subscribes via `/sim/ws`
  with their LM session → tenant.
- Tenant scoping: every read route resolves `tenant_id = _resolve_tenant(...)` →
  `spoke_id = hub.get_client_sim_spoke(tenant_id)` → reads `hub.simulations_cache[spoke_id]`.
  Non-admins get 403 if spoke's tenant not in their tenants.

### Gating
- Add `/sim` and `/sim/api` prefixes to `access_control_middleware` (cookie auth via `lm_session`).
- Serve `/sim` (index.html), mount `/sim/static` + `/sim/shared` via `StaticFiles` BEFORE the
  existing catch-all `serve_ui`. (Phase 3 wiring, but reserve the prefixes now.)

---

## Phase 2 contract — gathered (do not re-derive)

`hub.simulations_cache[spoke_id]` holds EXACTLY the output of
`cs/webui-spoke/server.py::_build_relay_telemetry_payload(spoke_id)` (server.py:6011-6161).
Top-level keys:

```
spoke_id, spoke_name, hostname, clients[], timestamp, sim_conf_content,
user_overrides_conf_content, hub_isolated, hub_last_checkin, hub_rtt_ms,
hub_processing_ms, hub_loop_lag_ms, telemetry_build_ms, ws_reconnect_count,
ws_last_reconnect_at, ws_last_error, sim_conf_read_error, reseed_in_progress,
proxmox{connected,last_seen,node,vm_count,running_count,vms[],usb_state,present_usb,
  unknown_usb,usb_count,agent_version,pve_version,cpu_1h_avg,mem_1h_avg,provision_halt,
  prov_run,cpu_est_avg,mem_est_avg,resource_samples_started,resource_sample_count,
  template_lock,reseed_in_progress,hw_faults,hw_last_reset,t3_pci_devices[],t3_pci_count,
  blacklisted_drivers[],usb_quarantine[],orphan_vms[]},
proxmox_vms[], usb_devices[], api_server{health{status,version,clients,repo_synced,
  repo_error,installer_version}, services{}, task_names[]},
central{status,wireless_clients,hardware_alerts,client_count_status,token_valid,
  token_state,site_mappings,monitored_checks,hardware_checks,central_alerts,
  central_insights,central_devices_by_site,central_clients_by_site,central_clients},
reclone_state{}
```

`clients[]` element (`serialize_client`, server.py:4275-4301):
```
hostname, has_usb, simulation_id, platform, hw_type, iteration, connected_ssid,
gateway_reachable, active_simulations[], config{}, effective_config{}, overrides{},
last_seen(iso-Z), online, recent_errors[], error_count
```

### Dashboard endpoints to reproduce (cs webui-hub shapes — app.js reads these by name)
Build a synthetic Spoke view per cached entry: `id=spoke_id, spoke_name=cs_data["spoke_name"],
hostname=cs_data["hostname"], last_seen=<your last-telemetry ts>, telemetry=cs_data,
config=cs_data.get("config") or {}, assigned_sites=[], status="approved"`.

- `GET /sim/api/aggregate/dashboard` → `{tenant_id, client_count, hardware_breakdown,
  checks_summary{pass,fail,warning}, spokes_online, spokes_total}`.
  `hardware_breakdown=Counter(c.hw_type or c.platform or "Unknown")`; `checks_summary` walks
  `central.status`/`central.hardware_alerts`/`central.client_count_status`.
- `GET /sim/api/aggregate/clients` → `{tenant_id, clients[]}` each client row = client dict +
  `{tenant_id, spoke_id, spoke_name, spoke_hostname, spoke_label, has_usb, has_t3_pci,
  t3_pci_count, t3_pci_devices}` (t3 from `proxmox.t3_pci_devices`/`t3_pci_count`).
- `GET /sim/api/aggregate/simulations` → `{tenant_id, simulations[]}` per (spoke, sim_name)
  from `clients[i].active_simulations` (fallback `simulation_id`); idle → row `simulation_name="—"`.
- `GET /sim/api/aggregate/proxmox` → `{tenant_id, hosts[]}` each with full proxmox dict +
  `proxmox_vms` (enriched prov_status), `usb_devices`, `reclone_state`, `api_server`,
  `hub_rtt_ms`/`hub_processing_ms`/`hub_loop_lag_ms`/`telemetry_build_ms`/`ws_reconnect_count`/
  `ws_last_error`/`sim_conf_read_error`, `spoke_config{...}`, `pending_command_count`.
- `GET /sim/api/aggregate/central`, `/central-status` → from `telemetry["central"]`.
- `GET /sim/api/{tenant}/spokes/{id}/config` → `{config, telemetry}` (telemetry = WHOLE cs_data
  verbatim — frontend renders raw keys; invariant: every top-level key above must be present).
- `GET /sim/api/{tenant}/config/simulation-conf`, `/user-overrides-conf`,
  `/sim-conf-override`, `/user-conf-override` → content from `sim_conf_content` /
  `user_overrides_conf_content` (+ tenant overrides store, Phase 4).
- `GET /sim/api/{tenant}/clients/sim-overrides`, `/usb-vidpids`, `/usb-config` → from store.
- `GET /sim/api/{tenant}/spokes` / `/spokes/{id}` → `_serialize_spoke`-like: `{id,tenant_id,
  hostname,label,spoke_name,assigned_sites,status,seed_config,processing_mode,
  config_version,applied_config_version,last_config_applied_at,last_seen,telemetry,
  created_at,config}`.
- `GET /sim/api/spokes/diag`, `/config`, `/config-diag` → from store + cache.
- `GET /sim/api/{tenant}/settings`, `/settings/processing-mode`, `/processing-summary` →
  from store (aruba/notifications/github serialized; processing_mode model_dump).
- `GET /sim/api/backup/config`, `/backup/status`, `/backup/status/{id}`, `/backup/templates`.
- `GET /sim/api/sites` (no-auth list), `/{tenant}/spokes` list.
- `GET /sim/api/{tenant}/spokes/{id}/t3/mac-profile`, `/t3/devices`, `/api/oui-pool`.
- `GET /sim/api/checks` → `[]` placeholder.
- `GET /sim/api/{tenant}/aggregate/fleet-reclone-status`, `/usb-provisioning-status`,
  `/monitored-items`, `/qa/provisioning-check`, `/qa/teardown-status`,
  `/api/aggregate/qa/system-health`, `/api/aggregate/api-server`.

### Auth adapter shapes (Phase 3 but reserve)
- `GET /sim/api/init` (unauth) → `{mode:"hub", app_version, installer_version}`.
- `GET /sim/api/health` → `{status:"ok", version, branch, sha}`.
- `GET /sim/api/auth/me` → `{id, username, is_superadmin, tenant_roles:[{tenant_id,role,tenant_name}]}`
  built from LM session (LM user → these four keys). **No `/api/version` route exists** in cs.
- `GET /sim/api/auth/providers` → `{providers:[...], active:[...]}`.

### Excluded (do NOT port; hide UI in Phase 5)
VNC console, browser shell, remote-logs, command-trace, provision-proxmox-token
(depends on `_vnc_queues`/`_shell_queues`/`_log_fetch_queues`/`_command_trace_queues`/`_provision_queues`).

### app.js caveat (Phase 3)
Unified hub+spoke bundle. Some `/api/*` paths are spoke-mode-only (`/api/proxmox/*` direct,
`/api/services/*`, `/api/repo/*`, `/api/logs/*`, `/api/relay/*`). In hub mode these map to
`/sim/api` reads of relayed data or are no-ops — confirm each against the hub router list above
before treating a missing route as a gap.

---

## Phase 3 — frontend port (NOT STARTED)
- Copy `cs/webui-hub/frontend/{templates/index.html, static/app.js, static/style.css}` + `shared/`
  into `lm/WebUI/simulations/`. Serve **verbatim** — do NOT edit app.js (781 KB).
- URL-rewriting shim (bootstrap `<script>` before app.js): wrap `window.fetch` + `WebSocket`
  ctor; rewrite same-origin `/api`→`/sim/api`, `/static/`→`/sim/static/`, `/shared/`→`/sim/shared`,
  `/ws`→`/sim/ws`; leave `ws://…/ws/console`, `/api/…/shell`, and cross-origin (jsdelivr noVNC)
  untouched. Extend the shim (never app.js) if a URL form slips through.
- lm/core serves `/sim` (index.html with `{{WEBUI_MODE}}`→`'hub'` + cache-bust), mounts
  `/sim/static` + `/sim/shared` (`StaticFiles`), registered BEFORE catch-all `serve_ui`.
- Auth adapters under `/sim/api` (shapes above). Add `/sim`+`/sim/api` to middleware gating.
- Wire LM WebUI Simulations nav to load `/sim` in its pane (`WebUI/main.js`
  `currentView === 'Simulations'` block ~line 1477; nav already appears when a Client-Sim spoke
  connects). Bump `lm/WebUI/index.html` cache-buster.

## Phase 4 — DONE (code-complete, committed lm a7ab7b9 / 0.27.23 + cs 3ff1f87 / 3.1.9, 2026-06-25)

Wired the ~14 `/sim/api` endpoints `sim-views.js` calls. Reads derive from
`hub.simulations_cache[spoke_id]`; config writes persist to the `SimulationsStore` AND
best-effort push `CS_CONFIG_UPDATE` to the tenant's spoke via `hub.request_response`.

**lm/core/src/simulations/**
- `service.py` — real shapers over the CS_TELEMETRY cache (were empty `[]` stubs):
  `get_clients/proxmox/central/central-status/api-server/dashboard/simulations/spoke-config`.
  Per-tenant via `hub.state.get_spoke_tenant(sid) == tenant_id`; `spoke_online` from
  `hub.active_connections`. Degrade to empty lists when the tenant has no cached spokes.
- `store.py` — JSON-backed (`simulations_store.json` in the hub data dir, atomic save) with
  buckets: `hub_config` (+`hub_config_enabled`), `central_config`, `onboarding_psks`,
  `processing_modes`, `notifications`, `sim_conf_content` (+ legacy sim/user overrides).
  Constructor now takes `data_dir` (matches `main.py:135` `SimulationsStore(self.state.data_dir)`).
- `routes.py` — new endpoints: reads `aggregate/central`, `aggregate/central-status`,
  `aggregate/api-server`; config `tenant/{t}/hub-config` GET/PUT, `{t}/config/simulation-conf`
  PUT, `{t}/settings` GET, `{t}/settings/notifications` POST, `hub/tenants/{t}/processing-modes`
  PATCH, `tenant/{t}/onboarding-psk` GET/POST/DELETE, `aggregate/config-push` POST,
  `aggregate/central` POST (central save). `_push_config(tenant_id, payload)` does the
  `request_response` push; handlers return `{saved, pushed_to_spokes}` (0 when no spoke
  connected — the write still persists). `sim-conf` GET reads `sim_conf_content` from telemetry.
  Literal-first-segment routes registered before `{tenant}/...` param routes so they don't shadow.
  Routes use the shared `hub.simulations_store` (was `SimulationsStore(hub)` → fresh empty store
  per request).

**cs/webui-spoke/lm_relay.py** — `CSBridge.handle_command` routes `CS_CONFIG_UPDATE` →
`server._apply_hub_config(data)`. One dispatch covers every config bucket (central_api/
central_config/notifications/sim_conf_override/user_conf_override/relay_onboarding_psk +
HUB_CONFIG_OWNED_KEYS). Returns the ack dict so the hub's `request_response` resolves.

**Latent bugs fixed (shipped Phase 2/3 code):**
- `get_tenant_id`/`get_is_admin` now annotate `request: Request` — FastAPI was treating the
  unannotated `request` param as a required query string, so every aggregate route returned 422.
- `/spokes/diag` no longer calls the non-existent `store.get_spokes_diag` (AttributeError);
  it now derives live diag from the cache.

**Verified:** `py_compile` + `import api` clean; a TestClient smoke harness against
`register_simulations_routes` (fake hub + cache, bypassing `create_app`'s auth middleware)
exercises every new endpoint — reads shape correctly, config writes round-trip the store +
push `CS_CONFIG_UPDATE` with the expected payloads, PSK gen/revoke + processing-modes +
notifications map and push, `auth/me` returns the UserResponse shape, store persists across
re-instantiation. **Not yet runtime-verified against a live hub+spoke.**

**Still out of scope / not wired** (sim-views shows graceful empty states, no white-screen):
per-target actions the cs webui-hub had — fleet reclone trigger, approve/revoke proxmox
agent, client control overrides, t3 push, update-agent, repo-sync, backup trigger. These
would map to additional `CS_*` commands through `_apply_relay_command_batch`; deferred.

## Phase 5 — DONE (code-complete, committed lm 8c6dfa6 / 0.27.19, 2026-06-25)

**PIVOT: abandoned the Phase 3 `<iframe src="/sim">` integration in favor of native LM views.**
The iframe approach (serve the verbatim cs frontend at `/sim` with a fetch/WS shim) worked but
duplicated the cs app.js/auth/topbar inside LM. Phase 5 replaces it with a set of native LM
views, the same pattern opnsense/ldap/netbox use.

**Files:**
- `lm/WebUI/sim-views.js` (NEW, 826 lines) — IIFE rendering the 7 Simulations sub-nav tabs
  (Simulations / Clients / Central / VM Server / API Server / Config / Setup) inline into
  `#cs-content`. Calls `/sim/api/*` directly with the same-origin `lm_session` cookie; reuses
  the hub `currentTenant` global for tenant scoping (like netbox/pxmx). Live updates over
  `/sim/ws` (telemetry/aruba_update → debounced `loadCSData` refresh); socket torn down on
  leaving the module. All symbols CS-prefixed; `loadCSData`/`connectCSWebSocket`/
  `disconnectCSWebSocket` exposed on `window` for main.js.
- `lm/WebUI/main.js` — `case 'cs'` in `_viewTemplate` returns native markup (`#cs-content` +
  `#cs-add-toolbar` + Refresh button) instead of the iframe; `renderTopNav` no longer blanks
  the cs sub-nav (it gets a normal sub-tab strip); `initView`/`setSubView` dispatch to
  `loadCSData`; `setView` tears down the CS socket when leaving. `VIEW_SUBMENUS['cs']` = the
  7 tabs.
- `lm/WebUI/index.html` — loads `sim-views.js?v=0.25` after main.js.
- `lm/core/src/api.py` — default `appearance.logo_url` `assets/logo.png`→`hpe-svg` (fresh
  installs without a custom logo render the HPE mark).

**UI-first:** only reads backed by `SimulationsService` are live — `aggregate/clients`,
`aggregate/proxmox`, `{tenant}/config/simulation-conf`. The other tabs call not-yet-wired
endpoints (`aggregate/central`, `aggregate/central-status`, `aggregate/api-server`,
`tenant/{t}/hub-config`, `aggregate/config-push`, `{t}/settings`, `{t}/onboarding-psk`,
`hub/tenants/{t}/processing-modes`, `{t}/settings/notifications`, PUT sim-conf, etc.) and
degrade gracefully to an empty/error state ("endpoint not wired in the backend yet"). No tab
white-screens. **Wiring these = the remaining Phase 4 backend work.**

**Exclusions honored:** VNC console + spoke shell buttons render disabled ("Available in
Phase 5"); the excluded interactive features (remote-logs, command-trace, provision-token)
are not surfaced. cs login overlay removed (LM owns auth).

**Superseded / inert dead code (left in place, unlinked from the UI):** the `/sim` route +
SPA fallback, `sim_shim.js`, and the verbatim ported `WebUI/simulations/{templates/index.html,
static/app.js, static/style.css}`. `app.js` got a one-line no-op for the removed login
overlay. Pending a later cleanup: delete or re-purpose. The original "never edit app.js"
constraint no longer applies to this dead copy.

**Verified:** `python3 -m py_compile` clean across `core/src/api.py` + `simulations/*.py`;
`import api` clean; `node --check` clean on `sim-views.js` + `main.js`; `VIEW_SUBMENUS['cs']`
matches the 7 tabs; no live `/sim` iframe references remain in the WebUI.

**Phase 5 runtime verify (when ready):** log into LM WebUI → Simulations → the 7 tabs render
inline; Clients/VM Server/Config(sim-conf) show live spoke data; other tabs show graceful
"not wired yet" states; `/sim/ws` pushes refresh the open tab on telemetry.

---

## Constraints to preserve
- Cannot leave the current directory; can read any subdir.
- `svr_pxmx` not part of the migration.
- SKIP `.deploy-secrets-prod.env.zip` (prod secrets) — never copy into repos.
- cs `.gitignore`: `.env`, `*.key`, `*.pem`, `*.p12`, `*.pfx`, `keys.json`, `hub_secret.json`,
  `secrets.json` — never commit.
- `lm/core/data/system.json` and `core/data/tenants.json` are runtime state — do NOT commit.
- lm: "commit and push" + "commit directly to main" authorized. Rebase over merge-bot `[skip ci]`
  version bumps; stash unstaged runtime state before rebasing.

## Resume point
Begin Phase 2: create `lm/core/src/simulations/{__init__.py,service.py,broadcaster.py,store.py,
routes.py}`, add `hub.simulations_broadcaster` + wire broadcast into CS_TELEMETRY ingest, add
`register_simulations_routes(...)` call + `/sim`+`/sim/api` gating in `api.py create_app`.
Implement read endpoints per the contract above. Then Phase 3 frontend port.