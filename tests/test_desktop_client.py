import copy
import json
from pathlib import Path
import tempfile
import unittest
from src.mcp_common import BridgeError
from src.desktop_client import DesktopClient, compact_status


def status(answer='DS answer.\nDS answer.', state='completed'):
    return {'session_id': 'owned', 'workspace_id': 'workspace',
        'job': {'job_id': 'job', 'state': state, 'result': answer, 'result_total_chars': len(answer), 'next_result_offset': len(answer), 'result_complete': True, 'result_truncated': False, 'submitted': True, 'started_at': 1, 'finished_at': 2, 'timeout_seconds': None, 'stop_reason': 'completed'},
        'capabilities': {'modelToolNames': ['tool_' + str(i) for i in range(27)], 'availableToolNames': ['tool_' + str(i) for i in range(27)], 'modelToolCount': 27, 'approvalPolicy': 'ask'},
        'capabilities_current': True, 'selection': {'provider': 'native-provider', 'model': 'native-model', 'reasoningEffort': 'low'},
        'host_policy': 'native-dsh', 'live_update_count': 30, 'legacy_inference_approved': None, 'reasoning_view': 'normal DSH desktop UI only', 'permission_view': 'normal DSH desktop UI only'}


class StoredAnswerTransport:
    def __init__(self, answer): self.answer = answer; self.calls = []; self.state = 'completed'; self.error = None
    async def call(self, value):
        self.calls.append(copy.deepcopy(value)); args = value['arguments']
        offset = args.get('result_offset', 0); limit = args.get('result_limit', 8192)
        result = status(self.answer, self.state)
        if self.error: result['job']['error'] = self.error
        result['job'].update(result=self.answer[offset:offset+limit], next_result_offset=min(len(self.answer), offset+limit), result_complete=offset+limit >= len(self.answer))
        return {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}


class CompactStatusTests(unittest.TestCase):
    def test_answer_spaces_unicode_and_repeated_paragraphs_are_unchanged(self):
        answer = '  两个  空格\n```python\na = 1\n```\nrepeat\nrepeat\tend  '
        original = status(answer); before = copy.deepcopy(original)
        result = compact_status(original)
        self.assertEqual(result['result'].encode(), answer.encode())
        self.assertEqual(original, before)
        self.assertEqual(set(result), {'session_id', 'job_id', 'result'})
    def test_state_and_error_are_omitted_without_changing_original(self):
        for state in ['running', 'waiting_permission', 'cancel_unconfirmed', 'interrupted_unconfirmed', 'failed', 'cancelled']:
            raw = status('kept', state)
            raw['job'].update(error='Native approval is pending', pending_permission_count=2, cancel_requested=True, result_truncated=True)
            before = copy.deepcopy(raw)
            self.assertEqual(compact_status(raw), {'session_id': 'owned', 'job_id': 'job', 'result': 'kept'})
            self.assertEqual(raw, before)
    def test_connection_and_legacy_metadata_are_only_in_full_status(self):
        raw = status(); raw.update(host_policy='unreachable', capabilities_current=False, legacy_inference_approved=False)
        self.assertEqual(set(compact_status(raw)), {'session_id', 'job_id', 'result'})
    def test_no_job_and_older_partial_page_keep_minimal_contract(self):
        raw = status(); raw['job'] = None
        self.assertEqual(compact_status(raw), {'session_id': 'owned', 'job_id': None, 'result': None})
        raw = status('old page'); del raw['job']['result_total_chars']; raw['job']['result_complete'] = False
        partial = compact_status(raw)
        self.assertEqual(partial, {'session_id': 'owned', 'job_id': 'job', 'result': 'old page'})
        raw['job']['result_complete'] = True
        self.assertEqual(compact_status(raw), partial)


class ClientStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.config = Path(self.temp.name)/'config.json'
        self.config.write_text(json.dumps({'link_file': 'run/desktop-host/link.json'}))
        self.transport = StoredAnswerTransport(('  Unicode 中文 😀\nrepeat\nrepeat\n') * 40000)
        self.client = DesktopClient(self.config, self.transport)
    async def asyncTearDown(self): await self.client.shutdown(); self.temp.cleanup()
    async def test_default_million_is_explicit_for_old_controller_and_verbose_pages_reconstruct_answer(self):
        args = {'session_id': 'owned', 'job_id': 'job'}
        compact = await self.client.tool('dsh_status', args)
        self.assertGreater(len(self.transport.answer), 1000000)
        self.assertEqual(compact['result'], self.transport.answer[:1000000])
        self.assertEqual(set(compact), {'session_id', 'job_id', 'result'})
        self.assertEqual(self.transport.calls[-1]['arguments']['result_limit'], 1000000)
        self.assertEqual(args, {'session_id': 'owned', 'job_id': 'job'})
        args = dict(args, verbose=True); parts = []
        while True:
            result = await self.client.tool('dsh_status', args); parts.append(result['job']['result'])
            if len(parts) == 1: self.assertEqual(len(result['job']['result']), 1000000)
            self.assertEqual(self.transport.calls[-1]['arguments']['result_limit'], 1000000)
            self.assertEqual(result['job']['result_total_chars'], len(self.transport.answer))
            if result['job']['result_complete']: break
            args = dict(args, result_offset=result['job']['next_result_offset'])
        self.assertEqual(''.join(parts), self.transport.answer)
    async def test_verbose_preserves_full_response_and_old_controller_compatibility(self):
        args = {'session_id': 'owned', 'job_id': 'job', 'result_offset': 9, 'result_limit': 25, 'verbose': True}; before = copy.deepcopy(args)
        verbose = await self.client.tool('dsh_status', args)
        self.assertIn('capabilities', verbose); self.assertIn('started_at', verbose['job'])
        self.assertEqual(verbose['job']['state'], 'completed')
        self.assertEqual(verbose['job']['result'], self.transport.answer[9:34])
        self.assertEqual(args, before); self.assertTrue(self.transport.calls[-1]['arguments']['verbose'])
        compact = await self.client.tool('dsh_status', dict(args, verbose=False))
        self.assertEqual(compact['result'], verbose['job']['result'])
        self.assertEqual(set(compact), {'session_id', 'job_id', 'result'})
        self.assertEqual(verbose['job']['next_result_offset'], 34)
        self.assertFalse(verbose['job']['result_complete'])
        self.assertNotIn('verbose', self.transport.calls[-1]['arguments'])
    async def test_verbose_preserves_failure_state_and_error_while_compact_omits_both(self):
        self.transport.state = 'failed'; self.transport.error = 'Synthetic sanitized failure'
        args = {'session_id': 'owned', 'result_limit': 12}
        verbose = await self.client.tool('dsh_status', dict(args, verbose=True))
        compact = await self.client.tool('dsh_status', args)
        self.assertEqual(verbose['job']['state'], 'failed')
        self.assertEqual(verbose['job']['error'], self.transport.error)
        self.assertEqual(compact, {'session_id': 'owned', 'job_id': 'job', 'result': self.transport.answer[:12]})
    async def test_incremental_offset_does_not_rewrite_or_deduplicate_body(self):
        first = await self.client.tool('dsh_status', {'session_id': 'owned', 'result_limit': 12})
        second = await self.client.tool('dsh_status', {'session_id': 'owned', 'result_offset': 12, 'result_limit': 12})
        self.assertEqual(first['result']+second['result'], self.transport.answer[:24])
    async def test_verbose_requires_boolean_and_errors_stay_errors(self):
        for value in [1, 'true', None]:
            with self.assertRaises(BridgeError): await self.client.tool('dsh_status', {'session_id': 'owned', 'verbose': value})
        class Refused:
            async def call(self, _): return {'isError': True, 'content': [{'type': 'text', 'text': 'Unknown job'}]}
        self.client.transport = Refused()
        with self.assertRaisesRegex(BridgeError, 'Unknown job'): await self.client.tool('dsh_status', {'session_id': 'owned'})


class AdmissionFormattingTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_schema_is_independent_and_verbose_is_never_forwarded(self):
        from src.desktop_client import TOOLS, RAW_TOOLS
        raw = {item[0]: item[2] for item in RAW_TOOLS}
        external = {item[0]: item[2] for item in TOOLS}
        for name in ['dsh_new', 'dsh_prompt']:
            self.assertNotIn('verbose', raw[name]['properties'])
            self.assertEqual(external[name]['properties']['verbose']['default'], False)
        conversation = {'sessionId': 'owned', 'workspaceId': 'workspace', 'cwd': '/synthetic', 'capabilities': {'approvalPolicy': 'ask'}}
        job = {'session_id': 'owned', 'job_id': 'job', 'state': 'preparing', 'completion_notification': 'armed'}
        class LegacyTransport:
            async def call(inner, request):
                self.assertNotIn('verbose', request['arguments'])
                value = conversation if request['name'] == 'dsh_new' else job
                return {'content': [{'type': 'text', 'text': json.dumps(value)}]}
        with tempfile.TemporaryDirectory() as root:
            config = Path(root)/'config.json'; config.write_text(json.dumps({'link_file': 'run/desktop-host/link.json'}))
            client = DesktopClient(config, LegacyTransport())
            for name, args, full, compact in [
                ('dsh_new', {'parent_thread_id': 'parent'}, conversation, {'session_id': 'owned'}),
                ('dsh_prompt', {'session_id': 'owned', 'prompt': 'task'}, job, {'session_id': 'owned', 'job_id': 'job'})]:
                before = copy.deepcopy(args)
                self.assertEqual(await client.tool(name, args), compact)
                self.assertEqual(await client.tool(name, dict(args, verbose=False)), compact)
                self.assertEqual(await client.tool(name, dict(args, verbose=True)), full)
                self.assertEqual(args, before)
                for invalid in [1, 'true', None]:
                    with self.assertRaises(BridgeError): await client.tool(name, dict(args, verbose=invalid))

    async def test_compact_admission_rejects_invalid_identity_without_echoing_payload(self):
        class InvalidTransport:
            async def call(inner, request):
                return {'content': [{'type': 'text', 'text': json.dumps(inner.payload)}]}
        with tempfile.TemporaryDirectory() as root:
            config = Path(root)/'config.json'; config.write_text(json.dumps({'link_file': 'run/desktop-host/link.json'}))
            transport = InvalidTransport(); client = DesktopClient(config, transport)
            for invalid in [None, '', '  ', 1, {'secret': 'untrusted marker'}]:
                transport.payload = {'sessionId': invalid}
                with self.assertRaisesRegex(BridgeError, '^Invalid shared controller conversation$'):
                    await client.tool('dsh_new', {'parent_thread_id': 'parent'})
                for key in ['session_id', 'job_id']:
                    transport.payload = {'session_id': 'owned', 'job_id': 'job', key: invalid}
                    with self.assertRaisesRegex(BridgeError, '^Invalid shared controller job$'):
                        await client.tool('dsh_prompt', {'session_id': 'owned', 'prompt': 'task'})
