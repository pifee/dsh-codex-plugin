"""Temporary-home uninstall fixtures; no installed DSH, live config or network."""
import fcntl
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from src import install as installer
from src import uninstall as uninstaller


class UninstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'plugin with spaces'
        self.home = self.base / 'home'
        self.profile = self.home / '.dsh/profiles/desktop'
        self.profile.mkdir(parents=True)
        for name, text in {
            'src/desktop_client.py': '# synthetic client\n',
            'src/desktop_host/index.mjs': '// synthetic host\n',
            'src/desktop_host/controller.mjs': '// synthetic controller\n',
            'src/desktop_host/package.json': '{"name":"@local/codex-dsh-desktop-link"}',
            'skill/deepseek-subagent/SKILL.md': 'synthetic skill\n',
            'config/bridge.example.json': '{"link_file":"run/desktop-host/link.json"}\n',
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.codex = self.home / '.codex/config.toml'
        self.yaml = self.profile / 'cordis.patch.yml'
        self.host_link = self.profile / 'node_modules/@local/codex-dsh-desktop-link'
        self.skill_link = self.home / '.codex/skills/deepseek-subagent'

    def install(self):
        return installer.apply_install(self.root, installer.plan_install(self.root, self.home, Path(sys.executable).resolve()))

    def plan(self):
        return uninstaller.plan_uninstall(self.root, self.home)

    def apply(self):
        return uninstaller.apply_uninstall(self.root, self.plan())

    def block(self, root=None, indent=0):
        body = (f'- id: {installer.HOST_ID}\n'
                f'  name: {json.dumps(installer.HOST_NAME)}\n'
                f'  config:\n'
                f'    projectRoot: {json.dumps(str(root or self.root))}\n'
                f'    enabled: true\n')
        return ''.join(' ' * indent + line for line in body.splitlines(keepends=True))

    def test_install_uninstall_install_round_trip(self):
        self.install()
        backup = self.apply()
        self.assertEqual(self.plan(), [])
        self.assertFalse(self.host_link.is_symlink())
        self.assertFalse(self.skill_link.is_symlink())
        self.assertNotIn('dsh_subagent', self.codex.read_text())
        self.assertNotIn(installer.HOST_ID, self.yaml.read_text())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup / 'manifest.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((backup / 'manifest.json').read_text())['status'], 'applied')
        self.install()
        self.assertEqual(installer.plan_install(self.root, self.home, Path(sys.executable).resolve()), [])
        self.assertTrue(self.host_link.is_symlink())

    def test_default_and_check_are_read_only(self):
        self.install()
        for mode in ([], ['--check'], ['--dry-run']):
            before = installer.fingerprint(self.base)
            with mock.patch('sys.stdout', new=io.StringIO()):
                result = uninstaller.main(['--root', str(self.root), '--home', str(self.home)] + mode)
            self.assertEqual(result, 0)
            self.assertEqual(installer.fingerprint(self.base), before)

    def test_missing_is_idempotent_and_creates_nothing(self):
        before = installer.fingerprint(self.base)
        self.assertEqual(self.plan(), [])
        self.assertIsNone(self.apply())
        self.assertEqual(installer.fingerprint(self.base), before)

    def test_removes_main_and_all_subtables_preserving_other_bytes(self):
        self.install()
        main = self.codex.read_text()
        prefix = '# Keep exact prefix\r\nmodel = "kept"\r\n'
        other = '[mcp_servers.other]\ncommand="other"\n'
        suffix = '[misc]\nvalue = "keep trailing"\n'
        source = (prefix + main + '[mcp_servers.dsh_subagent.tools.dsh_prompt]\n'
                  'approval_mode="prompt"\n' + other
                  + '[mcp_servers.dsh_subagent."quoted.tool"]\nflag=true\n'
                  + '[[mcp_servers.dsh_subagent.extra]]\nvalue="own"\n' + suffix)
        self.codex.write_bytes(source.encode())
        self.apply()
        self.assertEqual(self.codex.read_bytes(), (prefix + '\n\n' + other + suffix).encode())

    def test_quoted_main_and_descendant_are_supported(self):
        self.install()
        source = self.codex.read_text().replace('[mcp_servers.dsh_subagent]', '["mcp_servers"."dsh_subagent"]')
        source += '[mcp_servers."dsh_subagent".tools]\nflag=true\n'
        for fallback in (False, True):
            with mock.patch.object(installer, 'tomllib', None if fallback else installer.tomllib):
                self.assertEqual(uninstaller.remove_toml(source, self.root), '\n\n')

    def test_fallback_multiline_args(self):
        self.install()
        source = self.codex.read_text().replace('args = [', 'args = [\n ').replace('"--config", ', '"--config",\n ')
        with mock.patch.object(installer, 'tomllib', None):
            result = uninstaller.remove_toml(source, self.root)
        self.assertNotIn('dsh_subagent', result)

    def test_fallback_string_contents_cannot_prove_transport_ownership(self):
        for delimiter in ('"""', "'" * 3):
            source = ('[mcp_servers.dsh_subagent]\ncommand="unrelated"\n'
                      + 'notes=' + delimiter + '\nargs=' + json.dumps([
                          str(self.root / 'src/desktop_client.py'), '--config',
                          str(self.root / 'config/bridge.json')])
                      + '\ncwd=' + json.dumps(str(self.root)) + '\n' + delimiter
                      + '\n[mcp_servers.other]\ncommand="kept"\n')
            with mock.patch.object(installer, 'tomllib', None):
                with self.assertRaisesRegex(uninstaller.UninstallError, 'registration preserved'):
                    uninstaller.remove_toml(source, self.root)

    def test_table_like_text_inside_multiline_string_is_preserved(self):
        self.install()
        prefix = 'notes = """\n[mcp_servers.dsh_subagent.fake]\nkeep me\n"""\n'
        result = uninstaller.remove_toml(prefix + self.codex.read_text(), self.root)
        self.assertEqual(result, prefix + '\n\n')

    def test_same_prefix_server_preserved(self):
        self.install()
        other = '[mcp_servers.dsh_subagent_other]\ncommand="safe"\n'
        result = uninstaller.remove_toml(self.codex.read_text() + other, self.root)
        self.assertEqual(result, '\n\n' + other)

    def test_mcp_another_root_is_refused_without_any_writes(self):
        self.install()
        self.codex.write_text(self.codex.read_text().replace(str(self.root), str(self.base / 'other')))
        before = installer.fingerprint(self.base)
        with self.assertRaises(uninstaller.UninstallError):
            self.plan()
        self.assertEqual(installer.fingerprint(self.base), before)

    def test_mcp_ownership_requires_cwd_args_and_config(self):
        self.install()
        source = self.codex.read_text()
        for old, new in ((str(self.root / 'src/desktop_client.py'), '/other/client.py'),
                         (str(self.root / 'config/bridge.json'), '/other/config.json'),
                         ('cwd = ' + json.dumps(str(self.root)), 'cwd = "/other"'),
                         ('"--config"', '"--other"')):
            with self.assertRaises(uninstaller.UninstallError):
                uninstaller.remove_toml(source.replace(old, new), self.root)

    def test_inline_mcp_and_orphaned_subtable_are_refused(self):
        sources = ('mcp_servers = {dsh_subagent={command="unrelated"}}\n',
                   '[mcp_servers.dsh_subagent.tools]\nflag=true\n',
                   '[[mcp_servers.dsh_subagent]]\nargs=[]\n')
        for source in sources:
            with self.assertRaises(uninstaller.UninstallError):
                uninstaller.remove_toml(source, self.root)

    def test_empty_insert_wrapper_removed_with_other_sections_exact(self):
        prefix = '- id: model\n  config:\n    model: fixture\n'
        suffix = '- id: unrelated\n  disabled: !!js process.platform === "win32"\n'
        source = prefix + '- insert:\n' + self.block(indent=4) + suffix
        self.assertEqual(uninstaller.remove_yaml(source, self.root), prefix + suffix)

    def test_insert_wrapper_with_sibling_and_metadata_preserved(self):
        prefix = '- insert:\n'
        sibling = '    - id: other\n      disabled: !!js arbitrary.native.expression\n'
        metadata = '  before: agent-default-model\n'
        for rest in (sibling, metadata, sibling + metadata):
            source = prefix + self.block(indent=4) + rest
            self.assertEqual(uninstaller.remove_yaml(source, self.root), prefix + rest)

    def test_yaml_block_scalar_entries_cannot_establish_host_ownership(self):
        for marker in ('|', '>', '!!js |', '!custom >', '|2-'):
            fake = ('- id: unrelated\n  config:\n    notes: ' + marker + '\n'
                    + '      - insert:\n' + self.block(indent=10)
                    + '          - id: literal-sibling\n')
            with self.assertRaisesRegex(uninstaller.UninstallError, 'registration preserved'):
                uninstaller.remove_yaml(fake, self.root)
            # A real registration elsewhere is removed while all literal bytes remain.
            self.assertEqual(uninstaller.remove_yaml(fake + self.block(), self.root), fake)

    def test_separating_comments_and_blank_lines_are_preserved(self):
        before = '# prefix\n'
        after = '\n# next plugin comment\n- id: other\n  name: other\n'
        self.assertEqual(uninstaller.remove_yaml(before + self.block() + after, self.root), before + after)

    def test_yaml_other_root_module_duplicates_and_bad_nesting_refused(self):
        sources = (self.block(self.base / 'other'),
                   self.block().replace(installer.HOST_NAME, 'unrelated'),
                   self.block() + self.block(),
                   '- other:\n' + self.block(indent=4))
        for source in sources:
            with self.assertRaises(uninstaller.UninstallError):
                uninstaller.remove_yaml(source, self.root)

    def test_foreign_link_and_real_directory_are_preserved(self):
        self.install()
        self.skill_link.unlink()
        for directory in (False, True):
            if directory:
                self.skill_link.mkdir()
                (self.skill_link / 'valuable.txt').write_text('preserve')
            else:
                self.skill_link.symlink_to(self.base / 'another-skill')
            before = installer.fingerprint(self.base)
            with self.assertRaises(uninstaller.UninstallError):
                self.plan()
            self.assertEqual(installer.fingerprint(self.base), before)
            if not directory:
                self.skill_link.unlink()

    def test_owned_dangling_link_is_removed(self):
        self.skill_link.parent.mkdir(parents=True)
        self.skill_link.symlink_to(self.root / 'skill/deepseek-subagent')
        (self.root / 'skill/deepseek-subagent/SKILL.md').unlink()
        (self.root / 'skill/deepseek-subagent').rmdir()
        self.apply()
        self.assertFalse(self.skill_link.is_symlink())

    def test_registration_parent_symlink_is_refused(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (self.home / '.codex').symlink_to(outside)
        before = installer.fingerprint(self.base)
        with self.assertRaises(uninstaller.UninstallError):
            self.plan()
        self.assertEqual(installer.fingerprint(self.base), before)

    def test_file_modes_data_workspaces_sources_and_existing_state_modes_preserved(self):
        self.install()
        os.chmod(self.codex, 0o640)
        os.chmod(self.yaml, 0o644)
        data = self.root / 'data/projects/preserved/state.bin'
        data.parent.mkdir(parents=True)
        data.write_bytes(b'\x00fixture\xff')
        workspace = self.base / 'real-workspace'
        workspace.mkdir()
        (workspace / 'valuable.txt').write_text('kept')
        os.chmod(self.root / 'data', 0o750)
        config_before = installer.fingerprint(self.root / 'config')
        sources_before = installer.fingerprint(self.root / 'src')
        work_before = installer.fingerprint(workspace)
        data_before = installer.fingerprint(self.root / 'data/projects')
        self.apply()
        self.assertEqual(self.codex.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.yaml.stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.root / 'data').stat().st_mode & 0o777, 0o750)
        self.assertEqual(installer.fingerprint(self.root / 'config'), config_before)
        self.assertEqual(installer.fingerprint(self.root / 'src'), sources_before)
        self.assertEqual(installer.fingerprint(workspace), work_before)
        self.assertEqual(installer.fingerprint(self.root / 'data/projects'), data_before)

    def test_concurrent_mutation_prevents_writes_and_backup(self):
        self.install()
        changes = self.plan()
        self.codex.write_bytes(self.codex.read_bytes() + b'# concurrent change\n')
        before = installer.fingerprint(self.base)
        with self.assertRaises(uninstaller.UninstallError):
            uninstaller.apply_uninstall(self.root, changes)
        self.assertEqual(installer.fingerprint(self.base), before)
        self.assertFalse((self.root / 'data/global/uninstallation-backups').exists())

    def test_mid_apply_failure_restores_original_bytes_modes_and_links(self):
        self.install()
        os.chmod(self.codex, 0o640)
        before = {path: installer.fingerprint(path) for path in (self.codex, self.yaml, self.host_link, self.skill_link)}
        real = installer.atomic_write
        def fail_once(path, payload, mode=0o600):
            if path == self.yaml and not hasattr(fail_once, 'failed'):
                fail_once.failed = True
                raise OSError('synthetic failure')
            return real(path, payload, mode)
        with mock.patch.object(installer, 'atomic_write', side_effect=fail_once):
            with self.assertRaisesRegex(uninstaller.UninstallError, 'rolled back'):
                self.apply()
        for path, fingerprint in before.items():
            self.assertEqual(installer.fingerprint(path), fingerprint)
        manifest = next((self.root / 'data/global/uninstallation-backups').glob('*/manifest.json'))
        self.assertEqual(json.loads(manifest.read_text())['status'], 'rolled-back')

    def test_unlink_failure_restores_earlier_removed_symlink(self):
        self.install()
        before = {path: installer.fingerprint(path) for path in (self.codex, self.yaml, self.host_link, self.skill_link)}
        real = Path.unlink
        def fail(path, *args, **kwargs):
            if path == self.skill_link:
                raise OSError('synthetic unlink error')
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', autospec=True, side_effect=fail):
            with self.assertRaisesRegex(uninstaller.UninstallError, 'rolled back'):
                self.apply()
        for path, fingerprint in before.items():
            self.assertEqual(installer.fingerprint(path), fingerprint)

    def test_mid_apply_concurrent_edit_is_not_overwritten_by_rollback(self):
        self.install()
        foreign_bytes = b'model="concurrent foreign edit"\n'
        real = installer.atomic_write
        def interfere(path, payload, mode=0o600):
            if path == self.yaml:
                self.codex.write_bytes(foreign_bytes)
                raise OSError('synthetic stop')
            return real(path, payload, mode)
        with mock.patch.object(installer, 'atomic_write', side_effect=interfere):
            with self.assertRaisesRegex(uninstaller.UninstallError, 'rollback was blocked'):
                self.apply()
        self.assertEqual(self.codex.read_bytes(), foreign_bytes)
        self.assertTrue(self.host_link.is_symlink())

    def test_post_write_foreign_edit_does_not_authorize_rollback(self):
        self.install()
        foreign_bytes = b'model="foreign post-write edit"\n'
        real = installer.atomic_write
        def interfere(path, payload, mode=0o600):
            result = real(path, payload, mode)
            if path == self.codex and not hasattr(interfere, 'changed'):
                interfere.changed = True
                self.codex.write_bytes(foreign_bytes)
            return result
        with mock.patch.object(installer, 'atomic_write', side_effect=interfere):
            with self.assertRaisesRegex(uninstaller.UninstallError, 'rollback was blocked'):
                self.apply()
        self.assertEqual(self.codex.read_bytes(), foreign_bytes)
        self.assertTrue(self.host_link.is_symlink())

    def test_active_writer_launcher_and_exit_monitor_locks_refuse_apply(self):
        self.install()
        for relative in uninstaller.LOCKS:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a+') as held:
                held.write('untrusted token-like data never read'); held.flush()
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                before = installer.fingerprint(self.base)
                with self.assertRaisesRegex(uninstaller.UninstallError, 'Exit DSH and Codex normally'):
                    self.apply()
                self.assertEqual(installer.fingerprint(self.base), before)

    def test_released_locks_untouched_and_contents_not_read(self):
        self.install()
        paths = [self.root / relative for relative in uninstaller.LOCKS]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'\xffuntrusted diagnostic token-like fixture')
        before = {path: installer.fingerprint(path) for path in paths}
        self.apply()
        for path, value in before.items():
            self.assertEqual(installer.fingerprint(path), value)

    def test_symlinked_lock_and_state_directory_are_refused(self):
        self.install()
        lock = self.root / uninstaller.LOCKS[0]
        lock.parent.mkdir(parents=True)
        lock.symlink_to(self.base / 'outside.lock')
        with self.assertRaises(uninstaller.UninstallError):
            self.apply()
        lock.unlink()
        backup_base = self.root / 'data/global/uninstallation-backups'
        backup_base.symlink_to(self.base)
        with self.assertRaises(uninstaller.UninstallError):
            self.apply()
        self.assertTrue(self.host_link.is_symlink())

    def test_absent_run_lock_files_are_not_created(self):
        self.install()
        before = installer.fingerprint(self.root / 'run')
        self.apply()
        self.assertEqual(installer.fingerprint(self.root / 'run'), before)

    def test_native_security_and_old_registration_backups_are_not_restored(self):
        self.install()
        history = self.root / 'data/global/installation-backups'
        before = installer.fingerprint(history)
        native = self.profile / 'security.yml'
        native.write_text('native-policy: preserve\n')
        self.apply()
        self.assertEqual(installer.fingerprint(history), before)
        self.assertEqual(native.read_text(), 'native-policy: preserve\n')


if __name__ == '__main__':
    unittest.main()
