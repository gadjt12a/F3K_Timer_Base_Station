#!/usr/bin/env python3
"""F3K Base Station — TCP server on port 8765."""

import asyncio
import logging
import os
import sys
import time

import uvicorn

from frontend.app import app as web_app
from frontend.db import init_db
from frontend.state_machine import CompetitionStateMachine

DB_PATH = os.path.expanduser("~/f3k_base/f3k.db")
PORT = 8765

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("f3k")


def parse_params(parts):
    """Parse ['key=val', ...] into a dict."""
    result = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            result[k] = v
    return result


PING_TIMEOUT_S = 90   # evict if no PING received within this window

class TimerClient:
    def __init__(self, reader, writer, server):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.mac = None
        self.timer_id = None
        self.addr = writer.get_extra_info("peername")
        self.last_ping_at = time.monotonic()

    async def send(self, msg: str):
        self.writer.write((msg + "\n").encode())
        await self.writer.drain()

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass

    async def run(self):
        log.info(f"Connected: {self.addr}")
        try:
            async for raw in self.reader:
                line = raw.decode().strip()
                if line:
                    await self._dispatch(line)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self.server.remove(self)
            log.info(f"Disconnected: {self.addr} (id={self.timer_id})")

    async def _dispatch(self, line: str):
        log.info(f"<< [id={self.timer_id or '?'}] {line}")
        parts = line.split()
        cmd = parts[0] if parts else ""

        if cmd == "JOIN":
            params = parse_params(parts[1:])
            self.mac = params.get("mac", "unknown")
            self.last_ping_at = time.monotonic()
            self.server.evict_mac(self.mac)   # close any stale connection with same MAC
            self.timer_id = self.server.next_id()
            self.server.add(self)
            await self.send(f"ASSIGN id={self.timer_id}")
            asyncio.create_task(self.server.state_machine.send_catchup(self.send))

        elif cmd == "FLIGHT":
            params = parse_params(parts[1:])
            pilot_id = int(params.get("pilot", 0))
            dur_ms = int(params.get("dur", 0))
            self.server.record_flight(pilot_id, dur_ms)
            asyncio.create_task(self.server.state_machine.on_flight(pilot_id, dur_ms))
            log.info(f"Flight: pilot={pilot_id} {dur_ms / 1000:.2f}s")

        elif cmd == "PING":
            self.last_ping_at = time.monotonic()
            await self.send("PONG")

        else:
            log.warning(f"Unknown command: {line}")


class F3KServer:
    def __init__(self):
        self._clients: dict[int, TimerClient] = {}
        self._id_counter = 1
        self.db = init_db(DB_PATH)
        web_app.state.server = self
        self.state_machine = CompetitionStateMachine(self)
        web_app.state.state_machine = self.state_machine

    def next_id(self) -> int:
        tid = self._id_counter
        self._id_counter += 1
        return tid

    def add(self, client: TimerClient):
        if client.timer_id is not None:
            self._clients[client.timer_id] = client

    def remove(self, client: TimerClient):
        if client.timer_id in self._clients:
            del self._clients[client.timer_id]

    def evict_mac(self, mac: str):
        """Close any existing connection from the same MAC (handles timer reboot)."""
        stale = [c for c in self._clients.values() if c.mac == mac]
        for c in stale:
            log.info(f"Evicting stale connection from MAC {mac} (id={c.timer_id})")
            self.remove(c)
            c.close()

    async def _watchdog(self):
        """Periodically evict connections that have stopped sending PINGs."""
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            stale = [c for c in list(self._clients.values())
                     if now - c.last_ping_at > PING_TIMEOUT_S]
            for c in stale:
                log.warning(f"PING timeout — evicting id={c.timer_id} {c.addr}")
                self.remove(c)
                c.close()

    def record_flight(self, pilot_id: int, dur_ms: int):
        self.db.execute(
            "INSERT INTO flights (pilot_id, duration_ms) VALUES (?, ?)",
            (pilot_id, dur_ms),
        )
        self.db.commit()

    async def broadcast(self, msg: str):
        """Send a message to all connected timers (TASK, START, STOP, PILOTS)."""
        log.info(f">> ALL ({len(self._clients)} timers): {msg}")
        for client in list(self._clients.values()):
            try:
                await client.send(msg)
            except Exception as e:
                log.error(f"Broadcast to {client.addr} failed: {e}")

    async def send_to(self, timer_id: int, msg: str):
        client = self._clients.get(timer_id)
        if client:
            await client.send(msg)

    async def _handle(self, reader, writer):
        client = TimerClient(reader, writer, self)
        await client.run()

    async def _cli(self):
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        log.info("CLI ready — PILOTS 1:Name,2:Name | TASK wt=600 | START | STOP")
        async for raw in reader:
            cmd = raw.decode().strip()
            if not cmd:
                continue
            if self._clients:
                await self.broadcast(cmd)
            else:
                log.warning(f"No timers connected — ignored: {cmd}")

    async def _web(self):
        try:
            config = uvicorn.Config(web_app, host="0.0.0.0", port=8080, loop="none",
                                    log_level="info")
            await uvicorn.Server(config).serve()
        except Exception:
            log.exception("Web server failed")

    async def run(self):
        srv = await asyncio.start_server(self._handle, "0.0.0.0", PORT)
        log.info(f"F3K Base Station listening on 0.0.0.0:{PORT}")
        async with srv:
            asyncio.create_task(self._watchdog())
            asyncio.create_task(self._web())
            if sys.stdin.isatty():
                asyncio.create_task(self._cli())
            await srv.serve_forever()


if __name__ == "__main__":
    server = F3KServer()
    asyncio.run(server.run())
