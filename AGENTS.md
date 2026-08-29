# AGENTS.md — `nw`

**Network devices module.** Switch/AP discovery, scanning and polling.

- **Repo:** `github.com/lbockenstedt/nw`
- **Module type:** `module_type = "nw"`
- **Canonical docs:** [`lm/docs/nw.md`](../lm/docs/nw.md) *(in the `lm` repo — the master registry)*
- **Fleet map:** [`../AGENTS.md`](../AGENTS.md) *(only present in a side-by-side checkout)*

## Context

This repo is **one of 16** that make up **Lab Manager (LM)** — a hub-and-spoke
"single pane of glass" orchestrator for lab/datacenter infrastructure. One hub (the `lm`
repo) runs the control plane, REST API and WebUI. Every other repo is a **spoke** wrapping
exactly one external system and dialling the hub over a WebSocket on port 443.

Read [`lm/docs/architecture-topology.md`](../lm/docs/architecture-topology.md) — a verbatim
copy also lives in this repo's `docs/` — before making structural changes.

## Layout

`src/nw_spoke.py` (spoke), `src/control_plane.py` (hub link), `src/nw_engine.py` (device
operations), `src/nw_scanner.py` (discovery), `src/nw_poll_scheduler.py`, `src/heartbeat.py`,
`src/transports/` (per-vendor transports). `API_SPEC.md` documents the surface.

## nw-specific gotchas

- **Highest-churn repo in the fleet.** Expect the most drift between docs and code; verify against `src/`.
- **`CLAUDE.md` in this repo is an unedited generic template** — it prescribes `npm run build`, Vitest and camelCase for what is a **Python** repo. **Ignore it.** Prefer `RULES.md` and this file.
- Extra automation lives here that other repos lack: `.pipeline-templates/`, `install_automation.sh`, `issue_automation.yml`, `bugfix_main.py`, `purge_github_reports.py`, `delete-closed-issues.sh`. These touch GitHub state — read before running.
- `test_new_file.txt` is stray scratch, not source. `LM-SIM-PORT-STATUS.md` tracks the sim port-over.

## Fleet conventions (identical in every LM repo)

- **Python 3.11**, FastAPI + `websockets` + `asyncio`. WebUI is dependency-free vanilla JS — **no npm build step exists anywhere in this project**.
- **`VERSION` is `MAJOR.NN` and branch-owned.** A bot bumps the last segment. **Never bump it by hand.** Promotion carries code only.
- **Branching: `dev -> qa -> main`.** `qa` and `main` need a PR; `ci.yml` is the required check. Direct pushes to `dev` are allowed.
- **CI runs one pytest process per component.** Components share top-level module names (`control_plane.py` exists in most repos) and collide in a single process.
- **Installers are idempotent** — re-running updates code and preserves credentials. Common flags: `--hub` (bare hostname is normalised to `wss://...:443`), `--id`/`--name`, `--secret`, `--hub-secret`, `--all-prereqs`.
- **Transport:** WebSocket on 443, mailbox pattern, **push-ack-retry — no fire-and-forget**. Heartbeat 30s; yellow at >=120s, red at >=300s. Hub queues 24h for offline spokes.
- **TLS:** encrypted but **verify-OFF by default** (self-signed hub cert). Verification is opt-in at install time via `--tls-verify` / `--tls-ca-cert` — never by hand-editing `.env`.
- **Heavy lifting belongs in the spoke, not the hub.** The hub is transport, state, policy and UI. See `lm/docs/architecture-spoke-heavy-lifting.md`.
- **API-first:** every operation exposes an API; the WebUI only ever calls that API.
- **Atomic transactions:** a mid-chain failure rolls back every preceding step and reports a before/after diff. No zombie resources.
- **Multitenancy is not optional:** isolation rides on Proxmox labels + NetBox tenant IDs. New resources carry tenant context.

## Rules

1. **One repo per change.** Cross-repo work is separate PRs, and the wire contract must stay backward-compatible because the two sides deploy independently.
2. **Read the canonical doc first** (linked above) — it is usually more current than this repo's README.
3. **Never hand-edit `VERSION`.**
4. **Check you are editing the live path,** not a preserved legacy one.
5. Match surrounding style. Comment only what needs clarifying.
