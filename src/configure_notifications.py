#!/usr/bin/env python3
"""Check an exact local parent, optionally save the scoped notification config.

No model requests, DSH prompts, process signals, service startup or credential
inspection. The next legal bridge call refreshes notification settings; verify
runtime diagnostics after saving.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
try:
    from .mcp_common import BridgeError, atomic_json, private_dir
    from .codex_notifications import (AppServerClient, DeliveryRejected,
        DeliveryUnavailable, UNAVAILABLE_CODES, settings)
    from .layout import PackageLayout
    from .project_store import ProjectStore
except ImportError:
    from mcp_common import BridgeError, atomic_json, private_dir
    from codex_notifications import (AppServerClient, DeliveryRejected,
        DeliveryUnavailable, UNAVAILABLE_CODES, settings)
    from layout import PackageLayout
    from project_store import ProjectStore



async def configure(config_file, parent, url, *, apply=False, client=None,
                    expected_parent_model=None, enabled=True):
    if expected_parent_model is not None and (not isinstance(expected_parent_model, str) or not expected_parent_model.strip() or len(expected_parent_model) > 128 or any(ord(char) < 32 for char in expected_parent_model)):
        raise BridgeError('Invalid expected parent model')
    if not isinstance(enabled, bool): raise BridgeError('Invalid notification enabled flag')
    config_file = Path(config_file)
    if config_file.is_symlink() or not config_file.is_file():
        raise BridgeError('Physical project configuration required')
    original = config_file.read_bytes()
    config = json.loads(original)
    previous = config.get('codex_notifications', {})
    if url is None and isinstance(previous, dict) and previous:
        proposed = dict(previous, enabled=enabled)
    else:
        proposed = {'enabled': enabled, 'websocket_url': url or 'ws://127.0.0.1:4500',
                    'rpc_timeout_seconds': 10, 'retry_seconds': 30}
    notification = settings(proposed)
    record = {'checked_utc': datetime.now(timezone.utc).isoformat(),
              'parent_thread_id': parent, 'websocket_url': notification['websocket_url'], 'ready': False,
              'expected_parent_model': expected_parent_model,
              'config_applied': False, 'controller_reloaded': False,
              'completion_events_sent': 0, 'model_requests_sent': 0,
              'thread_resumed': False}
    if enabled:
        try:
            metadata = await (client or AppServerClient(notification)).probe(parent)
        except DeliveryUnavailable as error:
            record['error'] = str(error) if str(error) in UNAVAILABLE_CODES else 'connection_unavailable'
            return record
        except DeliveryRejected:
            record['error'] = 'app_server_rejected'
            return record
        record.update(metadata)
        if metadata.get('parent_loaded') is not True:
            record['error'] = 'parent_not_loaded_in_this_server'
            return record
        if expected_parent_model is not None and metadata.get('parent_model') != expected_parent_model:
            record['error'] = 'development_parent_model_mismatch'
            return record
    layout = PackageLayout.from_config(config_file)
    # Reading an absent index is a valid empty store; no prepare/save on dry-run.
    store = ProjectStore(layout)
    counts = Counter()
    for session in store.state['sessions'].values():
        counts.update(job['state'] for job in session.get('jobs', {}).values())
    record['bridge_job_states'] = dict(counts)
    record.update(ready=True, controller_reload_required=False,
                  runtime_refresh_required=config.get('codex_notifications') != proposed,
                  runtime_readback='After the next legal dsh_list, verify completion_notifications enabled/config_state/error and endpoint; saving does not establish runtime activation.',
                  proposed_notification_config=proposed)
    if not apply:
        return record
    if config_file.is_symlink() or config_file.read_bytes() != original:
        raise BridgeError('Project configuration changed during verification; not overwritten')
    private_dir(layout.root / 'data')
    private_dir(layout.global_data)
    backups = layout.global_data / 'notification-backups'
    private_dir(backups)
    backup = backups / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex)
    private_dir(backup)
    backup_file = backup / config_file.name
    with backup_file.open('xb') as saved:
        backup_file.chmod(0o600)
        saved.write(original)
    updated = dict(config, codex_notifications=proposed)
    atomic_json(config_file, updated)
    record.update(config_applied=True, backup_file=str(backup_file.resolve()))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/bridge.json',
                        help='Plugin configuration (default: config/bridge.json)')
    parser.add_argument('--parent-thread-id', help='Required when enabling; disabling does not contact the server')
    parser.add_argument('--websocket-url', help='Override endpoint; otherwise retain existing endpoint or use ws://127.0.0.1:4500')
    parser.add_argument('--expected-parent-model',
                        help='Optional exact expected model for this invocation only')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--enable', dest='enabled', action='store_true', default=True)
    mode.add_argument('--disable', dest='enabled', action='store_false')
    parser.add_argument('--apply', action='store_true',
                        help='Save only the notification block after verification; never reload')
    args = parser.parse_args()
    if args.enabled and not args.parent_thread_id: parser.error('--parent-thread-id is required with --enable')
    try:
        record = asyncio.run(configure(args.config,
            args.parent_thread_id, args.websocket_url, apply=args.apply,
            expected_parent_model=args.expected_parent_model, enabled=args.enabled))
    except BridgeError as error:
        print(json.dumps({'ready': False, 'config_applied': False, 'error': str(error)}))
        return 2
    except Exception:
        print(json.dumps({'ready': False, 'config_applied': False,
                          'error': 'configuration_check_failed'}))
        return 2
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record['ready'] else 2


if __name__ == '__main__':
    sys.exit(main())
