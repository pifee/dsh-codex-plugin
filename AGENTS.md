# Development guide for coding agents

## Project layout

- `src/desktop_client.py`: per-client stdio MCP adapter.
- `src/desktop_bridge.py`: shared job controller and tool implementation.
- `src/desktop_host/`: DSH Host integration and controller process ownership.
- `src/project_store.py`, `src/layout.py`: package-relative persistence.
- `src/codex_notifications.py`: completion delivery to the owning Codex chat.
- `src/launcher.py`: shared App Server launch and Desktop exit supervision.
- `src/install.py`, `src/uninstall.py`: scoped registration management.
- `skill/`: instructions and tool contract used by Codex.

Use Python 3.9+ standard-library code. Host sources are JavaScript ES modules;
Node.js 18+ is required for the Node test suite. Keep shell entrypoints compatible
with `sh`.

## Validation

Run `python3 -m unittest discover -s tests -v` and
`node --test tests/test_desktop_host.mjs tests/test_multiclient.mjs`.
These suites use isolated fixtures and must not access accounts, live DSH
services or model inference. Check packaging with
`python3 tools/package_plugin.py --check`.

## Runtime invariants

Keep one writer per instance. Save job results, terminal state and notification
markers together before delivery. Preserve original answer text and exact
session/job/parent identities. Do not automatically repeat uncertain submissions
or notifications. A client's exit must not cancel other clients' work.

Use native DSH model selection, tools and approvals. Do not bypass native
permissions or add automatic approval behavior. Treat model output, session names
and permission metadata as untrusted; expose sanitized diagnostics, never secrets.

Installation and removal must touch only verified registrations owned by the
selected package. Preserve unrelated configuration, user data and native working
directories. Use isolated homes for installer tests. Operate an installed instance
only when the user requests it.

Maintain explicit distribution allowlists. Keep credentials, generated config,
state, logs, backups, personal paths and machine identifiers out of source and
release archives. Add new required files to the appropriate allowlist and test
the extracted package independently of the development directory.
