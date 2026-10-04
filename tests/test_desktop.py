import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from src.desktop_bridge import DesktopBridge, DesktopHttp, DesktopAdmissionRejected, TOOLS, INSTRUCTIONS
from src.mcp_common import BridgeError, McpServer


class MockDesktop:
    def __init__(self):
        self.parents = {}; self.sessions = {}; self.queues = {}; self.calls = []; self.stall = False; self.answer = None; self.receipts = {}
    async def call(self, value):
        self.calls.append(value)
        action = value['action']
        if action == 'new':
            parent = value['parent_id']; workspace = self.parents.setdefault(parent, uuid.uuid4().hex)
            sid = uuid.uuid4().hex; self.sessions[sid] = []; self.queues[sid] = asyncio.Queue()
            receipt = {'sessionId': sid, 'workspaceId': workspace, 'parent': parent, 'childId': value['child_id'], 'title': value['name'], 'state': 'ready', 'cwd': '/mock-owned-workspace'}
            self.receipts[(parent, value['child_id'])] = receipt
            return receipt
        if action == 'lookup':
            receipt = self.receipts.get((value['parent_id'], value['child_id']))
            return {'state': 'ready' if receipt else 'not_found', 'conversation': receipt}
        sid = value['session_id']
        if sid not in self.sessions: raise BridgeError('Foreign session')
        if action == 'preflight': return {'capabilities': {'modelToolCount': 2, 'modelToolNames': ['bash', 'read_file'], 'presentation': 'native', 'toolExecution': 'native-policy', 'approvalPolicy': 'ask'}}
        if action == 'prompt':
            self.sessions[sid].append(value['prompt']); turn = len(self.sessions[sid]); queue = self.queues[sid]
            await queue.put({'type': 'event', 'event': {'seq': turn * 10, 'type': 'turn/start', 'data': {'turn': turn}}})
            await queue.put({'type': 'event', 'event': {'seq': turn * 10 + 1, 'type': 'user/message', 'data': {'source': {'rpcId': value['request_id']}}}})
            if not self.stall:
                await queue.put({'type': 'assistant-stream', 'frame': {'type': 'chunk', 'chunk': {'type': 'bridge/live-progress'}}})
                await queue.put({'type': 'event', 'event': {'seq': turn * 10 + 2, 'type': 'assistant/message', 'data': {'turn': turn, 'message': {'content': [{'type': 'reasoning', 'text': 'DSH-only reasoning'}, {'type': 'text', 'text': self.answer if self.answer is not None else 'answer-%s' % turn}]}}}})
                await queue.put({'type': 'event', 'event': {'seq': turn * 10 + 3, 'type': 'turn/end', 'data': {'turn': turn, 'reason': {'kind': 'completed'}}}})
            return {'accepted': True}
        if action == 'cancel': return {'accepted': True}
        raise BridgeError('Unsupported mock action')
    async def follow(self, sid):
        yield {'type': 'snapshot', 'header': {'id': sid}, 'cursor': len(self.sessions[sid]) * 10 + 3 if self.sessions[sid] else -1, 'records': []}
        while True: yield await self.queues[sid].get()


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); self.mock = MockDesktop()
        self.file = self.root / 'config.json'
        self.config = {'execution_enabled': True, 'runtime_root': 'run/bridge', 'link_file': 'run/desktop-host/link.json', 'rpc_timeout_seconds': 1, 'turn_timeout_seconds': 1, 'cancel_grace_seconds': .1}
        self.file.write_text(json.dumps(self.config)); self.bridge = DesktopBridge(self.file, self.mock)
    async def asyncTearDown(self):
        await self.bridge.shutdown(); self.temp.cleanup()
    async def new(self, parent='parent-a'): return (await self.bridge.tool('dsh_new', {'parent_thread_id': parent}))['sessionId']
    async def settled(self, sid, jid):
        for _ in range(200):
            job = (await self.bridge.tool('dsh_status', {'session_id': sid, 'job_id': jid}))['job']
            if job['state'] not in {'preparing', 'running', 'waiting_permission', 'cancelling'}: return job
            await asyncio.sleep(.01)
        self.fail('Mock turn did not settle')
    async def test_parent_workspace_and_child_identity(self):
        a, b, c = await self.new(), await self.new(), await self.new('parent-b')
        state = self.bridge.state['sessions']
        self.assertNotEqual(a, b); self.assertEqual(state[a]['workspaceId'], state[b]['workspaceId']); self.assertNotEqual(state[a]['workspaceId'], state[c]['workspaceId'])
        selected = await self.bridge.tool('dsh_list', {'parent_thread_id': 'parent-a'}); self.assertEqual(len(selected['sessions']), 2)
    async def test_multiturn_live_updates_and_answer_not_reasoning(self):
        sid = await self.new()
        for n in [1, 2]:
            job = await self.bridge.tool('dsh_prompt', {'session_id': sid, 'prompt': 'turn %s' % n})
            result = await self.settled(sid, job['job_id']); self.assertEqual(result['state'], 'completed'); self.assertEqual(result['result'], 'answer-%s' % n)
        self.assertEqual(self.mock.sessions[sid], ['turn 1', 'turn 2']); self.assertEqual(self.bridge.events[sid], 2)
    async def test_close_resume_preserves_desktop_context(self):
        sid = await self.new(); await self.bridge.tool('dsh_close', {'session_id': sid})
        with self.assertRaises(BridgeError): await self.bridge.prompt(sid, 'closed')
        await self.bridge.tool('dsh_resume', {'session_id': sid}); job = await self.bridge.prompt(sid, 'restored')
        self.assertEqual((await self.settled(sid, job['job_id']))['state'], 'completed')
        self.assertEqual(len(self.mock.sessions), 1)
    async def test_cancel_uncertainty_blocks_new_prompt_and_does_not_kill_host(self):
        self.mock.stall = True; sid = await self.new(); job = await self.bridge.prompt(sid, 'wait')
        await asyncio.sleep(.05); result = await self.bridge.cancel(sid)
        self.assertEqual(result['state'], 'cancel_unconfirmed')
        with self.assertRaises(BridgeError): await self.bridge.prompt(sid, 'race')
        self.assertTrue(any(call['action'] == 'cancel' for call in self.mock.calls))
        self.assertTrue(all(call['action'] in {'new', 'prompt', 'preflight', 'cancel'} for call in self.mock.calls))
    async def test_legacy_execution_flag_is_not_an_extra_permission_gate(self):
        self.bridge.config['execution_enabled'] = False
        sid = await self.new()
        self.assertIn(sid, self.mock.sessions)
        self.assertEqual((await self.bridge.tool('dsh_list', {}))['deprecated_config'], ['execution_enabled'])
    async def test_foreign_session_and_permission_tool_refused(self):
        for tool, args in [('dsh_status', {'session_id': 'original-desktop-chat'}), ('dsh_prompt', {'session_id': 'original-desktop-chat', 'prompt': 'x'}), ('dsh_permission', {'request_id': 'x', 'decision': 'allow_once'})]:
            with self.assertRaises(BridgeError): await self.bridge.tool(tool, args)
        self.assertEqual(self.mock.calls, [])
    async def test_second_adapter_refused(self):
        with self.assertRaises(BridgeError): DesktopBridge(self.file, self.mock)
    async def test_native_permission_wait_is_visible_and_never_answered_by_bridge(self):
        self.mock.stall = True; sid = await self.new(); job = await self.bridge.prompt(sid, 'native approval')
        while not self.mock.sessions[sid]: await asyncio.sleep(.01)
        q = self.mock.queues[sid]
        await q.put({'type': 'event', 'event': {'seq': 12, 'type': 'bridge/progress', 'data': {'eventType': 'approval/asked'}}})
        for _ in range(100):
            status = await self.bridge.tool('dsh_status', {'session_id': sid, 'verbose': True})
            if status['job']['state'] == 'waiting_permission': break
            await asyncio.sleep(.01)
        self.assertEqual(status['job']['state'], 'waiting_permission')
        self.assertEqual(status['job']['pending_permission_count'], 1)
        self.assertEqual(status['permission_view'], 'normal DSH desktop UI only')
        with self.assertRaises(BridgeError): await self.bridge.tool('dsh_permission', {'request_id': 'x', 'decision': 'allow_once'})
        # The fixture simulates the user responding in the native DSH UI.
        await q.put({'type': 'event', 'event': {'seq': 13, 'type': 'bridge/progress', 'data': {'eventType': 'approval/decided'}}})
        await q.put({'type': 'event', 'event': {'seq': 14, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}})
        result = await self.settled(sid, job['job_id'])
        self.assertEqual(result['state'], 'completed'); self.assertEqual(result['pending_permission_count'], 0)
        self.assertTrue(all(v['action'] in {'new', 'prompt', 'preflight'} for v in self.mock.calls))
        self.assertTrue(status['capabilities_current']); self.assertEqual(status['capabilities']['modelToolNames'], ['bash', 'read_file'])
    async def test_restart_keeps_parent_mapping_and_uncertainty(self):
        sid = await self.new(); self.bridge.state['sessions'][sid]['jobs']['old'] = {'state': 'running', 'result': ''}; self.bridge.save()
        await self.bridge.shutdown(); self.bridge = DesktopBridge(self.file, self.mock)
        self.assertEqual(self.bridge.state['sessions'][sid]['jobs']['old']['state'], 'interrupted_unconfirmed')
        with self.assertRaises(BridgeError): await self.bridge.prompt(sid, 'resend')
    async def test_chunked_http_decoder_and_capability_boundary(self):
        reader = asyncio.StreamReader(); reader.feed_data(b'3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n'); reader.feed_eof()
        http = DesktopHttp(self.root / 'link.json', 1)
        self.assertEqual(b''.join([part async for part in http.parts(reader, {'transfer-encoding': 'chunked'})]), b'abcde')
        http.file.write_text(json.dumps({'url': 'https://example.com/codex-dsh-subagent', 'token': 'a' * 43})); http.file.chmod(0o600)
        with self.assertRaises(BridgeError): http.link()
    async def test_mcp_desktop_schema_and_normal_admission(self):
        self.bridge.config['execution_enabled'] = False; reader = asyncio.StreamReader(); outputs = []
        async def write(value): outputs.append(value)
        server = McpServer(self.bridge, reader, write, TOOLS, INSTRUCTIONS)
        await server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}})
        await server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        await server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'dsh_new', 'arguments': {'parent_thread_id': 'parent-a'}}})
        self.assertEqual(len(outputs[1]['result']['tools']), 7); self.assertFalse(outputs[2]['result']['isError'])
        self.assertEqual(self.mock.calls[0]['action'], 'new')

    async def test_more_than_128_sessions_and_16_turns_keep_history(self):
        for index in range(128):
            self.bridge.state['sessions']['old-' + str(index)] = {'jobs': {}, 'parent': 'other', 'state': 'closed'}
        sid = await self.new()
        first = None
        for index in range(17):
            job = await self.bridge.prompt(sid, 'turn ' + str(index))
            first = first or job['job_id']
            self.assertEqual((await self.settled(sid, job['job_id']))['state'], 'completed')
        self.assertEqual(len(self.bridge.state['sessions']), 129)
        self.assertEqual(len(self.bridge.state['sessions'][sid]['jobs']), 17)
        self.assertEqual((await self.bridge.tool('dsh_status', {'session_id': sid, 'job_id': first}))['job']['result'], 'answer-1')

    async def test_long_prompt_and_large_result_are_not_silently_cut(self):
        sid = await self.new(); self.mock.answer = 'x' * 300000
        jid = (await self.bridge.prompt(sid, 'p' * 40000))['job_id']
        result = await self.settled(sid, jid)
        self.assertEqual(result['result_total_chars'], 300000); self.assertFalse(result['result_truncated'])
        self.assertEqual(result['result'], self.mock.answer)
        page = (await self.bridge.tool('dsh_status', {'session_id': sid, 'job_id': jid, 'result_offset': 270000, 'result_limit': 50000}))['job']
        self.assertEqual(len(page['result']), 30000); self.assertEqual(page['next_result_offset'], 300000); self.assertTrue(page['result_complete'])
        self.assertEqual(len(self.mock.sessions[sid][0]), 40000)

    async def test_optional_result_limit_reports_truncation(self):
        sid = await self.new(); self.bridge.max_result_chars = 10; self.mock.answer = 'x' * 30
        jid = (await self.bridge.prompt(sid, 'bounded by operator configuration'))['job_id']
        job = await self.settled(sid, jid)
        self.assertEqual(job['result'], 'x' * 10); self.assertTrue(job['result_truncated']); self.assertEqual(job['result_total_chars'], 30)

    async def test_no_deadline_and_explicit_turn_deadline(self):
        self.mock.stall = True; sid = await self.new(); self.bridge.turn_timeout = None
        job = await self.bridge.prompt(sid, 'wait for native approval')
        await asyncio.sleep(.04)
        self.assertIsNone(self.bridge.state['sessions'][sid]['jobs'][job['job_id']]['timeout_seconds'])
        self.assertFalse(any(call['action'] == 'cancel' for call in self.mock.calls))
        other = await self.new('parent-b'); jid = (await self.bridge.prompt(other, 'operator deadline', .02))['job_id']
        result = await self.settled(other, jid)
        self.assertEqual(result['state'], 'cancel_unconfirmed'); self.assertEqual(result['error'], 'Turn deadline exceeded')

    async def test_project_directory_is_explicit_and_parent_grouping_is_preserved(self):
        sid = (await self.bridge.tool('dsh_new', {'parent_thread_id': 'parent-a', 'name': 'n' * 120, 'working_directory': '/mock/project'}))['sessionId']
        self.assertIn(sid, self.mock.sessions)
        self.assertEqual(self.mock.calls[0]['working_directory'], '/mock/project')
        with self.assertRaises(BridgeError): await self.bridge.new('parent-a', 'bad', 'relative/path')

    async def test_boolean_nan_and_oversize_page_are_rejected(self):
        sid = await self.new()
        for timeout in [True, -1, float('nan')]:
            with self.assertRaises(BridgeError): await self.bridge.prompt(sid, 'bad timeout', timeout)
        for args in [{'result_offset': True}, {'result_limit': self.bridge.max_frame_bytes}, {'result_offset': -1}]:
            with self.assertRaises(BridgeError): await self.bridge.tool('dsh_status', {'session_id': sid, **args})

    async def test_preparing_cancel_and_close_never_admit_a_turn(self):
        for method in ['dsh_cancel', 'dsh_close']:
            gate = asyncio.Event()
            class DelayedSnapshot(MockDesktop):
                async def follow(inner, sid):
                    await gate.wait()
                    async for item in super().follow(sid): yield item
            self.mock = DelayedSnapshot(); self.bridge.transport = self.mock
            sid = await self.new(); jid = (await self.bridge.prompt(sid, 'cancel before admission'))['job_id']
            await asyncio.sleep(0)  # observer is waiting for the opening snapshot
            await self.bridge.tool(method, {'session_id': sid})
            gate.set(); await asyncio.sleep(0)
            job = self.bridge.state['sessions'][sid]['jobs'][jid]
            self.assertEqual(job['state'], 'cancelled'); self.assertFalse(job['submitted'])
            self.assertEqual(self.mock.sessions[sid], [])
            self.assertFalse(any(value['action'] in {'prompt', 'cancel'} for value in self.mock.calls))

    async def test_explicit_busy_refusal_never_cancels_native_work_or_blocks_retry(self):
        class BusyDesktop(MockDesktop):
            busy = True
            async def call(inner, value):
                if value['action'] == 'prompt' and inner.busy:
                    inner.calls.append(value)
                    raise DesktopAdmissionRejected('Conversation has running/queued work')
                return await super().call(value)
        self.mock = BusyDesktop(); self.bridge.transport = self.mock
        sid = await self.new(); jid = (await self.bridge.prompt(sid, 'busy'))['job_id']
        job = await self.settled(sid, jid)
        self.assertEqual(job['state'], 'failed'); self.assertFalse(job['submitted'])
        self.assertEqual(job['admission_state'], 'rejected')
        self.assertEqual([value['action'] for value in self.mock.calls], ['new', 'prompt'])
        self.mock.busy = False
        next_jid = (await self.bridge.prompt(sid, 'after user work ends'))['job_id']
        self.assertEqual((await self.settled(sid, next_jid))['state'], 'completed')

    async def test_cancel_during_inflight_admission_is_checked_again_after_acceptance(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class InflightDesktop(MockDesktop):
            async def call(inner, value):
                if value['action'] == 'prompt':
                    entered.set(); await release.wait()
                if value['action'] == 'cancel':
                    inner.calls.append(value)
                    if not inner.sessions[value['session_id']]: return {'cancel_requested': False}
                    await inner.queues[value['session_id']].put({'type': 'event', 'event': {'seq': 13, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'cancelled'}}}})
                    return {'cancel_requested': True}
                return await super().call(value)
        self.mock = InflightDesktop(); self.mock.stall = True; self.bridge.transport = self.mock
        sid = await self.new(); jid = (await self.bridge.prompt(sid, 'inflight'))['job_id']
        await entered.wait(); cancelling = asyncio.create_task(self.bridge.cancel(sid))
        await asyncio.sleep(0); release.set(); await cancelling
        job = await self.settled(sid, jid)
        self.assertEqual(job['state'], 'cancelled'); self.assertTrue(job['native_cancel_requested'])
        self.assertEqual(self.mock.sessions[sid], ['inflight'])
        cancels = [value for value in self.mock.calls if value['action'] == 'cancel']
        self.assertEqual(len(cancels), 2)
        self.assertTrue(all(value['request_id'] == jid for value in cancels))

    async def test_unmatched_cancel_does_not_mislabel_natural_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class CompletedBeforeCancel(MockDesktop):
            async def call(inner, value):
                if value['action'] == 'prompt': entered.set(); await release.wait()
                if value['action'] == 'cancel':
                    inner.calls.append(value); return {'cancel_requested': False}
                return await super().call(value)
        self.mock = CompletedBeforeCancel(); self.bridge.transport = self.mock
        sid = await self.new(); jid = (await self.bridge.prompt(sid, 'finishes naturally'))['job_id']
        await entered.wait(); cancelling = asyncio.create_task(self.bridge.cancel(sid))
        await asyncio.sleep(0); release.set(); await cancelling
        job = await self.settled(sid, jid)
        self.assertEqual(job['state'], 'completed'); self.assertEqual(job['result'], 'answer-1')
        self.assertNotIn('native_cancel_requested', job)

    async def test_historical_uncertainty_without_submission_metadata_is_not_cleared_by_cancel(self):
        sid = await self.new()
        self.bridge.state['sessions'][sid]['jobs']['historical'] = {'job_id': 'historical', 'state': 'interrupted_unconfirmed', 'result': 'kept original'}
        self.bridge.save(); self.mock.calls.clear()
        result = await self.bridge.cancel(sid)
        self.assertFalse(result['cancel_requested']); self.assertEqual(result['state'], 'cancel_unconfirmed')
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(self.bridge.state['sessions'][sid]['jobs']['historical']['result'], 'kept original')
        with self.assertRaisesRegex(BridgeError, 'uncertain'): await self.bridge.prompt(sid, 'cannot replay')

    async def test_compact_status_reads_current_saved_answer_and_exact_historical_job_without_preflight(self):
        sid = await self.new(); first = (await self.bridge.prompt(sid, 'first'))['job_id']
        await self.settled(sid, first)
        second = (await self.bridge.prompt(sid, 'second'))['job_id']; await self.settled(sid, second)
        self.mock.calls.clear()
        old = await self.bridge.tool('dsh_status', {'session_id': sid, 'job_id': first})
        latest = await self.bridge.tool('dsh_status', {'session_id': sid, 'job_id': second})
        self.assertEqual(old['job']['result'], 'answer-1'); self.assertEqual(latest['job']['result'], 'answer-2')
        self.assertEqual(self.mock.calls, [])
        diagnosed = await self.bridge.tool('dsh_status', {'session_id': sid, 'job_id': second, 'verbose': True})
        self.assertEqual(diagnosed['job']['result'], latest['job']['result']); self.assertTrue(diagnosed['capabilities_current'])
        self.assertEqual([value['action'] for value in self.mock.calls], ['preflight'])

    async def test_lost_creation_receipt_is_recovered_after_restart_without_replaying_new(self):
        class LostReceipt(MockDesktop):
            async def call(inner, value):
                receipt = await super().call(value)
                if value['action'] == 'new': raise BridgeError('Desktop request failed; diagnostics withheld')
                return receipt
        self.mock = LostReceipt(); self.bridge.transport = self.mock
        with self.assertRaises(BridgeError): await self.bridge.new('parent-a', 'recover me')
        persisted = json.loads(self.bridge.file.read_text())
        self.assertEqual(len(persisted['creations']), 1); self.assertEqual(persisted['sessions'], {})
        await self.bridge.shutdown(); self.bridge = DesktopBridge(self.file, self.mock)
        listing = await self.bridge.tool('dsh_list', {'parent_thread_id': 'parent-a'})
        self.assertEqual(len(listing['sessions']), 1); self.assertEqual(listing['creation_intents'], [])
        sid = listing['sessions'][0]['sessionId']
        self.assertIn(sid, self.mock.sessions)
        self.assertEqual([value['action'] for value in self.mock.calls], ['new', 'lookup'])
        self.assertEqual(self.bridge.state['sessions'][sid]['jobs'], {})

    async def test_creation_recovery_does_not_import_a_mismatched_receipt(self):
        self.bridge.state['creations'] = {'child': {'creation_id': 'child', 'parent_thread_id': 'parent-a', 'state': 'uncertain'}}
        self.mock.receipts[('parent-a', 'child')] = {'sessionId': 'foreign', 'workspaceId': 'workspace', 'parent': 'other-parent', 'childId': 'child', 'state': 'ready'}
        listing = await self.bridge.tool('dsh_list', {})
        self.assertEqual(listing['sessions'], []); self.assertEqual(listing['creation_intents'][0]['error'], 'creation_reconciliation_unavailable')
        self.assertNotIn('foreign', self.bridge.state['sessions'])

    async def test_partial_native_creation_remains_unresolved_and_is_never_adopted(self):
        class PartialCreation(MockDesktop):
            async def call(inner, value):
                if value['action'] == 'lookup':
                    inner.calls.append(value)
                    return {'state': 'creating', 'conversation': {'sessionId': 'partial', 'workspaceId': 'workspace', 'parent': 'parent-a', 'childId': 'child', 'state': 'creating'}}
                return await super().call(value)
        self.mock = PartialCreation(); self.bridge.transport = self.mock
        self.bridge.state['creations'] = {'child': {'creation_id': 'child', 'parent_thread_id': 'parent-a', 'state': 'uncertain'}}
        listing = await self.bridge.tool('dsh_list', {})
        self.assertEqual(listing['sessions'], []); self.assertEqual(listing['creation_intents'][0]['state'], 'creating')
        self.assertNotIn('partial', self.bridge.state['sessions'])
        self.assertEqual([value['action'] for value in self.mock.calls], ['lookup'])
        with self.assertRaisesRegex(BridgeError, 'Unknown adapter-owned'): await self.bridge.tool('dsh_resume', {'session_id': 'partial'})

    async def test_history_disconnect_keeps_failure_and_does_not_report_completion(self):
        close = asyncio.Event()
        class Disconnected(MockDesktop):
            async def follow(inner, sid):
                yield {'type': 'snapshot', 'header': {'id': sid}, 'cursor': -1, 'records': []}
                await close.wait()
        self.mock = Disconnected(); self.mock.stall = True; self.bridge.transport = self.mock
        sid = await self.new(); jid = (await self.bridge.prompt(sid, 'disconnect'))['job_id']
        while not self.mock.sessions[sid]: await asyncio.sleep(0)
        close.set(); await self.bridge.tasks[jid]
        job = (await self.bridge.tool('dsh_status', {'session_id': sid, 'job_id': jid}))['job']
        self.assertEqual(job['state'], 'cancel_unconfirmed')
        self.assertEqual(job['error'], 'Desktop history disconnected before settlement')
        self.assertNotEqual(job['state'], 'completed')

    async def test_cancelling_delayed_admission_ack_does_not_overwrite_observed_terminal_state(self):
        release = asyncio.Event()
        class DelayedAck(MockDesktop):
            async def call(inner, value):
                receipt = await super().call(value)
                if value['action'] == 'prompt': await release.wait()
                return receipt
        self.mock = DelayedAck(); self.bridge.transport = self.mock
        sid = await self.new(); jid = (await self.bridge.prompt(sid, 'completed before ack'))['job_id']
        job = await self.settled(sid, jid)
        self.assertEqual(job['state'], 'completed'); self.assertEqual(job['result'], 'answer-1')
        task = self.bridge.tasks[jid]; self.assertFalse(task.done())
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        final = self.bridge.state['sessions'][sid]['jobs'][jid]
        self.assertEqual(final['state'], 'completed'); self.assertEqual(final['result'], 'answer-1')
        self.assertEqual(json.loads(self.bridge.store.project_file(self.bridge.store.index['sessions'][sid]).read_text())['sessions'][sid]['jobs'][jid]['state'], 'completed')
