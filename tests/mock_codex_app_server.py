"""Synthetic WebSocket App Server; temporary UDS, no real threads or models."""
import asyncio
import base64
import contextlib
import hashlib
import json
from pathlib import Path
import struct
import tempfile


class MockAppServer:
    def __init__(self, scenario):
        self.scenario = scenario
        self.messages = []
        self.tasks = set()
        self.connections = 0
        self.disconnected = asyncio.Event()

    async def __aenter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsh-ws-', dir='/tmp')
        self.path = str(Path(self.temp.name)/'s')
        try: self.server = await asyncio.start_unix_server(self.handle, path=self.path)
        except BaseException:
            self.temp.cleanup()
            raise
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.tasks): task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.temp.cleanup()

    async def send(self, writer, value):
        data = json.dumps(value).encode()
        prefix = bytes([0x81, len(data)]) if len(data) < 126 else bytes([0x81, 126]) + struct.pack('!H', len(data))
        writer.write(prefix + data)
        await writer.drain()

    async def receive(self, reader):
        first, second = await reader.readexactly(2)
        assert first & 0x80 and second & 0x80
        size = second & 0x7f
        if size == 126: size = struct.unpack('!H', await reader.readexactly(2))[0]
        elif size == 127: size = struct.unpack('!Q', await reader.readexactly(8))[0]
        assert size < 8388608
        mask = await reader.readexactly(4)
        data = await reader.readexactly(size)
        data = bytes(v ^ mask[i % 4] for i, v in enumerate(data))
        if first & 15 == 8: return None
        assert first == 0x81
        return json.loads(data)

    async def handle(self, reader, writer):
        task = asyncio.current_task(); self.tasks.add(task); self.connections += 1
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            assert headers.startswith(b'GET / HTTP/1.1\r\n')
            fields = dict(line.split(b': ', 1) for line in headers.split(b'\r\n')[1:-2])
            key = fields[b'Sec-WebSocket-Key']
            if self.scenario == 'slow-handshake':
                await reader.read()
                return
            accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
            writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
            await writer.drain()
            while True:
                value = await self.receive(reader)
                if value is None: return
                self.messages.append(value)
                method = value['method']; key = value.get('id')
                if key is None: continue
                if method == 'initialize':
                    await self.send(writer, {'id': key, 'method': 'item/commandExecution/requestApproval', 'params': {'secret': 'synthetic-secret'}})
                    await self.send(writer, {'id': True, 'error': {'message': 'synthetic-secret'}})
                    result = {'userAgent': 'synthetic-codex/0.160.0'}
                elif method == 'thread/loaded/list':
                    result = {'data': [] if self.scenario == 'stored-only' else [
                        '00000000-0000-4000-8000-000000000001'], 'nextCursor': None}
                    if self.scenario == 'invalid-loaded-list': result['data'] = [True]
                    if self.scenario == 'partial-loaded-list': result['nextCursor'] = 'synthetic-cursor'
                elif method == 'thread/read':
                    if self.scenario == 'slow-before':
                        await reader.read(); return
                    result = {'thread': {'id': 'foreign' if self.scenario == 'foreign' else value['params']['threadId'],
                        'status': {'type': 'notLoaded' if self.scenario == 'not-loaded' else 'active' if self.scenario == 'active' else 'idle'},
                        'canAcceptDirectInput': self.scenario != 'no-input', 'model': 'synthetic-model', 'preview': 'synthetic-secret'}}
                elif method == 'turn/start':
                    if self.scenario == 'disconnect': return
                    if self.scenario == 'slow-after':
                        await reader.read(); return
                    if self.scenario == 'reject':
                        await self.send(writer, {'id': key, 'error': {'code': -32000, 'message': 'synthetic-secret'}})
                        continue
                    result = {'turn': {'id': 'synthetic-turn', 'status': 'inProgress', 'items': []}}
                else: raise AssertionError('Unexpected method')
                await self.send(writer, {'id': key, 'result': result})
        except (asyncio.IncompleteReadError, ConnectionError): pass
        finally:
            writer.close()
            with contextlib.suppress(Exception): await writer.wait_closed()
            self.tasks.discard(task); self.disconnected.set()
