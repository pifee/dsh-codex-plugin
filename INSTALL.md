# Installation and removal

Keep the extracted `dsh-codex-plugin` folder in a permanent location writable by
your user. The package's `config/`, `data/` and `run/` directories remain there.
Do not install from a temporary directory and then delete it.

## Install

Install and open Codex Desktop and DSH Desktop first. Ensure Python 3.9+ is
available as `python3`. From the extracted folder, run:

```sh
sh install.sh --check
sh install.sh --apply
```

The default and `--check` modes are read-only. `--apply` creates private backups
and writes only the integration's registrations:

- `mcp_servers.dsh_subagent` transport fields in `~/.codex/config.toml`.
- The `codex-dsh-desktop-link` Host entry in the DSH Desktop profile patch.
- A Host link at `~/.dsh/profiles/desktop/node_modules/@local/codex-dsh-desktop-link`.
- A skill link at `~/.codex/skills/deepseek-subagent`.
- Initial `config/bridge.json` when it does not already exist.

Other MCP servers, settings and approval policies are preserved. The installer
does not install software, log in, change native permissions, launch applications
or submit model tasks. It refuses to replace unrecognized registrations.

After active work settles, quit DSH and Codex normally. Reopen DSH, then run:

```sh
sh start.sh
```

Use this entrypoint for subsequent Codex launches. The launcher expects the
standard `/Applications/ChatGPT.app` installation and binds the official shared
App Server to `ws://127.0.0.1:4500`. If Desktop is already running in ordinary
mode, quit it normally before launching in shared mode.

`sh start.sh --check` checks the existing connection without launching anything.
`--adopt` attaches exit supervision to an already running shared pair.

## Uninstall

The uninstaller removes this package's Codex MCP registration and its tool
subsections, the DSH Host entry, and the two owned links. It preserves unrelated
settings, the portable package, `config/`, `data/` and all working files.

First inspect the removal plan:

```sh
sh uninstall.sh --check
```

Let active work finish. Quit DSH and Codex Desktop normally so their bridge and
launcher locks are released, then run from a terminal:

```sh
sh uninstall.sh --apply
```

Apply refuses while the package's runtime locks are held. It does not terminate
applications or cancel jobs. Missing registrations are harmless; entries that
belong to another package are not removed. Registration changes are backed up in
`data/global/uninstallation-backups/`, checked for concurrent edits, and rolled
back if removal fails.

You can then launch Codex and DSH from their normal application icons. To delete
the portable folder as well, first save any wanted results and working files.
In particular, `data/projects/*/workspace` may contain real work. Native DSH
history and working directories elsewhere are not removed by this uninstaller.

## Update or move the package

Stop the bridge before changing an installed package. Back it up, replace program
and documentation files, and preserve `config/bridge.json`, `data/` and working
files. Run the installer again and restart the applications normally.

After moving the package, rerun installation to update registration paths.
Existing native DSH sessions retain their original working directories; moving
the package does not rewrite those paths. Keep their working files at the
locations DSH expects.

Installation backups are under `data/global/installation-backups/`. Restore a
registration backup with `sh install.sh --rollback BACKUP_ID --apply` only when
the corresponding program files are available. Rollback refuses to overwrite
subsequently changed registrations. Rollback is not uninstallation and does not
undo model tasks or restore discarded answers.

## Diagnose completion delivery

Completion messages require the exact parent chat to be loaded in the configured
shared App Server. The bridge reports notification state through `dsh_list` and
verbose status. A saved configuration alone does not prove live activation.

For a read-only endpoint check, use:

```sh
python3 src/configure_notifications.py --parent-thread-id ACTUAL_PARENT_ID
```

Use `--help` for endpoint, enable/disable and explicit `--apply` options. This
tool does not resume chats or send a test model request. The default configuration
has no task deadline; transport timeouts diagnose communication failures.
