#!/usr/bin/env python3
"""Remove this package's registrations only; read-only unless --apply is given."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

try:
    from . import install as installer
except ImportError:
    import install as installer


class UninstallError(Exception):
    """A sanitized public failure, without configuration or diagnostic contents."""


TARGET = ('mcp_servers', 'dsh_subagent')
LOCKS = ('run/bridge/owner.lock', 'run/launcher/launcher.lock',
         'run/launcher/exit-monitor.lock')


def _physical_parents(path: Path, anchor: Path):
    """Do not follow registration/state directory links out of the selected root."""
    try:
        relative = path.relative_to(anchor)
    except ValueError:
        raise UninstallError('Registration path is outside the selected home/package.') from None
    current = anchor
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise UninstallError('Registration and state parent paths must be physical directories.')


def _header_keys(raw: str):
    parts = re.findall(r'(?:"(?:[^"\\]|\\.)*"|\'[^\']*\'|[A-Za-z0-9_-]+)\s*(?:\.|$)', raw)
    # Full consumption prevents accepting malformed/ambiguous dotted keys.
    if not parts or ''.join(parts).replace(' ', '').replace('\t', '') != raw.replace(' ', '').replace('\t', ''):
        raise UninstallError('Unsupported Codex table syntax; no registrations changed.')
    keys = []
    for part in parts:
        part = part.strip().removesuffix('.').strip()
        try:
            keys.append(installer.transport_value(part) if part.startswith(('"', "'")) else part)
        except ValueError:
            raise UninstallError('Unsupported Codex table syntax; no registrations changed.') from None
    return tuple(keys)


def _toml_headers(source: str):
    """Find actual table lines, excluding table-like text inside multiline strings."""
    headers, offset, multiline = [], 0, None
    for line in source.splitlines(keepends=True):
        if multiline is None:
            match = re.match(r'^[ \t]*(\[\[?)(.*?)(\]\]?)[ \t]*(?:#.*)?(?:\r?\n)?$', line)
            if match:
                if len(match.group(1)) != len(match.group(3)):
                    raise UninstallError('Invalid Codex table delimiters.')
                headers.append((offset, _header_keys(match.group(2).strip()), len(match.group(1)) == 2))
        index, quote = 0, None
        while index < len(line):
            if multiline:
                end = line.find(multiline, index)
                if end < 0:
                    break
                # A basic-string delimiter preceded by an odd number of backslashes is escaped.
                slashes, pos = 0, end - 1
                while pos >= 0 and line[pos] == '\\':
                    slashes += 1; pos -= 1
                if multiline == '"""' and slashes % 2:
                    index = end + 1; continue
                index, multiline = end + 3, None
                continue
            character = line[index]
            if quote:
                if quote == '"' and character == '\\':
                    index += 2; continue
                if character == quote:
                    quote = None
            elif character == '#':
                break
            elif line[index:index + 3] in ('"""', "'''"):
                multiline, index = line[index:index + 3], index + 3
                continue
            elif character in ('"', "'"):
                quote = character
            index += 1
        offset += len(line)
    return headers


def _fallback_transport(body: str, key: str):
    # Python 3.9/3.10 has no general TOML parser. Refuse multiline target
    # values so field-shaped text inside a string can never prove ownership.
    if '"""' in body or "'''" in body:
        raise UninstallError('Multiline Codex target values need a TOML parser; registration preserved.')
    if re.search(rf'^[ \t]*[\'\"]{key}[\'\"]\s*=', body, re.M):
        raise UninstallError('Unsupported Codex transport field syntax.')
    matches = list(re.finditer(rf'^[ \t]*{key}\s*=\s*', body, re.M))
    if len(matches) != 1:
        raise UninstallError('Codex registration ownership could not be verified.')
    start = matches[0].end()
    end = body.find('\n', start)
    if end < 0:
        end = len(body)
    while True:
        try:
            return installer.transport_value(body[start:end])
        except ValueError:
            if end == len(body):
                raise UninstallError('Unsupported Codex transport field syntax.') from None
            end = body.find('\n', end + 1)
            if end < 0:
                end = len(body)


def _bound_path(raw, expected: Path):
    # Absolute paths make ownership independent of the caller's working directory/home.
    return isinstance(raw, str) and Path(raw).is_absolute() and Path(raw).resolve() == expected


def remove_toml(source: str, root: Path):
    """Delete the exact owned main table and its subtables, preserving other bytes."""
    root = root.resolve()
    document = None
    if installer.tomllib is not None:
        try:
            document = installer.tomllib.loads(source)
        except installer.tomllib.TOMLDecodeError:
            raise UninstallError('Codex configuration is invalid TOML.') from None
    headers = _toml_headers(source)
    owned = [(index, start, keys, array) for index, (start, keys, array) in enumerate(headers) if keys[:2] == TARGET]
    main = [entry for entry in owned if entry[2] == TARGET]
    if any(entry[3] for entry in main):
        raise UninstallError('Codex target array tables are unsupported; registration preserved.')
    servers = document.get('mcp_servers', {}) if document is not None else None
    if servers is not None and not isinstance(servers, dict):
        raise UninstallError('Codex MCP server map is unsupported; configuration preserved.')
    if not owned:
        if document is not None and 'dsh_subagent' in servers:
            raise UninstallError('Inline Codex target maps are unsupported; registration preserved.')
        if document is None and re.search(r'^[ \t]*mcp_servers\s*=', source, re.M):
            raise UninstallError('Inline MCP maps cannot be verified; registration preserved.')
        return source
    if len(main) != 1:
        raise UninstallError('Codex target needs one explicit main table; registration preserved.')
    index, start, _, _ = main[0]
    end = headers[index + 1][0] if index + 1 < len(headers) else len(source)
    if document is not None:
        fields = servers['dsh_subagent']
        if not isinstance(fields, dict):
            raise UninstallError('Codex target must be a table; registration preserved.')
        args, cwd = fields.get('args'), fields.get('cwd')
    else:
        body = source[source.find('\n', start) + 1:end]
        args, cwd = _fallback_transport(body, 'args'), _fallback_transport(body, 'cwd')
    if (not isinstance(args, list) or len(args) != 3 or args[1] != '--config'
            or not _bound_path(args[0], root / 'src/desktop_client.py')
            or not _bound_path(args[2], root / 'config/bridge.json')
            or not _bound_path(cwd, root)):
        raise UninstallError('Codex target belongs to another package or is unverified; registration preserved.')
    result = source
    for index, start, _, _ in reversed(owned):
        end = headers[index + 1][0] if index + 1 < len(headers) else len(source)
        result = result[:start] + result[end:]
    return result


def _meaningful(line):
    return bool(line.strip()) and not line.lstrip().startswith('#')


def _indent(line):
    return len(line) - len(line.lstrip(' '))


def remove_yaml(source: str, root: Path):
    """Remove one verified Host block, including an empty native insert wrapper."""
    if '\t' in source:
        raise UninstallError('Tab-indented DSH profile is unsupported.')
    lines = source.splitlines(keepends=True)
    matches = []
    payload_lines = installer.yaml_payload_lines(lines)
    for index, line in enumerate(lines):
        if index in payload_lines:
            continue
        match = re.match(r'^( *)- id:\s*(.*?)\s*$', line.rstrip('\r\n'))
        if match and installer.yaml_scalar(match.group(2)) == installer.HOST_ID:
            matches.append((index, len(match.group(1))))
    if not matches:
        if installer.HOST_ID in source:
            raise UninstallError('Unsupported DSH target syntax; registration preserved.')
        return source
    if len(matches) != 1:
        raise UninstallError('Duplicate DSH target entries; registrations preserved.')
    start, indent = matches[0]
    end = next((i for i in range(start + 1, len(lines))
                if _meaningful(lines[i]) and _indent(lines[i]) <= indent), len(lines))
    # Keep separator whitespace and comments belonging to the following section.
    while end > start + 1 and (not lines[end - 1].strip()
            or (lines[end - 1].lstrip().startswith('#') and _indent(lines[end - 1]) <= indent)):
        end -= 1
    block = lines[start:end]
    names = [installer.yaml_scalar(m.group(1)) for line in block
             if (m := re.match(rf'^ {{{indent + 2}}}name:\s*(.*)$', line.rstrip('\r\n')))]
    if names != [installer.HOST_NAME]:
        raise UninstallError('DSH target ID belongs to another module; registration preserved.')
    config = [i for i, line in enumerate(block)
              if re.match(rf'^ {{{indent + 2}}}config:\s*(?:#.*)?$', line.rstrip('\r\n'))]
    if len(config) != 1:
        raise UninstallError('DSH target requires one block-style config mapping.')
    config_end = next((i for i in range(config[0] + 1, len(block))
                       if _meaningful(block[i]) and _indent(block[i]) <= indent + 2), len(block))
    roots = [installer.yaml_scalar(m.group(1)) for line in block[config[0] + 1:config_end]
             if (m := re.match(rf'^ {{{indent + 4}}}projectRoot:\s*(.*)$', line.rstrip('\r\n')))]
    if len(roots) != 1 or not _bound_path(roots[0], root.resolve()):
        raise UninstallError('DSH target belongs to another package or is unverified; registration preserved.')
    delete = set(range(start, end))
    if indent:
        ancestor = next((i for i in range(start - 1, -1, -1)
                         if _meaningful(lines[i]) and _indent(lines[i]) < indent), None)
        if ancestor is None or not re.match(r'^ *- insert:\s*(?:#.*)?$', lines[ancestor].rstrip('\r\n')):
            raise UninstallError('Unsupported DSH target nesting; registration preserved.')
        wrapper_end = next((i for i in range(ancestor + 1, len(lines))
                            if _meaningful(lines[i]) and _indent(lines[i]) <= _indent(lines[ancestor])), len(lines))
        if not any(_meaningful(lines[i]) for i in range(ancestor + 1, wrapper_end) if i not in delete):
            delete.add(ancestor)
    return ''.join(line for i, line in enumerate(lines) if i not in delete)


def plan_uninstall(root: Path, home: Path):
    root, home = root.resolve(), home.resolve()
    changes = []
    for path, transform in (
        (home / '.codex/config.toml', remove_toml),
        (home / '.dsh/profiles/desktop/cordis.patch.yml', remove_yaml),
    ):
        _physical_parents(path, home)
        before = installer.fingerprint(path)
        source = installer.text_file(path)
        payload = transform(source, root).encode('utf-8')
        if installer.fingerprint(path) != before:
            raise UninstallError('Concurrent registration change detected while planning; uninstall refused.')
        if before['kind'] == 'file' and source.encode('utf-8') != payload:
            changes.append({'path': str(path), 'before': before, 'payload': payload, 'kind': 'write'})
    for path, source in (
        (home / '.dsh/profiles/desktop/node_modules/@local/codex-dsh-desktop-link', root / 'src/desktop_host'),
        (home / '.codex/skills/deepseek-subagent', root / 'skill/deepseek-subagent'),
    ):
        _physical_parents(path, home)
        before = installer.fingerprint(path)
        if before['kind'] == 'absent':
            continue
        if before['kind'] != 'symlink' or path.resolve() != source:
            raise UninstallError('An existing registration link is unrelated or a physical entity; it was preserved.')
        if installer.fingerprint(path) != before:
            raise UninstallError('Concurrent registration link change detected; uninstall refused.')
        changes.append({'path': str(path), 'before': before, 'kind': 'unlink'})
    return changes


@contextmanager
def inactive_runtime(root: Path):
    """Hold existing lock files without creating paths or reading their contents."""
    descriptors = []
    try:
        for relative in LOCKS:
            path = root / relative
            _physical_parents(path, root)
            try:
                fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            except OSError:
                raise UninstallError('Runtime lock cannot be verified; registration preserved.') from None
            descriptors.append(fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UninstallError('Runtime lock must be a physical regular file.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UninstallError('Package runtime is active. Exit DSH and Codex normally, then retry uninstall.') from None
            observed = path.lstat()
            actual = os.fstat(fd)
            if (observed.st_dev, observed.st_ino) != (actual.st_dev, actual.st_ino):
                raise UninstallError('Runtime lock changed concurrently; registration preserved.')
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _manifest(backup: Path, manifest):
    installer.atomic_write(backup / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())


def _rollback(backup: Path, manifest):
    changed = []
    for item in manifest['changes']:
        if item.get('after') is None:
            continue
        actual = installer.fingerprint(Path(item['path']))
        if actual == item['before']:
            continue  # A prepared operation failed before changing its destination.
        if actual != item['after']:
            raise UninstallError('Registration changed concurrently; automatic rollback refused.')
        changed.append(item)
    for item in reversed(changed):
        path, before = Path(item['path']), item['before']
        if installer.fingerprint(path) != item['after']:
            raise UninstallError('Registration changed during rollback; automatic rollback refused.')
        if before['kind'] == 'file':
            installer.atomic_write(path, (backup / item['backup']).read_bytes(), before['mode'])
        elif before['kind'] == 'symlink':
            path.symlink_to(before['target'], target_is_directory=True)
    manifest['status'] = 'rolled-back'
    _manifest(backup, manifest)


def apply_uninstall(root: Path, changes):
    root = root.resolve()
    with inactive_runtime(root):
        for item in changes:
            if installer.fingerprint(Path(item['path'])) != item['before']:
                raise UninstallError('Concurrent registration change detected; uninstall refused.')
        if not changes:
            return None
        # Validate every state parent before creating any backup. Preserve existing modes.
        base = root / 'data/global/uninstallation-backups'
        _physical_parents(base / 'entry', root)
        for directory in (root / 'data', root / 'data/global', base):
            if not directory.exists():
                directory.mkdir(mode=0o700)
        backup = base / (time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:10])
        backup.mkdir(mode=0o700)
        manifest = {'version': 1, 'operation': 'uninstall', 'status': 'partial',
                    'root': str(root), 'changes': []}
        for index, item in enumerate(changes):
            path = Path(item['path'])
            if installer.fingerprint(path) != item['before']:
                raise UninstallError('Concurrent registration change detected; uninstall refused.')
            stored = {key: value for key, value in item.items() if key not in ('payload', 'kind')}
            stored.update(backup=f'entry-{index}', after=None)
            if item['before']['kind'] == 'file':
                installer.atomic_write(backup / stored['backup'], path.read_bytes())
            manifest['changes'].append(stored)
        _manifest(backup, manifest)
        try:
            for item, stored in zip(changes, manifest['changes']):
                path = Path(item['path'])
                if installer.fingerprint(path) != item['before']:
                    raise UninstallError('Concurrent registration change detected; uninstall refused.')
                # Only our planned result authorizes rollback, never arbitrary
                # bytes observed after a concurrent writer raced our mutation.
                expected = ({'kind': 'file', 'sha256': hashlib.sha256(item['payload']).hexdigest(),
                             'mode': item['before']['mode']} if item['kind'] == 'write'
                            else {'kind': 'absent'})
                stored['after'] = expected
                if item['kind'] == 'write':
                    installer.atomic_write(path, item['payload'], item['before']['mode'])
                else:
                    path.unlink()
                if installer.fingerprint(path) != expected:
                    raise UninstallError('Registration changed during uninstall; recovery requires review.')
                _manifest(backup, manifest)
            manifest['status'] = 'applied'
            _manifest(backup, manifest)
        except Exception:
            try:
                _manifest(backup, manifest)
                _rollback(backup, manifest)
            except Exception:
                raise UninstallError('Uninstall stopped; automatic rollback was blocked. Preserve the uninstallation backup for review.') from None
            raise UninstallError('Uninstall stopped and registration changes were rolled back.') from None
        return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--home', type=Path, default=Path.home(), help='User home (test fixtures may override).')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--apply', action='store_true', help='Remove registrations owned by this package.')
    modes.add_argument('--check', action='store_true', help='Read-only registration check.')
    modes.add_argument('--dry-run', action='store_true', help='Read-only plan (default).')
    args = parser.parse_args(argv)
    root, home = args.root.resolve(), args.home.resolve()
    try:
        changes = plan_uninstall(root, home)
        if not args.apply:
            print(f'Read-only uninstall check: {len(changes)} registration change(s) required.')
            for item in changes:
                print(f"  {item['kind']}: {item['path']}")
            print('Run with --apply to unregister this package. Package files, configuration and data are retained.')
            return 0
        backup = apply_uninstall(root, changes)
        if backup:
            print(f'Unregistered this package. Restricted recovery backup: {backup.name}')
        else:
            print('This package has no remaining registrations.')
        print('Package files, configuration, data and workspaces were retained. No applications were stopped.')
        return 0
    except (UninstallError, installer.InstallError, OSError, UnicodeError, ValueError):
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, (UninstallError, installer.InstallError))
              else 'Uninstall failed; no diagnostic contents were exposed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
