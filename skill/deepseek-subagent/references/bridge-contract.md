# DSH tool contract

Use the schema exposed by the current MCP connection.

| Tool | Arguments |
| --- | --- |
| `dsh_new` | Required `parent_thread_id`; optional `name`, `working_directory` (absolute existing directory), `verbose` (false). |
| `dsh_list` | Optional `parent_thread_id`; lists bridge-owned conversations without inference. |
| `dsh_prompt` | Required `session_id`, `prompt`; optional `parent_thread_id`, `timeout_seconds`, `verbose` (false). Parent omission inherits the session's creation parent. Deadline omission uses the configured default; normal configuration has none. 0 disables it. |
| `dsh_status` | Required `session_id`; optional `job_id` (latest by default), `result_offset` (0), `result_limit` (1000000), `verbose` (false). Offsets count characters. |
| `dsh_cancel`, `dsh_close`, `dsh_resume` | Required `session_id`. |

An explicit directory that conflicts with the parent's existing workspace is
rejected. If the user deleted that workspace, a new child recreates a workspace
for the same verified directory; old child bindings remain unchanged. The bridge
accesses its own created conversations, even when the authorized directory already
has a native DSH workspace.

## Detailed receipts and status

`verbose: true` on `dsh_new` returns native creation metadata, using camelCase
keys such as `sessionId` and `workspaceId`. `dsh_list` uses the same native session
metadata. Default new returns only snake_case `session_id`.

Verbose `dsh_prompt` adds the initial state and notification intent to the same
submission's receipt. Do not resubmit merely to obtain diagnostics: read verbose
status for the existing job.

Default status has exactly `session_id`, `job_id`, `result`. When no job exists,
the last two fields are null. It reads saved job data without refreshing the
model/tool catalog. The live follow subscription saves committed answer messages
and saves settlement before making the completion event eligible for delivery.
A running job can therefore have a partial result; compact output is intended
for an exact job already reported completed.

Verbose status adds the nested `job`, state/error, timing, pagination, notification
receipt and current model/tool/permission diagnostics. Unavailable live diagnostics
are marked stale/unreachable. They do not make an uncertain job complete.

| Job state | Meaning |
| --- | --- |
| `preparing`, `running` | Submission or execution is in progress. |
| `waiting_permission` | The user must respond in DSH. |
| `completed`, `cancelled`, `failed` | Confirmed settlement; only completed indicates success. |
| `cancel_unconfirmed`, `interrupted_unconfirmed` | Native outcome is uncertain; another submission is blocked. |

If creation loses its acknowledgement, call `dsh_list` with the exact parent id
to reconcile the recorded creation intent. Save the recovered `sessionId` from
`sessions`. If an intent remains unresolved/uncertain, report that outcome;
do not guess the session or repeat `dsh_new`, which would create another child.

## Long answers

The bridge returns original committed answer text without summarizing or
deduplicating it. Default storage has no character cap. For multiple pages, use
verbose `next_result_offset` as the next `result_offset`; repeating 0 repeats the
first page. `result_complete` means the current stored text has been paged through,
and can be true while a job is still running.

Verbose status reports `result_total_chars` and `result_truncated`. Text discarded
under an earlier storage limit cannot be restored by increasing that limit; the
ordinary DSH history remains in its UI. If a response exceeds the transport frame
limit, request a smaller page. Character and frame counts are not token counts.

## Notification recovery

The event contains session id, job id and settlement state, not the answer. The
receipt in verbose status reports delivery state and a sanitized failure code:

- `armed`: intent saved; awaiting settlement.
- `pending`: waiting to deliver, including retryable connection failure before submission.
- `sending`: submission may be in flight.
- `delivered`: App Server acknowledged it; this alone does not prove the parent read it.
- `uncertain`: submission may have succeeded but acknowledgement was lost; no automatic replay.
- `rejected` or `blocked`: inspect the diagnostic and use verbose status to read the job.

The event preserves uncertain DSH states and the original parent binding. It does
not certify a later native outcome. Historical jobs without notification intent
are not announced retroactively. Neither a missing event nor a lost prompt
acknowledgement authorizes another inference request.

## Images

The text-only `dsh_prompt` payload has no attachment argument. An authorized native
DSH image-reading tool can provide pixels only when the selected provider/model
supports that route. Check the actual capability and payload path before relying
on visual analysis; a registered tool or a path/URL in text is insufficient.
