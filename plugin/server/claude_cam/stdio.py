"""MCP over stdio, so Claude Code can start Claude Cam by itself (this is how the plugin runs it).

The first claude-cam process on the machine hosts the phone server on the port. Any other
process (a second Claude Code session, or a `claude-cam serve` service) is reached through its
HTTP MCP endpoint. If the hosting process exits, a waiting one takes over the port, and the
phone app reconnects to it within a few seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading

import aiohttp

from .server import VERSION, CamError, McpEndpoint, PhoneServer, Settings

log = logging.getLogger("claudecam.stdio")


class Bridge:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession):
        self.settings = settings
        self.session = session
        self.server: PhoneServer | None = None
        self.base = f"http://127.0.0.1:{settings.port}"
        self.lock = asyncio.Lock()
        self.peer_version: str | None = None

    async def peer_alive(self) -> bool:
        try:
            async with self.session.get(self.base + "/api/status", timeout=aiohttp.ClientTimeout(total=2)) as r:
                status = await r.json() if r.status == 200 else {}
                self.peer_version = status.get("server_version")
                return self.peer_version is not None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return False

    async def ensure(self) -> bool:
        """True if this process hosts the phone server, False if another claude-cam does."""
        async with self.lock:
            if self.server:
                return True
            server = PhoneServer(self.settings)
            try:
                await server.start()
            except OSError:
                if await self.peer_alive():
                    return False
                raise CamError(
                    f"Port {self.settings.port} is used by another program, so Claude Cam can't start. "
                    "Set the CLAUDE_CAM_PORT environment variable to a free port, and enter the same "
                    "port in the phone app (gear button)."
                ) from None
            self.server = server
            return True

    async def watchdog(self) -> None:
        """Take over the port when the process hosting it goes away."""
        while True:
            await asyncio.sleep(3)
            if self.server is None:
                try:
                    if await self.ensure():
                        log.info("took over hosting the phone server")
                except CamError:
                    pass

    async def call_tool(self, name: str, args: dict) -> list[dict]:
        if await self.ensure():
            return await self.server.tools.call(name, args)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
        try:
            async with self.session.post(self.base + "/mcp", json=body, timeout=aiohttp.ClientTimeout(total=None)) as r:
                result = (await r.json())["result"]
        except aiohttp.ClientConnectionError:
            # The host went away mid-call: take over and answer from here.
            if await self.ensure():
                return await self.server.tools.call(name, args)
            raise CamError(
                "Lost the connection to the Claude Cam server. Tell the user in your reply; it usually "
                "recovers within a few seconds."
            ) from None
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result["content"] if c.get("type") == "text")
            if text.startswith("Unknown tool") and self.peer_version != VERSION:
                raise CamError(
                    f"Another Claude Code session is running an older Claude Cam ({self.peer_version}) and holds the "
                    f"phone connection, so {name} isn't available. Ask the user to restart their other Claude Code "
                    f"sessions (or all of them) so they pick up Claude Cam {VERSION}."
                )
            raise CamError(text)
        return result["content"]


def _stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    # A daemon thread, so a blocked read never keeps the process alive on exit.
    for line in sys.stdin.buffer:
        loop.call_soon_threadsafe(queue.put_nowait, line)
    loop.call_soon_threadsafe(queue.put_nowait, b"")


async def run_stdio(settings: Settings) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    out_lock = asyncio.Lock()

    async def write(msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode()
        async with out_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async with aiohttp.ClientSession() as session:
        bridge = Bridge(settings, session)
        mcp = McpEndpoint(bridge.call_tool)

        async def handle(raw: bytes) -> None:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                return
            for m in msg if isinstance(msg, list) else [msg]:
                reply = await mcp.handle(m)
                if reply is not None:
                    await write(reply)

        # Host straight away so the phone can connect before the first tool call.
        try:
            await bridge.ensure()
        except CamError as e:
            log.warning("%s", e)
        watchdog = asyncio.create_task(bridge.watchdog())
        threading.Thread(target=_stdin_reader, args=(loop, queue), daemon=True).start()
        tasks: set[asyncio.Task] = set()
        try:
            while line := await queue.get():
                if line.strip():
                    task = asyncio.create_task(handle(line))  # long tool calls must not block pings
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        finally:
            watchdog.cancel()
            for task in tasks:
                task.cancel()
            if bridge.server:
                await bridge.server.stop()
