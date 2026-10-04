# Contributing

## Environment

Python 3.9+ and Node.js 18+ are sufficient for the offline suites. No package
installation, API keys, running Codex or DSH services, or model requests are
required. The macOS launcher uses native process identity and exit-event APIs;
its tests exercise those interactions through fixtures.

## Architecture

| Component | Responsibility |
| --- | --- |
| `desktop_client.py` | One stdio MCP connection per Codex client; compact output formatting. |
| `desktop_host/` | Scoped DSH access and ownership of a single Python controller. |
| `desktop_bridge.py` | Native task admission, live result tracking, cancellation and status. |
| `project_store.py` | Project grouping and atomic persistence of results and delivery markers. |
| `codex_notifications.py` | Delivery to the exact owning parent through the shared App Server. |
| `launcher.py` | Shared launch, process identity validation and paired shutdown. |
| `install.py`, `uninstall.py` | Scoped, reversible registration changes. |

A client disconnect must not terminate shared work. Keep uncertain outcomes
explicit: do not retry a submission or completion message when it may already
have been accepted. Preserve native DSH approvals and original result text.

## Tests

Run from the repository root:

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_desktop_host.mjs tests/test_multiclient.mjs
```

Installer and uninstaller tests use temporary package directories and temporary
user homes. They must never alter the developer's own registrations. Use synthetic
thread IDs, session IDs, paths and model names in fixtures.

Live integration checks are separate from this suite and require an explicitly
authorized local instance and task. Do not add paid or network-dependent tests
to the offline commands.

## Build

```sh
python3 tools/package_plugin.py --check
python3 tools/package_plugin.py
```

The build creates two deterministic ZIP archives in `dist/`:

- `dsh-codex-plugin.zip`: installation and runtime files.
- `dsh-codex-plugin-source.zip`: those files plus tests, the build tool and
  developer documentation; suitable for starting a Git repository.

Both archives have one `dsh-codex-plugin/` root. The packager uses explicit
allowlists, validates package-relative files and rejects local machine identity
in published content. Add new files to the appropriate allowlist. Extracted
packages are tested for imports, CLI entrypoints and isolated registration flows.

Generated instance configuration, credentials, runtime state, backups, logs and
workspaces are excluded from both archives. Keep them out of commits as well.
Do not publish an installed directory containing user data.

Contributions are licensed under the project's [MIT license](LICENSE).
