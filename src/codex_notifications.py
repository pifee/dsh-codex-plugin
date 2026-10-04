"""Durable, scoped completion delivery through an existing Codex App Server.

The WebSocket client connects only; this module never starts an App Server,
resumes a thread, reads credential files or changes a thread's runtime settings.
"""
import asyncio
import json
import math
import re
import time

try:
    from .mcp_common import BridgeError
    from .app_server_websocket import WebSocket, WebSocketError, endpoint
except ImportError:
    from mcp_common import BridgeError
    from app_server_websocket import WebSocket, WebSocketError, endpoint

FRAME_BYTES = 8 * 1024 * 1024
NOTIFIABLE = {'completed', 'cancelled', 'failed', 'cancel_unconfirmed', 'interrupted_unconfirmed'}
THREAD_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
UNAVAILABLE_CODES = {'connection_unavailable', 'invalid_app_server_response',
    'invalid_parent_thread_id', 'parent_identity_mismatch',
    'parent_not_loaded_in_this_server', 'parent_cannot_accept_input'}


class DeliveryUnavailable(Exception):
    """No mutating request was submitted; reconnecting is safe."""


class DeliveryRejected(Exception):
    """The App Server returned an explicit rejection."""


class DeliveryUncertain(Exception):
    """A request was submitted without a trustworthy acknowledgement."""


def settings(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {'enabled', 'socket_path', 'websocket_url', 'rpc_timeout_seconds', 'retry_seconds'}:
        raise BridgeError('Invalid Codex notification configuration')
    enabled = value.get('enabled', False)
    if not isinstance(enabled, bool): raise BridgeError('Invalid Codex notification enabled flag')
    socket = value.get('socket_path')
    url = value.get('websocket_url')
    try: endpoint(socket, url)
    except WebSocketError: raise BridgeError('Invalid local Codex App Server endpoint') from None
    result = {'enabled': enabled, 'socket_path': socket, 'websocket_url': url}
    for key, default in [('rpc_timeout_seconds', 10), ('retry_seconds', 30)]:
        number = value.get(key, default)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 < number <= 300:
            raise BridgeError('Invalid Codex notification ' + key)
        result[key] = number
    return result


class AppServerClient:
    def __init__(self, config): self.config = config

    async def open(self):
        return await WebSocket.connect(socket_path=self.config['socket_path'],
            websocket_url=self.config['websocket_url'], max_message_bytes=FRAME_BYTES)

    async def exchange(self, connection, method, params, request_id=None):
        message = {'method': method, 'params': params}
        if request_id is not None: message['id'] = request_id
        await connection.send_json(message)
        if request_id is None: return None
        while True:
            value = await connection.receive_json()
            if not isinstance(value, dict): raise DeliveryUnavailable('invalid_app_server_response')
            # Never answer server requests, approvals or unrelated notifications.
            if type(value.get('id')) is not type(request_id) or value.get('id') != request_id or 'method' in value: continue
            if 'error' in value: raise DeliveryRejected('app_server_rejected')
            if 'result' not in value: raise DeliveryUnavailable('invalid_app_server_response')
            return value['result']

    async def connect(self, connection, parent):
        await self.exchange(connection, 'initialize', {'clientInfo': {'name': 'dsh-completion-notifier', 'version': '0.2.0'}, 'capabilities': {'experimentalApi': True}}, 1)
        await self.exchange(connection, 'initialized', {})
        loaded = await self.exchange(connection, 'thread/loaded/list', {}, 2)
        # A persisted thread or Cloud parent id does not establish ownership here.
        # With no limit the official method returns all in-memory ids in one page.
        if (not isinstance(loaded, dict) or not isinstance(loaded.get('data'), list)
                or any(not isinstance(item, str) for item in loaded['data'])
                or loaded.get('nextCursor') is not None):
            raise DeliveryUnavailable('invalid_app_server_response')
        if parent not in loaded['data']:
            raise DeliveryUnavailable('parent_not_loaded_in_this_server')
        result = await self.exchange(connection, 'thread/read', {'threadId': parent, 'includeTurns': False}, 3)
        thread = result.get('thread', {}) if isinstance(result, dict) else {}
        if not isinstance(thread, dict): raise DeliveryUnavailable('invalid_app_server_response')
        state = thread.get('status', {}).get('type') if isinstance(thread.get('status'), dict) else None
        if thread.get('id') != parent: raise DeliveryUnavailable('parent_identity_mismatch')
        if state not in {'idle', 'active'}: raise DeliveryUnavailable('parent_not_loaded_in_this_server')
        if thread.get('canAcceptDirectInput') is not True: raise DeliveryUnavailable('parent_cannot_accept_input')
        return {'parent_loaded': True, 'parent_state': state, 'parent_model': thread.get('model')}

    async def probe(self, parent):
        """Read metadata for this exact parent; never submits a model turn."""
        if not isinstance(parent, str) or not THREAD_ID.fullmatch(parent): raise DeliveryUnavailable('invalid_parent_thread_id')
        connection = None
        async def read():
            nonlocal connection
            connection = await self.open()
            return await self.connect(connection, parent)
        try:
            return await asyncio.wait_for(read(), self.config['rpc_timeout_seconds'])
        except asyncio.CancelledError: raise
        except (DeliveryUnavailable, DeliveryRejected): raise
        except Exception: raise DeliveryUnavailable('connection_unavailable')
        finally:
            if connection is not None: await connection.close()

    async def deliver(self, parent, event, before_submit):
        if not isinstance(parent, str) or not THREAD_ID.fullmatch(parent): raise DeliveryUnavailable('invalid_parent_thread_id')
        connection = None; submitted = False
        async def send():
            nonlocal connection, submitted
            connection = await self.open()
            metadata = await self.connect(connection, parent)
            before_submit()  # Persist uncertainty before writing the mutation.
            submitted = True
            response = await self.exchange(connection, 'turn/start', {'threadId': parent, 'input': [],
                'toolOutput': {'name': 'dsh_completion', 'namespace': 'dsh_subagent',
                    'output': json.dumps(event, ensure_ascii=False)}}, 4)
            turn = response.get('turn', {}) if isinstance(response, dict) else {}
            if not isinstance(turn.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', turn['id']): raise DeliveryUncertain('invalid_delivery_acknowledgement')
            return dict(metadata, accepted_turn_id=turn['id'])
        try:
            return await asyncio.wait_for(send(), self.config['rpc_timeout_seconds'])
        except asyncio.CancelledError: raise
        except DeliveryRejected: raise
        except DeliveryUncertain: raise
        except Exception as error:
            if submitted: raise DeliveryUncertain('delivery_acknowledgement_unknown')
            raise DeliveryUnavailable(str(error) if isinstance(error, DeliveryUnavailable) else 'connection_unavailable')
        finally:
            if connection is not None: await connection.close()


class CompletionNotifications:
    def __init__(self, config, sessions, save, transport=None):
        self.config = settings(config); self.sessions = sessions; self.save = save
        self.transport = transport or AppServerClient(self.config)
        self.worker = None; self.wake = asyncio.Event(); self.closed = False
        self.dispatch_lock = asyncio.Lock()
        self.reconfigure_lock = asyncio.Lock(); self.paused = False
        # A crash after submission cannot establish whether Codex accepted it.
        changed = False
        for session in self.sessions().values():
            for job in session.get('jobs', {}).values():
                marker = job.get('completion_notification')
                if marker and marker.get('state') == 'sending':
                    marker.update(state='uncertain', error='delivery_acknowledgement_unknown'); changed = True
        if changed: self.save()

    @property
    def enabled(self): return self.config['enabled']

    async def reconfigure(self, config):
        """Pause unsubmitted work while preserving its durable parent binding."""
        candidate = settings(config)
        async with self.reconfigure_lock:
            if self.closed: raise BridgeError('notification_notifier_closed')
            if candidate == self.config: return
            if any(job.get('completion_notification', {}).get('state') == 'sending'
                   for session in self.sessions().values() for job in session.get('jobs', {}).values()):
                raise BridgeError('notification_work_unsettled')
            # Set this before yielding: neither worker nor manual dispatch may submit
            # while cancellation waits. arm/enqueue remain durable during the pause.
            self.paused = True
            try:
                if self.worker is not None:
                    worker = self.worker
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    self.worker = None
                async with self.dispatch_lock:
                    if self.closed: raise BridgeError('notification_notifier_closed')
                    self.config = candidate
                    if isinstance(self.transport, AppServerClient): self.transport.config = candidate
            finally:
                if self.worker is not None and self.worker.done(): self.worker = None
                self.paused = False
                self.start()

    def arm(self, session, job):
        if self.enabled:
            job['completion_notification'] = {'state': 'armed', 'parent_thread_id': job.get('parent_thread_id', session['parent'])}

    def enqueue(self, sid, jid):
        session = self.sessions()[sid]; job = session['jobs'][jid]
        marker = job.get('completion_notification')
        if not marker or marker.get('state') != 'armed' or job['state'] not in NOTIFIABLE: return False
        parent = marker.get('parent_thread_id')
        if parent != job.get('parent_thread_id', session.get('parent')) or not isinstance(parent, str) or not THREAD_ID.fullmatch(parent):
            marker.update(state='blocked', error='invalid_parent_binding'); self.save(); return False
        marker.update(state='pending', event_id=sid + ':' + jid, attempts=0, next_attempt_at=0)
        self.save(); self.wake.set(); return True

    def start(self):
        if not self.enabled or self.closed or self.paused or self.worker is not None: return
        # Recover only previously armed work. Historical unarmed jobs stay quiet.
        for sid, session in self.sessions().items():
            for jid in session.get('jobs', {}): self.enqueue(sid, jid)
        self.worker = asyncio.create_task(self.run())

    async def dispatch_once(self):
        if not self.enabled or self.closed or self.paused: return
        async with self.dispatch_lock:
            for sid, session in list(self.sessions().items()):
                for jid, job in list(session.get('jobs', {}).items()):
                    marker = job.get('completion_notification')
                    if self.closed or self.paused or not self.enabled or not marker or marker.get('state') != 'pending' or marker.get('next_attempt_at', 0) > time.time(): continue
                    if marker.get('parent_thread_id') != job.get('parent_thread_id', session.get('parent')) or job['state'] not in NOTIFIABLE:
                        marker.update(state='blocked', error='invalid_parent_binding'); self.save(); continue
                    event = {'session_id': sid, 'job_id': jid, 'state': job['state']}
                    marker['attempts'] += 1
                    def before_submit():
                        if self.paused or self.closed or not self.enabled:
                            raise DeliveryUnavailable('connection_unavailable')
                        marker.update(state='sending', submitted_at=time.time()); self.save()
                    try:
                        result = await self.transport.deliver(marker['parent_thread_id'], event, before_submit)
                        if marker['state'] != 'sending': raise DeliveryUncertain('invalid_delivery_acknowledgement')
                        marker.update(state='delivered', delivered_at=time.time(), accepted_turn_id=result['accepted_turn_id'])
                        marker.pop('error', None); marker.pop('next_attempt_at', None)
                    except asyncio.CancelledError:
                        if marker['state'] == 'sending': marker.update(state='uncertain', error='delivery_acknowledgement_unknown')
                        self.save(); raise
                    except DeliveryUnavailable as error:
                        if marker['state'] == 'sending': marker.update(state='uncertain', error='delivery_acknowledgement_unknown')
                        else:
                            wait = min(300, self.config['retry_seconds'] * 2 ** min(marker['attempts'] - 1, 4))
                            code = str(error) if str(error) in UNAVAILABLE_CODES else 'connection_unavailable'
                            marker.update(state='pending', error=code, next_attempt_at=time.time() + wait)
                    except DeliveryRejected:
                        marker.update(state='rejected', error='app_server_rejected')
                    except Exception:
                        marker.update(state='uncertain', error='delivery_acknowledgement_unknown')
                    self.save()

    async def run(self):
        while not self.closed:
            self.wake.clear()
            await self.dispatch_once()
            due = [job['completion_notification'].get('next_attempt_at', 0)
                for session in self.sessions().values() for job in session.get('jobs', {}).values()
                if job.get('completion_notification', {}).get('state') == 'pending']
            try: await asyncio.wait_for(self.wake.wait(), max(.01, min(due) - time.time()) if due else None)
            except asyncio.TimeoutError: pass

    async def close(self):
        self.closed = True
        if self.worker is not None:
            self.worker.cancel(); await asyncio.gather(self.worker, return_exceptions=True)
