#!/usr/bin/env python3
"""Per-client STDIO MCP proxy to one controller on the existing DSH Host route."""
import argparse
import copy
import asyncio
import json
from pathlib import Path
import signal
import sys
try:
    from .mcp_common import BridgeError, McpServer
    from .desktop_bridge import DesktopHttp, TOOLS as RAW_TOOLS, INSTRUCTIONS as RAW_INSTRUCTIONS, DEFAULT_FRAME_BYTES, DEFAULT_RESULT_LIMIT, number
    from .layout import PackageLayout
except ImportError:
    from mcp_common import BridgeError, McpServer
    from desktop_bridge import DesktopHttp, TOOLS as RAW_TOOLS, INSTRUCTIONS as RAW_INSTRUCTIONS, DEFAULT_FRAME_BYTES, DEFAULT_RESULT_LIMIT, number
    from layout import PackageLayout


# Admission formatting stays local; status verbosity selects native diagnostics.
TOOLS = copy.deepcopy(RAW_TOOLS)
for index, (name, description, input_schema) in enumerate(TOOLS):
    if name in {'dsh_new', 'dsh_prompt'}:
        input_schema['properties']['verbose'] = {'type': 'boolean', 'default': False,
            'description': 'Optional, default false. Return full native conversation or job metadata'}
        description = (
            'Create an ordinary DSH conversation grouped by its actual Codex parent. '
            'Default output contains only session_id. verbose returns full conversation metadata. '
            'Optionally use the authorized task project directory.' if name == 'dsh_new' else
            'Submit a normal DSH user message. Default output contains only session_id and job_id. '
            'verbose returns full job metadata including completion_notification when armed. '
            'Read status once after dsh_completion when completion delivery is configured; otherwise check status. '
            'Native DSH owns model, tools and approvals.')
        TOOLS[index] = (name, description, input_schema)
INSTRUCTIONS = RAW_INSTRUCTIONS + (
    ' Default dsh_new output is session_id; dsh_prompt output is session_id and job_id; '
    'dsh_status output is session_id, job_id and the original result. '
    'Use verbose:true on these tools for full metadata, including notification arming and job state/error. '
    'Notification arming is preserved internally even when compact output omits it.')
COMPACT_TOOLS = {'dsh_new', 'dsh_prompt', 'dsh_status'}


def compact_status(value):
    """Return only session/job identity and the unmodified original answer page.

    Empty sessions retain all three fields with null job_id and result.
    """
    if not isinstance(value, dict) or 'session_id' not in value or 'job' not in value:
        raise BridgeError('Invalid shared controller status')
    full = value['job']
    if full is not None and not isinstance(full, dict):
        raise BridgeError('Invalid shared controller status')
    return {'session_id': value['session_id'],
            'job_id': full.get('job_id') if full is not None else None,
            'result': full.get('result') if full is not None else None}


class DesktopClient:
    def __init__(self, config_file, transport=None):
        layout = PackageLayout.from_config(config_file)
        config = json.loads(Path(config_file).read_text())
        if config.get('link_file', 'run/desktop-host/link.json') != 'run/desktop-host/link.json':
            raise BridgeError('Desktop capability path must be the dedicated link file')
        self.rpc_timeout = number(config.get('rpc_timeout_seconds', 10), 'RPC timeout', .001)
        self.cancel_grace = number(config.get('cancel_grace_seconds', 5), 'cancel grace')
        self.max_frame_bytes = number(config.get('max_frame_bytes', DEFAULT_FRAME_BYTES), 'max_frame_bytes', 8192, integer=True)
        self.max_pending_requests = number(config.get('max_pending_requests', 32), 'max_pending_requests', 1, integer=True)
        self.transport = transport or DesktopHttp(layout.link_file, self.rpc_timeout + self.cancel_grace + 2, self.max_frame_bytes)

    async def tool(self, name, args):
        definition = next((item for item in TOOLS if item[0] == name), None)
        if not definition or not isinstance(args, dict) or set(args) - set(definition[2]['properties']) or any(key not in args for key in definition[2]['required']):
            raise BridgeError('Unknown tool or arguments')
        verbose = args.get('verbose', False) if name in COMPACT_TOOLS else False
        if not isinstance(verbose, bool): raise BridgeError('Invalid verbose flag')
        forwarded = {key: value for key, value in args.items() if key != 'verbose'} if name in COMPACT_TOOLS else args
        if name == 'dsh_status':
            forwarded.setdefault('result_limit', DEFAULT_RESULT_LIMIT)
            if verbose: forwarded['verbose'] = True
        try:
            # The STDIO client formats receipts and answer pages for MCP callers.
            result = await self.transport.call({'action': 'mcp', 'name': name, 'arguments': forwarded})
        except BridgeError as error:
            if str(error) == 'Unsupported action/fields':
                raise BridgeError('Connected Host does not support the shared controller protocol')
            raise
        except Exception:
            raise BridgeError('Shared controller request failed; admission may be uncertain; do not automatically retry')
        if not isinstance(result, dict) or not isinstance(result.get('content'), list) or len(result['content']) != 1 or result['content'][0].get('type') != 'text':
            raise BridgeError('Invalid shared controller result')
        if result.get('isError'):
            raise BridgeError(result['content'][0]['text'])
        value = json.loads(result['content'][0]['text'])
        if verbose: return value
        if name == 'dsh_status': return compact_status(value)
        if name == 'dsh_new':
            if not isinstance(value, dict) or not isinstance(value.get('sessionId'), str) or not value['sessionId'].strip():
                raise BridgeError('Invalid shared controller conversation')
            return {'session_id': value['sessionId']}
        if name == 'dsh_prompt':
            if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key].strip() for key in ('session_id', 'job_id')):
                raise BridgeError('Invalid shared controller job')
            return {key: value[key] for key in ('session_id', 'job_id')}
        return value

    async def shutdown(self):
        # A client owns its STDIO connection, never the shared jobs or controller.
        pass


async def serve(file):
    client = DesktopClient(file)
    reader = asyncio.StreamReader(limit=client.max_frame_bytes)
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    lock = asyncio.Lock()
    async def write(value):
        async with lock:
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n'); sys.stdout.flush()
    current = asyncio.current_task()
    for sig in [signal.SIGINT, signal.SIGTERM]: loop.add_signal_handler(sig, current.cancel)
    try: await McpServer(client, reader, write, TOOLS, INSTRUCTIONS).run()
    except asyncio.CancelledError: await client.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try: asyncio.run(serve(args.config))
    except Exception:
        print('Desktop client stopped; diagnostics withheld', file=sys.stderr); sys.exit(1)
