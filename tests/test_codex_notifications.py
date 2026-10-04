import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.mcp_common import BridgeError, atomic_json
from src.codex_notifications import (AppServerClient, CompletionNotifications, DeliveryRejected,
    DeliveryUnavailable, DeliveryUncertain, settings)
from src.desktop_bridge import DesktopBridge
from src.desktop_client import DesktopClient, compact_status
from tests.test_desktop import MockDesktop
from tests.mock_codex_app_server import MockAppServer

PARENT = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'
EVENT = {'session_id': 'owned', 'job_id': 'job', 'state': 'completed'}


class FakeDelivery:
    def __init__(self): self.calls = []; self.mode = 'ok'; self.entered = asyncio.Event(); self.release = None
    async def deliver(self, parent, event, before_submit):
        self.calls.append((parent, copy.deepcopy(event))); self.entered.set()
        if self.mode == 'unavailable': raise DeliveryUnavailable('connection_unavailable')
        before_submit()
        if self.release is not None: await self.release.wait()
        if self.mode == 'lost': raise DeliveryUncertain('delivery_acknowledgement_unknown')
        if self.mode == 'rejected': raise DeliveryRejected('synthetic-secret')
        return {'accepted_turn_id': 'acknowledged-turn'}


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.file = Path(self.temp.name)/'state.json'
        self.sessions = {'owned': {'parent': PARENT, 'jobs': {'job': {'job_id': 'job', 'state': 'running', 'result': 'answer stays here'}}}}
        self.delivery = FakeDelivery(); self.snapshots = []
        self.outbox = self.make_outbox()
    def save(self):
        atomic_json(self.file, {'sessions': self.sessions}); self.snapshots.append(copy.deepcopy(self.sessions))
    def make_outbox(self, enabled=True):
        return CompletionNotifications({'enabled': enabled, 'retry_seconds': .01}, lambda: self.sessions, self.save, self.delivery)
    def enqueue(self, state='completed'):
        job = self.sessions['owned']['jobs']['job']; self.outbox.arm(self.sessions['owned'], job)
        job['state'] = state; self.outbox.enqueue('owned', 'job')
        return job['completion_notification']
    async def asyncTearDown(self): await self.outbox.close(); self.temp.cleanup()
    async def test_scoped_event_is_durable_and_delivered_once_without_answer(self):
        marker = self.enqueue(); await self.outbox.dispatch_once(); await self.outbox.dispatch_once()
        self.assertEqual(self.delivery.calls, [(PARENT, EVENT)])
        self.assertEqual(marker['state'], 'delivered'); self.assertEqual(marker['accepted_turn_id'], 'acknowledged-turn')
        self.assertIn('sending', [s['owned']['jobs']['job']['completion_notification']['state'] for s in self.snapshots])
        self.assertEqual(json.loads(self.file.read_text())['sessions']['owned']['jobs']['job']['result'], 'answer stays here')
    async def test_unavailable_before_submission_can_reconnect_but_lost_ack_cannot_retry(self):
        marker = self.enqueue(); self.delivery.mode = 'unavailable'; await self.outbox.dispatch_once()
        self.assertEqual(marker['state'], 'pending'); self.assertNotIn('submitted_at', marker)
        self.delivery.mode = 'lost'; marker['next_attempt_at'] = 0; await self.outbox.dispatch_once()
        self.assertEqual(marker['state'], 'uncertain')
        count = len(self.delivery.calls); await self.outbox.dispatch_once(); self.assertEqual(len(self.delivery.calls), count)
    async def test_rejection_and_crash_after_submission_do_not_replay(self):
        marker = self.enqueue(); self.delivery.mode = 'rejected'; await self.outbox.dispatch_once()
        self.assertEqual(marker['state'], 'rejected'); self.assertNotIn('synthetic-secret', json.dumps(marker))
        marker['state'] = 'sending'; reopened = self.make_outbox()
        try:
            self.assertEqual(marker['state'], 'uncertain'); await reopened.dispatch_once()
            self.assertEqual(len(self.delivery.calls), 1)
        finally: await reopened.close()
    async def test_changed_parent_binding_is_never_used_as_a_destination(self):
        marker = self.enqueue(); self.sessions['owned']['parent'] = OTHER
        await self.outbox.dispatch_once()
        self.assertEqual(marker['state'], 'blocked'); self.assertEqual(self.delivery.calls, [])
    async def test_parallel_dispatch_has_one_sender(self):
        marker = self.enqueue(); self.delivery.release = asyncio.Event()
        first = asyncio.create_task(self.outbox.dispatch_once()); await self.delivery.entered.wait()
        second = asyncio.create_task(self.outbox.dispatch_once()); self.delivery.release.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(self.delivery.calls), 1); self.assertEqual(marker['state'], 'delivered')
    async def test_worker_shutdown_marks_inflight_delivery_uncertain(self):
        marker = self.enqueue(); self.delivery.release = asyncio.Event(); self.outbox.start()
        await self.delivery.entered.wait(); await self.outbox.close()
        self.assertEqual(marker['state'], 'uncertain')
    async def test_historical_unarmed_jobs_and_disabled_feature_stay_quiet(self):
        self.sessions['owned']['jobs']['job']['state'] = 'completed'
        self.assertFalse(self.outbox.enqueue('owned', 'job')); await self.outbox.dispatch_once()
        disabled = self.make_outbox(False)
        try:
            disabled.arm(self.sessions['owned'], self.sessions['owned']['jobs']['job']); disabled.start()
            await disabled.dispatch_once(); self.assertEqual(self.delivery.calls, [])
        finally: await disabled.close()
    async def test_pending_can_pause_and_change_endpoint_without_replaying_settled_work(self):
        marker = self.enqueue(); self.delivery.mode = 'unavailable'
        await self.outbox.dispatch_once()
        await self.outbox.reconfigure({'enabled': False})
        self.assertEqual(marker['state'], 'pending')
        await self.outbox.dispatch_once(); self.assertEqual(len(self.delivery.calls), 1)
        marker['next_attempt_at'] = 0; self.delivery.mode = 'ok'
        await self.outbox.reconfigure({'enabled': True, 'websocket_url': 'ws://127.0.0.1:4501'})
        await self.outbox.dispatch_once()
        self.assertEqual(marker['state'], 'delivered')
        await self.outbox.reconfigure({'enabled': False})
        await self.outbox.reconfigure({'enabled': True})
        await self.outbox.dispatch_once(); self.assertEqual(len(self.delivery.calls), 2)
        marker['state'] = 'uncertain'
        await self.outbox.dispatch_once(); self.assertEqual(len(self.delivery.calls), 2)

    async def test_sending_cannot_reconfigure_and_cancel_never_replays(self):
        marker = self.enqueue(); self.delivery.release = asyncio.Event()
        self.outbox.start(); await self.delivery.entered.wait()
        with self.assertRaisesRegex(BridgeError, 'notification_work_unsettled'):
            await self.outbox.reconfigure({'enabled': False})
        await self.outbox.close()
        self.assertEqual(marker['state'], 'uncertain')
        self.assertEqual(len(self.delivery.calls), 1)

    async def test_reconfigure_during_manual_preflight_prevents_old_endpoint_submission(self):
        marker = self.enqueue(); entered = asyncio.Event(); release = asyncio.Event(); submitted = []
        class Preflight:
            async def deliver(inner, parent, event, before_submit):
                entered.set(); await release.wait()
                before_submit(); submitted.append(parent)
                return {'accepted_turn_id': 'ack'}
        self.outbox.transport = Preflight()
        dispatch = asyncio.create_task(self.outbox.dispatch_once()); await entered.wait()
        refresh = asyncio.create_task(self.outbox.reconfigure({'enabled': False}))
        await asyncio.sleep(0)
        self.assertTrue(self.outbox.paused)
        # Concurrent admission is retained; no mutation may cross the pause.
        job = {'state': 'running'}; self.outbox.arm(self.sessions['owned'], job)
        self.assertEqual(job['completion_notification']['parent_thread_id'], PARENT)
        release.set(); await asyncio.gather(dispatch, refresh)
        self.assertEqual(submitted, []); self.assertEqual(marker['state'], 'pending')
        self.assertFalse(self.outbox.enabled)

    async def test_armed_pause_preserves_binding_and_enqueues_after_settlement(self):
        job = self.sessions['owned']['jobs']['job']; self.outbox.arm(self.sessions['owned'], job)
        await self.outbox.reconfigure({'enabled': False})
        self.assertEqual(job['completion_notification']['state'], 'armed')
        job['state'] = 'completed'; self.outbox.enqueue('owned', 'job')
        self.assertEqual(job['completion_notification']['state'], 'pending')
        await self.outbox.reconfigure({'enabled': True})
        await self.outbox.dispatch_once()
        self.assertEqual(job['completion_notification']['state'], 'delivered')

    async def test_settlement_and_uncertainty_states_are_reported_unchanged(self):
        for state in ['failed', 'cancelled', 'cancel_unconfirmed', 'interrupted_unconfirmed']:
            job = self.sessions['owned']['jobs']['job']; job.pop('completion_notification', None)
            marker = self.enqueue(state); await self.outbox.dispatch_once()
            self.assertEqual(self.delivery.calls[-1][1]['state'], state); self.assertEqual(marker['state'], 'delivered')


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def client(self, server, timeout=1):
        return AppServerClient(settings({'enabled': True, 'socket_path': server.path, 'rpc_timeout_seconds': timeout}))

    async def test_idle_and_active_parents_receive_standalone_tool_output_without_overrides(self):
        for scenario in ['idle', 'active']:
            async with MockAppServer(scenario) as server:
                client = self.client(server); submitted = []
                result = await client.deliver(PARENT, EVENT, lambda: submitted.append(True))
                self.assertEqual([r['method'] for r in server.messages], ['initialize', 'initialized', 'thread/loaded/list', 'thread/read', 'turn/start'])
                params = server.messages[-1]['params']
                self.assertEqual(set(params), {'threadId', 'input', 'toolOutput'})
                self.assertEqual(params['threadId'], PARENT); self.assertEqual(params['input'], [])
                self.assertEqual(json.loads(params['toolOutput']['output']), EVENT)
                self.assertEqual(result['parent_state'], scenario); self.assertEqual(submitted, [True])
                await asyncio.wait_for(server.disconnected.wait(), 1)
                self.assertTrue(server.server.is_serving())
                # Closing one client must not close the shared service.
                await client.probe(PARENT)
                self.assertEqual(server.connections, 2)

    async def test_foreign_unloaded_and_nonaccepting_parents_are_not_submitted(self):
        for scenario in ['foreign', 'not-loaded', 'no-input']:
            async with MockAppServer(scenario) as server:
                submitted = []
                with self.assertRaises(DeliveryUnavailable): await self.client(server).deliver(PARENT, EVENT, lambda: submitted.append(True))
                self.assertEqual(submitted, [])
                self.assertNotIn('turn/start', [r['method'] for r in server.messages])

    async def test_stored_only_cloud_parent_never_reads_history_or_submits(self):
        async with MockAppServer('stored-only') as server:
            with self.assertRaisesRegex(DeliveryUnavailable, 'parent_not_loaded_in_this_server'):
                await self.client(server).deliver(PARENT, EVENT, lambda: self.fail('submitted'))
            self.assertEqual([r['method'] for r in server.messages],
                ['initialize', 'initialized', 'thread/loaded/list'])

    async def test_invalid_or_partial_loaded_list_never_submits(self):
        for scenario in ['invalid-loaded-list', 'partial-loaded-list']:
            async with MockAppServer(scenario) as server:
                with self.assertRaisesRegex(DeliveryUnavailable, 'invalid_app_server_response'):
                    await self.client(server).deliver(PARENT, EVENT, lambda: self.fail('submitted'))
                self.assertNotIn('turn/start', [r['method'] for r in server.messages])

    async def test_rejection_and_connection_loss_after_submission_are_distinct(self):
        for scenario, error in [('reject', DeliveryRejected), ('disconnect', DeliveryUncertain)]:
            async with MockAppServer(scenario) as server:
                submitted = []
                with self.assertRaises(error) as caught: await self.client(server).deliver(PARENT, EVENT, lambda: submitted.append(True))
                self.assertEqual(submitted, [True]); self.assertNotIn('synthetic-secret', str(caught.exception))

    async def test_timeouts_cover_handshake_preflight_and_ack_without_closing_service(self):
        for scenario, error, sent in [('slow-handshake', DeliveryUnavailable, []), ('slow-before', DeliveryUnavailable, []), ('slow-after', DeliveryUncertain, [True])]:
            async with MockAppServer(scenario) as server:
                submitted = []
                with self.assertRaises(error): await self.client(server, .2).deliver(PARENT, EVENT, lambda: submitted.append(True))
                self.assertEqual(submitted, sent)
                await asyncio.wait_for(server.disconnected.wait(), 1)
                self.assertTrue(server.server.is_serving())

    async def test_probe_reads_only_exact_parent_metadata_and_never_starts_a_turn(self):
        async with MockAppServer('idle') as server:
            self.assertEqual((await self.client(server).probe(PARENT))['parent_model'], 'synthetic-model')
            self.assertEqual([r['method'] for r in server.messages], ['initialize', 'initialized', 'thread/loaded/list', 'thread/read'])

    async def test_config_and_invalid_parent_fail_before_connecting(self):
        self.assertFalse(settings(None)['enabled'])
        for config in [{'enabled': 1}, {'command': 'unrecognized'}, {'socket_path': 'relative'}, {'rpc_timeout_seconds': True}, {'retry_seconds': 0}, {'socket_path': '/tmp/s', 'websocket_url': 'ws://127.0.0.1:4500'}, {'websocket_url': 'ws://192.168.1.1:4500'}]:
            with self.assertRaises(BridgeError): settings(config)
        class MustNotConnect(AppServerClient):
            async def open(self): raise AssertionError('Unexpected connection')
        client = MustNotConnect(settings(None))
        with self.assertRaisesRegex(DeliveryUnavailable, 'invalid_parent_thread_id'):
            await client.deliver('parent-name', EVENT, lambda: self.fail('submitted'))
        with self.assertRaisesRegex(DeliveryUnavailable, 'invalid_parent_thread_id'):
            await client.probe('parent-name')


class BridgeNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); self.delivery = FakeDelivery(); self.desktop = MockDesktop()
        self.config = self.root/'config.json'
        self.config.write_text(json.dumps({'runtime_root': 'run/bridge', 'link_file': 'run/desktop-host/link.json', 'codex_notifications': {'enabled': True, 'retry_seconds': .01}}))
        self.bridge = DesktopBridge(self.config, self.desktop, self.delivery)
    async def asyncTearDown(self): await self.bridge.shutdown(); self.temp.cleanup()
    def configure_notifications(self, enabled, **extra):
        value = json.loads(self.config.read_text())
        value['codex_notifications'] = {'enabled': enabled, 'retry_seconds': .01, **extra}
        self.config.write_text(json.dumps(value))

    async def test_reused_conversation_binds_each_job_to_its_actual_submitting_chat(self):
        sid = (await self.bridge.tool('dsh_new', {'parent_thread_id': PARENT}))['sessionId']
        first = await self.bridge.tool('dsh_prompt', {'session_id': sid, 'prompt': 'first parent'})
        await asyncio.wait_for(self.bridge.tasks[first['job_id']], 1)
        second = await self.bridge.tool('dsh_prompt', {'session_id': sid, 'prompt': 'different submitting chat', 'parent_thread_id': OTHER})
        await asyncio.wait_for(self.bridge.tasks[second['job_id']], 1)
        await self.bridge.notifications.dispatch_once()
        session = self.bridge.state['sessions'][sid]
        self.assertEqual(session['parent'], PARENT)
        for jid, parent in [(first['job_id'], PARENT), (second['job_id'], OTHER)]:
            job = session['jobs'][jid]
            self.assertEqual(job['parent_thread_id'], parent)
            self.assertEqual(job['completion_notification']['parent_thread_id'], parent)
            self.assertIn((parent, {'session_id': sid, 'job_id': jid, 'state': 'completed'}), self.delivery.calls)
        self.assertEqual((await self.bridge.tool('dsh_list', {'parent_thread_id': OTHER}))['sessions'][0]['sessionId'], sid)
        project_id = self.bridge.store.index['sessions'][sid]
        saved = json.loads(self.bridge.store.project_file(project_id).read_text())['sessions'][sid]
        self.assertEqual(saved['jobs'], session['jobs'])

    async def test_idle_refresh_toggles_same_notifier_without_history_replay_or_duplicate_worker(self):
        self.configure_notifications(False)
        await self.bridge.tool('dsh_list', {})
        notifier = self.bridge.notifications
        sid = (await self.bridge.tool('dsh_new', {'parent_thread_id': PARENT}))['sessionId']
        job = await self.bridge.prompt(sid, 'historical unarmed answer')
        await asyncio.wait_for(self.bridge.tasks[job['job_id']], 1)
        self.configure_notifications(True)
        diagnostics = (await self.bridge.tool('dsh_list', {}))['completion_notifications']
        self.assertTrue(diagnostics['enabled'])
        worker = notifier.worker
        await self.bridge.tool('dsh_list', {})
        self.assertIs(notifier, self.bridge.notifications)
        self.assertIs(worker, notifier.worker)
        await notifier.dispatch_once()
        self.assertEqual(self.delivery.calls, [])
        self.assertNotIn('completion_notification', self.bridge.state['sessions'][sid]['jobs'][job['job_id']])
        self.configure_notifications(False)
        self.assertFalse((await self.bridge.tool('dsh_list', {}))['completion_notifications']['enabled'])
        self.assertIsNone(notifier.worker)
        self.assertTrue(worker.done())
        self.assertFalse(notifier.closed)

    async def test_cancelled_readonly_refresh_restores_old_worker_and_shutdown_does_not_restart_it(self):
        notifier = self.bridge.notifications
        await self.bridge.tool('dsh_list', {})
        for close_concurrently in (False, True):
            started = asyncio.Event(); stopping = asyncio.Event(); release = asyncio.Event()
            if notifier.worker is not None:
                notifier.worker.cancel(); await asyncio.gather(notifier.worker, return_exceptions=True)
            async def slow_worker():
                started.set()
                try: await asyncio.Event().wait()
                finally:
                    stopping.set(); await release.wait()
            old_worker = notifier.worker = asyncio.create_task(slow_worker())
            await started.wait()
            self.configure_notifications(False)
            refresh = asyncio.create_task(self.bridge.tool('dsh_list', {}))
            await stopping.wait()
            closing = asyncio.create_task(notifier.close()) if close_concurrently else None
            refresh.cancel()
            with self.assertRaises(asyncio.CancelledError): await refresh
            if closing: await closing
            self.assertTrue(old_worker.done())
            self.assertTrue(notifier.enabled)
            if close_concurrently:
                self.assertIsNone(notifier.worker)
            else:
                self.assertIsNotNone(notifier.worker)
                self.assertIsNot(notifier.worker, old_worker)
                self.assertFalse(notifier.worker.done())
                self.configure_notifications(True)
                restored = notifier.worker
                await self.bridge.tool('dsh_list', {})
                self.assertIs(notifier.worker, restored)

    async def test_sending_marker_refuses_refresh_and_preserves_old_settings(self):
        sid = (await self.bridge.tool('dsh_new', {'parent_thread_id': PARENT}))['sessionId']
        for marker_state in ('sending',):
            self.bridge.state['sessions'][sid]['jobs']['old'] = {'job_id': 'old', 'state': 'running',
                'completion_notification': {'state': marker_state, 'parent_thread_id': PARENT}}
            self.configure_notifications(False)
            diagnostics = (await self.bridge.tool('dsh_list', {}))['completion_notifications']
            self.assertTrue(diagnostics['enabled'])
            self.assertEqual(diagnostics['error'], 'notification_work_unsettled')
            self.assertEqual(self.bridge.state['sessions'][sid]['jobs']['old']['completion_notification']['state'], marker_state)
        self.assertFalse(any(item['action'] == 'cancel' for item in self.desktop.calls))

    async def test_invalid_or_nonnotification_edit_retains_runtime_without_leaking_data(self):
        self.config.write_text('malformed secret-content')
        diagnostics = (await self.bridge.tool('dsh_list', {}))['completion_notifications']
        self.assertEqual(diagnostics['error'], 'notification_config_invalid')
        self.assertNotIn('secret-content', json.dumps(diagnostics))
        self.config.write_text(json.dumps({'runtime_root': '.runtime/other', 'codex_notifications': {'enabled': False}}))
        diagnostics = (await self.bridge.tool('dsh_list', {}))['completion_notifications']
        self.assertEqual(diagnostics['error'], 'notification_nonnotification_config_changed')
        self.assertTrue(diagnostics['enabled'])
        self.assertEqual(self.bridge.root, self.root.resolve()/'run/bridge')

    async def test_shutdown_leaves_historical_uncertain_untouched_and_cancels_current_task(self):
        historical = (await self.bridge.tool('dsh_new', {'parent_thread_id': PARENT}))['sessionId']
        jobs = self.bridge.state['sessions'][historical]['jobs']
        jobs['old'] = {'job_id': 'old', 'state': 'cancel_unconfirmed', 'result': 'original'}
        before = copy.deepcopy(jobs)
        active = (await self.bridge.tool('dsh_new', {'parent_thread_id': PARENT}))['sessionId']
        self.desktop.stall = True
        task = await self.bridge.prompt(active, 'current admitted task')
        while not self.desktop.sessions[active]: await asyncio.sleep(.001)
        self.bridge.grace = .01
        await self.bridge.shutdown()
        self.assertEqual(jobs, before)
        self.assertEqual([item['session_id'] for item in self.desktop.calls if item['action'] == 'cancel'], [active])
        self.assertNotIn(task['job_id'], self.bridge.tasks)
    async def test_compact_proxy_admission_keeps_armed_notification_and_verbose_metadata(self):
        bridge = self.bridge
        class RawController:
            async def call(inner, request):
                self.assertNotIn('verbose', request['arguments'])
                value = await bridge.tool(request['name'], request['arguments'])
                return {'content': [{'type': 'text', 'text': json.dumps(value)}]}
        client = DesktopClient(self.config, RawController())
        for verbose in (False, True):
            created = await client.tool('dsh_new', {'parent_thread_id': PARENT})
            self.assertEqual(set(created), {'session_id'})
            sid = created['session_id']
            args = {'session_id': sid, 'prompt': 'synthetic notification task', 'verbose': verbose}
            admitted = await client.tool('dsh_prompt', args)
            if verbose:
                self.assertEqual(admitted['completion_notification'], 'armed')
                self.assertEqual(admitted['state'], 'preparing')
            else:
                self.assertEqual(set(admitted), {'session_id', 'job_id'})
            stored = bridge.state['sessions'][sid]['jobs'][admitted['job_id']]
            self.assertEqual(stored['completion_notification']['state'], 'armed')
            task = bridge.tasks[admitted['job_id']]
            await asyncio.wait_for(task, 1)
            await bridge.notifications.dispatch_once()
            self.assertEqual(stored['completion_notification']['state'], 'delivered')
            self.assertIn((PARENT, {'session_id': sid, 'job_id': admitted['job_id'], 'state': 'completed'}), self.delivery.calls)

    async def test_follow_completion_sends_notification_without_parent_status_poll(self):
        sid = (await self.bridge.tool('dsh_new', {'parent_thread_id': PARENT}))['sessionId']
        job = await self.bridge.tool('dsh_prompt', {'session_id': sid, 'prompt': 'original prompt with spaces'})
        self.assertEqual(job['completion_notification'], 'armed')
        await asyncio.wait_for(self.delivery.entered.wait(), 1)
        self.assertEqual(self.delivery.calls, [(PARENT, {'session_id': sid, 'job_id': job['job_id'], 'state': 'completed'})])
        stored = self.bridge.state['sessions'][sid]['jobs'][job['job_id']]
        self.assertEqual(stored['result'], 'answer-1'); self.assertEqual(stored['completion_notification']['state'], 'delivered')
        self.assertEqual(self.desktop.sessions[sid], ['original prompt with spaces'])
        raw = await self.bridge.tool('dsh_status', {'session_id': sid, 'job_id': job['job_id']})
        self.assertEqual(raw['job']['completion_notification']['state'], 'delivered')
        self.assertEqual(compact_status(raw), {'session_id': sid, 'job_id': job['job_id'], 'result': 'answer-1'})
    async def test_restarting_preserves_delivery_receipt_and_does_not_notify_history(self):
        sid = (await self.bridge.tool('dsh_new', {'parent_thread_id': PARENT}))['sessionId']
        job = await self.bridge.prompt(sid, 'one'); await asyncio.wait_for(self.delivery.entered.wait(), 1)
        await self.bridge.shutdown(); self.bridge = DesktopBridge(self.config, self.desktop, self.delivery)
        await self.bridge.tool('dsh_list', {}); await self.bridge.notifications.dispatch_once()
        self.assertEqual(len(self.delivery.calls), 1)
        self.assertEqual(self.bridge.state['sessions'][sid]['jobs'][job['job_id']]['completion_notification']['state'], 'delivered')
