#!/usr/bin/env python3
"""Build portable runtime and open-source ZIPs from explicit file allowlists."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import socket
import sys
import zipfile

PRODUCTION = (
    'src/__init__.py', 'src/layout.py', 'src/project_store.py',
    'src/desktop_client.py', 'src/desktop_bridge.py', 'src/mcp_common.py',
    'src/codex_notifications.py', 'src/app_server_websocket.py',
    'src/install.py', 'src/uninstall.py', 'src/configure_notifications.py',
    'src/launcher.py',
    'src/desktop_host/index.mjs', 'src/desktop_host/controller.mjs', 'src/desktop_host/package.json',
    'skill/deepseek-subagent/SKILL.md', 'skill/deepseek-subagent/agents/openai.yaml',
    'skill/deepseek-subagent/references/bridge-contract.md',
    'README.md', 'INSTALL.md', 'LICENSE', 'install.sh', 'uninstall.sh', 'start.sh', 'config/bridge.example.json',
)
DEVELOPMENT = (
    'AGENTS.md', '.gitignore', 'CONTRIBUTING.md', 'tools/package_plugin.py',
    'tests/test_project_store.py', 'tests/mock_desktop_client.py',
    'tests/test_shared_launcher.py', 'tests/test_desktop.py',
    'tests/test_desktop_host.mjs', 'tests/test_codex_notifications.py',
    'tests/test_notification_configuration.py', 'tests/test_multiclient.mjs',
    'tests/test_app_server_websocket.py',
    'tests/mock_codex_app_server.py', 'tests/test_desktop_client.py',
    'tests/mock_desktop_controller.py', 'tests/test_installer.py',
    'tests/test_uninstaller.py', 'tests/test_package.py',
)
SOURCE = PRODUCTION + DEVELOPMENT
SKELETON = ('config/', 'data/', 'data/global/', 'data/projects/', 'run/')
PREFIX = 'dsh-codex-plugin/'


class PackageError(Exception):
    pass


def package_root(root: Path):
    if root.is_symlink() or not root.is_dir():
        raise PackageError('Package root must be a physical directory.')
    return root.resolve()


def machine_specific(payload: bytes, root: Path):
    # Read local identity at validation time, never embed the author's identity in
    # this tool. Generic sample paths in synthetic tests remain portable.
    for path in (root, Path.home()):
        value = str(path).encode()
        if len(path.parts) > 1 and re.search(re.escape(value) + rb'(?=$|[/\\\s\"\'])', payload):
            return True
    hostname = socket.gethostname()
    names = {hostname} if '.' in hostname else {hostname + '.local'}
    for name in names:
        if re.search(rb'(?<![\w.-])' + re.escape(name.encode()) + rb'(?![\w.-])', payload, re.I):
            return True
    return False


def selected_files(root: Path, names):
    root = package_root(root)
    selected = []
    for name in names:
        path = root / name
        if any((root / parent).is_symlink() for parent in Path(name).parents) or path.is_symlink() or not path.is_file():
            raise PackageError('Required package file is missing or symlinked: ' + name)
        try:
            path.resolve().relative_to(root)
        except ValueError:
            raise PackageError('Source escapes the package: ' + name) from None
        payload = path.read_bytes()
        if machine_specific(payload, root):
            raise PackageError('Machine-specific identity found in package file: ' + name)
        selected.append((name, payload, bool(path.stat().st_mode & 0o111)))
    return selected


def production_files(root: Path):
    root = package_root(root)
    selected = selected_files(root, PRODUCTION)
    try:
        defaults = json.loads((root / 'config/bridge.example.json').read_text())
    except (ValueError, UnicodeError):
        raise PackageError('Default bridge configuration is invalid.') from None
    if not isinstance(defaults, dict):
        raise PackageError('Default bridge configuration must be an object.')
    if defaults.get('runtime_root') not in (None, 'run/bridge') or defaults.get('link_file') != 'run/desktop-host/link.json':
        raise PackageError('Default paths must remain package-relative.')
    notifications = defaults.get('codex_notifications', {})
    if not isinstance(notifications, dict):
        raise PackageError('Default notification configuration must be an object.')
    if set(defaults) - {'runtime_root', 'link_file', 'rpc_timeout_seconds', 'turn_timeout_seconds', 'cancel_grace_seconds', 'max_frame_bytes', 'max_result_chars', 'codex_notifications'}:
        raise PackageError('Default configuration contains unknown or instance-specific fields.')
    if set(notifications) - {'enabled', 'websocket_url', 'rpc_timeout_seconds', 'retry_seconds'}:
        raise PackageError('Default notification configuration contains unknown or instance-specific fields.')
    if notifications.get('websocket_url') not in (None, 'ws://127.0.0.1:4500'):
        raise PackageError('Default endpoint must be the local shared App Server.')
    if any(key in notifications for key in ('parent_thread_id', 'generation', 'token', 'api_key')):
        raise PackageError('Default configuration contains instance state.')
    return selected


def source_files(root: Path):
    return production_files(root) + selected_files(root, DEVELOPMENT)


def write_archive(selected, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name('.' + output.name + '.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name in SKELETON:
                info = zipfile.ZipInfo(PREFIX + name, date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o40700 << 16) | 0x10
                archive.writestr(info, b'')
            for name, payload, executable in selected:
                info = zipfile.ZipInfo(PREFIX + name, date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if executable else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, payload)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return len(selected)


def build_package(root: Path, output: Path):
    """Build the runtime distribution (without tests or development tools)."""
    return write_archive(production_files(root), output)


def build_source_package(root: Path, output: Path):
    """Build the GitHub-ready source distribution, excluding local history/state."""
    return write_archive(source_files(root), output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, help='Default: ROOT/dist/dsh-codex-plugin.zip')
    parser.add_argument('--source-output', type=Path, help='Default: ROOT/dist/dsh-codex-plugin-source.zip')
    parser.add_argument('--check', action='store_true', help='Read-only validation of both package allowlists.')
    args = parser.parse_args(argv)
    try:
        runtime = production_files(args.root)
        source = runtime + selected_files(args.root, DEVELOPMENT)
        if args.check:
            print(f'Package checks passed: {len(runtime)} runtime files, {len(source)} source files.')
        else:
            output = args.output or args.root / 'dist/dsh-codex-plugin.zip'
            source_output = args.source_output or args.root / 'dist/dsh-codex-plugin-source.zip'
            if output.resolve() == source_output.resolve():
                raise PackageError('Runtime and source archives require different output paths.')
            write_archive(runtime, output)
            write_archive(source, source_output)
            print(f'Created runtime archive with {len(runtime)} files: {output}')
            print(f'Created source archive with {len(source)} files: {source_output}')
        return 0
    except (PackageError, OSError):
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, PackageError) else 'Packaging failed; no diagnostic contents were exposed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
