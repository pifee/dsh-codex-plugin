"""Offline controller process: actual bridge/MCP with isolated synthetic history."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
from src.mcp_common import BridgeError, McpServer, atomic_json
from src.desktop_bridge import DesktopBridge, TOOLS, INSTRUCTIONS
from src.layout import PackageLayout


class SyntheticDesktop:
    def __init__(self, root):
        self.file = root / 'synthetic-dsh.json'
        self.data = json.loads(self.file.read_text()) if self.file.exists() else {'parents': {}, 'sessions': {}}
        self.queues = {}; self.tasks = {}
    def save(self): atomic_json(self.file, self.data)
    def event(self, sid, kind, data):
        records = self.data['sessions'][sid]['records']
        event = {'seq': len(records), 'type': kind, 'data': data}
        records.append({'type': 'event', 'event': event}); self.save()
        for queue in self.queues.get(sid, []): queue.put_nowait({'type': 'event', 'event': event})
    async def call(self, value):
        action = value['action']
        if action == 'new':
            parent = value['parent_id']; workspace = self.data['parents'].setdefault(parent, uuid.uuid4().hex)
            sid = uuid.uuid4().hex
            self.data['sessions'][sid] = {'records': [], 'messages': [], 'running': False, 'turn': 0}
            self.save()
            return {'sessionId': sid, 'workspaceId': workspace, 'parent': parent, 'childId': value['child_id'], 'title': value['name'], 'cwd': '/synthetic/' + hashlib.sha256(parent.encode()).hexdigest(), 'state': 'ready'}
        sid = value['session_id']
        if sid not in self.data['sessions']: raise BridgeError('Foreign session')
        session = self.data['sessions'][sid]
        if action == 'preflight': return {'capabilities': {'modelToolCount': 0, 'modelToolNames': [], 'approvalPolicy': 'ask'}, 'inferencePolicy': 'synthetic-test-only'}
        if action == 'prompt':
            if session['running']: raise BridgeError('Conversation has running/queued work')
            session['turn'] += 1; session['running'] = True; session['messages'].append(value['prompt']); self.save()
            self.event(sid, 'turn/start', {'turn': session['turn']})
            self.event(sid, 'user/message', {'source': {'rpcId': value['request_id']}})
            if value['prompt'] != 'hold': self.tasks[sid] = asyncio.create_task(self.complete(sid, value['prompt']))
            return {'accepted': True}
        if action == 'cancel':
            if session['running']:
                task = self.tasks.pop(sid, None)
                if task: task.cancel()
                session['running'] = False; self.event(sid, 'turn/end', {'turn': session['turn'], 'reason': {'kind': 'cancelled'}})
            return {'accepted': True}
        raise BridgeError('Unsupported synthetic action')
    async def complete(self, sid, text):
        await asyncio.sleep(.15)
        session = self.data['sessions'][sid]
        for queue in self.queues.get(sid, []): queue.put_nowait({'type': 'assistant-stream'})
        self.event(sid, 'assistant/message', {'turn': session['turn'], 'message': {'content': [{'type': 'text', 'text': 'synthetic answer: ' + text}]}})
        session['running'] = False; self.event(sid, 'turn/end', {'turn': session['turn'], 'reason': {'kind': 'completed'}})
    async def follow(self, sid):
        if sid not in self.data['sessions']: raise BridgeError('Foreign session')
        queue = asyncio.Queue(); self.queues.setdefault(sid, []).append(queue)
        try:
            records = self.data['sessions'][sid]['records']
            yield {'type': 'snapshot', 'header': {'id': sid}, 'cursor': len(records)-1, 'records': list(records)}
            while True: yield await queue.get()
        finally: self.queues[sid].remove(queue)


async def main(file):
    bridge = DesktopBridge(file, SyntheticDesktop(PackageLayout.from_config(file).root))
    reader = asyncio.StreamReader(limit=bridge.max_frame_bytes)
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    async def write(value):
        sys.stdout.write(json.dumps(value) + '\n'); sys.stdout.flush()
    await McpServer(bridge, reader, write, TOOLS, INSTRUCTIONS).run()


if __name__ == '__main__': asyncio.run(main(sys.argv[-1]))
