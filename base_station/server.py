#!/usr/bin/env python3
"""F3K Base Station — TCP server on port 8765."""

import asyncio
import collections
import itertools
import logging
import os
import shutil
import sys
import time

import uvicorn

from frontend.app import app as web_app
from frontend.db import init_db
from frontend.state_machine import CompetitionStateMachine

# The Pi's console is UTF-8, but a Windows console defaults to cp1252 and every
# arrow, bullet and em dash in our startup output and log lines then raises
# UnicodeEncodeError — the server dies before it binds. Force UTF-8 so the same
# code runs on a dev box as on the field unit. [I-14]
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "f3k.db")
_LEGACY_DB = os.path.expanduser("~/f3k_base/f3k.db")
if not os.path.exists(DB_PATH) and os.path.exists(_LEGACY_DB):
    shutil.copy2(_LEGACY_DB, DB_PATH)
    print(f"[startup] DB migrated from {_LEGACY_DB} → {DB_PATH}", flush=True)

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
        self.fw = None              # firmware version reported in JOIN (fw-v17+)

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
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.remove(self)
            self.server.log_event("disconnect", self.mac, self.timer_id, str(self.addr))
            log.info(f"Disconnected: {self.addr} (id={self.timer_id})")
            asyncio.create_task(self.server.broadcast_timers())

    def _attribute(self, pilot_id: int, what: str) -> int:
        """Resolve pilot=0 against this timer's last known binding. [I-25]

        A timer that reconnects mid-round loses its pilot selection, so everything
        it reports afterwards arrives as pilot=0. That used to be logged and
        dropped — and because `ACK` means "received and decided", the timer cleared
        it from the pending queue and believed it delivered. Silent loss at both
        ends, and the one hole the ACK queue can never plug.

        The base is not actually ignorant here: the binding is saved on eviction
        and restored by MAC on JOIN, so it knows who the timer was flying for even
        when the timer has forgotten. Losing a flight time is the worst outcome
        this system has, so attribute it and say so loudly.
        """
        if pilot_id > 0:
            return pilot_id
        if self.last_pilot_id:
            log.warning(
                f"{what} arrived with pilot=0 — attributing to this timer's last "
                f"bound pilot {self.last_pilot_id}. The timer lost its selection "
                f"(most likely a reconnect mid-round)."
            )
            return self.last_pilot_id
        log.error(f"{what} arrived with pilot=0 and timer id={self.timer_id} has no "
                  f"bound pilot — DROPPED. This flight is lost; enter it by hand.")
        return 0

    async def _alert_recovered(self, kind: str, pilot_id: int, detail: str) -> None:
        """Tell the run page that data arrived late and the log has changed.

        A recovery is good news, but it is still the flight log changing after the
        round ended, which the CD would otherwise never see. Silence in either
        direction is the thing to avoid.
        """
        row = self.server.db.execute(
            "SELECT name FROM pilots WHERE id = ?", (pilot_id,)).fetchone()
        from frontend.app import manager
        await manager.broadcast({
            "type": "recovered",
            "kind": kind,
            "timer_id": self.timer_id,
            "pilot_id": pilot_id,
            "pilot_name": row["name"] if row else f"Pilot {pilot_id}",
            "detail": detail,
        })

    async def _dispatch(self, line: str):
        log.info(f"<< [id={self.timer_id or '?'}] {line}")
        parts = line.split()
        cmd = parts[0] if parts else ""

        if cmd == "JOIN":
            params = parse_params(parts[1:])
            self.mac = params.get("mac", "unknown")
            # Absent on fw-v16 and earlier, which predate the field. None is the
            # honest value: "we don't know", which the UI reports as older rather
            # than pretending to a version.
            self.fw = params.get("fw")
            self.last_ping_at = time.monotonic()
            self.server.evict_mac(self.mac)   # saves pilot binding, closes stale socket
            is_reconnect = self.mac in self.server._mac_to_id
            self.timer_id = self.server.assign_id(self.mac)  # same ID on reconnect
            self.last_pilot_id = self.server._mac_to_pilot.get(self.mac)  # restore pilot
            self.server.add(self)
            self.server.log_event(
                "reconnect" if is_reconnect else "connect",
                self.mac, self.timer_id, str(self.addr)
            )
            log.info(f"{'Reconnect' if is_reconnect else 'New'} timer: "
                     f"MAC={self.mac} id={self.timer_id} pilot={self.last_pilot_id}")
            await self.send(f"ASSIGN id={self.timer_id}")
            asyncio.create_task(self.server.state_machine.send_catchup(self.send))
            asyncio.create_task(self.server.broadcast_timers())

        elif cmd == "FLIGHT":
            params = parse_params(parts[1:])
            pilot_id = int(params.get("pilot", 0))
            dur_ms = int(params.get("dur", 0))
            # rc=1 marks an end-of-round reconciliation copy. Absent on fw-v19 and
            # earlier, and on every live report.
            recovered = params.get("rc") == "1"
            pilot_id = self._attribute(pilot_id, f"FLIGHT dur={dur_ms}ms")
            if pilot_id > 0:
                self.last_pilot_id = pilot_id
                if self.server.record_flight(pilot_id, dur_ms):
                    asyncio.create_task(self.server.state_machine.on_flight(pilot_id, dur_ms))
                    log.info(f"Flight: pilot={pilot_id} {dur_ms / 1000:.2f}s")
                    if recovered:
                        # An insert from a reconciliation copy means the live report
                        # never made it: a real gap, caught. Never let this be quiet
                        # — the CD has to know the log changed after the round.
                        log.warning(
                            f"RECOVERED flight for pilot={pilot_id} "
                            f"({dur_ms / 1000:.2f}s) — the live report was lost and "
                            f"the end-of-round resend caught it."
                        )
                        asyncio.create_task(self._alert_recovered(
                            "flight", pilot_id, f"{dur_ms / 1000:.2f}s"))
            # ACK unconditionally — including the discarded no-pilot case and dups.
            # ACK means "received and decided", not "stored": the timer retries until
            # ACKed, so withholding one from a message we deliberately drop would put
            # the timer in a retry loop it can never escape.
            await self.send(f"ACK {line}")

        elif cmd == "JUMPED":
            # Pilot launched before the start horn — CD gets a note in the run
            # page flight log only. Never recorded in the DB (invalid flight).
            params = parse_params(parts[1:])
            pilot_id = int(params.get("pilot", 0))
            dur_ms = int(params.get("dur", 0))
            if pilot_id > 0:
                row = self.server.db.execute(
                    "SELECT name FROM pilots WHERE id = ?", (pilot_id,)
                ).fetchone()
                pilot_name = row["name"] if row else f"Pilot {pilot_id}"
                log.warning(f"Jumped start: pilot={pilot_id} ({pilot_name}) {dur_ms / 1000:.2f}s")
                from frontend.app import manager
                asyncio.create_task(manager.broadcast({
                    "type": "jumped",
                    "pilot_id": pilot_id,
                    "pilot_name": pilot_name,
                    "duration_ms": dur_ms,
                }))
            await self.send(f"ACK {line}")

        elif cmd == "ALTITUDE":
            params = parse_params(parts[1:])
            pilot_id = int(params.get("pilot", 0))
            flight_no = int(params.get("flight", 0))
            alt_m = int(params.get("alt", 0))
            pilot_id = self._attribute(
                pilot_id, f"ALTITUDE flight={flight_no} alt={alt_m}m")
            recovered = params.get("rc") == "1"
            if pilot_id > 0:
                changed = self.server.record_altitude(pilot_id, flight_no, alt_m)
                log.info(f"Altitude: pilot={pilot_id} flight={flight_no} alt={alt_m}m")
                if recovered and changed:
                    log.warning(
                        f"RECOVERED altitude for pilot={pilot_id} flight={flight_no} "
                        f"({alt_m}m) — the live report was lost.")
                    asyncio.create_task(self._alert_recovered(
                        "altitude", pilot_id, f"flight {flight_no}: {alt_m}m"))
                from frontend.app import manager
                asyncio.create_task(manager.broadcast({
                    "type": "altitude",
                    "pilot_id": pilot_id,
                    "flight_no": flight_no,
                    "altitude_m": alt_m,
                }))
            await self.send(f"ACK {line}")   # unconditional — see FLIGHT above

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
            await self.send(f"ACK {line}")   # unconditional — see FLIGHT above

        elif cmd == "PING":
            self.last_ping_at = time.monotonic()
            await self.send("PONG")

        else:
            log.warning(f"Unknown command: {line}")


class F3KServer:
    def __init__(self):
        self._clients: dict[int, TimerClient] = {}
        self._mac_to_id: dict[str, int] = {}      # MAC → timer ID; cache over the timer_ids table
        self._mac_to_pilot: dict[str, int] = {}   # MAC → last pilot_id (restored on reconnect)
        self.events = collections.deque(maxlen=100)  # connection diagnostics ring buffer
        self.db = init_db(DB_PATH)
        web_app.state.server = self
        self.state_machine = CompetitionStateMachine(self)
        web_app.state.state_machine = self.state_machine

    def assign_id(self, mac: str) -> int:
        """Return the persistent timer ID for this MAC, allocating one if unseen.

        Backed by the `timer_ids` table, not just the in-memory map: the number is
        printed on the timer's own screen and pilots refer to it out loud, so a
        service restart must not renumber the field mid-competition.
        """
        cached = self._mac_to_id.get(mac)
        if cached is not None:
            self._touch_timer_id(mac)
            return cached

        row = self.db.execute(
            "SELECT timer_id FROM timer_ids WHERE mac = ?", (mac,)).fetchone()
        if row:
            self._mac_to_id[mac] = row["timer_id"]
            self._touch_timer_id(mac)
            return row["timer_id"]

        # Allocate the lowest free number rather than a running counter, so
        # renumbering (or a removed timer) leaves no permanent gap.
        used = {r["timer_id"] for r in
                self.db.execute("SELECT timer_id FROM timer_ids").fetchall()}
        used |= set(self._mac_to_id.values())
        new_id = next(i for i in itertools.count(1) if i not in used)
        try:
            self.db.execute(
                "INSERT INTO timer_ids (mac, timer_id, last_seen)"
                " VALUES (?, ?, CURRENT_TIMESTAMP)", (mac, new_id))
            self.db.commit()
        except Exception:
            # Never let a bookkeeping failure stop a timer joining mid-round —
            # it still gets a working number for this session.
            log.exception("could not persist timer id for %s", mac)
        self._mac_to_id[mac] = new_id
        return new_id

    def _touch_timer_id(self, mac: str) -> None:
        try:
            self.db.execute(
                "UPDATE timer_ids SET last_seen = CURRENT_TIMESTAMP WHERE mac = ?", (mac,))
            self.db.commit()
        except Exception:
            log.exception("could not update last_seen for %s", mac)

    def renumber_timers(self) -> int:
        """Forget every MAC→number assignment. Numbers are handed out again, from
        1, as timers reconnect.

        Persistence means a decommissioned or mistyped timer would otherwise hold
        its number for good, so there has to be a way back. Connected timers keep
        the number they were given until they next reconnect — renumbering live
        would change what the CD is looking at without the timer's own screen
        agreeing.
        """
        n = self.db.execute("SELECT COUNT(*) FROM timer_ids").fetchone()[0]
        self.db.execute("DELETE FROM timer_ids")
        self.db.commit()
        self._mac_to_id.clear()
        log.info("Timer numbering reset — %d assignment(s) cleared", n)
        return n

    def add(self, client: TimerClient):
        if client.timer_id is not None:
            self._clients[client.timer_id] = client

    def remove(self, client: TimerClient):
        # Identity check, not just key match: with MAC-sticky IDs a reconnecting
        # timer reuses the same timer_id, so a freshly-added client can occupy the
        # slot before the OLD socket's run() loop unwinds. Only delete if the stored
        # client is still this exact client — otherwise the late-firing cleanup of
        # the dead socket would evict the live replacement.
        if self._clients.get(client.timer_id) is client:
            del self._clients[client.timer_id]

    def evict_mac(self, mac: str):
        """Close any existing connection from the same MAC (handles timer reboot).
        Saves the pilot binding so it can be restored when the timer reconnects."""
        stale = [c for c in self._clients.values() if c.mac == mac]
        for c in stale:
            if c.last_pilot_id:
                self._mac_to_pilot[mac] = c.last_pilot_id
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
                "fw": c.fw,
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
        as a keepalive by the timer (resets its _lastRxMs).

        A successful send also resets last_ping_at so the watchdog doesn't evict a
        timer that is receiving our PONGs but hasn't yet hit its 30s PING interval."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            for c in list(self._clients.values()):
                try:
                    await c.send("PONG")
                    # Successful send proves the link is alive in at least one direction;
                    # reset the ping clock so the watchdog doesn't evict a timer that is
                    # receiving our keepalives but hasn't hit its 30s PING interval yet.
                    c.last_ping_at = time.monotonic()
                except Exception:
                    # Send failed = OS-level socket error (broken pipe, connection reset).
                    # Log it; the watchdog will evict via ping_timeout once last_ping_at
                    # ages past PING_TIMEOUT_S. Don't evict here — a single send error
                    # could be a transient flush failure, and premature eviction during a
                    # flight would force pilot re-selection even though the ring buffer
                    # would preserve the flight data.
                    log.warning(f"Keepalive send failed id={c.timer_id} {c.addr}")

    def record_flight(self, pilot_id: int, dur_ms: int) -> bool:
        group_id = self.state_machine._loaded.get("group_id") if self.state_machine._loaded else None
        # Dedup: same pilot + exact duration in the same group = duplicate, regardless of
        # when it arrived. Covers both the old "double-tap" case and ACK-retry replays
        # after reconnect (timer retransmits unACKed FLIGHTs on reconnect).
        dup = self.db.execute(
            "SELECT id FROM flights WHERE pilot_id = ? AND group_id IS ? AND duration_ms = ?",
            (pilot_id, group_id, dur_ms),
        ).fetchone()
        if dup:
            log.warning(f"Duplicate FLIGHT suppressed: pilot={pilot_id} dur={dur_ms}ms group={group_id}")
            return False
        next_no = self.db.execute(
            "SELECT COALESCE(MAX(flight_no), 0) + 1 FROM flights WHERE pilot_id = ? AND group_id IS ?",
            (pilot_id, group_id),
        ).fetchone()[0]
        self.db.execute(
            "INSERT INTO flights (pilot_id, duration_ms, group_id, flight_no) VALUES (?, ?, ?, ?)",
            (pilot_id, dur_ms, group_id, next_no),
        )
        self.db.commit()
        return True

    def record_altitude(self, pilot_id: int, flight_no: int, alt_m: int) -> bool:
        """Set the altitude on the flight the timer named. Returns True if changed.

        This used to write to the most recently inserted flight and ignore
        `flight_no` altogether. F5K altitudes are entered after the round, one
        flight at a time, so every altitude in a multi-flight round landed on the
        last row: flight 1's height overwrote flight 4's, then flight 2's did, and
        the earlier flights kept none at all. [I-26]
        """
        group_id = self.state_machine._loaded.get("group_id") if self.state_machine._loaded else None
        row = None
        if flight_no > 0:
            row = self.db.execute(
                "SELECT id, altitude_m FROM flights"
                " WHERE pilot_id = ? AND group_id IS ? AND flight_no = ?",
                (pilot_id, group_id, flight_no),
            ).fetchone()
        if row is None:
            # No flight_no match — an older timer, or a flight that never reached
            # us. Fall back to the previous behaviour rather than dropping it.
            row = self.db.execute(
                "SELECT id, altitude_m FROM flights"
                " WHERE pilot_id = ? AND group_id IS ? ORDER BY id DESC LIMIT 1",
                (pilot_id, group_id),
            ).fetchone()
            if row is not None and flight_no > 0:
                log.warning(
                    f"ALTITUDE for pilot={pilot_id} flight={flight_no}: no flight with "
                    f"that number in this group — applied to the most recent instead."
                )
        if row is None:
            log.error(f"ALTITUDE for pilot={pilot_id} flight={flight_no} alt={alt_m}m "
                      f"has no flight to attach to — DROPPED.")
            return False
        changed = row["altitude_m"] != alt_m
        self.db.execute(
            "UPDATE flights SET altitude_m = ?, altitude_source = 'timer' WHERE id = ?",
            (alt_m, row["id"]),
        )
        self.db.commit()
        return changed

    async def broadcast_timers(self):
        """Push current timer list to all web clients so the run page stays in sync."""
        from frontend.app import manager
        await manager.broadcast({"type": "timers_update", "timers": self.timers_info()})

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
