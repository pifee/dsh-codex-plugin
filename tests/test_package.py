"""Packaging uses synthetic trees; extracted CLI checks use temporary homes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock
import zipfile

_SPEC = importlib.util.spec_from_file_location('portable_packager', Path(__file__).resolve().parents[1] / 'tools/package_plugin.py')
packager = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(packager)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / 'source').resolve()
        self.root.mkdir()
        for name in packager.SOURCE:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# synthetic portable source\n')
        defaults = {'link_file': 'run/desktop-host/link.json', 'codex_notifications': {'enabled': True, 'websocket_url': 'ws://127.0.0.1:4500'}}
        (self.root / 'config/bridge.example.json').write_text(json.dumps(defaults))
        self.output = Path(self.temp.name) / 'dist/plugin.zip'

    def test_whitelist_excludes_all_instance_and_development_material(self):
        for name in ('config/bridge.json', 'data/global/project-index.json', 'data/projects/id/state.json', 'run/desktop-host/link.json', 'run/bridge/owner.lock', 'evidence/result.json', '.runtime/backup/secret', 'src/__pycache__/cache.pyc', 'tools/package_plugin.py', 'tests/test.py', 'launcher.swift', 'python3'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SENSITIVE_SENTINEL')
        packager.build_package(self.root, self.output)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), {packager.PREFIX + name for name in packager.PRODUCTION + packager.SKELETON})
            for name in archive.namelist():
                self.assertNotIn(b'SENSITIVE_SENTINEL', archive.read(name))

    def test_refuses_missing_or_symlinked_production_file(self):
        path = self.root / 'src/desktop_client.py'
        path.unlink()
        with self.assertRaises(packager.PackageError):
            packager.build_package(self.root, self.output)
        path.symlink_to(self.root / 'README.md')
        with self.assertRaises(packager.PackageError):
            packager.build_package(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_symlinked_source_directory_even_inside_the_root(self):
        directory = self.root / 'tools'
        directory.rename(self.root / 'tools-copy')
        directory.symlink_to(self.root / 'tools-copy', target_is_directory=True)
        with self.assertRaises(packager.PackageError):
            packager.build_source_package(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_symlinked_package_root(self):
        linked = Path(self.temp.name) / 'source-link'
        linked.symlink_to(self.root, target_is_directory=True)
        for builder in (packager.build_package, packager.build_source_package):
            with self.assertRaises(packager.PackageError):
                builder(linked, self.output)

    def test_refuses_machine_paths_and_instance_defaults(self):
        source = self.root / 'README.md'
        source.write_text(str(self.root / 'private/source'))
        with self.assertRaises(packager.PackageError):
            packager.build_package(self.root, self.output)
        source.write_text('portable')
        config = self.root / 'config/bridge.example.json'
        config.write_text('{"link_file":"run/desktop-host/link.json", "codex_notifications":{"parent_thread_id":"private"}}')
        with self.assertRaises(packager.PackageError):
            packager.build_package(self.root, self.output)

    def test_refuses_non_object_defaults_without_raw_errors(self):
        for config in ('[]', '{"link_file":"run/desktop-host/link.json", "codex_notifications":null}'):
            (self.root / 'config/bridge.example.json').write_text(config)
            with self.assertRaises(packager.PackageError):
                packager.build_package(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_local_identity_without_rejecting_generic_fixtures(self):
        source = self.root / 'tests/test_package.py'
        source.write_text('/Users/example/private/source')
        packager.build_source_package(self.root, self.output)
        fixture_home = Path(self.temp.name) / 'local-home'
        source.write_text(str(fixture_home / 'private'))
        with mock.patch.object(packager.Path, 'home', return_value=fixture_home):
            with self.assertRaises(packager.PackageError):
                packager.build_source_package(self.root, self.output)
        source.write_text('portable-build.local')
        with mock.patch.object(packager.socket, 'gethostname', return_value='portable-build'):
            with self.assertRaises(packager.PackageError):
                packager.build_source_package(self.root, self.output)

    def test_source_allowlist_includes_development_but_excludes_private_material(self):
        for name in ('HANDOFF.md', 'evidence/result.json', '.runtime/backup/secret',
                     'config/bridge.json', 'data/global/state.json', 'run/owner.lock',
                     'tests/private-session.json', 'tools/private_helper.py', '.env',
                     'src/future_private_module.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SENSITIVE_SENTINEL')
        packager.build_source_package(self.root, self.output)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), {packager.PREFIX + name for name in packager.SOURCE + packager.SKELETON})
            for name in archive.namelist():
                self.assertNotIn(b'SENSITIVE_SENTINEL', archive.read(name))

    def test_cli_builds_and_checks_both_packages(self):
        self.assertEqual(packager.main(['--root', str(self.root), '--check']), 0)
        self.assertFalse((self.root / 'dist').exists())
        self.assertEqual(packager.main(['--root', str(self.root)]), 0)
        for filename, allowlist in (('dsh-codex-plugin.zip', packager.PRODUCTION),
                                    ('dsh-codex-plugin-source.zip', packager.SOURCE)):
            with zipfile.ZipFile(self.root / 'dist' / filename) as archive:
                self.assertEqual(set(archive.namelist()), {packager.PREFIX + name for name in allowlist + packager.SKELETON})
        (self.root / 'tests/test_package.py').unlink()
        self.assertEqual(packager.main(['--root', str(self.root), '--check']), 1)

    def test_cli_validates_both_packages_before_writing(self):
        (self.root / 'tests/test_package.py').unlink()
        self.assertEqual(packager.main(['--root', str(self.root)]), 1)
        self.assertFalse((self.root / 'dist').exists())

    def test_cli_refuses_same_output_path(self):
        self.assertEqual(packager.main(['--root', str(self.root), '--output', str(self.output),
                                       '--source-output', str(self.output)]), 1)
        self.assertFalse(self.output.exists())

    def test_repeat_build_is_identical(self):
        for builder in (packager.build_package, packager.build_source_package):
            builder(self.root, self.output)
            first = self.output.read_bytes()
            builder(self.root, self.output)
            self.assertEqual(self.output.read_bytes(), first)

    def test_actual_archive_runs_from_a_fresh_directory(self):
        actual = Path(__file__).resolve().parents[1]
        packager.build_package(actual, self.output)
        extracted = Path(self.temp.name) / 'unpacked'
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(extracted)
        plugin = extracted / 'dsh-codex-plugin'
        environment = dict(__import__('os').environ)
        environment['PYTHONPATH'] = str(plugin / 'src')
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        smoke = subprocess.run([sys.executable, '-c', 'import desktop_client, desktop_bridge, mcp_common, codex_notifications, app_server_websocket, layout, project_store'], cwd=plugin, env=environment, capture_output=True, timeout=20)
        self.assertEqual(smoke.returncode, 0, smoke.stderr.decode(errors='replace'))
        for name in ('desktop_client.py', 'install.py', 'uninstall.py', 'configure_notifications.py', 'launcher.py'):
            help_result = subprocess.run([sys.executable, str(plugin / 'src' / name), '--help'], cwd=plugin, env=environment, capture_output=True, timeout=20)
            self.assertEqual(help_result.returncode, 0, help_result.stderr.decode(errors='replace'))
        home = Path(self.temp.name) / 'isolated-home'
        (home / '.dsh/profiles/desktop').mkdir(parents=True)
        before = installer_tree(plugin, home)
        check = subprocess.run([sys.executable, str(plugin / 'src/install.py'), '--home', str(home), '--check'], cwd=plugin, env=environment, capture_output=True, timeout=20)
        self.assertEqual(check.returncode, 0, check.stderr.decode(errors='replace'))
        self.assertIn(b'Read-only check', check.stdout)
        self.assertEqual(installer_tree(plugin, home), before)
        shell_help = subprocess.run(['sh', str(plugin / 'uninstall.sh'), '--help'], cwd=plugin,
                                    env=environment, capture_output=True, timeout=20)
        self.assertEqual(shell_help.returncode, 0, shell_help.stderr.decode(errors='replace'))
        codex_config = home / '.codex/config.toml'
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text('model = "keep-model"\n[mcp_servers.other]\ncommand = "keep-other"\n')
        profile = home / '.dsh/profiles/desktop/cordis.patch.yml'
        profile.write_text('- id: keep-other-module\n  config:\n    enabled: true\n')
        state = plugin / 'data/projects/fixture-project/state.json'
        state.parent.mkdir(parents=True)
        state.write_text('{"keep_workspace": true}\n')
        for command, action in (('install.py', '--apply'), ('uninstall.py', '--check'),
                                ('uninstall.py', '--apply'), ('uninstall.py', '--apply')):
            before_check = installer_tree(plugin, home)
            result = subprocess.run([sys.executable, str(plugin / 'src' / command), '--home', str(home), action],
                                    cwd=plugin, env=environment, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            if action == '--check':
                self.assertEqual(installer_tree(plugin, home), before_check)
            if command == 'install.py':
                self.assertTrue((home / '.codex/skills/deepseek-subagent').is_symlink())
                self.assertTrue((home / '.dsh/profiles/desktop/node_modules/@local/codex-dsh-desktop-link').is_symlink())
        self.assertIn('keep-model', codex_config.read_text())
        self.assertIn('keep-other', codex_config.read_text())
        self.assertIn('keep-other-module', profile.read_text())
        self.assertNotIn('mcp_servers.dsh_subagent', codex_config.read_text())
        self.assertNotIn('codex-dsh-desktop-link', profile.read_text())
        self.assertFalse((home / '.codex/skills/deepseek-subagent').is_symlink())
        self.assertFalse((home / '.dsh/profiles/desktop/node_modules/@local/codex-dsh-desktop-link').is_symlink())
        self.assertTrue((plugin / 'config/bridge.json').is_file())
        self.assertEqual(state.read_text(), '{"keep_workspace": true}\n')

    def test_actual_source_archive_checks_from_a_fresh_directory(self):
        actual = Path(__file__).resolve().parents[1]
        packager.build_source_package(actual, self.output)
        extracted = Path(self.temp.name) / 'unpacked-source'
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(extracted)
        plugin = extracted / 'dsh-codex-plugin'
        environment = dict(__import__('os').environ)
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run([sys.executable, str(plugin / 'tools/package_plugin.py'), '--check'],
                                cwd=plugin, env=environment, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertFalse((plugin / 'dist').exists())


def installer_tree(*roots):
    return {(str(path.relative_to(root)), root.name): path.read_bytes() for root in roots for path in root.rglob('*') if path.is_file()}


if __name__ == '__main__':
    unittest.main()
