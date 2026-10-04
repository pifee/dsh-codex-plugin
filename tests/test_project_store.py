import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.layout import PackageLayout
from src.mcp_common import BridgeError
from src.project_store import ProjectStore


class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.layout = PackageLayout(self.root/'package'); self.layout.prepare()
    def tearDown(self): self.temp.cleanup()

    def test_canonical_directory_groups_multiple_parents_and_survives_restart(self):
        directory = self.root/'actual-project'; directory.mkdir()
        alias = self.root/'alias'; alias.symlink_to(directory, target_is_directory=True)
        store = ProjectStore(self.layout)
        first = store.register(str(directory), 'parent-a'); second = store.register(str(alias), 'parent-b')
        self.assertEqual(first, second)
        for sid, parent in [('one', 'parent-a'), ('two', 'parent-b')]:
            store.state['sessions'][sid] = {'parent': parent, 'cwd': str(directory), 'jobs': {}}
        store.save(); reopened = ProjectStore(self.layout)
        self.assertEqual(len(reopened.index['projects']), 1)
        self.assertEqual(reopened.index['sessions'], {'one': first, 'two': first})
        self.assertEqual(set(reopened.state['sessions']), {'one', 'two'})

    def test_parent_isolation_without_directory_and_project_identity_validation(self):
        store = ProjectStore(self.layout)
        first = store.register(None, 'parent-a'); second = store.register(None, 'parent-b')
        self.assertNotEqual(first, second)
        with self.assertRaises(BridgeError): store.register('/other-project', 'parent-b', first)
        with self.assertRaises(BridgeError): self.layout.project_directory('../escape')

    def test_global_index_never_contains_answers_and_project_commits_job_with_notification(self):
        store = ProjectStore(self.layout)
        original = {'job_id': 'job', 'state': 'completed', 'result': '  exact 中文\nrepeat\nrepeat  ',
                    'completion_notification': {'state': 'pending', 'parent_thread_id': 'actual-parent', 'event_id': 'owned:job'}}
        store.state['sessions']['owned'] = {'parent': 'actual-parent', 'cwd': '/project-one', 'jobs': {'job': copy.deepcopy(original)}}
        store.state['sessions']['other'] = {'parent': 'other-parent', 'cwd': '/project-two', 'jobs': {}}
        store.save()
        project_id = store.index['sessions']['owned']; file = store.project_file(project_id)
        self.assertEqual(json.loads(file.read_text())['sessions']['owned']['jobs']['job'], original)
        self.assertNotIn('exact 中文', self.layout.index_file.read_text())
        untouched = store.project_file(store.index['sessions']['other']); previous_mtime = untouched.stat().st_mtime_ns
        store.state['sessions']['owned']['jobs']['job']['completion_notification']['state'] = 'delivered'
        store.save()
        self.assertEqual(untouched.stat().st_mtime_ns, previous_mtime)
        saved = json.loads(file.read_text())['sessions']['owned']['jobs']['job']
        self.assertEqual(saved['result'], original['result']); self.assertEqual(saved['state'], 'completed')
        self.assertEqual(saved['completion_notification']['state'], 'delivered')

    def test_config_in_config_directory_resolves_package_root_not_process_workdir(self):
        config = self.layout.root/'config/bridge.json'
        self.assertEqual(PackageLayout.from_config(config).root, self.layout.root)
