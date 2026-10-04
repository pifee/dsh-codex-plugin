# DSH–Codex Bridge

A portable local integration that lets Codex delegate work to DeepSeek Harness
(DSH), reuse persistent DSH conversations and receive completion messages before
retrieving the original results.

The bundle installs a Codex MCP server and skill, plus a DSH Host plugin. It is
managed by its shell scripts; it is not a plugin registered in Codex's Plugins
Directory. No interpreter or third-party runtime is bundled.

## Requirements

- macOS with Codex Desktop installed at `/Applications/ChatGPT.app`.
- DSH Desktop installed and opened at least once, with its Desktop profile
  available in the user's home directory.
- Python 3.9+ available as `python3`; no pip packages are required.
- DSH supplies the JavaScript runtime used by the Host integration. Node.js 18+
  is needed separately only when running the development tests.

This integration depends on the installed applications' Host and App Server
interfaces. It currently targets the local macOS Desktop workflow.

## Install and run

Extract the runtime archive into a permanent, writable directory. From that
folder:

```sh
sh install.sh --check
sh install.sh --apply
```

After existing work has settled, quit DSH and Codex Desktop normally. Reopen DSH,
then start Codex through:

```sh
sh start.sh
```

The launcher reuses a verified shared App Server or starts the official bundled
server, then opens Codex Desktop. Quitting Desktop with Cmd+Q stops its paired
server; closing a window does not. Repeated launches reuse the existing pair.
See [INSTALL.md](INSTALL.md) for setup, updates, diagnostics and removal.

## How it works

```text
Codex MCP client → DSH Host → shared bridge → native DSH conversation
                                  ↓
                         saved result and state
                                  ↓
                       shared Codex App Server
                                  ↓
                       owning chat notification
```

DSH keeps its native model, tools and permission prompts. Independent children
can run concurrently; follow-up messages can reuse the same child. Normal tasks
have no execution deadline and stored answers have no character cap.

Default tool responses are deliberately small:

```json
{"session_id":"codex-..."}
{"session_id":"codex-...","job_id":"..."}
{"session_id":"codex-...","job_id":"...","result":"Original answer"}
```

Read status using both IDs from a completed event. Use `verbose: true` for
progress, errors, permission requests or pagination. See the
[skill](skill/deepseek-subagent/SKILL.md) and its
[tool contract](skill/deepseek-subagent/references/bridge-contract.md).

## Files and data

```text
dsh-codex-plugin/
  install.sh       Register the integration
  uninstall.sh     Remove its registrations, preserving data
  start.sh         Launch Codex with the shared App Server
  src/             Python bridge and DSH Host sources
  skill/           Codex usage instructions
  config/          Default configuration and generated instance configuration
  data/global/     Project index, Host mappings and registration backups
  data/projects/   Per-project conversations, jobs, results and delivery records
  run/             Local connection capabilities and process locks
```

Project metadata is grouped by canonical working directory; different Codex chats
can share the same project. Omitted working directories create isolated workspaces
inside `data/projects/`. Business files stay in the selected working directory;
the bridge does not create `.codex-dsh` folders in external projects.

Account credentials and native DSH history remain under the applications' own
management. Package updates preserve `config/bridge.json`, `data/` and working
files. Share the clean distribution archives, not a running instance directory.

## Development

See `CONTRIBUTING.md` in the source distribution for architecture,
offline tests and reproducible packaging. The runtime distribution contains only
files needed to install and use the integration.

## License

[MIT](LICENSE). Codex and DeepSeek Harness are external dependencies and are not
included in this distribution.
