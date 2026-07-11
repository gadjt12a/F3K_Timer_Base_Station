#!/usr/bin/env python3
"""F3K Base Station — TCP server on port 8765."""

import asyncio
import collections
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


PING_TIMEOUT_S = 90       # evict if no PING received within this window
KEEPALIVE_INTERVAL_S = 15  # proactively ping timers so the link never idles
BT_RECONNECT_INTERVAL_S = 30  # re-check/reconnect the BT speaker this often

class TimerClient:
    def __init__(self, reader, writer, server):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.mac = None
        self.timer_id = None
        self.addr = writer.get_extra_info("peername")
        self.last_ping_at = time.monotonic()
        self.connected_at = time.time()
        self.last_pilot_id = None   # pilot of the most recent FLIGHT from this timer

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
            self.server.log_event("disconnect", self.mac, self.timer_id, str(self.addr))
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
            self.server.log_event("connect", self.mac, self.timer_id, str(self.addr))
            await self.send(f"ASSIGN id={self.timer_id}")
            asyncio.create_task(self.server.state_machine.send_catchup(self.send))

        elif cmd == "FLIGHT":
            params = parse_params(parts[1:])
            pilot_id = int(params.get("pilot", 0))
            dur_ms = int(params.get("dur", 0))
            if pilot_id <= 0:
                # No bound pilot (e.g. a timer that reconnected and lost its selection).
                # Park it rather than writing an orphan row into the flight log.
                log.warning(f"FLIGHT with no pilot (dur={dur_ms}ms) — ignored")
            else:
                self.last_pilot_id = pilot_id
                if self.server.record_flight(pilot_id, dur_ms):
                    asyncio.create_task(self.server.state_machine.on_flight(pilot_id, dur_ms))
                    log.info(f"Flight: pilot={pilot_id} {dur_ms / 1000:.2f}s")

        elif cmd == "ALTITUDE":
            params = parse_params(parts[1:])
            pilot_id = int(params.get("pilot", 0))
            flight_no = int(params.get("flight", 0))
            alt_m = int(params.get("alt", 0))
            if pilot_id > 0:
                self.server.record_altitude(pilot_id, flight_no, alt_m)
                log.info(f"Altitude: pilot={pilot_id} flight={flight_no} alt={alt_m}m")
                from frontend.app import manager
                asyncio.create_task(manager.broadcast({
                    "type": "altitude",
                    "pilot_id": pilot_id,
                    "flight_no": flight_no,
                    "altitude_m": alt_m,
                }))

        elif cmd == "SELECT":
            params = parse_params(parts[1:])
            pilot_id = int(params.get("pilot", 0))
            if pilot_id > 0:
                self.last_pilot_id = pilot_id
                row = self.server.db.execute(
                    "SELECT name FROM pilots WHERE id = ?", (pilot_id,)
                ).fetchone()
                pilot_name = row["name"] if row else f"Pilot {pilot_id}"
                from frontend.app import manager
                asyncio.create_task(manager.broadcast({
                    "type": "timer_pilot",
                    "timer_id": self.timer_id,
                    "pilot_id": pilot_id,
                    "pilot_name": pilot_name,
                }))
                log.info(f"Timer {self.timer_id} selected pilot {pilot_id} ({pilot_name})")

        elif cmd == "PING":
            self.last_ping_at = time.monotonic()
            await self.send("PONG")

        else:
            log.warning(f"Unknown command: {line}")


class F3KServer:
    def __init__(self):
        self._clients: dict[int, TimerClient] = {}
        self._id_counter = 1
        self.events = collections.deque(maxlen=100)  # connection diagnostics ring buffer
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
            self.log_event("evicted", mac, c.timer_id, "reconnect from same MAC")
            self.remove(c)
            c.close()

    def log_event(self, kind: str, mac=None, timer_id=None, detail: str = ""):
        """Record a connection-lifecycle event for the diagnostics view."""
        self.events.append({
            "t": time.time(), "kind": kind, "mac": mac, "id": timer_id, "detail": detail,
        })

    def timers_info(self) -> list[dict]:
        """Snapshot of currently-connected timers for the diagnostics view."""
        now = time.monotonic()
        out = []
        for c in self._clients.values():
            pilot_name = None
            if c.last_pilot_id:
                row = self.db.execute(
                    "SELECT name FROM pilots WHERE id = ?", (c.last_pilot_id,)
                ).fetchone()
                pilot_name = row["name"] if row else f"Pilot {c.last_pilot_id}"
            out.append({
                "id": c.timer_id,
                "mac": c.mac,
                "ip": c.addr[0] if c.addr else None,
                "last_ping_age_s": round(now - c.last_ping_at, 1),
                "connected_at": c.connected_at,
                "last_pilot_id": c.last_pilot_id,
                "last_pilot_name": pilot_name,
            })
        return sorted(out, key=lambda t: (t["id"] is None, t["id"]))

    def recent_events(self, limit: int = 40) -> list[dict]:
        return list(self.events)[-limit:][::-1]   # newest first

    async def _watchdog(self):
        """Periodically evict connections that have stopped sending PINGs."""
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            stale = [c for c in list(self._clients.values())
                     if now - c.last_ping_at > PING_TIMEOUT_S]
            for c in stale:
                log.warning(f"PING timeout — evicting id={c.timer_id} {c.addr}")
                self.log_event("ping_timeout", c.mac, c.timer_id,
                               f"no PING for >{PING_TIMEOUT_S}s")
                self.remove(c)
                c.close()

    async def _bt_reconnect(self):
        """Reconnect the configured Bluetooth speaker if it drops (idle/out of range)."""
        from frontend import audio_control
        while True:
            await asyncio.sleep(BT_RECONNECT_INTERVAL_S)
            mac = audio_control.load_config().get("bt_mac")
            if not mac:
                continue
            try:
                status = await audio_control.bt_status()
                connected = status.get("connected_mac") == mac
                # "connected" per bluetoothctl isn't enough — the A2DP PCM can idle-die
                # while the link shows connected (aplay: "No such device"). Only treat
                # the speaker as healthy when the PCM is really there.
                alive = connected and await audio_control.pcm_alive()
                if not alive:
                    log.info(f"[AUDIO] speaker {mac} "
                             f"{'PCM dead' if connected else 'disconnected'} — reconnecting")
                    if connected:
                        # A fresh connect won't rebuild the PCM while still "connected";
                        # drop it first.
                        await audio_control.bt_disconnect(mac)
                        await asyncio.sleep(1)
                    r = await audio_control.bt_connect(mac)
                    log.info("[AUDIO] speaker reconnected" if r.get("ok")
                             else f"[AUDIO] reconnect failed: {r.get('error')}")
            except Exception:
                log.exception("[AUDIO] bt reconnect loop error")

    async def _keepalive(self):
        """Proactively send a keepalive to every timer so the link never idles.

        The primary fix for the mid-round drop is firmware-side (WiFi.setSleep(false)),
        but sending regular traffic here is cheap insurance against the watch's
        RX-timeout reconnect during quiet prep periods. Unsolicited PONG is treated
        as a keepalive by the timer (resets its _lastRxMs)."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            for c in list(self._clients.values()):
                try:
                    await c.send("PONG")
                except Exception:
                    pass

    def record_flight(self, pilot_id: int, dur_ms: int) -> bool:
        group_id = self.state_machine._loaded.get("group_id") if self.state_machine._loaded else None
        # Dedup: reject a repeat of the same flight arriving within 10 seconds
        # (guards against watch sending FLIGHT twice due to any edge case)
        dup = self.db.execute(
            """SELECT id FROM flights
               WHERE pilot_id = ? AND group_id IS ? AND duration_ms = ?
               AND recorded_at >= datetime('now', '-10 seconds')""",
            (pilot_id, group_id, dur_ms),
        ).fetchone()
        if dup:
            log.warning(f"Duplicate FLIGHT suppressed: pilot={pilot_id} dur={dur_ms}ms group={group_id}")
            return False
        self.db.execute(
            "INSERT INTO flights (pilot_id, duration_ms, group_id) VALUES (?, ?, ?)",
            (pilot_id, dur_ms, group_id),
        )
        self.db.commit()
        return True

    def record_altitude(self, pilot_id: int, flight_no: int, alt_m: int):
        group_id = self.state_machine._loaded.get("group_id") if self.state_machine._loaded else None
        # Update the most recently inserted flight for this pilot in this group
        self.db.execute(
            """UPDATE flights SET altitude_m = ?
               WHERE id = (
                   SELECT id FROM flights
                   WHERE pilot_id = ? AND group_id IS ?
                   ORDER BY id DESC LIMIT 1
               )""",
            (alt_m, pilot_id, group_id),
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
            asyncio.create_task(self._keepalive())
            asyncio.create_task(self._bt_reconnect())
            asyncio.create_task(self._web())
            if sys.stdin.isatty():
                asyncio.create_task(self._cli())
            await srv.serve_forever()


if __name__ == "__main__":
    server = F3KServer()
    asyncio.run(server.run())
