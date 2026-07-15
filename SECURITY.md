# SECURITY — Cross-Repo Audit Log

**Date:** 2026-06-24
**Scope:** all 12 git repos under `/Users/lbockenstedt/vscode/` — `bugfixer`, `cppm`, `cs`, `dhcp`, `dns`, `kvm`, `ldap`, `lm`, `netbox`, `opnsense`, `pxmx`, `qa` (~58k lines of Python across 131 files).
**Method:** deterministic sweep (`py_compile` every file + `grep` for `shell=True` / `eval` / `exec` / hardcoded secrets / bare `except`) followed by 5 parallel read-only review agents grouped by repo size (cs, lm, bugfixer, medium-group, small-group). All findings line-verified by the agents.
**Status:** scan only — **no code was modified** for this audit (the `bugfixer` Resolved-button fix shipped separately as commit `fa2b657`). This file is a record for later triage.

---

## Headline numbers

- **Syntax errors: 0** (131 Python files compiled clean)
- `shell=True`: 0 · `eval`/`exec`: 0 · hardcoded secrets in source: 0
- Bare `except:`: 12 (all in bugfixer; assessed individually — most are acceptable best-effort cleanup)
- **Critical: 11 · High: ~30** across the fleet

---

## Critical — remote code execution / full compromise (fix first)

| # | Repo | Location | Issue |
|---|------|----------|-------|
| C1 | **bugfixer** | `main.py:7568` + all routes | **No authentication on any endpoint** while the app runs **as root** on `0.0.0.0:8000`. Anyone reachable can `/restart`, overwrite the GitHub PAT + all LLM keys, close/resolve issues, run sandbox shell commands, clear history. *Enables C2–C5 below.* Fix: mandatory bearer token from a `0600` root-owned secret; refuse to start if unset; bind `127.0.0.1` by default. |
| C2 | **bugfixer** | `main.py:5760` + `templates/index.html:476,555` | `/settings` renders the **GitHub token and all LLM API keys into the HTML** response. Unauthenticated full secret theft (combined with C1). Fix: send only masked placeholders / "configured" flags (mirror `/api/llm/config` at 6056). |
| C3 | **bugfixer** | `main.py:5939` + `run_sandboxed_command:1518` | `repo_tests` accepts an **arbitrary shell string** run as root in a Docker container with the cloned repo mounted RW + default capabilities + outbound network → RCE that can read `.env`/secrets. Fix: restrict to a fixed enum (`pytest`/`npm test`/`go test`/`make test`) or validated argv; harden container (`--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--network=none`, non-root user); auth on `/save_settings`. |
| C4 | **lm** | `core/.../control_plane.py:724` (+ `dhcp`, `dns`, `opnsense:407-411`) | **`sed` script-injection RCE** in `SPOKE_SET_HOSTNAME`: hub-supplied `new_hostname` is interpolated into a sed script; a `/`-bearing payload closes the replacement early and GNU sed's `e` command executes its argument as a shell command — RCE as root (via sudo). Fix: validate `new_hostname` against `^[A-Za-z0-9._-]{1,63}$` (RFC 1123) before any subprocess call; prefer a templated `/etc/hosts` write. |
| C5 | **lm** | `core/.../control_plane.py:800-806` (+ `opnsense:446`) | **Signature verification silently disables itself** when no spoke secret is set (`--hub-secret` defaults to empty in dhcp/dns mains). With it off, every hub command — incl. C4's hostname RCE and `SPOKE_UPDATE` git re-point — is accepted unsigned. Fix: empty spoke/hub secret = hard startup error; require signatures for all non-handshake commands. |
| C6 | **lm** | `main.py:2094` | Hub WebSocket server has **no TLS** (`websockets.serve(..., ssl=None)`); root/session HMAC secrets traverse the network in cleartext. A MITM recovers spoke/hub secrets and impersonates the hub. Fix: terminate TLS at `websockets.serve(..., ssl=ssl_context)` or a reverse proxy; refuse to send secrets over unencrypted transport. |
| C7 | **netbox** | `netbox_spoke.py:103-139` | **Unauthenticated privileged `handle_command`**: `UPDATE_CONFIG` rewrites the NetBox API token/URL (111-121), and `SPOKE_UPDATE` runs `git pull` then `sudo systemctl restart lm-netbox` (130-135) — zero auth. Fix: HMAC/admin gate both; never accept a token from an unauthenticated payload. |
| C8 | **cppm** | `src/client.py:43`, `src/spoke.py:96` + `spoke.py:81,127-138` | **Global `verify=False`** (MITM on all CPPM traffic incl. OAuth tokens) **+ SSRF** via `UPDATE_CONFIG` re-pointing `self.host` then `PROBE_API` issuing arbitrary `method`/`path` requests through `self.client._request`. Fix: default verify on; allowlist `host`; restrict `PROBE_API` path to known CPPM endpoint prefixes. |
| C9 | **pxmx** | `src/control_plane.py:129-130,154-157` + `agent/src/agent.py:459,491` | **Plaintext WebSocket** (`ws://`, `0.0.0.0`, no TLS) carries the shared `AGENT_SECRET` and PVE root creds — sniffable by any network observer. Single shared secret means one compromised node impersonates the fleet. Fix: TLS on the listener (`ssl=…`), require `wss://`; per-agent secret with `hmac.compare_digest`. |
| C10 | **ldap** | `src/ldap_spoke.py:21,39` | **Hardcoded fallback bind password `"admin"`** — `admin_pw=self.config.get("LDAP_ADMIN_PW", "admin")`. If `LDAP_ADMIN_PW` is unset (misconfigured `.env` after update), the spoke silently binds to the directory as `admin/admin`. Fix: fail closed — refuse to construct the manager if the password is unset/empty. |
| C11 | **cs** | `routers/aggregate.py:860` (7 call sites: 826, 892, 906, 932, 946, 1020, 1220) | Calls **`store.get_spokes(...)` which is undefined** (only `list_spokes` exists) → `AttributeError` at runtime. Config-version overrides **never propagate to spokes** across 7 endpoints; the feature path is dead. Fix: rename call site to `store.list_spokes(tenant_id)`. |

---

## High — by theme

### Auth / access control (recurring pattern)
- **lm** `api.py:254-287` + write cluster (firewall 899-965, agent-command 3624-3641, proxmox 1664-1721, netbox 2626-2736, ldap 2295-2448): `/api/*` writes have **no admin check, no tenant check** — any authenticated user can add/delete firewall rules, send arbitrary commands to any connected agent (`command`/`data` user-supplied), revoke/rename Proxmox agents, edit NetBox devices/prefixes/IPs, reset LDAP passwords across tenants. Fix: gate destructive writes with admin/per-tenant ownership check like `/setup/*`.
- **lm** `api.py:4089-4099`: **path traversal** in `serve_ui` catch-all (`os.path.join(ui_path, full_path)` with `{full_path:path}`, no realpath containment, outside auth-gated prefixes) → unauthenticated arbitrary file read. Fix: `os.path.realpath` containment check.
- **lm** `api.py:3595-3603`: `/api/generic/provision` leaks the hub shared secret to any connected agent; auth-only, no admin check. Fix: admin-only; don't re-send `hub_secret` to provisioned agents.
- **lm** `simulations/routes.py:23,26,182`: `/sim/api/*` auth deps broken at runtime (awaiting a sync `resolve_tenant_fn` → TypeError → 500 on every call); `/sim/ws` `accept()` runs before any session check and HTTP middleware doesn't run on WS. Fix: drop the `await`; close 1008 before accept if no session.
- **lm** `main.py:666-670` + `broadcaster.py:10`: broadcaster arity mismatch (`broadcast(self, spoke_id, data)` called with 3 positional args) → `TypeError` on every `CS_TELEMETRY` frame, swallowed at DEBUG → **`/sim/ws` live telemetry is completely broken and silent**. Fix: `async def broadcast(self, spoke_id, data, tenant_id=None)`.
- **lm** `main.py:502`: unauthenticated/pending spoke inserted into `active_connections` unconditionally → attacker claims any `spoke_id` slot and shadows/DoSs the legitimate connection.
- **cs** `webui-hub/app/auth_providers.py:103` + `webui-spoke/server.py:3304`: **LDAP injection** in login (username interpolated into search filter via `str.format`). Fix: `ldap3.utils.conv.escape_filter_chars(username)`.
- **cs** `lm-spoke/src/control_plane.py:55-78`: standalone control plane binds `0.0.0.0:8000` with no auth on `/config` (overwrites engine), `/simulate/trigger`, `/status`. Fix: shared-secret token or bind `127.0.0.1`.
- **cppm** `src/control_plane.py:67` / **opnsense** `src/control_plane.py:42-43` / **qa** `control_plane.py:33` + `main.py:46-47`: hardcoded weak default secrets (`lm-secret`, empty hub-secret, `admin:password`). Fix: required, fail fast if absent.
- **cs** `routers/auth.py:55`, `routers/qa.py:22`, `webui-spoke/server.py:8750`: **no rate limiting** on login / QA-token-exchange (QA token grants 2h tenant-admin on one guess). Fix: per-IP/per-identifier throttle with backoff.
- **bugfixer** `main.py:3437`: GitHub PAT embedded in clone URL (`url.replace("https://", f"https://{token}@")`) leaks into `bugfixer.log` (via `logger.exception` on `GitCommandError`) and HTTP error bodies (via `catch_exceptions_mid:1550` returning `str(e)`), reachable via unauthenticated `/logs:5644`. Fix: use a credential helper / `GIT_ASKPASS` / `github.Auth.Token`; add a log sanitizer; stop echoing `str(e)`.
- **bugfixer** `main.py:5244-5312`: `/api/diagnostics` leaks PID, commit SHAs, `started_at`, `main_mtime`, `last_known_good_commit`, `failed_commits`, `restart_log` to any caller. Fix: auth-gate or strip sensitive fields.
- **bugfixer** `main.py:5853-5865` + `_request_*` (1002/1064/1122/1166): **SSRF** via user-controllable LLM `base_url` (accepted verbatim, redirects followed, `_normalize_lmstudio_url` accepts RFC1918) → root process issues authenticated POSTs to internal targets (`169.254.169.254` etc.); response bodies reachable via `/api/task-details:5037` / `/api/chat/stream:7444`. Fix: allowlist provider hosts; reject private/loopback/link-local unless explicit local-LLM flag; `allow_redirects=False`.
- **ldap** `src/ldap_spoke.py:46-103` + `ldap_manager.py:16-19`: **blocking sync LDAP I/O inside async** (`simple_bind_s`/`search_s`/`add_s`), re-binds on every call, **no timeout** (`OPT_NETWORK_TIMEOUT` unset), never `unbind_s`. Fix: `asyncio.to_thread`, set timeouts, pool/unbind.
- **ldap** `src/ldap_manager.py:2,225`: `ldap.filter` imported but manual filter escaping used (misses null/`/`) → filter-injection / DoS. Fix: `ldap.filter.escape_filter_chars(q)`.
- **ldap** `base_structure.ldif:16`: seed LDIF ships `userPassword: password123`. Fix: hashed/`CHANGEME` placeholder.
- **pxmx** `agent/src/security_utils.py:30-37`: signature verification has **no replay protection** (no timestamp window / nonce). Fix: timestamp window + nonce cache.
- **pxmx** `agent/src/agent.py:117-124`: provisioned secret written to `.env` with **no file-mode restriction** (world-readable under default umask). Fix: `os.chmod 0o600`, store under `/etc/pxmx-agent/` `0o700`.
- **kvm** `src/control_plane.py:69`: agent WebSocket binds `0.0.0.0` with **no TLS** — agent secret/telemetry in cleartext. Fix: bind management iface/localhost, serve over TLS.
- **dns** `src/dns_manager.py:56-60`: no validation of `rtype`/`value` → malformed records corrupt unbound zone data. Fix: allowlist RR types, reject control chars.
- **lm** `core/.../mailbox.py:41-49`: mailbox `acknowledge` deletes a pending message on `correlation_id` with **no sender authorization** — any spoke that learns a `corr_id` can drop another's messages. Fix: verify `ack.sender_id == msg.header.destination_id`.
- **lm** `core/.../control_plane.py:621-637` (+ opnsense 347-370): `SPOKE_UPDATE` trusts hub-supplied `repo_url` to re-point git origin → attacker checks out malicious content (hooks/filter drivers) that runs on next `sudo systemctl restart`. Fix: fixed origin allowlist.
- **lm** `agent/.../agent_spoke.py:31-35`: `curl ... | bash` supply-chain RCE in agent role deploy (unsigned GitHub script). Fix: pin to commit SHA + verify detached signature.
- **lm** `main.py:924,927-930`: shell injection via admin-writable `hub_repo`/`branch` in `_git_update` (`create_subprocess_shell(f"cd … && git remote set-url origin {hub_repo} && git pull … origin {branch}")`). Fix: `create_subprocess_exec` + validate `branch` against `^[A-Za-z0-9._/-]+$`.
- **lm** `encryption.py:33,76`: legacy Fernet key derived from machine-id with hardcoded salt `b'lab-manager-salt-2026'` + MAC-address fallback → decryptable by anyone who knows the host's MAC. Fix: drop legacy path after migration, or per-install random secret.
- **lm** `signer.py:51,59`: secret material logged on HMAC mismatch (expected HMAC + full payload). Fix: log only that a mismatch occurred + short hash.
- **lm** `key_manager.py:63-66,134`: silent plaintext fallback for encrypted secret stores on decrypt failure. Fix: log WARNING + refuse to fall back once `LM_FERNET_KEY` set.
- **qa** `api_server.py:101-140` + `main.py:18`/`control_plane.py:45`: no auth on `/api/run` (network-automation trigger; sidecar binds `0.0.0.0`) — any network client POSTs `{"module":"opnsense"}` and triggers real firewall/LDAP/PXMX mutations. Fix: `Depends(verify_x_api_key)`; bind `127.0.0.1`.
- **qa** `test_engine.py:163-176`: `test_opnsense_rules` leaks firewall rules if delete fails (cleanup not in `try/finally`) → `pass TCP 80 "QA Test Rule"` accumulates on the production firewall. Fix: `try/finally` the delete.
- **opnsense** `src/opnsense_engine.py:33-37` + `opn_spoke.py:178`: TLS verification disabled by default (`-k` unless `LM_OPNSENSE_VERIFY_TLS=1`; `curl -k` hardcoded). Fix: default verify on.

### Performance — blocking the event loop (recurring pattern)
- **cs** `routers/spokes.py:440,1296,394,851,929,952,1238,1347`: sync `store.*` (file I/O under `threading.RLock`+`fcntl.flock`) called directly inside `async def` endpoints on the spoke-ack hot path — each ack does 4 blocking store writes, stalling the single event loop (exactly what cs's own `loop_lag_monitor` warns about). Fix: wrap every `store.*` in `asyncio.to_thread` (pattern exists at 354,379,385,389).
- **bugfixer** `main.py:6490,6567`: `/delete_issue` and `/resolve_issue` are `async def` doing synchronous PyGithub + disk I/O, blocking every other request for seconds. *(Directly relevant to the just-shipped Resolved work.)* Fix: `await asyncio.to_thread(...)` (pattern used at 5614).
- **bugfixer** `/update_now:6651`, `/trigger_hub_update:6768`, `/api/models:5175`, `/api/fetch-models:5192`: sync `git`/`requests` inline in async handlers. Fix: `asyncio.to_thread`; `asyncio.gather` the two model fetches.
- **bugfixer** `main.py:1977-2078`: N+1 GitHub API calls in `find_global_duplicate_issue` — `O(errors × repos × pages)` per scan cycle, re-fetching identical issue lists. Fix: one `{repo: [issues]}` snapshot per scan phase.
- **bugfixer** `main.py:3435-3439`: no clone cache; fresh full `git clone` per issue (concurrent via `ThreadPoolExecutor:4452`). Fix: `clone_from(..., depth=1)` or one bare mirror per repo per cycle with `--reference`.
- **lm** + **dhcp/dns/ldap/opnsense/pxmx/qa**: blocking `subprocess`/`requests`/`python-ldap` inside async handlers across the spokes. Fix: `asyncio.to_thread` / async clients (`httpx.AsyncClient`).
- **lm** `generic_agent/.../agent.py:56-62`: `psutil.cpu_percent(interval=1)` blocks 1s inside async `_telemetry_loop` every 60s. Fix: `asyncio.to_thread`.
- **lm** `main.py:242`: busy-poll `while…: await asyncio.sleep(0.1)` in `request_response` (10 wakeups/sec, 100ms latency). Fix: `asyncio.Future` per `msg_id` + `wait_for`.
- **lm** `main.py:1259,1273-1276`: `collect_all_logs` reads each log fully then O(n²) trim loop. Fix: `collections.deque(f, maxlen=500)`.
- **netbox** `netbox_engine.py:9,67`: `Semaphore(1)` + **no timeout** on `http_session.get` → one hung request blocks every subsequent call across the engine indefinitely. Fix: session default timeout + backoff; raise semaphore to 4-8.
- **netbox** `netbox_engine.py:62-71` (callers 86,95,117,202,274,538,558,576,601,613): silent pagination truncation — `_api_get` never follows `next`, every caller uses `limit=500`; large inventories silently drop rows. Fix: loop on `data["next"]` or flag `truncated`.
- **netbox** `netbox_engine.py:541,561,579`: `search()` makes 3 sequential `_api_get` calls. Fix: `concurrent.futures`/gather.
- **netbox** `netbox_spoke.py:58-67,87`: KEA sync loop — fresh `httpx.AsyncClient` per scope (no pooling), sequential, flat `sleep(300)` no backoff. Fix: shared client, gather-batch with concurrency cap, exponential backoff.
- **opnsense** `opn_spoke.py:73-85` + `opnsense_engine.py:279-316`: `refresh_cache` awaits 8 engine methods serially (~120s worst case vs ~15s parallel); `get_nat_policies` probes 3 endpoints serially. Fix: `asyncio.gather(..., return_exceptions=True)`.
- **opnsense** `opnsense_engine.py:48-53`: no `asyncio.wait_for` around `process.communicate()` — only curl's `--max-time` bounds it. Fix: wrap in `wait_for(..., timeout=20)`, `process.kill()` on timeout.
- **pxmx** `agent/src/agent.py:206,297,236-251,313-323`: sequential `_pvesh` calls; `/cluster/resources` fetched twice per 60s cycle. Fix: `asyncio.gather` telemetry; fetch once.
- **pxmx** `src/control_plane.py:278`: `_save_disk_cache` writes the full cache on every 60s telemetry frame, synchronous `open`/`json.dump`/`os.replace` on the event loop. Fix: coalescing task + `run_in_executor`.
- **pxmx** `src/control_plane.py:190`: no auth-recv timeout on inbound WS (`json.loads(await websocket.recv())`) → slowloris/DoS. Fix: `asyncio.wait_for(..., timeout=10)`, cap connections.
- **pxmx** `agent/src/agent.py:459`: `websockets.connect` with no `open_timeout`/`ping_interval`/`ping_timeout`. Fix: set them.
- **dhcp** `src/dhcp_manager.py:20`: fresh `httpx.AsyncClient` per Kea command (N subnets = N connection setups). Fix: long-lived shared client.
- **dns** `src/dns_manager.py:73-99`: `sync_records` sequential subprocess-per-record. Fix: `asyncio.gather` (bounded) or single `unbound-control` batch.
- **cs** `routers/spokes.py:462-466` → `store.py:621-669,573-586`: O(T × S) cross-tenant disk scan in `register_spoke` per reconnect under global lock. Fix: in-memory indexes built at startup, updated on write.
- **cs** `aggregate.py:857,1286,1329,1355,1845` + `superadmin.py:608,637` + `spokes.py:941`: O(S²) read-modify-write of `spokes.json` in fan-out loops (50-spoke "push config to all" = ~50 full re-reads + rewrites under global lock). Fix: single load → mutate all in memory → one batch write.
- **cs** `aggregate.py:1384,1453,1502`: no pagination on aggregate list endpoints (clients/sims/proxmox) → multi-MB in-memory responses. Fix: server-side `?limit=&offset=`.
- **lm** `broadcaster.py:26-31`: sequential broadcast fanout; one slow client blocks all subscribers. Fix: `asyncio.gather(..., return_exceptions=True)`.

### Optimization — re-reading disk on every call
- **bugfixer** `main.py:122-161`: `load_config()` re-reads+parses `config.json` on **~40 call sites with no cache** — every HTTP request and every worker iteration pays a disk read + JSON parse + dict merge for config that only changes on Save Settings. *Biggest single waste in the codebase.* Fix: mtime-invalidated `_CONFIG_CACHE`, rebuilt on Save.
- **bugfixer** `load_processed()` called up to **7× inside one `process_single_issue`** (3375,3384,3408,3466,3489,3538,3567). Fix: load once at entry, mutate, save once per persistent exit.
- **bugfixer** `main.py:343,371,382,334,310`: duplicated provider-config parsing — 5 helpers each independently iterate `llm_entries`; legacy flat keys + vault shape coexist (`/save_settings` writes flat, vault writes `llm_entries[]`) → consistency surface. Fix: one `_resolve_slots(config) -> {1..4: ProviderSlot}` memoized on config identity; pick one canonical shape.
- **bugfixer** `main.py:2495-2524`: `heartbeat_worker` re-parses all 4 provider slots every 5s forever (12 disk reads/min); duplicated in `connectivity_worker:2480` + `poller_worker:4986`. Fix: drive refresh off config-cache invalidation, or 60s+ interval.
- **bugfixer** `discover_labels:1852` + `get_monitored_repos:1652`: re-fetched every scan cycle (effectively static). Fix: TTL cache / cache by config identity per cycle.
- **cs** `store.py:325,361,600,728,608,…`: `json.load` from disk on every `get_tenant`/`list_spokes`/`get_approved_spoke_by_*`; no in-memory cache; `save_spoke`/`ensure_config_update_command` always full-file read+rewrite. Fix: module-level mtime-keyed cache invalidated on `save_*`.
- **lm** `core/.../control_plane.py:125-172` vs `621-690`: duplicated git self-update logic within core (`perform_self_update_check` and `SPOKE_UPDATE` handler duplicate fetch/rebase/abort/reset/restart). Fix: extract `_do_git_update(cwd, repo_url=None)`.
- **lm** `api.py:2866-2890` + `2908-2917`: duplicated module→script-path table. Fix: hoist to module-level constant.
- **lm** `main.py:242-272,309,1030,1114`: `_legacy` dict defined twice; near-identical `_type_to_key`/`_prefix_to_*` maps. Fix: hoist.
- **lm** `state/manager.py:296-300`: `get_tenant` linear-scans all tenants on every miss **and logs the full tenant list** on a hot path (tenant-id enumeration in logs). Fix: normalize keys on write.
- **lm** `core/.../global_lock.py:28,36,46`: `TaskRequest.priority` captured but `_queue` is FIFO `deque.popleft()` — priority never consulted. Fix: `heapq` by `(priority, seq)` or drop the field.
- **lm** `simulations/store.py:22-36`: dead store methods; `SimulationsStore(self.state.data_dir)` passes a path string as the `hub` arg (`main.py:135`) — latent bug. Fix: wire or remove; pass `self`.
- **cs** `webui-hub/shared/shared_utils.py` vs `webui-hub/frontend/shared/shared_utils.py`: two divergent copies; frontend copy missing `vh_connected`+`template_lock` and imported by no Python. Fix: delete frontend copy / symlink.
- **cs** `webui-spoke/server.py:4275`: spoke reimplements `serialize_client` (diverges on `has_usb`, override merge). Fix: import shared fn.
- **cs** `aggregate.py:184,209`; `console.py:31`; `settings.py:75,82,89`; `sites.py:51,58`; `spokes.py:264`; `backups.py:66`: duplicated tenant/role-check helpers across 5 routers with semantic drift (`require_tenant_member` vs `require_tenant_access`; `_require_tenant_demo_or_above` byte-identical to `_require_tenant_access`). Fix: promote to `Depends`-able callables in `app/auth.py`.
- **cs** `aggregate.py:1914-1919,2247-2255,2277-2293` (+ ~20 more): Aruba `decrypt_dict → ArubaClient → is_configured()` boilerplate repeated ~23×. Fix: `_aruba_client_for_tenant` helper.
- **cs** `acme.py` (21 sites): fresh `httpx.AsyncClient(timeout=20)` each call. Fix: shared `_acme_request` helper.
- **cs** `store.py:608,659,493,694,1353,1396,1755,1788`: dead store funcs (zero refs): `get_spoke_by_api_key`, `get_spoke_by_name`, `get_spoke_by_pending_hostname`, `get_pending_spoke_by_name`, `get_spoke_processing_stats`, `approve_spoke`, `list_mac_profiles`/`save_oui_pool`. Fix: delete.
- **bugfixer** `main.py:1391-1395`: inconsistent error contract from `handle_hub_request`. Fix: standardize envelope.

### Workflow / concurrency
- **bugfixer** `main.py:3514,3528,3706,3794,6511,6631,6513,6633,3707,3795,6642`: **counter read-modify-write race** with no lock under `ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FIXES:4452)` + `retry_all_failed:6743` — `success_count`/`failure_count`/`closed_count` `+=` and `max(0,x-1)` are non-atomic; two workers can both read the same value and both write `+1`, losing an increment. UI buckets drift from reality. Fix: dedicated `threading.Lock`, or derive counts from `state["processed"]` on read (startup rehydration at 1587-1592 already does this). *(Ties to the lifecycle work.)*
- **bugfixer** `main.py:6451-6463` (`clear_history`): resets `success_count`/`failure_count` **but not `closed_count`** → after Clear History the UI shows closed issues while `processed` is empty, inconsistent with startup rehydration. Fix: add `state["closed_count"] = 0` at 6459. *(Drift in the lifecycle feature just shipped — one-liner.)*
- **bugfixer** `main.py:7553-7557`: workers started at module top level with no shutdown coordination (`@app.on_event("shutdown")`/`lifespan`/`atexit`/signal handler all absent). A `process_single_issue` can be killed mid-`git clone`/mid-`save_processed`, leaving `processed_issues.json` half-written or a temp clone orphaned. Under `gunicorn --workers N`, each worker starts its own poller/heartbeat/updater/restart threads → N pollers racing on the same repos and `processed_issues.json`, double-processing and clobbering saves. Fix: start workers in `lifespan` startup; shutdown event checked in `while True` loops; single-instance file lock.
- **bugfixer** `state["processed"] = processed` at 3379,3416,3497,3546,3575,3725,6514,6638: replaced wholesale without a lock; concurrent saves lose updates (last writer wins on same disk snapshot). Fix: serialize with a lock or single-writer design.
- **bugfixer** `_log_restart_event:3260-3269` + `daily_fixes_count`/`daily_budget_date:4887-4888`: never persisted → Diagnostics panel loses history on restart and a mid-day restart resets the daily fix budget to 0 (cap bypass). Likewise `state["paused"]/["blackout"]:5011,5017` never saved → restart un-pauses and resumes autonomous fixing unexpectedly. Fix: persist to `config.json`/state file.
- **bugfixer** `main.py:3637`: bare `except:` swallows `SystemExit`/`KeyboardInterrupt` for control flow (checkout-then-create-branch) — Ctrl-C during checkout is swallowed and branch creation attempted instead of letting the process die; wrong exception type. Fix: `except git.exc.GitCommandError:`.
- **lm** `broadcaster.py:25-31,33-45`: race on subscriber sets — `broadcast` iterates `self._subscribers[tenant_id]`/`self._admins` while `await ws.send_json(...)` yields; a concurrent `subscribe`/`unsubscribe` mutates the same set → `RuntimeError: Set changed size during iteration`. Fix: snapshot before iterating (`for ws in list(...)`), or `asyncio.Lock`.
- **lm** `core/.../mailbox.py:81-115`: race — acked message resurrected by retry loop (no lock): in `retry_loop`, message read into `to_retry`, then `await send_func(msg)` yields; a concurrent `acknowledge` can `del pending_ack[msg_id]` during that await, and line 108 re-inserts, re-sending an already-acknowledged message. Fix: re-check `msg_id in pending_ack` after the await and that the stored tuple still matches before re-inserting; guard with a lock.
- **lm** `main.py:641`: `response_cache` grows unbounded — entries only popped by a matching waiter; responses arriving after a timeout (or for no waiter) are never evicted → slow memory leak. Fix: TTL or Futures.
- **lm** `main.py:212-228` + `3384-3393`: logout only stops the cache task for the session's *first* tenant. Login starts `_start_cache_for_tenant` for every tenant in `tenants` (3349-3350); `_stop_cache_for_tenant` is called only with `sess.user.tenant_id` (first). For multi-tenant users, tasks for tenants[1:] are never cancelled and keep running across logout/relogin. Fix: iterate `sess.user.tenants` on logout.
- **lm** `main.py:758-759`: `finally` mutates `spoke_telemetry[spoke_id]` without existence guard → `KeyError` inside `except ConnectionClosed` masks the real close reason. Fix: `setdefault`.
- **lm** `main.py:290-296`: tenant isolation bypass in `get_client_sim_spoke` fallback — when `tenant_id` is set but no CS spoke is bound, falls back to `cands[0]` (first available approved CS spoke) → serves another tenant's simulations. Fix: return `None` when binding requested but absent.
- **lm** `core/.../control_plane.py:169,676`: `os._exit(3)` mid-handler bypasses caller `finally` and skips `_hb_task`/`_lr_task` cancellation (546-553). Fix: raise a dedicated `RestartRequested` caught at the `run()` loop boundary.
- **lm** `generic_agent/.../agent.py:135`: `SET_LOG_LEVEL` mutates the *root* logger (changes verbosity for every library in the process). Fix: scope to `logging.getLogger("GenericLeafAgent")`.
- **lm** `generic_agent/.../agent.py:80-96`: mutual-auth failure silently tolerated — on auth-failure exception logs warning and falls through to start heartbeat/telemetry/command loops and accept `SPOKE_COMMAND`s. Fix: `await websocket.close(...)` and `return`.
- **lm** `api.py:170-201`: `_cache_refresh_loop` swallows per-task exceptions via `gather(*tasks, return_exceptions=True)` without inspecting results → silent cache failures leave a module in `"error"` with no hub-log trace. Fix: log non-None exceptions.
- **lm** `core/.../control_plane.py:621-637` (+ opnsense 381-388): rotated hub secret kept only in memory; not persisted (`SPOKE_SET_HUB_SECRET` never writes to disk, unlike `SPOKE_UPDATE_SESSION_KEY` → `_persist_session_secret`). On restart the rotated hub secret is lost. Fix: persist rotated hub secrets.
- **lm** `routes.py:184-188`: WS loop `except Exception:` catches everything with no logging then unsubscribes → connection failures vanish silently. Fix: catch `WebSocketDisconnect` explicitly, log others at WARNING.
- **lm** `key_manager.py:250-257` vs `get_valid_key:192-221`: `verify_signature` ignores key history while `get_valid_key` accepts current+history → messages signed during a rotation window are dropped at `main.py:594-597`. Fix: have `verify_signature` iterate `history`.
- **cs** `tasks.py:62-77` (`loop_lag_monitor`), `webui-spoke/server.py:3880-3937` (`_event_loop_lag_monitor`), `webui-spoke/server.py:5768-5775` (`hub_isolation_monitor`): background `while True` with **no try/except** — single exception kills the telemetry that exists to catch blocking, silently, forever. Fix: standard `try/except asyncio.CancelledError: raise; except Exception: logger.exception(...)`.
- **cs** `webui-spoke/server.py:424-429`: `_load_persisted_settings` `except Exception: return {}` with no log → corrupt settings.json / key rotation boots spoke with empty defaults (relay/central/auth all reset) with no operator warning. Fix: log at ERROR.
- **cs** `lm-spoke/src/simulation_engine.py:257-262` (via `cs_spoke.py:104-108`): `SimulationEngine.start()` task has no `add_done_callback` — if `run_loop` raises, sim dies silently while hub believes it's running. Fix: callback that logs + sets `status="ERROR"`.
- **cs** `routers/settings.py:345-348,384-388`: silent decrypt failure on Aruba/github config update drops `webhook_id`/`webhook_api_key` with no log → existing Central webhook registration destroyed on next save after key rotation. Fix: log warning + refuse overwrite (503/409).
- **cs** `store.py:1856-1862` (`validate_qa_api_key`): persists `last_used_at` to disk **before** constructing `QAApiKey`; on construction failure returns `None` (treated invalid) but disk was mutated, no log. Fix: construct first, persist only on success.
- **dhcp** `dhcp_spoke.py:81-82` / `dns_spoke.py:68-69`: non-atomic delete-then-add (no rollback if add fails → reservation lost). Fix: stage both, commit once.
- **dhcp** `dhcp_manager.py:67-68`: `list_reservations` `except Exception` logs at DEBUG only and continues → failing subnet silently dropped, no partial-failure signal. Fix: log at WARNING, surface `partial_failure`.
- **dhcp** `dhcp_manager.py:104-109`: per-reservation failures swallowed into a `skipped` counter, no `{ip, error}` detail. Fix: return a `failed` list.
- **dns** `dns_manager.py:76-78`: `sync_records` catches `list_records` failure and proceeds with `existing = set()` → every record treated as new → potential duplicates. Fix: abort sync if existing-records load fails.
- **dns** `dns_manager.py:41-53`: `list_records` manual `split()` parsing; invalid TTLs silently rewritten to 300 (hides malformed data). Fix: strict parsing that surfaces errors.
- **kvm** `src/control_plane.py:141-146`: dangling `pending_responses` futures on agent disconnect (never cancelled). Fix: cancel/resolve pending futures for that agent in `finally`.
- **kvm** `src/control_plane.py:181-191`: `broadcast_to_agents` races on `connected_agents` dict (mutated on disconnect during `gather`); `zip(live_dict, results)` misattributes results. Fix: snapshot before `gather`.
- **kvm** `src/control_plane.py:76,103`: `json.loads(await websocket.recv())` on untrusted agent with no size cap. Fix: set `max_size` on `websockets.serve`, wrap parses in try/except.
- **opnsense** `opn_spoke.py:154-160` vs `:217-221`: duplicate `OPNSENSE_ADD_RULE`/`OPNSENSE_DEL_RULE` branches — first branch wins (non-applying variant), `_and_apply` versions unreachable → rules silently left un-applied (partial state). Fix: delete one branch.
- **opnsense** `opn_spoke.py:104`: `self.config = data` replaces whole dict instead of merging → an update carrying only `{"api_key":...}` discards `opn_host`/`refresh_interval`. Fix: `self.config.update(data)`.
- **opnsense** `opn_spoke.py:43-45`: refresh-loop recovery promise unfulfilled — on `RuntimeError` logs "will be started manually" but no path ever starts it → spoke runs with stale cache forever. Fix: lazy-start on first `handle_command`.
- **opnsense** `opn_spoke.py:94-98`: weak credential masking (`first4...last4` leaks 8 chars of any secret >8 chars). Fix: mask to fixed `"***"`.
- **pxmx** `agent/src/agent.py:97-98,347-348,418-419`: silent exception swallowing (`_load_secret` bare `except: pass` no log; `send_log` swallows all; `_update_check_loop` logs only DEBUG). Fix: log at WARNING with `repr(e)`.
- **pxmx** `agent/src/agent.py:354-396` note: `pip install -r requirements.txt` runs code from the pulled git branch on every agent — acceptable under the self-update trust model but flagged (compromised upstream branch = arbitrary code on every agent).
- **qa** `hub_client.py:38-40`: broad exception swallow converts all failures to `{"status":"ERROR","message":str(e)}` — callers can't distinguish transient timeout from real hub error; no retry. Fix: distinguish transport errors, add backoff for `httpx.ConnectError`/`TimeoutException`.
- **qa** `hub_client.py:34`, `test_engine.py:251,269`, `main.py:28`: fresh `httpx.AsyncClient` per call (no pooling, no retry); timeouts hardcoded in 4 places. Fix: shared client per `HubClient` + small retry.
- **qa** `api_server.py:164-167`: WS handler `except (WebSocketDisconnect, Exception): pass` + `_ws_clients.remove(websocket)` in `finally` raises `ValueError` if never appended (also swallowed) → stream bugs disappear, client list drifts. Fix: catch `WebSocketDisconnect` separately, log others, guard `remove`.
- **qa** `test_engine.py:124-141`: `test_security_invalid_sig` false-fails on buffered messages (`recv()` may return a pending heartbeat from an earlier test) → masks a real auth-bypass regression. Fix: drain pending messages first, correlate response id.
- **qa** `main.py:41-95` vs `control_plane.py:71-94`: two duplicate bootstrap entrypoints (ports 8080 vs 8090) with drift; `main.py` effectively dead/legacy. Fix: fold `main.py` into `control_plane.py`.

---

## Per-repo — additional detail (lower-severity / context)

### bugfixer — bare `except:` assessment (pre-identified set)
- **Acceptable (best-effort cleanup, not findings):** `watchdog.py:25,39,83,130,141` (supervisor must survive transient I/O/systemctl hiccups); `main.py:220` (`get_version` best-effort read); `main.py:5032` (dashboard `datetime.fromisoformat` fallback).
- **Low — should log, not reported as findings but worth a tweak:** `main.py:172,181,205` silently fall back to empty state on corrupt JSON — a corrupt `processed_issues.json` would silently wipe issue history with no log line. Change to `except json.JSONDecodeError as e: logger.error(...)`; same for `main.py:3394` (timestamp parse) — catch `(ValueError, TypeError)` and `logger.debug`.
- **Problematic (reported as workflow finding):** `main.py:3637` (swallows `SystemExit`).

### cs — additional
- **S7 Low** `aruba.py:83-113`: TOCTOU/DNS-rebinding in `validate_cluster_url` (resolves at validate-time, re-resolves at request-time) → DNS flip lets validation pass, request hits internal IP. Fix: pin resolved IP via custom transport, or re-validate before each request.
- **S8 Low** `routers/spokes.py:1306,1311`; `routers/console.py:278,358`; `webui-spoke/server.py:5984`: long-lived tokens passed in URL query (`?api_key=`/`?token=`) → leak via proxy/access logs, Referer, history. Fix: short-lived single-use ticket, or `Sec-WebSocket-Protocol` subprotocol; scrub from logs.
- **S9 Low** `spokes.py:250,550`; `superadmin.py:467`; `backups.py:276`; `store.py:1854`: non-constant-time secret comparisons (vs `secrets.compare_digest` used correctly in `webhook.py:75`). Fix: `secrets.compare_digest`.
- **S10 Low** `backups.py:35,289`; `aggregate.py:550,596`; `webhook.py:69`: raw exception text echoed to clients. Fix: generic client message, full `exc` logged server-side.
- **O9 Low** `store.py:333`: `get_tenant_by_hint` scans tenant list twice. Fix: single loop.
- **W7 Medium** `routers/superadmin.py:458-464`: `_get_tenant_psks` silently skips undecryptable PSKs (`except: pass`) → after secret rotation admin sees empty/short PSK list with no indication. Fix: `logger.warning(...)`.
- **W8 Medium** `webui-spoke/server.py:1452-1468,1474-1482`: silent `except Exception: pass` loading client-count baseline / 7-day history → corrupt baseline suppresses `HUB_CLIENT_COUNT_DROP_PCT` alerting (NO_DATA) with no log. Fix: `logger.warning(...)`.
- **W10 Low** `routers/spokes.py:64-69` + `routers/backups.py:203-232`: reseed retry treats all `error/failed` states identically (no transient-vs-permanent) → burns 3 retries (~1h) on permanent failures. Fix: classify error strings; skip retry for permanent classes.
- **W11 Low** `aggregate.py:47-54`: `_load_browse_disk_cache` `except Exception: pass; return None` → recurring parse error never surfaces. Fix: `logger.debug(...)`.
- **W12 Low** `main.py:140-152` + `webui-spoke/server.py:3077-3114`: background tasks created via `create_task` stored but no `add_done_callback` to alert on unexpected exit (W2–W4 are concrete instances). Fix: generic done-callback logging non-cancelled termination.

### lm — additional
- **S9 Medium** `api.py:235-241`: CORS `allow_origins=["*"]` with `allow_credentials=True` — invalid per spec; canonical misconfiguration. Fix: explicit origin list when credentials enabled.
- **S10 Medium** `broadcaster.py:29-31`: admin broadcast fanout ignores tenant → a left-open admin browser receives every tenant's telemetry. Fix: tag each admin socket with authorized tenants and filter.
- **S11 Medium** `dns/.../unbound_manager.py:36-46`: unbound config injection via unescaped record value (breaks out of quoted `local-data:`). Fix: reject/escape `"` and newlines; validate per record type.
- **S12 Medium** `qa/.../qa_tester.py:111-113`: hardcoded weak default secrets (`qa-secret-123`/`hub-secret-abc`) if env unset. Fix: fail closed (no default).
- **P9 Low** `mailbox.py:81-117`: `retry_loop` full-scans `pending_ack` every 1s (O(n)/s). Fix: heap keyed by next-retry.
- **W13 Low** `core/.../control_plane.py:39-40` (+ opnsense 31-32): bare `except Exception: pass` in `_SpokeLogRelayHandler.emit` drops WARNING/ERROR logs under load. Fix: track a dropped counter surfaced via `get_status`.
- **W14 Low** `core/.../control_plane.py:574-576`: bare `except` on `LM_HEARTBEAT_INTERVAL_S` parse keeps default silently. Fix: log a warning.
- **W15 Low** `core/.../heartbeat.py:10-36`: `last_seen` grows unbounded; no pruning of long-stale/RED entries. Fix: TTL eviction.
- **W16 Low** `routes.py:74-75,91-95`: `/sim` API mixes `JSONResponse(401, ...)` with `HTTPException(403)` → clients can't uniformly parse errors. Fix: standardize envelope.

### opnsense — additional
- **O11 Low** `opn_spoke.py:62-71,133-142` + `opnsense_engine.py` (16×): `cache_map` defined twice (drift risk); error-check template `if res.get("status")=="ERROR"...` repeated 16×; mutate-then-reconfigure 3-line block repeated 9×. Fix: single cache registry + `_request_and_check`/`_mutate_then_reconfigure` helpers.

### cppm — additional
- **#6 Medium** `queries.py:120-129`, `spoke.py:53-55`: 3-4 sequential API calls in `get_nac_status`/`refresh_cache` that should be concurrent. Fix: `asyncio.gather`/`asyncio.to_thread`.
- **#7 Medium** `spoke.py:38-40`, `queries.py:140-143,206-215,278-280`: silent exception swallowing — `get_version` bare `except Exception: pass`; list methods convert ERROR dicts to empty lists, masking real failures as "no results." Fix: log and surface.
- **#8 Medium** `spoke.py:141-143`: stale cache served with no TTL — `CACHED` returns `self._cache[n]` forever until manual refresh. Fix: per-entry timestamp + configurable TTL.
- **#9 Low** `queries.py:52-80,182-202`; `client.py:100-121`/`spoke.py:97-108`: duplicated logic (session-field extraction, OAuth candidate building). Fix: factor into shared helpers.
- **#10 Low** `spoke.py:42-44`, `main.py:27`, `tests/test_queries.py:25,60,82`: dead/broken code — `_sync_call` unused; `main.py` calls non-existent `list_endpoints`; tests assert wrong shapes (`list_endpoints`, `params={"mac":...}`, `/api/logs/auth`) → false-confidence suite. Fix: delete dead code, rewrite tests against real query shapes.

### netbox — additional
- **#9 Low** `netbox_spoke.py:300-301`, `netbox_engine.py:15-20`: silent `get_version` swallow; engine version read via relative `open("VERSION")` at import time (cwd-dependent) vs spoke's `__file__`-relative path — inconsistent. Fix: log at debug, use `__file__`-relative path.

### ldap — additional
- **M5 Medium** `ldap_manager.py:17`: no TLS / startTLS on the LDAP connection → admin bind password and all directory data in cleartext. Fix: `ldaps://` or `start_tls_s()` + cert verify.
- **M6 Medium** `ldap_manager.py:75`: `create_user` returns the plaintext password in the result (flows spoke → hub WS). Fix: return only a token/flag, deliver password out-of-band, or hash server-side.
- **L1 Low** `ldap_spoke.py:120`: `get_version` returns hardcoded `"1.0.0"` (unlike dhcp/dns/kvm which read `VERSION`) → version bumps never reflected. Fix: read `../VERSION`.

### kvm — additional
- **L2 Low** `control_plane.py:49`: hardcoded config path `/etc/lm-kvm/config.json` with no env/arg override. Fix: allow `LM_KVM_CONFIG` env / `--config` arg.
- **L3 Low** `install_kvm.sh:75`: `User=root` in the systemd unit. Fix: dedicated `libvirt`-group service account.
- **L4 Low** `install_kvm.sh:19`: deprecated `--admin-token` silently swallowed. Fix: emit deprecation warning to stderr.

### dhcp — additional
- **L2 Low** `dhcp_spoke.py:49,69,81,82,93`: `int(subnet_id)` can `ValueError`; the validation message doesn't cover the "not an int" case. Fix: validate and return a clear 400 before casting.

### dns — additional
- **L1 Low** `dns_manager.py:76-78`: (see workflow) abort sync if existing-records load fails.

---

## Cross-cutting structural issue (fixes the duplication at the root)

The same `control_plane.py` / spoke boilerplate is **copy-pasted across `dhcp`, `dns`, `kvm`, `ldap`, `cppm`, `netbox`, `opnsense`, `pxmx`, `qa`, and `lm`'s `core`**. The `sed` RCE (C4), the signature-bypass (C5), the git self-update logic, the `_run_sync` shim, the `--secret lm-secret` default, the broad `except Exception → str(e)` error-mapping, and the sequential-round-trip patterns are all **duplicated verbatim** — which is why C4/C5 had to be found and would have to be fixed *four times*.

Specifically duplicated:
1. **`_run_sync` executor shim** — `netbox_spoke.py:98-101`, `cppm/src/spoke.py`, `qa`, `pxmx/agent.py:409-417` (cppm uses deprecated `asyncio.get_event_loop()` instead of `get_running_loop()`/`asyncio.to_thread()`).
2. **Control-plane `run()` template** — register module → start sync/agent task → `await super().run()` → finally stop — verbatim in `cppm/control_plane.py:46-55`, `netbox/control_plane.py:46-55`, `opnsense/control_plane.py:33-37`, `qa/control_plane.py:40-68` + `main.py:41-95`, `pxmx/control_plane.py:391-406`.
3. **Import shim** — dual `try/except ImportError` for `core.src.messaging.control_plane` vs `messaging.control_plane` in `pxmx/control_plane.py:12-17` and `proxmox_spoke.py:5-8` (and equivalents fleet-wide).
4. **Hardcoded default secrets `lm-secret` / empty hub secret** — same arg-parser pattern in `cppm/control_plane.py:67` and `opnsense/control_plane.py:42-43`. One fix in a shared CLI builder fixes both.
5. **`httpx.AsyncClient(timeout=5.0)` literal** — duplicated across `qa` (`hub_client.py:34`, `main.py:28`, `test_engine.py:251,269`) and `netbox_spoke.py:87`. Should be a shared constant/config.
6. **Broad `except Exception` returning `str(e)`** — universal anti-pattern across all five repos (netbox_engine, opnsense_engine, qa hub_client, cppm queries, pxmx agent) — should be a shared error-mapping helper that logs server-side and returns typed codes to callers.
7. **Sequential round-trips where `asyncio.gather` applies** — `cppm` NAC status, `netbox` search/KEA sync, `opnsense` cache refresh + NAT, `pxmx` telemetry. A shared "fan-out with bounded concurrency + return_exceptions" helper would fix all consistently.
8. **Command dispatch ladder** (uppercase `cmd == "GET_VERSION"` / `UPDATE_CONFIG` / per-module / unknown) — duplicated verbatim in `dhcp_spoke.py`, `dns_spoke.py`, `kvm_spoke.py`, `ldap_spoke.py`. `get_version()` VERSION-file read duplicated in dhcp/dns/kvm (ldap's hardcoded version is a symptom of this drift).

**Recommendation:** lift a shared `BaseControlPlane` / `BaseModuleSpoke` into the `lm-core` package (HMAC handshake, signature verify with mandatory secret, git-update with origin allowlist, command dispatch, error mapper, secret-arg parser, bounded-concurrency fan-out helper). Each spoke then declares only its delta — and the Critical security fixes land once instead of per-spoke.

---

## Recommended fix order

1. **bugfixer auth (C1) + bind `127.0.0.1`** — one change defangs C2, C3, S3, S8 and the SSRF/diagnostics leaks. Mandatory bearer token from a `0600` root-owned secret; refuse to start if unset.
2. **lm signature-bypass (C5) + sed RCE (C4) + hub WS TLS (C6)** — make empty secret a hard startup error; validate `new_hostname` against `^[A-Za-z0-9._-]{1,63}$`; terminate TLS on the hub WS.
3. **netbox unauth privileged commands (C7) + cppm `verify=False`/SSRF (C8) + pxmx WS TLS (C9) + ldap `admin` fallback (C10)** — per-spoke auth + fail-closed secrets.
4. **cs `store.get_spokes` → `list_spokes` (C11)** — one-line fix that un-breaks config-override propagation across 7 endpoints.
5. **bugfixer perf/consistency batch:** `asyncio.to_thread` for `/delete_issue`, `/resolve_issue`, `/update_now`, `/api/models`; mtime `_CONFIG_CACHE`; guard counters (or derive from `processed`); `clear_history` reset `closed_count`; stop embedding the PAT in clone URLs + log sanitizer.
6. **cs perf:** wrap sync `store.*` in `asyncio.to_thread`; in-memory store cache; batch the spokes fan-out write.
7. **Structural:** extract `BaseControlPlane`/`BaseSpoke` so the 4–10× duplicated fixes become 1×.

### Two trivial fixes tied to just-shipped lifecycle work
- `clear_history` not resetting `closed_count` (`main.py:6459`) — one line: `state["closed_count"] = 0`.
- `/resolve_issue` and `/delete_issue` blocking the event loop — wrap the handler bodies in `await asyncio.to_thread(...)`.

---

## Appendix — deterministic sweep results (for reference)

- `py_compile`: 131 files, **0 syntax errors**. (The only syntax issue found during the session was a linter-mangled f-string in bugfixer's uncommitted "File a Bug" WIP — fixed locally, left uncommitted as it's not this audit's code.)
- `shell=True`: **0** across all repos.
- `eval(` / `exec(`: **0**.
- Hardcoded credential literals in source: **0** found (the credential issues above are defaults/argv/env-fallbacks, not literal secrets in committed Python — except `ldap/base_structure.ldif:16` `password123` and `qa/qa_tester.py:111` `qa-secret-123`/`hub-secret-abc`).
- Bare `except:`: **12**, all in bugfixer (`watchdog.py` ×5, `main.py` ×7) — assessed individually above.
- `subprocess` call sites: **76** across repos (reviewed for timeouts/silent failures — see per-repo performance/workflow sections).

---

*This file is a static record. Triage by severity; each finding above includes file:line, why it matters, and a concrete fix. No code was changed to produce this audit.*