"""A fake TAK Server: a local asyncio TCP server that records what clients
send and can push events to them. Used by the end-to-end test."""

from __future__ import annotations

import asyncio

from takbridge import cot


class FakeTakServer:
    def __init__(self):
        self.received: list[bytes] = []
        self.clients: list[asyncio.StreamWriter] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self._new_event = asyncio.Event()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.clients.append(writer)
        buf = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                events, buf = cot.split_stream(buf)
                self.received.extend(events)
                if events:
                    self._new_event.set()
        finally:
            if writer in self.clients:
                self.clients.remove(writer)

    async def push(self, data: bytes) -> None:
        for w in list(self.clients):
            w.write(data)
            await w.drain()

    async def wait_for(self, predicate, timeout: float = 5.0) -> bytes:
        async def scan():
            seen = 0
            while True:
                for data in self.received[seen:]:
                    if predicate(cot.parse_event(data)):
                        return data
                seen = len(self.received)
                self._new_event.clear()
                await self._new_event.wait()
        return await asyncio.wait_for(scan(), timeout)

    async def wait_connected(self, timeout: float = 5.0) -> None:
        async def poll():
            while not self.clients:
                await asyncio.sleep(0.02)
        await asyncio.wait_for(poll(), timeout)

    async def stop(self) -> None:
        for w in list(self.clients):
            w.close()
        if self.server:
            self.server.close()
