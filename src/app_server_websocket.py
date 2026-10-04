"""Bounded WebSocket JSON transport for a local, existing Codex App Server."""
import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import struct
from urllib.parse import urlsplit


class WebSocketError(Exception):
    """Sanitized local transport error; never includes peer content."""


def endpoint(socket_path=None, websocket_url=None):
    if socket_path is not None and websocket_url is not None:
        raise WebSocketError('Choose one App Server endpoint')
    if websocket_url is None:
        path = str(Path.home() / '.codex/app-server-control/app-server-control.sock') if socket_path is None else socket_path
        if not isinstance(path, str) or not Path(path).is_absolute() or any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise WebSocketError('Invalid App Server socket path')
        return ('unix', path, None, 'localhost', '/')
    try:
        if not isinstance(websocket_url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in websocket_url):
            raise ValueError()
        parsed = urlsplit(websocket_url)
        address = ipaddress.ip_address(parsed.hostname or '')
        if parsed.scheme != 'ws' or not address.is_loopback or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError()
        port = parsed.port
        if port is None or not 1 <= port <= 65535:
            raise ValueError()
        host = str(address)
        authority = ('[' + host + ']' if address.version == 6 else host) + ':' + str(port)
        path = parsed.path or '/'
        path.encode('ascii')
        return ('tcp', host, port, authority, path)
    except (ValueError, TypeError, UnicodeError):
        raise WebSocketError('App Server WebSocket URL must use an explicit loopback IP and port') from None


class WebSocket:
    def __init__(self, reader, writer, maximum):
        self.reader = reader
        self.writer = writer
        self.maximum = maximum
        self.closed = False
        self.write_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, *, socket_path=None, websocket_url=None, max_message_bytes=8 * 1024 * 1024):
        if isinstance(max_message_bytes, bool) or not isinstance(max_message_bytes, int) or max_message_bytes < 1:
            raise WebSocketError('Invalid WebSocket message limit')
        kind, address, port, authority, path = endpoint(socket_path, websocket_url)
        writer = None
        try:
            if kind == 'unix':
                reader, writer = await asyncio.open_unix_connection(address, limit=16384)
            else:
                reader, writer = await asyncio.open_connection(address, port, limit=16384)
            connection = cls(reader, writer, max_message_bytes)
            await connection.handshake(authority, path)
            return connection
        except BaseException as error:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 1)
            if isinstance(error, (OSError, EOFError)):
                raise WebSocketError('App Server connection unavailable') from None
            raise

    async def handshake(self, authority, path):
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = ('GET ' + path + ' HTTP/1.1\r\nHost: ' + authority + '\r\n'
            'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            'Sec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self.writer.write(request.encode('ascii'))
        await self.writer.drain()
        total = 0
        lines = []
        while True:
            try:
                line = await self.reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                raise WebSocketError('Invalid WebSocket handshake') from None
            total += len(line)
            if not line or not line.endswith(b'\r\n') or total > 16384:
                raise WebSocketError('Invalid WebSocket handshake')
            if line == b'\r\n':
                break
            lines.append(line[:-2])
        try:
            status = lines[0].decode('ascii').split(' ', 2)
            if status[:2] != ['HTTP/1.1', '101']:
                raise ValueError()
            headers = {}
            for line in lines[1:]:
                name, value = line.decode('ascii').split(':', 1)
                name = name.lower()
                if not name or name.strip() != name or name in headers:
                    raise ValueError()
                headers[name] = value.strip()
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
            if headers.get('upgrade', '').lower() != 'websocket' or 'upgrade' not in {v.strip().lower() for v in headers.get('connection', '').split(',')} or headers.get('sec-websocket-accept') != accept:
                raise ValueError()
            if 'sec-websocket-extensions' in headers or 'sec-websocket-protocol' in headers:
                raise ValueError()
        except (ValueError, UnicodeError, IndexError):
            raise WebSocketError('Invalid WebSocket handshake') from None

    async def frame(self, opcode, payload):
        if self.closed:
            raise WebSocketError('App Server connection closed')
        size = len(payload)
        if (opcode >= 8 and size > 125) or (opcode < 8 and size > self.maximum):
            raise WebSocketError('WebSocket message exceeds limit')
        if size < 126:
            prefix = bytes((0x80 | opcode, 0x80 | size))
        elif size < 65536:
            prefix = bytes((0x80 | opcode, 0xFE)) + struct.pack('!H', size)
        else:
            prefix = bytes((0x80 | opcode, 0xFF)) + struct.pack('!Q', size)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        async with self.write_lock:
            self.writer.write(prefix + mask + masked)
            await self.writer.drain()

    async def send_json(self, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8')
        await self.frame(1, payload)

    async def receive_json(self):
        message = bytearray()
        fragmented = False
        try:
            while True:
                first, second = await self.reader.readexactly(2)
                final = bool(first & 0x80)
                opcode = first & 0x0F
                if first & 0x70 or second & 0x80 or opcode not in {0, 1, 8, 9, 10}:
                    raise WebSocketError('Invalid WebSocket frame')
                size = second & 0x7F
                if opcode >= 8 and (not final or size > 125):
                    raise WebSocketError('Invalid WebSocket control frame')
                if size == 126:
                    size = struct.unpack('!H', await self.reader.readexactly(2))[0]
                    if size < 126:
                        raise WebSocketError('Invalid WebSocket frame length')
                elif size == 127:
                    size = struct.unpack('!Q', await self.reader.readexactly(8))[0]
                    if size < 65536 or size >= (1 << 63):
                        raise WebSocketError('Invalid WebSocket frame length')
                if opcode < 8 and (size > self.maximum or len(message) + size > self.maximum):
                    raise WebSocketError('WebSocket message exceeds limit')
                payload = await self.reader.readexactly(size)
                if opcode == 8:
                    if len(payload) == 1:
                        raise WebSocketError('Invalid WebSocket close frame')
                    if len(payload) >= 2:
                        code = struct.unpack('!H', payload[:2])[0]
                        if code < 1000 or code >= 5000 or code in {1004, 1005, 1006, 1015}:
                            raise WebSocketError('Invalid WebSocket close frame')
                        payload[2:].decode('utf8')
                    raise WebSocketError('App Server connection closed')
                if opcode == 9:
                    await self.frame(10, payload)
                    continue
                if opcode == 10:
                    continue
                if (opcode == 0 and not fragmented) or (opcode == 1 and fragmented):
                    raise WebSocketError('Invalid WebSocket continuation')
                message.extend(payload)
                if final:
                    return json.loads(message.decode('utf8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                fragmented = True
        except (asyncio.IncompleteReadError, UnicodeError, ValueError):
            raise WebSocketError('Invalid or closed App Server connection') from None

    async def close(self):
        if self.closed:
            return
        try:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.frame(8, struct.pack('!H', 1000)), .25)
        finally:
            self.closed = True
            self.writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.writer.wait_closed(), 1)
