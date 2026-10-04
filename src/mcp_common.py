"""Shared MCP framing and private-state helpers, independent of ACP or DSH policy."""
import asyncio
import contextlib
import json
import os
import uuid


class BridgeError(Exception):
    pass


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise BridgeError("Runtime directory must be a physical directory")
    if path.stat().st_mode & 0o077:
        raise BridgeError("Runtime directory must be private (mode 700)")


def atomic_json(path, value):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(str(tmp), str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


class McpServer:
    def __init__(self, bridge, reader, writer, definitions=(), instructions="", version="0.1.0"):
        self.bridge, self.reader, self.writer = bridge, reader, writer
        self.definitions, self.instructions = definitions, instructions
        self.version = version
        self.initialized = False
        self.tasks = {}
        self.cancellable = set()
        self.mutations = asyncio.Lock()

    async def handle(self, message):
        request_id = message.get("id")
        method, params = message.get("method"), message.get("params", {})
        try:
            if method == "initialize":
                supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
                version = params.get("protocolVersion")
                self.initialized = True
                result = {"protocolVersion": version if version in supported else "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "dsh-subagent-bridge", "version": self.version}, "instructions": self.instructions}
            elif method == "ping":
                result = {}
            elif not self.initialized:
                raise BridgeError("MCP initialize required")
            elif method == "tools/list":
                result = {"tools": [{"name": name, "description": desc, "inputSchema": spec, "annotations": {"readOnlyHint": name in {"dsh_list", "dsh_status"}, "destructiveHint": False, "idempotentHint": name in {"dsh_list", "dsh_status", "dsh_cancel", "dsh_close"}, "openWorldHint": name not in {"dsh_list", "dsh_status", "dsh_permission"}}} for name, desc, spec in self.definitions]}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments", {})
                try:
                    if name in {"dsh_list", "dsh_status", "dsh_permission"}:
                        value = await self.bridge.tool(name, args)
                    else:
                        async with self.mutations:
                            value = await self.bridge.tool(name, args)
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
                except BridgeError as error:
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            else:
                await self.writer({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
                return
            await self.writer({"jsonrpc": "2.0", "id": request_id, "result": result})
        except asyncio.CancelledError:
            # Request cancellation does not abandon an admitted background turn.
            return
        except Exception as error:
            await self.writer({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(error) if isinstance(error, BridgeError) else "Request failed; diagnostics withheld"}})

    async def run(self):
        try:
            while True:
                try:
                    line = await self.reader.readline()
                    if not line:
                        break
                    message = json.loads(line)
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise ValueError()
                except (ValueError, UnicodeError):
                    await self.writer({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid NDJSON request"}})
                    continue
                if "id" not in message:
                    # Mutation requests are shielded: cancelled admission must still
                    # publish its resulting session id/job id or finish cleanup.
                    if message.get("method") == "notifications/cancelled":
                        key = message.get("params", {}).get("requestId")
                        task = self.tasks.get(key)
                        if task and key in self.cancellable:
                            task.cancel()
                    continue
                request_id = message["id"]
                if not isinstance(request_id, (int, str)) or isinstance(request_id, bool) or request_id in self.tasks or len(self.tasks) >= getattr(self.bridge, 'max_pending_requests', 32):
                    await self.writer({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid or duplicate request id"}})
                    continue
                task = asyncio.create_task(self.handle(message))
                self.tasks[request_id] = task
                if message.get("params", {}).get("name") in {"dsh_list", "dsh_status"}:
                    self.cancellable.add(request_id)
                def finished(done, key=request_id):
                    self.tasks.pop(key, None)
                    self.cancellable.discard(key)
                task.add_done_callback(finished)
        finally:
            # Finish short admissions first, then quiesce all DSH sessions.
            tasks = list(self.tasks.values())
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=self.bridge.rpc_timeout + self.bridge.cancel_grace)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.bridge.shutdown()


