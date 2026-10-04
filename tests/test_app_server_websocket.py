"""WebSocket wire-contract tests against disposable local Unix socket peers."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from src.app_server_websocket import WebSocket, WebSocketError, endpoint


GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
DEADLINE = 2


def server_frame(payload=b'', *, opcode=1, final=True, rsv=0, masked=False):
    """Build RFC 6455 frames, including deliberately invalid server frames."""
    first = (0x80 if final else 0) | rsv | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, (0x80 if masked else 0) | length])
    elif length <= 65535:
        header = bytes([first, (0x80 if masked else 0) | 126]) + struct.pack('!H', length)
    else:
        header = bytes([first, (0x80 if masked else 0) | 127]) + struct.pack('!Q', length)
    if masked:
        mask = b'wire'
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        header += mask
    return header + payload


async def client_frame(reader):
    first, second = await reader.readexactly(2)
    length = second & 127
    if length == 126:
        length = struct.unpack('!H', await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack('!Q', await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if second & 128 else None
    payload = await reader.readexactly(length)
    if mask is not None:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return first, mask, payload


def upgrade_response(headers, *, status='101 Switching Protocols', accept=None,
                     upgrade='websocket', connection='Upgrade'):
    if accept is None:
        key = headers['sec-websocket-key']
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode('ascii')).digest()).decode('ascii')
    lines = [f'HTTP/1.1 {status}', f'Sec-WebSocket-Accept: {accept}']
    if upgrade is not None:
        lines.append(f'Upgrade: {upgrade}')
    if connection is not None:
        lines.append(f'Connection: {connection}')
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('ascii')


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # macOS Unix socket names have a short length limit.
        self.temp = tempfile.TemporaryDirectory(prefix='ws-', dir='/tmp')
        self.servers = []
        self.tasks = []
        self.writers = []
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            try:
                await asyncio.wait_for(client.close(), DEADLINE)
            except (WebSocketError, OSError, asyncio.TimeoutError):
                pass
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for writer in self.writers:
            writer.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(writer.wait_closed() for writer in self.writers), return_exceptions=True)
        self.temp.cleanup()

    async def peer(self, action=None, response=None, tcp=False):
        path = str(Path(self.temp.name) / f'{len(self.servers)}.sock')
        request = asyncio.get_running_loop().create_future()
        accepted = asyncio.get_running_loop().create_future()

        async def handle(reader, writer):
            self.writers.append(writer)
            try:
                raw = await reader.readuntil(b'\r\n\r\n')
                lines = raw.decode('ascii').split('\r\n')
                headers = {}
                for line in lines[1:]:
                    if line:
                        key, value = line.split(':', 1)
                        headers[key.lower()] = value.strip()
                request.set_result((lines[0], headers))
                writer.write(upgrade_response(headers) if response is None else response(headers))
                await writer.drain()
                if action is not None:
                    return await action(reader, writer)
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        def connected(reader, writer):
            task = asyncio.create_task(handle(reader, writer))
            self.tasks.append(task)
            accepted.set_result(task)

        if tcp:
            server = await asyncio.start_server(connected, host='127.0.0.1', port=0)
            path = 'ws://127.0.0.1:' + str(server.sockets[0].getsockname()[1]) + '/'
        else:
            server = await asyncio.start_unix_server(connected, path=path)
        self.servers.append(server)
        return path, request, accepted

    async def connect(self, path, **kwargs):
        client = await asyncio.wait_for(WebSocket.connect(socket_path=path, **kwargs), DEADLINE)
        self.clients.append(client)
        return client

    async def receive(self, client):
        return await asyncio.wait_for(client.receive_json(), DEADLINE)

    async def frames_peer(self, frames):
        async def action(reader, writer):
            writer.write(frames)
            await writer.drain()
            # Keep the peer open: rejection must not depend on EOF.
            await reader.read()
        return await self.peer(action)

    async def test_upgrade_request_and_masked_json_round_trip_all_length_encodings(self):
        values = [{'text': '中文 😀', 'items': [1, True, None]}, {'text': 'x' * 300}, {'text': 'y' * 70000}]

        async def action(reader, writer):
            received = []
            for value in values:
                first, mask, payload = await client_frame(reader)
                self.assertEqual(first, 0x81)
                self.assertIsNotNone(mask, 'Every client data frame must be masked')
                received.append(json.loads(payload.decode('utf-8')))
            writer.write(server_frame(json.dumps(received, ensure_ascii=False).encode('utf-8')))
            await writer.drain()
            await reader.read()

        path, request, accepted = await self.peer(action)
        client = await self.connect(path)
        request_line, headers = await request
        self.assertTrue(request_line.startswith('GET '))
        self.assertTrue(request_line.endswith(' HTTP/1.1'))
        self.assertEqual(headers['upgrade'].lower(), 'websocket')
        self.assertIn('upgrade', [item.strip().lower() for item in headers['connection'].split(',')])
        self.assertEqual(headers['sec-websocket-version'], '13')
        self.assertEqual(len(base64.b64decode(headers['sec-websocket-key'], validate=True)), 16)
        for value in values:
            await client.send_json(value)
        self.assertEqual(await self.receive(client), values)
        await client.close()
        await asyncio.wait_for(await accepted, DEADLINE)

    async def test_upgrade_header_names_and_connection_tokens_are_case_insensitive(self):
        def response(headers):
            return upgrade_response(headers, upgrade='WebSocket', connection='keep-alive, UpGrAdE').replace(
                b'Sec-WebSocket-Accept:', b'sec-websocket-accept:')
        path, _, _ = await self.peer(response=response)
        await self.connect(path)

    async def test_rejects_unsuccessful_or_invalid_upgrade(self):
        cases = [dict(status='200 OK'), dict(accept='not-the-request-key'),
                 dict(upgrade=None), dict(upgrade='h2c'), dict(connection=None),
                 dict(connection='keep-alive')]
        for options in cases:
            with self.subTest(options=options):
                path, _, _ = await self.peer(response=lambda headers: upgrade_response(headers, **options))
                with self.assertRaises(WebSocketError):
                    await self.connect(path)

    async def test_eof_during_upgrade_is_a_transport_error(self):
        async def action(reader, writer):
            writer.close()
        path, _, _ = await self.peer(action, response=lambda _: b'HTTP/1.1 101 Switching Protocols\r\n')
        with self.assertRaises(WebSocketError):
            await self.connect(path)

    async def test_fragmented_utf8_with_interleaved_ping_and_masked_pong(self):
        value = {'text': 'before 😀 中文 after'}
        payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
        split = payload.index('😀'.encode('utf-8')) + 2

        async def action(reader, writer):
            writer.write(server_frame(payload[:split], final=False) + server_frame(b'are-you-there', opcode=9))
            await writer.drain()
            first, mask, pong = await client_frame(reader)
            self.assertEqual(first, 0x8A)
            self.assertIsNotNone(mask)
            self.assertEqual(pong, b'are-you-there')
            writer.write(server_frame(payload[split:], opcode=0))
            await writer.drain()
            await reader.read()

        path, _, accepted = await self.peer(action)
        client = await self.connect(path)
        self.assertEqual(await self.receive(client), value)
        await client.close()
        await asyncio.wait_for(await accepted, DEADLINE)

    async def test_unsolicited_pong_does_not_replace_json_message(self):
        path, _, _ = await self.frames_peer(server_frame(b'pong', opcode=10) + server_frame(b'{"ok":true}'))
        client = await self.connect(path)
        self.assertEqual(await self.receive(client), {'ok': True})

    async def test_control_frames_work_when_data_message_limit_is_one_byte(self):
        async def action(reader, writer):
            writer.write(server_frame(b'p' * 125, opcode=9))
            await writer.drain()
            first, mask, pong = await client_frame(reader)
            self.assertEqual(first, 0x8A)
            self.assertIsNotNone(mask)
            self.assertEqual(pong, b'p' * 125)
            writer.write(server_frame(b'1'))
            await writer.drain()
            first, mask, payload = await client_frame(reader)
            self.assertEqual(first, 0x88)
            self.assertIsNotNone(mask)
            self.assertNotEqual(len(payload), 1)
            self.assertLessEqual(len(payload), 125)
            self.assertEqual(await reader.read(), b'')
        path, _, accepted = await self.peer(action)
        client = await self.connect(path, max_message_bytes=1)
        self.assertEqual(await self.receive(client), 1)
        await asyncio.wait_for(client.close(), DEADLINE)
        await asyncio.wait_for(await accepted, DEADLINE)

    async def test_rejects_invalid_frame_types_and_fragment_sequences(self):
        cases = {
            'reserved bit': server_frame(b'{}', rsv=0x40),
            'binary': server_frame(b'{}', opcode=2),
            'masked server': server_frame(b'{}', masked=True),
            'reserved opcode': server_frame(b'{}', opcode=3),
            'continuation without start': server_frame(b'{}', opcode=0),
            'new text before continuation': server_frame(b'{', final=False) + server_frame(b'{}'),
            'fragmented ping': server_frame(b'ping', opcode=9, final=False),
            'oversized ping': server_frame(b'x' * 126, opcode=9),
            'one-byte close payload': server_frame(b'x', opcode=8),
            'invalid utf8': server_frame(b'"\xff"'),
            'invalid json': server_frame(b'{broken'),
        }
        for name, frames in cases.items():
            with self.subTest(name=name):
                path, _, _ = await self.frames_peer(frames)
                client = await self.connect(path)
                with self.assertRaises(WebSocketError):
                    await self.receive(client)
                await client.close()

    async def test_rejects_noncanonical_and_invalid_64_bit_lengths_without_waiting_for_payload(self):
        cases = {
            'unnecessary 16-bit length': b'\x81\x7e\x00\x02{}',
            'unnecessary 64-bit length': b'\x81\x7f' + struct.pack('!Q', 2) + b'{}',
            '64-bit high bit': b'\x81\x7f' + struct.pack('!Q', 1 << 63),
        }
        for name, frames in cases.items():
            with self.subTest(name=name):
                path, _, _ = await self.frames_peer(frames)
                client = await self.connect(path)
                with self.assertRaises(WebSocketError):
                    await self.receive(client)
                await client.close()

    async def test_fragment_message_limit_counts_combined_utf8_bytes(self):
        payload = json.dumps({'text': '😀😀'}, ensure_ascii=False).encode('utf-8')
        frames = server_frame(payload[:8], final=False) + server_frame(payload[8:], opcode=0)
        path, _, _ = await self.frames_peer(frames)
        client = await self.connect(path, max_message_bytes=len(payload))
        self.assertEqual(await self.receive(client), {'text': '😀😀'})
        path, _, _ = await self.frames_peer(frames)
        client = await self.connect(path, max_message_bytes=len(payload) - 1)
        with self.assertRaises(WebSocketError):
            await self.receive(client)

    async def test_oversized_advertised_message_is_rejected_without_payload(self):
        path, _, _ = await self.frames_peer(b'\x81\x7e' + struct.pack('!H', 5000))
        client = await self.connect(path, max_message_bytes=100)
        with self.assertRaises(WebSocketError):
            await self.receive(client)

    async def test_message_limit_applies_to_outgoing_utf8_bytes(self):
        path, _, _ = await self.peer()
        client = await self.connect(path, max_message_bytes=8)
        with self.assertRaises(WebSocketError):
            await client.send_json({'text': '😀😀'})

    async def test_eof_between_or_inside_frames_is_a_transport_error(self):
        for partial in [b'', b'\x81', b'\x81\x05ab', server_frame(b'{', final=False)]:
            with self.subTest(partial=partial):
                async def action(reader, writer):
                    writer.write(partial)
                    await writer.drain()
                    writer.close()
                path, _, _ = await self.peer(action)
                client = await self.connect(path)
                with self.assertRaises(WebSocketError):
                    await self.receive(client)

    async def test_server_close_is_reported_and_client_cleanup_finishes(self):
        path, _, accepted = await self.frames_peer(server_frame(struct.pack('!H', 1000), opcode=8))
        client = await self.connect(path)
        with self.assertRaises(WebSocketError):
            await self.receive(client)
        await asyncio.wait_for(client.close(), DEADLINE)
        await asyncio.wait_for(await accepted, DEADLINE)

    async def test_close_is_idempotent_and_releases_socket_when_peer_does_not_reply(self):
        async def action(reader, writer):
            first, mask, payload = await client_frame(reader)
            self.assertEqual(first, 0x88)
            self.assertIsNotNone(mask)
            self.assertLessEqual(len(payload), 125)
            # No close response: local cleanup must still be bounded.
            self.assertEqual(await reader.read(), b'')
        path, _, accepted = await self.peer(action)
        client = await self.connect(path)
        await asyncio.wait_for(client.close(), DEADLINE)
        await asyncio.wait_for(client.close(), DEADLINE)
        await asyncio.wait_for(await accepted, DEADLINE)
        with self.assertRaises(WebSocketError):
            await client.send_json({'after': 'close'})

    async def test_invalid_endpoint_selection_is_rejected_before_connecting(self):
        # Default selection is valid; inspect it without touching a real socket.
        self.assertEqual(endpoint()[0], 'unix')
        cases = [{'socket_path': 'relative.sock'},
                 {'socket_path': '/tmp/unused.sock', 'websocket_url': 'ws://127.0.0.1:1234'},
                 {'websocket_url': 'ws://example.com:1234'},
                 {'websocket_url': 'wss://127.0.0.1:1234'},
                 {'websocket_url': 'ws://0.0.0.0:1234'},
                 {'websocket_url': 'ws://user:secret@127.0.0.1:1234'}]
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaises(WebSocketError):
                    await asyncio.wait_for(WebSocket.connect(**options), DEADLINE)

    async def test_loopback_websocket_url_performs_masked_round_trip(self):
        async def action(reader, writer):
            first, mask, payload = await client_frame(reader)
            self.assertEqual(first, 0x81)
            self.assertIsNotNone(mask)
            self.assertEqual(json.loads(payload), {'synthetic': True})
            writer.write(server_frame(b'{"ack":true}'))
            await writer.drain()
            await reader.read()
        url, request, accepted = await self.peer(action, tcp=True)
        client = await asyncio.wait_for(WebSocket.connect(websocket_url=url), DEADLINE)
        self.clients.append(client)
        await client.send_json({'synthetic': True})
        self.assertEqual(await self.receive(client), {'ack': True})
        line, headers = await request
        self.assertEqual(line, 'GET / HTTP/1.1')
        self.assertEqual(headers['host'], url[len('ws://'):-1])
        await client.close()
        await asyncio.wait_for(await accepted, DEADLINE)


if __name__ == '__main__':
    unittest.main()
