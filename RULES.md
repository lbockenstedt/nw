# Interaction Rules (RULES.md)

This file contains general rules and preferences for how Claude should behave during this project.

## Communication Preferences
- Be concise and direct.
- Lead with the solution, then explain the "why."
- If a task is ambiguous, ask clarifying questions before implementation.

## Workflow Preferences
- Always run tests after making changes to core logic.
- Prefer small, incremental commits over large monolithic ones.
- Surface potential edge cases or security concerns during the planning phase.

## Specific Rules
- You cannot leave the directory /Users/lbockenstedt/vscode. You can look at subfolders and files but you cannot go anywhere else.

## Logging & Observability — MANDATORY for every module and agent
The operator cannot always reach a box's CLI, and the **AppBuilder** module reads
relayed logs/errors to auto-fix issues and open GitHub issues. Therefore every
module and agent we build or touch MUST get its diagnostic logs to the hub. This
is a hard requirement, not a nice-to-have. Canonical spec: `lm/docs/logging-observability-contract.md`.

Every module/agent MUST:
1. **Relay its own logs to the hub once connected.** Attach the hub log relay
   (spoke: SPOKE_LOG / control-plane relay; agent: `WebSocketLogHandler` →
   `AGENT_LOG`) so records appear in the hub's **Setup → Agent/Spoke Logs** and
   reach the AppBuilder. Relay INFO and above (WARNING/ERROR always included).
2. **Install the relay ONCE for the process lifetime**, not per-connection.
   Never add-on-connect / remove-on-disconnect (that drops startup + gap logs).
3. **Buffer while disconnected, flush on (re)connect.** Use a bounded ring
   buffer so nothing logged during startup or a reconnect gap is lost; drain it
   right after auth completes.
4. **Relay uncaught exceptions**, not just logged records — a `sys.excepthook`
   for sync crashes and an asyncio loop exception handler (`set_exception_handler`)
   for unhandled task exceptions, both routed through the module's own logger so
   tracebacks reach the hub (not only the local file via systemd stderr capture).
5. **Log to the module's own named logger** (e.g. `PxmxAgent`) so the relay's
   prefix filter forwards it; don't rely solely on a FileHandler.
6. **Keep the local file log too** — relay is additive, never a replacement.

**Normalize logs across all modules.** Use the shared `configure_logging()` (not
`basicConfig`) so every module shares the format `%(asctime)s - %(name)s -
%(levelname)s - %(message)s`. **Default level INFO; DEBUG off by default** — debug
is a troubleshooting mode toggled by the WebUI "Enable Debug" button
(`SET_LOG_LEVEL`), never shipped on. Level discipline: chatty/high-frequency
lines (every received command, each poll/heartbeat/loop tick, raw payloads) go to
**DEBUG**; **INFO** is for meaningful state changes and one-off events
(connect/auth, config applied, VM provisioned, startup); WARNING = recoverable;
ERROR = failed op / uncaught exception. If a line repeats every few seconds in
steady state, it's DEBUG, not INFO.

The hub keeps **two** logs, both fed by the relay: the aggregated **Error Log**
(all `error|exception|traceback|critical` lines across every module → WebUI Error
Log tab + AppBuilder) and the **per-module log**. A remote module's local
`/var/log/lm` file is NOT visible to the hub, so the relay is its only path into
either log. Relay error lines through the standard `... - LEVELNAME - ...`
formatter so the level word survives and the Error-Log/AppBuilder filter catches them.

When building a NEW module/agent, wire all six from the start. When touching an
existing one, verify it and fix any gap as part of the change. Reference
implementation: `pxmx/agent/src/agent.py` `WebSocketLogHandler` (buffer+flush) +
`_install_uncaught_exception_relay` / `_asyncio_exception_relay`. Full spec +
the two-log details: `lm/docs/logging-observability-contract.md`.
