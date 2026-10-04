---
name: deepseek-subagent
description: "Prefer the user's DSH MCP bridge for suitable delegated analysis, planning, coding and review, or when $deepseek-subagent is requested. Reuse persistent DSH conversations and native settings, tools and approvals."
---

# DeepSeek subagent

Use the callable `dsh_new`, `dsh_list`, `dsh_prompt`, `dsh_status`, `dsh_cancel`,
`dsh_close` and `dsh_resume` tools. Server prefixes may vary. If they are missing,
report that limitation; do not bypass the connection or silently substitute a
Codex subagent when the user requested DeepSeek.

Delegate the authorized task and relevant context. Ordinary follow-ups retain
that authorization; do not add paid test requests or send unrelated files or
secrets. DSH uses its selected model, tools and native approvals. If the selected
provider conflicts with the user's request, report it. Native permission requests
must be answered by the user in DSH.

## Submit work

- Use the actual owning Codex `parent_thread_id`, including the supplied parent
  id when working as a delegated agent. `dsh_list` finds that parent's children.
- Create a child for independent work with `dsh_new`. For project work, pass the
  authorized project's absolute `working_directory`; omission creates an isolated
  directory. Save the returned `session_id`. Bridge metadata is managed inside
  the plugin; do not create a project-local `.codex-dsh` directory.
- Send the task, relevant paths and expected deliverable with `dsh_prompt`.
  Keep natural-language prompts readable, with spaces and separate rules.
  Pass the actual submitting `parent_thread_id`, especially when reusing a child
  from another Codex chat; completion binds to this job's parent. Save both
  returned ids. The call submits asynchronously. Normal tasks have no
  deadline; set `timeout_seconds` only when required by the task, with 0 disabling it.
- Reuse the same session for follow-ups after the previous job has settled.

Default receipts:

```json
{"session_id":"codex-..."}
{"session_id":"codex-...","job_id":"..."}
```

## Read results

When completion notifications are working, continue independent work or finish
the current turn. The bridge sends `dsh_completion` to the owning parent after
saving the job's result and state. For a `completed` event, read `dsh_status`
once with **both exact ids from the event**. The response is:

```json
{"session_id":"codex-...","job_id":"...","result":"Original answer"}
```

This reads the result saved by the live DSH subscription; it does not re-run the
task. Explicit `job_id` avoids accidentally reading a later job. Compact status
contains no state or error and does not itself establish completion. For any
other event state, or a progress/connection check, use `verbose: true`.

Notifications require the exact parent to be loaded in the configured shared
App Server. If the route is unavailable or disabled, check progress with verbose
status. Never substitute a different parent. `dsh_list` and verbose status expose
notification diagnostics. A submitted receipt alone does not prove delivery.

The default answer page is 1,000,000 characters. Use verbose status for pagination,
errors, model/tool information or `waiting_permission`; the latter requires user
action in DSH. Details are in the [tool contract](references/bridge-contract.md).

`dsh_cancel` requests cancellation. Unconfirmed cancellation or interrupted
submission remains uncertain: do not automatically resubmit, erase state or
close/resume to bypass it. `dsh_close` releases a local handle while preserving
DSH history; `dsh_resume` reopens it. Independent children can run concurrently.

Verify material file/test claims before adopting the result. `dsh_prompt` accepts
text, not image attachments. An image path alone does not supply pixels; image
work needs a supported model-visible image route. Label OCR/descriptions as such.
