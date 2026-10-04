#!/usr/bin/env python3
"""STDIO MCP adapter to ordinary sessions on the existing DSH Desktop Host."""
import argparse
import asyncio
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
import urllib.parse
import uuid
try:
    from .mcp_common import BridgeError, McpServer, atomic_json, private_dir, schema
    from .codex_notifications import CompletionNotifications, settings as notification_settings
    from .layout import PackageLayout
    from .project_store import ProjectStore
except ImportError:
    from mcp_common import BridgeError, McpServer, atomic_json, private_dir, schema
    from codex_notifications import CompletionNotifications, settings as notification_settings
    from layout import PackageLayout
    from project_store import ProjectStore

DEFAULT_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_RESULT_LIMIT = 1000000
TERMINAL = {'completed', 'cancelled', 'failed'}


class DesktopAdmissionRejected(BridgeError):
    """Host confirmed this request did not reach native prompt admission."""


def number(value, label, minimum=0, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum or (integer and not isinstance(value, int)):
        raise BridgeError('Invalid ' + label)
    return value

SID = {'type': 'string', 'description': 'Bridge-owned ordinary DSH Desktop session id'}

TOOLS = [
    ('dsh_new', 'Create an ordinary DSH conversation grouped by its Codex parent. Optionally use the authorized task project directory.', schema({'parent_thread_id': {'type': 'string', 'description': 'Actual parent Codex thread id; reuse for its child conversations'}, 'name': {'type': 'string'}, 'working_directory': {'type': 'string', 'description': 'Optional absolute existing task project directory already authorized by the user; this capability grants no unrelated directory access. Native DSH permissions apply; omitted uses an isolated directory'}}, ['parent_thread_id'])),
    ('dsh_list', 'List only conversations created by this adapter; never list ordinary desktop history.', schema({'parent_thread_id': {'type': 'string'}})),
    ('dsh_prompt', 'Submit a normal DSH user message; the client returns session_id and job_id by default. Completion arming remains internal; client verbose mode includes its receipt. Read the original result after its completion event, or check status when delivery is unavailable. Native DSH owns model, tools and approvals.', schema({'session_id': SID, 'prompt': {'type': 'string', 'minLength': 1}, 'parent_thread_id': {'type': 'string', 'description': 'Actual submitting Codex chat; omitted inherits the conversation creation parent. Each job binds its own completion recipient.'}, 'timeout_seconds': {'type': 'number', 'minimum': 0, 'description': 'Optional turn deadline; 0 disables it. Otherwise inherit the bridge default (no deadline)'}}, ['session_id', 'prompt'])),
    ('dsh_status', 'Read the original DS answer after completion. Compact output contains only top-level session_id, job_id and result; no job uses null job_id and result. verbose includes job state, error, pagination and diagnostics.', schema({'session_id': SID, 'job_id': {'type': 'string'}, 'result_offset': {'type': 'integer', 'minimum': 0}, 'result_limit': {'type': 'integer', 'minimum': 1, 'default': DEFAULT_RESULT_LIMIT, 'description': 'Answer page size in characters; default 1000000, explicitly overridable'}, 'verbose': {'type': 'boolean', 'default': False, 'description': 'Optional, default false. Include pagination, tool/model/permission metadata, stream counts and job diagnostics'}}, ['session_id'])),
    ('dsh_cancel', 'Request cancellation of this adapter-owned turn; do not kill the shared Desktop Host.', schema({'session_id': SID}, ['session_id'])),
    ('dsh_close', 'Cancel/drain this adapter job and release local observation; desktop conversation/history remains.', schema({'session_id': SID}, ['session_id'])),
    ('dsh_resume', 'Reopen local access to an adapter-owned desktop conversation; no second writer.', schema({'session_id': SID}, ['session_id'])),
]
INSTRUCTIONS = ('Use actual parent Codex thread id in dsh_new: its children share one visible DSH workspace. '
    'Each call creates an ordinary separate desktop conversation. Reuse its session_id for follow-ups. '
    'Delegate the requested task/context/working paths; normal DSH model settings, tools and native permissions apply. '
    'Desktop owns persistence, reasoning display and permission prompts; never approve them automatically. '
    'No import/list of other desktop history. If dsh_prompt arms a verified completion route, '
    'read dsh_status once after its dsh_completion event; otherwise check status normally. '
    'An armed notification intent is not a delivery guarantee. Close preserves normal desktop history. '
    'Cancellation uncertainty blocks another prompt. Never kill the shared desktop process.')


class DesktopHttp:
    def __init__(self, file, timeout, max_frame_bytes=DEFAULT_FRAME_BYTES):
        self.file, self.timeout, self.max_frame_bytes = file, timeout, max_frame_bytes

    def link(self):
        parent = self.file.parent
        if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o077 or parent.stat().st_uid != os.getuid():
            raise BridgeError('Private physical desktop capability directory required')
        if self.file.is_symlink(): raise BridgeError('Physical private desktop capability required')
        info = self.file.stat()
        if not self.file.is_file() or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise BridgeError('Private desktop capability ownership/mode required')
        data = json.loads(self.file.read_text())
        url = urllib.parse.urlsplit(data.get('url', ''))
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.username or url.password or url.path != '/codex-dsh-subagent' or url.query or url.fragment:
            raise BridgeError('Capability must address the exact existing loopback scoped route')
        if not isinstance(data.get('token'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', data['token']):
            raise BridgeError('Invalid desktop capability')
        return url.port, data['token']

    async def open(self, value):
        port, token = self.link()
        reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port, limit=self.max_frame_bytes), self.timeout)
        try:
            content = json.dumps(value, ensure_ascii=False).encode()
            if len(content) > self.max_frame_bytes: raise BridgeError('Request exceeds configured max_frame_bytes')
            header = ('POST /codex-dsh-subagent HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: %s\r\n\r\n' % (port, token, len(content))).encode()
            writer.write(header + content); await writer.drain()
            status = await asyncio.wait_for(reader.readline(), self.timeout)
            accepted = bool(re.match(rb'HTTP/1\.[01] 200 ', status))
            if not accepted and not re.match(rb'HTTP/1\.[01] 400 ', status): raise BridgeError('Scoped desktop request failed; diagnostics withheld')
            headers = {}; size = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), self.timeout); size += len(line)
                if size > 8192 or not line: raise BridgeError('Invalid desktop HTTP response')
                if line == b'\r\n': break
                key, val = line.decode('ascii').split(':', 1); headers[key.lower()] = val.strip().lower()
            if 'content-encoding' in headers: raise BridgeError('Unexpected encoded desktop response')
            if not accepted:
                raw = bytearray()
                async for part in self.parts(reader, headers):
                    raw.extend(part)
                    if len(raw) > 8192: raise BridgeError('Scoped desktop request failed; diagnostics withheld')
                refusal = json.loads(raw)
                reason = refusal.get('scope_reason')
                allowed = {'Normal Desktop preset unavailable', 'Workspace response contract mismatch', 'upstream-service-refused', 'Conversation ownership changed', 'Parent workspace already uses another directory', 'Existing absolute working directory required', 'Request too large', 'History frame too large', 'Conversation has running/queued work', 'Prompt request id already submitted; no automatic retry', 'Unknown bridge-owned desktop conversation', 'Workspace identity changed', 'Workspace membership changed', 'Owned conversation could not be resumed', 'DSH model selection unavailable', 'Unsupported action/fields', 'Inference disabled pending exact daily-use approval', 'Account inference requires the exact bounded approval', 'Multi-client controller not enabled', 'Unknown tool or arguments', 'Shared controller unavailable; admission may be uncertain; no automatic retry'}
                if reason in allowed:
                    if value.get('action') == 'prompt' and refusal.get('admission_state') == 'not_submitted': raise DesktopAdmissionRejected(reason)
                    raise BridgeError(reason)
                raise BridgeError('Scoped desktop request failed; diagnostics withheld')
            return reader, writer, headers
        except BaseException:
            writer.close(); await writer.wait_closed(); raise

    async def parts(self, reader, headers):
        if headers.get('transfer-encoding') == 'chunked':
            while True:
                line = await reader.readline()
                if len(line) > 100: raise BridgeError('Invalid HTTP chunk')
                try: length = int(line.split(b';')[0].strip(), 16)
                except ValueError: raise BridgeError('Invalid HTTP chunk')
                if not 0 <= length <= self.max_frame_bytes: raise BridgeError('HTTP chunk exceeds configured max_frame_bytes')
                if length == 0: return
                part = await reader.readexactly(length)
                if await reader.readexactly(2) != b'\r\n': raise BridgeError('Invalid HTTP chunk boundary')
                yield part
        elif 'content-length' in headers:
            length = int(headers['content-length'])
            if not 0 <= length <= self.max_frame_bytes: raise BridgeError('HTTP body exceeds configured max_frame_bytes')
            yield await reader.readexactly(length)
        else:
            while True:
                part = await reader.read(16384)
                if not part: return
                yield part

    async def call(self, value):
        async def run():
            reader, writer, headers = await self.open(value)
            try:
                raw = bytearray()
                async for part in self.parts(reader, headers):
                    raw.extend(part)
                    if len(raw) > self.max_frame_bytes: raise BridgeError('Desktop response exceeds configured max_frame_bytes')
                result = json.loads(raw)
                if result.get('ok') is not True: raise BridgeError('Scoped desktop operation refused; diagnostics withheld')
                return result['value']
            finally: writer.close(); await writer.wait_closed()
        return await asyncio.wait_for(run(), self.timeout)

    async def follow(self, sid):
        reader, writer, headers = await self.open({'action': 'follow', 'session_id': sid})
        try:
            if headers.get('content-type') != 'application/x-ndjson': raise BridgeError('Expected desktop NDJSON history')
            buffer = bytearray()
            async for part in self.parts(reader, headers):
                buffer.extend(part)
                if len(buffer) > self.max_frame_bytes: raise BridgeError('History frame exceeds configured max_frame_bytes')
                while b'\n' in buffer:
                    line, _, rest = buffer.partition(b'\n'); buffer = bytearray(rest)
                    yield json.loads(line)
            if buffer: raise BridgeError('Truncated desktop history frame')
        finally: writer.close(); await writer.wait_closed()


class DesktopBridge:
    def __init__(self, config_file, transport=None, notification_transport=None):
        self.layout = PackageLayout.from_config(config_file)
        self.base = self.layout.root
        self.config = json.loads(Path(config_file).read_text())
        self.config_file = Path(config_file).absolute()
        self.startup_config = json.loads(json.dumps(self.config))
        self.notification_refresh_lock = asyncio.Lock()
        self.notification_config_state = 'startup'
        self.notification_config_error = None
        self.generation = uuid.uuid4().hex
        allowed = {'execution_enabled', 'runtime_root', 'link_file', 'rpc_timeout_seconds', 'turn_timeout_seconds', 'cancel_grace_seconds', 'max_frame_bytes', 'max_result_chars', 'max_pending_requests', 'codex_notifications'}
        if set(self.config) - allowed: raise BridgeError('Unknown desktop configuration')
        notification_config = notification_settings(self.config.get('codex_notifications'))
        if self.config.get('link_file', 'run/desktop-host/link.json') != 'run/desktop-host/link.json': raise BridgeError('Desktop capability path must be the dedicated link file')
        if self.config.get('runtime_root', 'run/bridge') != 'run/bridge': raise BridgeError('Desktop runtime boundary violated')
        self.layout.prepare(); self.root = self.layout.bridge_run
        if self.layout.owner_lock.is_symlink(): raise BridgeError('Physical owner lock required')
        self.lock = self.layout.owner_lock.open('a+'); os.chmod(str(self.layout.owner_lock), 0o600)
        try: fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: self.lock.close(); raise BridgeError('Another desktop adapter owns this runtime')
        self.store = ProjectStore(self.layout)
        self.file = self.layout.index_file
        self.state = self.store.state
        self.tasks = {}; self.events = {}; self.closing = False
        for session in self.state['sessions'].values():
            for job in session['jobs'].values():
                if job['state'] in {'running', 'preparing', 'waiting_permission', 'cancelling'}: job['state'] = 'interrupted_unconfirmed'
        self.rpc_timeout = number(self.config.get('rpc_timeout_seconds', 10), 'RPC timeout', .001)
        self.turn_timeout = number(self.config.get('turn_timeout_seconds', 0), 'turn timeout') or None
        self.cancel_grace = self.grace = number(self.config.get('cancel_grace_seconds', 5), 'cancel grace')
        self.max_frame_bytes = number(self.config.get('max_frame_bytes', DEFAULT_FRAME_BYTES), 'max_frame_bytes', 8192, integer=True)
        self.max_result_chars = number(self.config.get('max_result_chars', 0), 'max_result_chars', integer=True)
        self.max_pending_requests = number(self.config.get('max_pending_requests', 32), 'max_pending_requests', 1, integer=True)
        self.transport = transport or DesktopHttp(self.layout.link_file, self.rpc_timeout, self.max_frame_bytes)
        self.save()
        self.notifications = CompletionNotifications(notification_config, lambda: self.state['sessions'], self.save, notification_transport)

    def save(self): self.store.save()
    async def refresh_notifications(self):
        async with self.notification_refresh_lock:
            try:
                if self.config_file.is_symlink() or not self.config_file.is_file(): raise ValueError()
                candidate = json.loads(self.config_file.read_text())
                if not isinstance(candidate, dict): raise ValueError()
                proposed = notification_settings(candidate.get('codex_notifications'))
            except Exception:
                self.notification_config_state = 'rejected'
                self.notification_config_error = 'notification_config_invalid'
                return
            if ({key: value for key, value in candidate.items() if key != 'codex_notifications'} !=
                    {key: value for key, value in self.startup_config.items() if key != 'codex_notifications'}):
                self.notification_config_state = 'rejected'
                self.notification_config_error = 'notification_nonnotification_config_changed'
                return
            try: await self.notifications.reconfigure(proposed)
            except BridgeError as error:
                self.notification_config_state = 'rejected'
                self.notification_config_error = str(error) if str(error) in {'notification_work_unsettled', 'notification_notifier_closed'} else 'notification_config_invalid'
                return
            self.notification_config_state = 'current'
            self.notification_config_error = None

    def notification_diagnostics(self):
        config = self.notifications.config
        result = {'enabled': self.notifications.enabled, 'config_state': self.notification_config_state,
                  'process_pid': os.getpid(), 'generation': self.generation,
                  'endpoint': {'websocket_url': config['websocket_url']} if config['websocket_url'] else
                              {'socket_path': config['socket_path'] or str(Path.home()/'.codex/app-server-control/app-server-control.sock')}}
        if self.notification_config_error: result['error'] = self.notification_config_error
        return result
    def owned(self, sid):
        if sid not in self.state['sessions']: raise BridgeError('Unknown adapter-owned conversation')
        return self.state['sessions'][sid]
    async def call(self, value):
        try: return await self.transport.call(value)
        except BridgeError: raise
        except Exception: raise BridgeError('Desktop request failed; diagnostics withheld')
    async def new(self, parent, name, working_directory=None):
        if not isinstance(parent, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', parent) or parent in {'__proto__', 'constructor', 'prototype'}: raise BridgeError('Actual parent thread id required')
        if not isinstance(name, str) or re.search(r'[\x00-\x1f]', name): raise BridgeError('Invalid title')
        if working_directory is not None and (not isinstance(working_directory, str) or not Path(working_directory).is_absolute()): raise BridgeError('Existing absolute working directory required')
        child_id = uuid.uuid4().hex
        intent = {'creation_id': child_id, 'parent_thread_id': parent, 'state': 'pending'}
        self.state.setdefault('creations', {})[child_id] = intent
        self.save()
        request = {'action': 'new', 'parent_id': parent, 'child_id': child_id, 'name': name}
        if working_directory is not None: request['working_directory'] = working_directory
        try:
            result = await self.call(request)
            self.adopt_creation(intent, result)
        except BaseException:
            intent['state'] = 'uncertain'; self.save()
            raise
        self.save(); return result
    def adopt_creation(self, intent, result):
        if not isinstance(result, dict): raise BridgeError('Desktop conversation identity mismatch')
        sid = result.get('sessionId')
        if not isinstance(sid, str) or not sid or result.get('parent') != intent['parent_thread_id'] or result.get('childId') != intent['creation_id'] or not isinstance(result.get('workspaceId'), str): raise BridgeError('Desktop conversation identity mismatch')
        existing = self.state['sessions'].get(sid)
        if existing is not None:
            if existing.get('parent') != result['parent'] or existing.get('childId') != result['childId']: raise BridgeError('Desktop conversation identity mismatch')
        else:
            project_id = self.store.register(result.get('cwd'), result['parent'], result.get('projectId'))
            self.store.index['sessions'][sid] = project_id
            self.state['sessions'][sid] = dict(result, state='open', jobs={})
        self.state.get('creations', {}).pop(intent['creation_id'], None)
    async def reconcile_creations(self, parent=None):
        for intent in list(self.state.get('creations', {}).values()):
            if parent and intent['parent_thread_id'] != parent: continue
            try:
                receipt = await self.call({'action': 'lookup', 'parent_id': intent['parent_thread_id'], 'child_id': intent['creation_id']})
                if not isinstance(receipt, dict): raise BridgeError('Invalid creation lookup')
                if receipt.get('state') == 'ready' and isinstance(receipt.get('conversation'), dict) and receipt['conversation'].get('state') == 'ready':
                    self.adopt_creation(intent, receipt['conversation'])
                else: intent['state'] = 'creating' if receipt.get('state') == 'creating' else 'unresolved'
                intent.pop('error', None)
            except BridgeError:
                intent['state'] = 'uncertain'; intent['error'] = 'creation_reconciliation_unavailable'
            self.save()
    async def cancel_native(self, sid, job):
        result = await self.call({'action': 'cancel', 'session_id': sid, 'request_id': job['job_id']})
        confirmed = isinstance(result, dict) and result.get('cancel_requested') is True
        if confirmed: job['native_cancel_requested'] = True; self.save()
        return confirmed
    async def prompt(self, sid, text, timeout_seconds=None, parent_thread_id=None):
        session = self.owned(sid)
        if session['state'] != 'open': raise BridgeError('Resume locally closed conversation first')
        if any(job['state'] not in TERMINAL for job in session['jobs'].values()): raise BridgeError('Running or uncertain turn blocks another prompt')
        if not isinstance(text, str) or not text.strip(): raise BridgeError('Invalid prompt text')
        if len(text.encode('utf8')) > self.max_frame_bytes: raise BridgeError('Prompt exceeds configured max_frame_bytes')
        parent = session['parent'] if parent_thread_id is None else parent_thread_id
        if not isinstance(parent, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', parent) or parent in {'__proto__', 'constructor', 'prototype'}: raise BridgeError('Actual submitting parent thread id required')
        timeout = self.turn_timeout if timeout_seconds is None else number(timeout_seconds, 'turn timeout') or None
        jid = uuid.uuid4().hex; job = {'job_id': jid, 'parent_thread_id': parent, 'state': 'preparing', 'result': '', 'result_total_chars': 0, 'result_truncated': False, 'submitted': False, 'started_at': time.time(), 'timeout_seconds': timeout}
        self.notifications.arm(session, job)
        session['jobs'][jid] = job
        self.save(); task = asyncio.create_task(self.run(sid, jid, text, job)); self.tasks[jid] = task
        task.add_done_callback(lambda done: self.tasks.pop(jid, None))
        result = {'session_id': sid, 'job_id': jid, 'state': 'preparing'}
        if self.notifications.enabled: result['completion_notification'] = 'armed'
        return result
    async def run(self, sid, jid, text, job):
        ready = asyncio.Event(); ended = asyncio.Event(); cursor = [-1]; turn = [None]; matched = [False]
        async def observe():
            async for item in self.transport.follow(sid):
                if item.get('type') == 'snapshot':
                    if item.get('header', {}).get('id') != sid or item['header'].get('origin') or item['header'].get('parentSession'): raise BridgeError('History identity mismatch')
                    cursor[0] = item['cursor']; ready.set(); continue
                if item.get('type') == 'bridge/error': raise BridgeError('Desktop history failed; diagnostics withheld')
                if item.get('type') == 'assistant-stream':
                    self.events[sid] = self.events.get(sid, 0) + 1; continue
                if item.get('type') != 'event': continue
                event = item['event']; seq = event['seq']
                if seq <= cursor[0]: continue
                cursor[0] = seq; data = event.get('data', {}); kind = event['type']
                if kind == 'turn/start': turn[0] = data.get('turn')
                if kind == 'user/message' and data.get('source', {}).get('rpcId') == jid: matched[0] = True
                if kind == 'bridge/progress' and matched[0]:
                    native = data.get('eventType')
                    if native in {'approval/asked', 'approval/decided'}:
                        pending = max(0, job.get('pending_permission_count', 0) + (1 if native == 'approval/asked' else -1))
                        job['pending_permission_count'] = pending
                        job['state'] = 'waiting_permission' if pending else 'running'
                        self.save()
                if kind == 'assistant/message' and matched[0] and data.get('turn') == turn[0]:
                    content = data.get('message', {}).get('content', [])
                    answer = ''.join(part.get('text', '') for part in content if part.get('type') == 'text')
                    job['result_total_chars'] = job.get('result_total_chars', len(job['result'])) + len(answer)
                    result = job['result'] + answer
                    job['result'] = result[:self.max_result_chars] if self.max_result_chars else result
                    job['result_truncated'] = job['result_total_chars'] > len(job['result']); self.save()
                if kind == 'turn/end' and matched[0] and data.get('turn') == turn[0]:
                    job['pending_permission_count'] = 0
                    reason = data.get('reason', {}).get('kind')
                    job['state'] = 'cancelled' if reason == 'cancelled' else 'failed' if reason == 'error' else 'completed'
                    job['stop_reason'] = reason; self.save(); ended.set(); return
            if not ended.is_set(): raise BridgeError('Desktop history disconnected before settlement')
        observer = asyncio.create_task(observe())
        try:
            await asyncio.wait_for(ready.wait(), self.rpc_timeout)
            if job.get('cancel_requested'):
                job['state'] = 'cancelled'; return
            # A submitted flag is committed before admission to represent timeout uncertainty.
            job['submitted'] = True; job['state'] = 'running'; self.save()
            await self.call({'action': 'prompt', 'session_id': sid, 'request_id': jid, 'prompt': text})
            job['admission_state'] = 'accepted'; self.save()
            if job.get('cancel_requested') and job['state'] not in TERMINAL: await self.cancel_native(sid, job)
            await asyncio.wait_for(asyncio.shield(observer), job['timeout_seconds'])
        except asyncio.CancelledError:
            if job['state'] not in TERMINAL:
                job['state'] = 'interrupted_unconfirmed' if job['submitted'] else 'cancelled' if job.get('cancel_requested') else 'failed'
            raise
        except DesktopAdmissionRejected as error:
            job['submitted'] = False; job['admission_state'] = 'rejected'; job['state'] = 'failed'; job['error'] = str(error)
        except Exception as error:
            if job['submitted'] and job['state'] not in {'completed', 'cancelled', 'failed'}:
                with contextlib.suppress(Exception): await self.cancel_native(sid, job)
                job['state'] = 'cancel_unconfirmed'
            elif not job['submitted']: job['state'] = 'failed'
            job['error'] = str(error) if isinstance(error, BridgeError) else 'Turn deadline exceeded' if isinstance(error, asyncio.TimeoutError) else 'Desktop turn failed; upstream diagnostics withheld'
        finally:
            observer.cancel(); await asyncio.gather(observer, return_exceptions=True)
            job['finished_at'] = time.time(); self.save()
            self.notifications.enqueue(sid, jid)
    async def cancel(self, sid, job_id=None):
        session = self.owned(sid)
        active = session['jobs'].get(job_id) if job_id is not None else next((job for job in reversed(list(session['jobs'].values())) if job['state'] not in TERMINAL), None)
        if active and active['state'] in TERMINAL: active = None
        if not active: return {'session_id': sid, 'cancel_requested': False}
        active['cancel_requested'] = True
        self.save()
        task = self.tasks.get(active['job_id'])
        if not active.get('submitted') and active['state'] == 'preparing' and task and not task.done():
            active['state'] = 'cancelled'
            if task:
                task.cancel(); await asyncio.gather(task, return_exceptions=True)
            active.setdefault('finished_at', time.time()); self.save()
            self.notifications.enqueue(sid, active['job_id'])
            return {'session_id': sid, 'cancel_requested': True, 'state': active['state']}
        if not active.get('submitted'):
            # A historical uncertain job has no live admission task to stop.
            # Missing legacy metadata cannot prove native work was never admitted.
            active['state'] = 'cancel_unconfirmed'; self.save()
            return {'session_id': sid, 'cancel_requested': False, 'state': active['state']}
        await self.cancel_native(sid, active)
        if task:
            with contextlib.suppress(asyncio.TimeoutError): await asyncio.wait_for(asyncio.shield(task), self.grace)
        if active['state'] not in {'completed', 'cancelled', 'failed'}: active['state'] = 'cancel_unconfirmed'
        self.save()
        if active.get('job_id'): self.notifications.enqueue(sid, active['job_id'])
        return {'session_id': sid, 'cancel_requested': True, 'state': active['state']}
    async def tool(self, name, args):
        definition = next((d for d in TOOLS if d[0] == name), None)
        if not definition or not isinstance(args, dict) or set(args) - set(definition[2]['properties']) or any(key not in args for key in definition[2]['required']): raise BridgeError('Unknown tool or arguments')
        await self.refresh_notifications()
        self.notifications.start()
        if name == 'dsh_new': return await self.new(args['parent_thread_id'], args.get('name', 'DS subagent'), args.get('working_directory'))
        if name == 'dsh_list':
            await self.reconcile_creations(args.get('parent_thread_id'))
            parent = args.get('parent_thread_id')
            visible = lambda session: not parent or session['parent'] == parent or any(job.get('parent_thread_id', session['parent']) == parent for job in session.get('jobs', {}).values())
            return {'sessions': [{key: value for key, value in session.items() if key != 'jobs'} for session in self.state['sessions'].values() if visible(session)], 'creation_intents': [dict(intent) for intent in self.state.get('creations', {}).values() if not parent or intent['parent_thread_id'] == parent], 'limits': {'turn_timeout_seconds': self.turn_timeout, 'max_result_chars': self.max_result_chars or None, 'max_frame_bytes': self.max_frame_bytes}, 'deprecated_config': ['execution_enabled'] if 'execution_enabled' in self.config else [], 'completion_notifications': self.notification_diagnostics()}
        sid = args['session_id']
        if name == 'dsh_resume' and sid not in self.state['sessions']: await self.reconcile_creations()
        session = self.owned(sid)
        if name == 'dsh_prompt': return await self.prompt(sid, args['prompt'], args.get('timeout_seconds'), args.get('parent_thread_id'))
        if name == 'dsh_status':
            verbose = args.get('verbose', False)
            if not isinstance(verbose, bool): raise BridgeError('Invalid verbose flag')
            current_capabilities = False
            preflight = None
            try:
                if verbose:
                    preflight = await self.call({'action': 'preflight', 'session_id': sid})
                    capabilities = preflight['capabilities']
                    if session.get('capabilities') != capabilities:
                        session['capabilities'] = capabilities; self.save()
                    current_capabilities = True
            except (BridgeError, KeyError): pass
            jid = args.get('job_id') or next(reversed(session['jobs']), None)
            if jid and jid not in session['jobs']: raise BridgeError('Unknown job')
            job = dict(session['jobs'][jid]) if jid else None
            offset = number(args.get('result_offset', 0), 'result offset', integer=True)
            limit = number(args.get('result_limit', DEFAULT_RESULT_LIMIT), 'result limit', 1, integer=True)
            if limit > self.max_frame_bytes // 8: raise BridgeError('Result page exceeds configured max_frame_bytes; request a smaller page')
            if job:
                result = job['result']; job.update(result=result[offset:offset + limit], next_result_offset=min(len(result), offset + limit), result_complete=offset + limit >= len(result))
            return {'session_id': sid, 'workspace_id': session['workspaceId'], 'job': job, 'live_update_count': self.events.get(sid, 0), 'capabilities': session.get('capabilities'), 'capabilities_current': current_capabilities, 'selection': preflight.get('selection') if preflight else None, 'host_policy': preflight.get('inferencePolicy', 'legacy-host') if preflight else 'unreachable', 'legacy_inference_approved': preflight.get('inferenceApproved') if preflight else None, 'reasoning_view': 'normal DSH desktop UI only', 'permission_view': 'normal DSH desktop UI only', 'completion_notifications': self.notification_diagnostics()}
        if name == 'dsh_cancel': return await self.cancel(sid)
        if name == 'dsh_close':
            await self.cancel(sid); session['state'] = 'closed'; self.save(); return {'session_id': sid, 'state': 'locally_closed', 'desktop_history_retained': True}
        if name == 'dsh_resume': session['state'] = 'open'; self.save(); return {'session_id': sid, 'state': 'open', 'workspace_id': session['workspaceId']}
    async def shutdown(self):
        if self.closing: return
        self.closing = True
        await self.notifications.close()
        active_tasks = {jid for jid, task in self.tasks.items() if not task.done()}
        for sid, session in self.state['sessions'].items():
            for jid, job in session['jobs'].items():
                if jid in active_tasks and job['state'] not in TERMINAL:
                    with contextlib.suppress(Exception): await self.cancel(sid, jid)
        for task in list(self.tasks.values()): task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True); self.save(); self.lock.close()


async def serve(file):
    bridge = DesktopBridge(file); bridge.notifications.start()
    reader = asyncio.StreamReader(limit=bridge.max_frame_bytes)
    loop = asyncio.get_running_loop(); await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    lock = asyncio.Lock()
    async def write(value):
        async with lock: sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n'); sys.stdout.flush()
    current = asyncio.current_task()
    for sig in [signal.SIGINT, signal.SIGTERM]: loop.add_signal_handler(sig, current.cancel)
    try: await McpServer(bridge, reader, write, TOOLS, INSTRUCTIONS).run()
    except asyncio.CancelledError: await bridge.shutdown()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--config', required=True); args = parser.parse_args()
    try: asyncio.run(serve(args.config))
    except Exception: print('Desktop adapter stopped; diagnostics withheld', file=sys.stderr); sys.exit(1)
