"""Independent proxy process; test-only frames emulate HTTP over anonymous pipes."""
import asyncio
import json
import sys
from src.mcp_common import McpServer
from src.desktop_client import DesktopClient, TOOLS, INSTRUCTIONS


async def main(file):
    futures = {}; sequence = 0; lock = asyncio.Lock()
    async def write(frame):
        async with lock:
            sys.stdout.write(json.dumps(frame) + '\n'); sys.stdout.flush()
    class PipeHttp:
        async def call(self, value):
            nonlocal sequence
            sequence += 1; key = sequence
            future = asyncio.get_running_loop().create_future(); futures[key] = future
            await write({'kind': 'host', 'id': key, 'value': value})
            response = await future
            if response.get('ok') is not True: raise RuntimeError('Synthetic route refused')
            return response['value']
    client = DesktopClient(file, PipeHttp())
    incoming = asyncio.StreamReader(); mcp = asyncio.StreamReader()
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(incoming), sys.stdin.buffer)
    async def pump():
        while True:
            line = await incoming.readline()
            if not line: mcp.feed_eof(); return
            frame = json.loads(line)
            if frame['kind'] == 'mcp': mcp.feed_data((json.dumps(frame['value']) + '\n').encode())
            else: futures.pop(frame['id']).set_result(frame['value'])
    async def output(value): await write({'kind': 'mcp', 'value': value})
    pump_task = asyncio.create_task(pump())
    try: await McpServer(client, mcp, output, TOOLS, INSTRUCTIONS).run()
    finally: pump_task.cancel(); await asyncio.gather(pump_task, return_exceptions=True)


if __name__ == '__main__': asyncio.run(main(sys.argv[-1]))
