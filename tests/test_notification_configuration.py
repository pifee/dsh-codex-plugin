"""Scoped config activation without live services or model requests."""
import json
from pathlib import Path
import tempfile
import unittest

from src.configure_notifications import configure
from src.mcp_common import BridgeError
from src.codex_notifications import DeliveryUnavailable

PARENT = '00000000-0000-4000-8000-000000000001'
URL = 'ws://127.0.0.1:4500'


class ReadOnlyParent:
    def __init__(self):
        self.metadata = {'parent_loaded': True, 'parent_state': 'idle', 'parent_model': 'synthetic-model'}
        self.error = None
        self.callback = None
        self.calls = []

    async def probe(self, parent):
        self.calls.append(parent)
        if self.callback: self.callback()
        if self.error: raise self.error
        return self.metadata


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.file = self.root / 'config/bridge.json'
        self.file.parent.mkdir()
        self.original = {'max_result_chars': 0, 'turn_timeout_seconds': 0}
        self.file.write_text(json.dumps(self.original, indent=2) + '\n')
        self.original_bytes = self.file.read_bytes()
        self.client = ReadOnlyParent()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def call(self, apply=True, expected_parent_model=None):
        return await configure(self.file, PARENT, URL, apply=apply, client=self.client,
                               expected_parent_model=expected_parent_model)

    async def test_dry_run_makes_no_files_and_only_probes_exact_parent(self):
        result = await self.call(False)
        self.assertTrue(result['ready'])
        self.assertFalse(result['config_applied'])
        self.assertEqual(self.file.read_bytes(), self.original_bytes)
        self.assertFalse((self.root / 'data').exists())
        self.assertFalse((self.root / 'run').exists())
        self.assertEqual(self.client.calls, [PARENT])

    async def test_apply_preserves_other_settings_and_exact_backup_without_reload(self):
        result = await self.call()
        updated = json.loads(self.file.read_text())
        self.assertEqual({k: v for k, v in updated.items() if k != 'codex_notifications'}, self.original)
        self.assertTrue(updated['codex_notifications']['enabled'])
        self.assertEqual(Path(result['backup_file']).read_bytes(), self.original_bytes)
        self.assertTrue(Path(result['backup_file']).is_relative_to(self.root / 'data/global/notification-backups'))
        self.assertEqual(Path(result['backup_file']).stat().st_mode & 0o777, 0o600)
        self.assertTrue(result['config_applied'])
        self.assertFalse(result['controller_reloaded'])
        self.assertEqual(result['completion_events_sent'], 0)

    async def test_absent_cloud_parent_or_wrong_model_leaves_config_untouched(self):
        self.client.error = DeliveryUnavailable('parent_not_loaded_in_this_server')
        self.assertFalse((await self.call())['ready'])
        self.client.error = None
        self.client.metadata['parent_model'] = 'other-model'
        self.assertEqual((await self.call(expected_parent_model='synthetic-model'))['error'], 'development_parent_model_mismatch')
        self.assertEqual(self.file.read_bytes(), self.original_bytes)
        self.assertFalse((self.root / 'data').exists())
        self.assertFalse((self.root / 'run').exists())

    async def test_active_dsh_work_does_not_block_configuration_change(self):
        self.write_project_state('waiting_permission')
        result = await self.call(False)
        self.assertTrue(result['ready'])
        self.assertEqual(result['bridge_job_states'], {'waiting_permission': 1})
        self.assertFalse(result['controller_reload_required'])

    async def test_explicit_model_override_is_scoped_and_requires_exact_match(self):
        self.client.metadata['parent_model'] = 'synthetic-model-override'
        result = await self.call(False, 'synthetic-model-override')
        self.assertTrue(result['ready'])
        self.assertEqual(result['expected_parent_model'], 'synthetic-model-override')
        default = await self.call(False)
        self.assertIsNone(default['expected_parent_model'])
        self.assertTrue(default['ready'])
        self.assertEqual(self.file.read_bytes(), self.original_bytes)
        self.assertFalse((self.root / 'data').exists())
        self.assertFalse((self.root / 'run').exists())

    async def test_model_override_does_not_bypass_loaded_or_direct_input_checks(self):
        self.client.metadata['parent_model'] = 'synthetic-model-override'
        self.client.metadata['parent_loaded'] = False
        self.assertEqual((await self.call(False, 'synthetic-model-override'))['error'], 'parent_not_loaded_in_this_server')
        self.client.metadata['parent_loaded'] = True
        self.client.error = DeliveryUnavailable('parent_cannot_accept_input')
        self.assertEqual((await self.call(False, 'synthetic-model-override'))['error'], 'parent_cannot_accept_input')
        self.assertEqual(self.file.read_bytes(), self.original_bytes)
        self.assertFalse((self.root / 'data').exists())
        self.assertFalse((self.root / 'run').exists())

    async def test_disable_works_without_server_and_reports_refresh_readback(self):
        self.client.error = DeliveryUnavailable('connection_unavailable')
        result = await configure(self.file, None, URL, apply=True, client=self.client, enabled=False)
        self.assertTrue(result['ready']); self.assertEqual(self.client.calls, [])
        self.assertFalse(json.loads(self.file.read_text())['codex_notifications']['enabled'])
        self.assertFalse(result['controller_reload_required'])
        self.assertTrue(result['runtime_refresh_required'])
        self.assertIn('dsh_list', result['runtime_readback'])

    def write_project_state(self, state):
        project_id = 'a' * 64
        global_data = self.root / 'data/global'; global_data.mkdir(parents=True, mode=0o700)
        project = self.root / 'data/projects' / project_id; project.mkdir(parents=True, mode=0o700)
        index = {'schema_version': 1, 'projects': {project_id: {'key': 'parent:' + PARENT, 'cwd': None}},
                 'sessions': {'owned': project_id}, 'creations': {}}
        (global_data / 'project-index.json').write_text(json.dumps(index))
        (project / 'state.json').write_text(json.dumps({'schema_version': 1, 'project_id': project_id,
            'sessions': {'owned': {'jobs': {'job': {'state': state}}}}}))

    async def test_indexed_store_count_is_read_only(self):
        self.write_project_state('running')
        files = {path: path.read_bytes() for path in (self.root / 'data').rglob('*') if path.is_file()}
        result = await self.call(False)
        self.assertTrue(result['ready']); self.assertEqual(result['bridge_job_states'], {'running': 1})
        self.assertEqual({path: path.read_bytes() for path in files}, files)
        self.assertFalse((self.root / 'run').exists())
        self.assertFalse((self.root / 'data/global/notification-backups').exists())

    async def test_concurrent_config_change_is_not_overwritten(self):
        replacement = b'{"max_result_chars": 17}'
        self.client.callback = lambda: self.file.write_bytes(replacement)
        with self.assertRaisesRegex(BridgeError, 'changed during verification'):
            await self.call()
        self.assertEqual(self.file.read_bytes(), replacement)
