"""Installation tests use temporary homes only; no live registration or services."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_SPEC = importlib.util.spec_from_file_location('portable_installer', Path(__file__).resolve().parents[1] / 'src/install.py')
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'plugin with spaces'
        self.home = self.base / 'home'
        self.root.mkdir()
        self.profile = self.home / '.dsh/profiles/desktop'
        self.profile.mkdir(parents=True)
        for name, body in {
            'src/desktop_client.py': '# fixture client\n',
            'src/desktop_host/index.mjs': '// fixture host\n',
            'src/desktop_host/controller.mjs': '// fixture controller\n',
            'src/desktop_host/package.json': '{"name":"@local/codex-dsh-desktop-link"}',
            'skill/deepseek-subagent/SKILL.md': '---\nname: deepseek-subagent\n---\n',
            'config/bridge.example.json': '{"link_file":"run/desktop-host/link.json"}',
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.python = Path(sys.executable).resolve()

    def plan(self):
        return installer.plan_install(self.root, self.home, self.python)

    def apply(self):
        return installer.apply_install(self.root, self.plan())

    def test_check_never_writes(self):
        before = installer.fingerprint(self.base)
        self.assertEqual(installer.main(['--root', str(self.root), '--home', str(self.home), '--check']), 0)
        self.assertEqual(installer.fingerprint(self.base), before)

    def test_install_links_and_defaults_then_is_idempotent(self):
        backup = self.apply()
        self.assertEqual(self.plan(), [])
        self.assertEqual((self.profile / 'node_modules/@local/codex-dsh-desktop-link').resolve(), self.root / 'src/desktop_host')
        self.assertEqual((self.home / '.codex/skills/deepseek-subagent').resolve(), self.root / 'skill/deepseek-subagent')
        self.assertEqual((backup.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((backup / 'manifest.json').stat().st_mode & 0o777), 0o600)
        self.assertEqual(json.loads((self.root / 'config/bridge.json').read_text())['link_file'], 'run/desktop-host/link.json')

    def test_preserves_user_configuration_and_data(self):
        config = self.root / 'config/bridge.json'
        config.write_text('{"turn_timeout_seconds": 123, "user_setting": "keep"}\n')
        state = self.root / 'data/projects/project-a/state.json'
        state.parent.mkdir(parents=True)
        state.write_text('{"original":true}')
        self.apply()
        self.assertEqual(config.read_text(), '{"turn_timeout_seconds": 123, "user_setting": "keep"}\n')
        self.assertEqual(state.read_text(), '{"original":true}')

    def test_toml_changes_only_transport_fields(self):
        source = '''# top comment
model = "unchanged"
[mcp_servers.other]
command = "other"
[mcp_servers.dsh_subagent]
command = "/old/python" # keep comment
args = [
  "/old/client.py", # old client
  "--config", "/old/config.json",
]
cwd = '/old'
enabled = true
default_tools_approval_mode = "writes"
[mcp_servers.dsh_subagent.tools.dsh_prompt]
approval_mode = "prompt"
'''
        for fallback in (False, True):
            with mock.patch.object(installer, 'tomllib', None if fallback else installer.tomllib):
                result = installer.update_toml(source, self.root, self.python)
            self.assertIn('default_tools_approval_mode = "writes"', result)
            self.assertIn('approval_mode = "prompt"', result)
            self.assertIn('command = "other"', result)
            self.assertIn('# keep comment', result)
            self.assertIn('"--config"', result)
            self.assertNotIn('/old/config.json', result)
            self.assertIn(str(self.root), result)

    def test_toml_literal_hash_is_not_comment(self):
        source = '[mcp_servers.dsh_subagent]\ncommand = "/old #inside"\nargs = ["/old"]\ncwd = "/old"\n'
        result = installer.update_toml(source, self.root, self.python)
        self.assertNotIn('#inside', result)

    def test_unsupported_toml_target_is_rejected(self):
        with self.assertRaises(installer.InstallError):
            installer.update_toml('[mcp_servers."dsh_subagent"]\ncommand="old"\n', self.root, self.python)
        with mock.patch.object(installer, 'tomllib', None):
            with self.assertRaises(installer.InstallError):
                installer.update_toml('[mcp_servers.dsh_subagent]\nargs = { unusual = "format" }\n', self.root, self.python)

    def test_nested_yaml_preserves_model_native_permissions_and_other_fields(self):
        source = '''- id: agent-default-model
  config:
    model: deepseek-flash
- insert:
    - id: codex-dsh-desktop-link
      name: '@local/codex-dsh-desktop-link'
      config:
        enabled: true
        multiClient: true
        projectRoot: /old
        custom: keep
    - id: other
      disabled: !!js process.platform === 'win32'
'''
        result = installer.update_yaml(source, self.root)
        self.assertEqual(result.replace(json.dumps(str(self.root)), '/old'), source)
        self.assertIn('multiClient: true', result)

    def test_yaml_rejects_ambiguous_or_unrelated_target(self):
        block = '- id: codex-dsh-desktop-link\n  name: "other"\n  config:\n    projectRoot: /old\n'
        for source in (block, block + block, '{ codex-dsh-desktop-link: strange }'):
            with self.assertRaises(installer.InstallError):
                installer.update_yaml(source, self.root)

    def test_unrelated_existing_host_or_skill_is_protected(self):
        path = self.profile / 'node_modules/@local/codex-dsh-desktop-link'
        path.mkdir(parents=True)
        (path / 'index.mjs').write_text('unrelated')
        with self.assertRaises(installer.InstallError):
            self.plan()
        self.assertEqual((path / 'index.mjs').read_text(), 'unrelated')

    def test_existing_symlink_to_unrelated_source_is_protected(self):
        target = self.home / '.codex/skills/deepseek-subagent'
        target.parent.mkdir(parents=True)
        target.symlink_to(self.base)
        with self.assertRaises(installer.InstallError):
            self.plan()

    def test_concurrent_change_refuses_apply_without_backup(self):
        plan = self.plan()
        config = self.home / '.codex/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('model="changed"\n')
        with self.assertRaises(installer.InstallError):
            installer.apply_install(self.root, plan)
        self.assertFalse((self.root / 'data/global/installation-backups').exists())
        self.assertEqual(config.read_text(), 'model="changed"\n')

    def test_rollback_restores_exact_configuration(self):
        config = self.home / '.codex/config.toml'
        config.parent.mkdir(parents=True)
        original = 'model = "kept"\n'
        config.write_text(original)
        backup = self.apply()
        installer.rollback(backup)
        self.assertEqual(config.read_text(), original)
        self.assertFalse((self.root / 'config/bridge.json').exists())
        self.assertFalse((self.home / '.codex/skills/deepseek-subagent').exists())

    def test_rollback_refuses_modified_installation(self):
        backup = self.apply()
        config = self.home / '.codex/config.toml'
        config.write_text(config.read_text() + '\n# user change\n')
        with self.assertRaises(installer.InstallError):
            installer.rollback(backup)
        self.assertIn('# user change', config.read_text())

    def test_upgrade_and_rollback_preserve_owned_directory_modes(self):
        old = self.base / 'old-plugin'
        shutil.copytree(self.root, old)
        source = old / 'src/desktop_host'
        target = self.profile / 'node_modules/@local/codex-dsh-desktop-link'
        target.parent.mkdir(parents=True)
        shutil.copytree(source, target)
        os.chmod(target / 'index.mjs', 0o640)
        (self.profile / 'cordis.patch.yml').write_text(f'- id: codex-dsh-desktop-link\n  name: "@local/codex-dsh-desktop-link"\n  config:\n    projectRoot: {json.dumps(str(old))}\n')
        before = installer.fingerprint(target)
        backup = self.apply()
        self.assertTrue(target.is_symlink())
        installer.rollback(backup)
        self.assertEqual(installer.fingerprint(target), before)

    def test_yaml_scalar_payload_is_preserved_and_cannot_establish_ownership(self):
        for marker in ('|', '>', '!!js |', '|2-'):
            fake = ('- id: unrelated\n  config:\n    notes: ' + marker + '\n'
                    '      - insert:\n          - id: codex-dsh-desktop-link\n'
                    '            name: "@local/codex-dsh-desktop-link"\n'
                    '            config:\n              projectRoot: /synthetic/literal-root\n')
            with self.assertRaises(installer.InstallError):
                installer.update_yaml(fake, self.root)
            real = ('- id: codex-dsh-desktop-link\n  name: "@local/codex-dsh-desktop-link"\n'
                    '  config:\n    projectRoot: /synthetic/registered-root\n')
            source = fake + real
            updated = installer.update_yaml(source, self.root)
            self.assertEqual(updated[:len(fake)], fake)
            self.assertEqual(installer.old_project_root(source), Path('/synthetic/registered-root'))

    def test_post_write_foreign_edit_does_not_authorize_install_rollback(self):
        foreign_bytes = b'model="foreign concurrent edit"\n'
        config = self.home / '.codex/config.toml'
        real = installer.atomic_write
        def interfere(path, payload, mode=0o600):
            result = real(path, payload, mode)
            if path == config and not hasattr(interfere, 'changed'):
                interfere.changed = True
                path.write_bytes(foreign_bytes)
            return result
        with mock.patch.object(installer, 'atomic_write', side_effect=interfere):
            with self.assertRaisesRegex(installer.InstallError, 'rollback was blocked'):
                self.apply()
        self.assertEqual(config.read_bytes(), foreign_bytes)
        self.assertFalse((self.profile / 'cordis.patch.yml').exists())

    def test_post_symlink_foreign_link_does_not_authorize_install_rollback(self):
        target = self.profile / 'node_modules/@local/codex-dsh-desktop-link'
        foreign = self.base / 'foreign-host'
        real = Path.symlink_to
        def interfere(path, other, target_is_directory=False):
            result = real(path, other, target_is_directory=target_is_directory)
            if path == target and not hasattr(interfere, 'changed'):
                interfere.changed = True
                path.unlink()
                real(path, foreign, target_is_directory=True)
            return result
        with mock.patch.object(Path, 'symlink_to', autospec=True, side_effect=interfere):
            with self.assertRaisesRegex(installer.InstallError, 'rollback was blocked'):
                self.apply()
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), foreign)

    def test_partial_failure_restores_prior_changes(self):
        config = self.home / '.codex/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('model="before"\n')
        real = installer.atomic_write
        def fail_once(path, payload, mode=0o600):
            if path == self.profile / 'cordis.patch.yml' and not hasattr(fail_once, 'failed'):
                fail_once.failed = True
                raise OSError('synthetic no secrets')
            return real(path, payload, mode)
        with mock.patch.object(installer, 'atomic_write', side_effect=fail_once):
            with self.assertRaises(installer.InstallError):
                self.apply()
        self.assertEqual(config.read_text(), 'model="before"\n')

    def test_symlink_failure_restores_owned_old_entity(self):
        old = self.base / 'old-plugin'
        shutil.copytree(self.root, old)
        target = self.profile / 'node_modules/@local/codex-dsh-desktop-link'
        target.parent.mkdir(parents=True)
        shutil.copytree(old / 'src/desktop_host', target)
        (self.profile / 'cordis.patch.yml').write_text(f'- id: codex-dsh-desktop-link\n  name: "@local/codex-dsh-desktop-link"\n  config:\n    projectRoot: {json.dumps(str(old))}\n')
        before = installer.fingerprint(target)
        original = Path.symlink_to
        def fail_link(path, other, target_is_directory=False):
            if path == target:
                raise OSError('fixture symlink error')
            return original(path, other, target_is_directory=target_is_directory)
        with mock.patch.object(Path, 'symlink_to', autospec=True, side_effect=fail_link):
            with self.assertRaises(installer.InstallError):
                self.apply()
        self.assertEqual(installer.fingerprint(target), before)

    def test_empty_target_table_without_trailing_newline_is_supported(self):
        with mock.patch.object(installer, 'tomllib', None):
            result = installer.update_toml('[mcp_servers.dsh_subagent]', self.root, self.python)
        self.assertIn('[mcp_servers.dsh_subagent]\n', result)


if __name__ == '__main__':
    unittest.main()
