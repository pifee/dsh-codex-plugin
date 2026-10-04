#!/usr/bin/env python3
"""Portable, opt-in registration installer. No application or model requests."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
try:
    import tomllib
except ImportError:  # Python 3.9/3.10: recognize only transport values.
    tomllib = None
import uuid

HOST_NAME = '@local/codex-dsh-desktop-link'
HOST_ID = 'codex-dsh-desktop-link'
SKILL_NAME = 'deepseek-subagent'


class InstallError(Exception):
    """A public, sanitized installer failure."""


def fingerprint(path: Path):
    if path.is_symlink():
        return {'kind': 'symlink', 'target': os.readlink(path)}
    if not path.exists():
        return {'kind': 'absent'}
    mode = stat.S_IMODE(path.stat().st_mode)
    if path.is_file():
        return {'kind': 'file', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': mode}
    if path.is_dir():
        return {'kind': 'directory', 'mode': mode,
                'children': {p.name: fingerprint(p) for p in sorted(path.iterdir())}}
    raise InstallError('Unsupported filesystem entry; no changes made.')


def text_file(path: Path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallError('Configuration must be a physical regular file.')
    return path.read_bytes().decode('utf-8') if path.exists() else ''


def transport_value(raw: str):
    """Strict TOML subset: strings and string arrays, whitespace and comments."""
    index = 0
    def skip():
        nonlocal index
        while index < len(raw):
            if raw[index].isspace():
                index += 1
            elif raw[index] == '#':
                end = raw.find('\n', index)
                index = len(raw) if end < 0 else end + 1
            else:
                break
    def string():
        nonlocal index
        skip()
        if index >= len(raw):
            raise ValueError('Incomplete value')
        if raw[index] == '"':
            if raw[index:index + 3] == '"""':
                raise ValueError('Unsupported multiline string')
            value, used = json.JSONDecoder().raw_decode(raw[index:])
            if not isinstance(value, str):
                raise ValueError('Expected string')
            index += used
            return value
        if raw[index] == "'":
            if raw[index:index + 3] == "'''":
                raise ValueError('Unsupported multiline string')
            end = raw.find("'", index + 1)
            if end < 0 or '\n' in raw[index:end]:
                raise ValueError('Incomplete literal string')
            value = raw[index + 1:end]
            index = end + 1
            return value
        raise ValueError('Expected string')
    skip()
    if index < len(raw) and raw[index] == '[':
        index += 1
        value = []
        while True:
            skip()
            if index < len(raw) and raw[index] == ']':
                index += 1
                break
            value.append(string())
            skip()
            if index < len(raw) and raw[index] == ',':
                index += 1
            elif index < len(raw) and raw[index] == ']':
                index += 1
                break
            else:
                raise ValueError('Incomplete array')
    else:
        value = string()
    skip()
    if index != len(raw):
        raise ValueError('Unexpected transport syntax')
    return value


def update_toml(source: str, root: Path, python: Path):
    """Replace only three transport fields; preserve policy and other servers."""
    document = {}
    if tomllib is not None:
        try:
            document = tomllib.loads(source)
        except tomllib.TOMLDecodeError:
            raise InstallError('Codex configuration is invalid TOML.') from None
    headers = list(re.finditer(r'^\[([^\[\]\n]+)\][ \t]*(?:#.*)?$', source, re.M))
    matches = [m for m in headers if m.group(1).strip() == 'mcp_servers.dsh_subagent']
    if re.search(r'^\s*mcp_servers\s*=', source, re.M):
        raise InstallError('Inline MCP server maps are unsupported; use explicit target tables.')
    values = {'command': str(python), 'args': [str(root / 'src/desktop_client.py'), '--config', str(root / 'config/bridge.json')], 'cwd': str(root)}
    if not matches:
        if 'dsh_subagent' in document.get('mcp_servers', {}) or re.search(r'^\s*\[.*dsh_subagent.*\]', source, re.M):
            raise InstallError('Unsupported Codex target table syntax; edit the target section explicitly.')
        suffix = '\n[mcp_servers.dsh_subagent]\n' + ''.join(f'{k} = {json.dumps(v, ensure_ascii=False)}\n' for k, v in values.items())
        return source.rstrip('\n') + '\n' + suffix
    if len(matches) != 1:
        raise InstallError('Ambiguous Codex MCP section.')
    target = matches[0]
    end = next((m.start() for m in headers if m.start() > target.start()), len(source))
    body_start = source.find('\n', target.end()) + 1
    if body_start == 0:
        body_start = target.end()
        source = source[:body_start] + '\n' + source[body_start:]
        body_start += 1
        end += 1
    body = source[body_start:end]
    for key in values:
        if re.search(rf'^\s*[\'\"]{key}[\'\"]\s*=', body, re.M) or any(m.group(1).strip().startswith('mcp_servers.dsh_subagent.' + key) for m in headers):
            raise InstallError('Unsupported Codex transport field representation.')
    for key, value in values.items():
        assignments = list(re.finditer(rf'^{key}\s*=\s*', body, re.M))
        if not assignments:
            body = f'{key} = {json.dumps(value, ensure_ascii=False)}\n' + body
            continue
        if len(assignments) != 1:
            raise InstallError('Ambiguous Codex transport field.')
        assignment = assignments[0]
        value_end = body.find('\n', assignment.end())
        if value_end < 0:
            value_end = len(body)
        while True:
            try:
                transport_value(body[assignment.end():value_end])
                break
            except ValueError:
                if value_end == len(body):
                    raise InstallError('Unsupported Codex transport field syntax.') from None
                value_end = body.find('\n', value_end + 1)
                if value_end < 0:
                    value_end = len(body)
        old = body[assignment.start():value_end]
        comment = ''
        # A standalone policy/comment line is never replaced. Preserve simple field comments.
        if '\n' not in old:
            quote, escaped = None, False
            for position, character in enumerate(old):
                if quote == '"' and character == '\\' and not escaped:
                    escaped = True
                    continue
                if character == quote and not escaped:
                    quote = None
                elif quote is None and character in ('"', "'"):
                    quote = character
                elif quote is None and character == '#':
                    comment = ' ' + old[position:]
                    break
                escaped = False
        replacement = f'{key} = {json.dumps(value, ensure_ascii=False)}{comment}'
        body = body[:assignment.start()] + replacement + body[value_end:]
    result = source[:body_start] + body + source[end:]
    if tomllib is not None:
        try:
            parsed = tomllib.loads(result)
        except tomllib.TOMLDecodeError:
            raise InstallError('Unable to preserve Codex TOML; no changes made.') from None
        if any(parsed['mcp_servers']['dsh_subagent'].get(k) != v for k, v in values.items()):
            raise InstallError('Unsupported Codex transport representation.')
    return result


def yaml_scalar(raw: str):
    value = raw.strip()
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            raise InstallError('Unsupported quoted YAML scalar.') from None
    if value.startswith("'"):
        if not value.endswith("'"):
            raise InstallError('Unsupported quoted YAML scalar.')
        return value[1:-1].replace("''", "'")
    return value.split(' #', 1)[0].rstrip()


def yaml_payload_lines(lines):
    """Identify block scalar payload, including native !!js |/> values."""
    payload, parent_indent = set(), None
    marker = re.compile(r'(?:^|:)[ \t]*(?:(?:![^\s]+|&[^\s]+)[ \t]+)*'
                        r'[|>](?:[1-9][+-]?|[+-][1-9]?|)[ \t]*(?:#.*)?$')
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip(' '))
        if parent_indent is not None:
            if not line.strip() or indent > parent_indent:
                payload.add(index)
                continue
            parent_indent = None
        content = line.rstrip('\r\n')
        if marker.search(content) or re.match(r'^ *- (?:(?:![^\s]+|&[^\s]+)[ \t]+)*[|>](?:[1-9][+-]?|[+-][1-9]?|)[ \t]*(?:#.*)?$', content):
            # A mapping key after a sequence dash is two columns deeper.
            parent_indent = indent + (2 if re.match(r'^ *- .*?:', content) else 0)
    return payload


def update_yaml(source: str, root: Path):
    """Support the native block-list patch shape; never parse or touch !!js."""
    if '\t' in source:
        raise InstallError('Tab-indented DSH profile is unsupported.')
    lines = source.splitlines(keepends=True)
    matches = []
    payload_lines = yaml_payload_lines(lines)
    for index, line in enumerate(lines):
        if index in payload_lines:
            continue
        match = re.match(r'^( *)- id:\s*(.*?)\s*$', line.rstrip('\r\n'))
        if match and yaml_scalar(match.group(2)) == HOST_ID:
            matches.append((index, len(match.group(1))))
    if not matches:
        if HOST_ID in source:
            raise InstallError('Unsupported DSH target entry syntax; edit it explicitly.')
        if source.strip() and not any(re.match(r'^- (?:id|insert):', line) for line in lines):
            raise InstallError('DSH profile must be a top-level block list.')
        return source.rstrip('\n') + '\n\n' + (f'- id: {HOST_ID}\n  name: "{HOST_NAME}"\n  config:\n    projectRoot: {json.dumps(str(root), ensure_ascii=False)}\n    enabled: true\n    multiClient: true\n')
    if len(matches) != 1:
        raise InstallError('Duplicate DSH plugin entries; no changes made.')
    start, indent = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if len(line) - len(line.lstrip(' ')) <= indent:
            end = index
            break
    block = lines[start:end]
    names = [yaml_scalar(m.group(1)) for line in block if (m := re.match(rf'^ {{{indent + 2}}}name:\s*(.*)$', line.rstrip('\r\n')))]
    if names != [HOST_NAME]:
        raise InstallError('The DSH target ID belongs to an unrelated or unsupported module.')
    config_positions = [i for i, line in enumerate(block) if re.match(rf'^ {{{indent + 2}}}config:\s*(?:#.*)?$', line.rstrip('\r\n'))]
    if len(config_positions) != 1:
        raise InstallError('DSH target requires one block-style config mapping.')
    config_start = config_positions[0]
    config_end = next((i for i in range(config_start + 1, len(block)) if block[i].strip() and not block[i].lstrip().startswith('#') and len(block[i]) - len(block[i].lstrip(' ')) <= indent + 2), len(block))
    positions = [i for i in range(config_start + 1, config_end) if re.match(rf'^ {{{indent + 4}}}projectRoot:', block[i])]
    if len(positions) > 1:
        raise InstallError('Duplicate DSH projectRoot field.')
    replacement = ' ' * (indent + 4) + 'projectRoot: ' + json.dumps(str(root), ensure_ascii=False) + '\n'
    if positions:
        raw = block[positions[0]].split(':', 1)[1].strip()
        if raw.startswith(('|', '>', '{', '[', '*', '&', '!')):
            raise InstallError('Unsupported DSH projectRoot scalar.')
        block[positions[0]] = replacement
    else:
        block.insert(config_start + 1, replacement)
    return ''.join(lines[:start] + block + lines[end:])


def old_project_root(source: str):
    # Ownership only: never derive a previous root from literal/native code data.
    lines = source.splitlines(keepends=True)
    payload = yaml_payload_lines(lines)
    for index, line in enumerate(lines):
        if index in payload:
            continue
        match = re.match(r'^( *)- id:\s*(.*?)\s*$', line.rstrip('\r\n'))
        if not match or yaml_scalar(match.group(2)) != HOST_ID:
            continue
        indent = len(match.group(1))
        end = next((i for i in range(index + 1, len(lines)) if lines[i].strip()
                    and not lines[i].lstrip().startswith('#')
                    and len(lines[i]) - len(lines[i].lstrip(' ')) <= indent), len(lines))
        values = [yaml_scalar(m.group(1)) for i in range(index + 1, end) if i not in payload
                  and (m := re.match(rf'^ {{{indent + 4}}}projectRoot:\s*(.*)$', lines[i].rstrip('\r\n')))]
        return Path(values[0]).expanduser() if len(values) == 1 else None
    return None


def validate_owned(path: Path, source: Path, old_root: Path | None, kind: str):
    if not path.exists() and not path.is_symlink():
        return
    allowed = [source.resolve()]
    if old_root:
        allowed.append(old_root / ('src/desktop_host' if kind == 'host' else 'skill/deepseek-subagent'))
    if path.is_symlink():
        if path.resolve() not in [p.resolve() for p in allowed]:
            raise InstallError('An existing registration link points to unrelated content.')
        return
    if not path.is_dir() or not old_root:
        raise InstallError('Existing plugin content has no verified previous registration.')
    def content_only(value):
        return {key: ({name: content_only(child) for name, child in item.items()} if key == 'children' else item)
                for key, item in value.items() if key != 'mode'}
    for candidate in allowed:
        if candidate.is_dir() and content_only(fingerprint(path)) == content_only(fingerprint(candidate)):
            return
    raise InstallError('Existing plugin content differs from the registered source; preserve it and resolve manually.')


def atomic_write(path: Path, payload: bytes, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_directory(path: Path):
    if path.is_symlink():
        raise InstallError('Package state directories must be physical directories.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise InstallError('Package state directory is unavailable.')
    os.chmod(path, 0o700)


def remove_entry(path: Path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def rollback(backup: Path):
    manifest_path = backup / 'manifest.json'
    if backup.is_symlink() or not manifest_path.is_file():
        raise InstallError('Backup manifest is unavailable.')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('version') != 1 or manifest.get('status') not in ('applied', 'partial'):
        raise InstallError('Backup is not an applied installation.')
    changed = []
    for item in manifest['changes']:
        if item.get('after') is None:
            continue
        actual = fingerprint(Path(item['path']))
        if actual == item['before']:
            continue
        if actual != item['after']:
            raise InstallError('Installed content changed after backup; rollback refused.')
        changed.append(item)
    for item in reversed(changed):
        path = Path(item['path'])
        if fingerprint(path) != item['after']:
            raise InstallError('Installed content changed during rollback; rollback refused.')
        saved = backup / item['backup']
        remove_entry(path)
        before = item['before']
        if before['kind'] == 'file':
            atomic_write(path, saved.read_bytes(), before['mode'])
        elif before['kind'] == 'directory':
            original = backup / (item['backup'] + '.original')
            if original.is_dir():
                os.rename(original, path)
            else:
                shutil.copytree(saved, path, symlinks=True)
        elif before['kind'] == 'symlink':
            path.symlink_to(before['target'])
    manifest['status'] = 'rolled-back'
    atomic_write(manifest_path, (json.dumps(manifest, indent=2) + '\n').encode())


def plan_install(root: Path, home: Path, python: Path):
    root = root.resolve()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise InstallError('Selected Python executable is unavailable.')
    for name in ('src/desktop_client.py', 'src/desktop_host/index.mjs', 'src/desktop_host/controller.mjs', 'src/desktop_host/package.json', 'skill/deepseek-subagent/SKILL.md', 'config/bridge.example.json'):
        candidate = root / name
        if candidate.is_symlink() or not candidate.is_file():
            raise InstallError('Plugin package is incomplete; expected production sources are missing.')
        try:
            candidate.resolve().relative_to(root)
        except ValueError:
            raise InstallError('Plugin source paths must remain inside the package.') from None
    for relative in ('config', 'data', 'data/global', 'data/projects', 'run', 'data/global/installation-backups'):
        if (root / relative).is_symlink():
            raise InstallError('Package configuration and state paths must be physical directories.')
    profile_dir = home / '.dsh/profiles/desktop'
    if not profile_dir.is_dir():
        raise InstallError('DSH Desktop profile was not found; open/install DSH first.')
    codex_path = home / '.codex/config.toml'
    yaml_path = profile_dir / 'cordis.patch.yml'
    codex_text, yaml_text = text_file(codex_path), text_file(yaml_path)
    previous = old_project_root(yaml_text)
    changes = []
    def write(path, payload):
        if fingerprint(path)['kind'] == 'file' and path.read_bytes() == payload:
            return
        changes.append({'path': str(path), 'before': fingerprint(path), 'payload': payload, 'kind': 'write'})
    write(codex_path, update_toml(codex_text, root, python).encode())
    write(yaml_path, update_yaml(yaml_text, root).encode())
    config = root / 'config/bridge.json'
    if config.exists() or config.is_symlink():
        text_file(config)
        try:
            json.loads(config.read_text())
        except (json.JSONDecodeError, UnicodeError):
            raise InstallError('Existing bridge configuration is invalid; preserve and repair it manually.') from None
    else:
        try:
            defaults = json.loads((root / 'config/bridge.example.json').read_text())
        except json.JSONDecodeError:
            raise InstallError('Package default configuration is invalid.') from None
        write(config, (json.dumps(defaults, ensure_ascii=False, indent=2) + '\n').encode())
    for target, source, kind in (
        (profile_dir / 'node_modules/@local/codex-dsh-desktop-link', root / 'src/desktop_host', 'host'),
        (home / '.codex/skills/deepseek-subagent', root / 'skill/deepseek-subagent', 'skill'),
    ):
        validate_owned(target, source, previous, kind)
        if target.is_symlink() and target.resolve() == source.resolve():
            continue
        changes.append({'path': str(target), 'before': fingerprint(target), 'target': str(source), 'kind': 'symlink'})
    return changes


def apply_install(root: Path, changes):
    # Verify all destinations before creating a backup or changing registration.
    for item in changes:
        if fingerprint(Path(item['path'])) != item['before']:
            raise InstallError('Concurrent installation change detected; refusing to overwrite.')
    for relative in ('data', 'data/global', 'data/projects', 'run', 'data/global/installation-backups'):
        private_directory(root / relative)
    backup = root / 'data/global/installation-backups' / (time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:10])
    private_directory(backup)
    manifest = {'version': 1, 'status': 'partial', 'root': str(root), 'changes': []}
    for index, item in enumerate(changes):
        stored = {key: value for key, value in item.items() if key not in ('payload', 'target', 'kind')}
        stored.update(backup=f'entry-{index}', after=None)
        path = Path(item['path'])
        saved = backup / stored['backup']
        if item['before']['kind'] == 'file':
            atomic_write(saved, path.read_bytes())
        elif item['before']['kind'] == 'directory':
            shutil.copytree(path, saved, symlinks=True)
            # Directory backups contain only the already-verified plugin files.
            for nested in saved.rglob('*'):
                if not nested.is_symlink():
                    os.chmod(nested, 0o700 if nested.is_dir() else 0o600)
            os.chmod(saved, 0o700)
        manifest['changes'].append(stored)
    manifest_path = backup / 'manifest.json'
    atomic_write(manifest_path, (json.dumps(manifest, indent=2) + '\n').encode())
    try:
        for item, stored in zip(changes, manifest['changes']):
            path = Path(item['path'])
            if fingerprint(path) != item['before']:
                raise InstallError('Concurrent installation change detected; refusing to overwrite.')
            if item['kind'] == 'write':
                stored['after'] = {'kind': 'file', 'sha256': hashlib.sha256(item['payload']).hexdigest(),
                                   'mode': item['before'].get('mode', 0o600)}
                atomic_write(path, item['payload'], stored['after']['mode'])
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Rename owned directories rather than destroying the previous entity.
                if path.exists() or path.is_symlink():
                    stored['after'] = {'kind': 'absent'}
                    os.rename(path, backup / (stored['backup'] + '.original'))
                    if fingerprint(path) != stored['after']:
                        raise InstallError('Installed content changed concurrently; recovery requires review.')
                    atomic_write(manifest_path, (json.dumps(manifest, indent=2) + '\n').encode())
                intermediate = stored['after']
                stored['after'] = {'kind': 'symlink', 'target': item['target']}
                try:
                    path.symlink_to(item['target'], target_is_directory=True)
                except Exception:
                    # A failed link syscall may leave the verified rename's
                    # absent intermediate state, which is still ours to restore.
                    if fingerprint(path) == {'kind': 'absent'}:
                        stored['after'] = intermediate
                    raise
            if fingerprint(path) != stored['after']:
                raise InstallError('Installed content changed concurrently; recovery requires review.')
            atomic_write(manifest_path, (json.dumps(manifest, indent=2) + '\n').encode())
        manifest['status'] = 'applied'
        atomic_write(manifest_path, (json.dumps(manifest, indent=2) + '\n').encode())
    except Exception:
        try:
            # Include the current attempted operation's expected result. Recovery
            # must not omit a committed write just because its verification failed.
            atomic_write(manifest_path, (json.dumps(manifest, indent=2) + '\n').encode())
            rollback(backup)
        except Exception:
            raise InstallError('Installation stopped; automatic rollback was blocked. Preserve the installation backup for review.') from None
        raise InstallError('Installation stopped and registration changes were rolled back.') from None
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--home', type=Path, default=Path.home(), help='User home (test fixtures may override).')
    parser.add_argument('--python', type=Path, default=Path(sys.executable), help='Python executable used for MCP registration.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Apply the reviewed registration plan.')
    mode.add_argument('--check', action='store_true', help='Read-only readiness/registration check.')
    mode.add_argument('--dry-run', action='store_true', help='Read-only plan (the default).')
    parser.add_argument('--rollback', metavar='BACKUP_ID', help='Restore a package installation backup; requires --apply.')
    args = parser.parse_args(argv)
    root, home, python = args.root.resolve(), args.home.resolve(), args.python.resolve()
    try:
        if args.rollback:
            if not args.apply or not re.fullmatch(r'[0-9]{8}T[0-9]{6}-[a-f0-9]{10}', args.rollback):
                raise InstallError('Rollback requires --apply and an exact installation backup ID.')
            rollback(root / 'data/global/installation-backups' / args.rollback)
            print('Installation registration restored. Reconnect Codex MCP and reload DSH when existing work is settled.')
            return 0
        changes = plan_install(root, home, python)
        if not args.apply:
            print(f'Read-only check: {len(changes)} registration/configuration change(s) required.')
            for item in changes:
                # Print paths/actions only; never configuration contents or diagnostics.
                print(f"  {item['kind']}: {item['path']}")
            print('Run with --apply to install. No applications were started or stopped.')
            return 0
        if changes:
            backup = apply_install(root, changes)
            print(f'Installed. Restricted rollback backup: {backup.name}')
        else:
            print('Registration already matches this package; existing configuration and data preserved.')
        print('Reconnect Codex MCP and reload/restart DSH after current work is settled. No applications were restarted.')
        return 0
    except (InstallError, OSError, UnicodeError, ValueError):
        # Raw OS/YAML/JSON errors can include untrusted content or private paths.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, InstallError) else 'Installation check failed; no diagnostic contents were exposed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
