"""TCP protocol — the ACK contract the timer's retry logic depends on.

The timer holds FLIGHT/JUMPED/ALTITUDE/SELECT in a pending buffer and only drops
an entry when the base echoes it back as `ACK <line>`. That makes a withheld ACK
far worse than a lost message: the timer cannot tell "you ignored this" from
"this never arrived", so it retries forever.

These tests exist because three of those four handlers originally ACKed *inside*
an `if pilot_id > 0` branch, which would have made every no-pilot message an
unbreakable retry loop the moment the timer side shipped.

See docs/PROTOCOL_ACK.md.
"""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server as srv  # noqa: E402
from frontend.db import init_db  # noqa: E402


class _Writer:
    def get_extra_info(self, _):
        return ("10.0.0.9", 5000)

    def write(self, _):
        pass

    async def drain(self):
        pass


class _StateMachine:
    async def on_flight(self, pilot_id, dur_ms):
        pass

    async def send_catchup(self, send):
        pass


class _Server:
    """Enough of TimerServer for _dispatch to run without a socket or event loop."""

    def __init__(self, db):
        self.db = db
        self._mac_to_id = {}
        self._mac_to_pilot = {}
        self.state_machine = _StateMachine()

    def assign_id(self, mac):
        return self._mac_to_id.setdefault(mac, 1)

    def add(self, client):
        pass

    def evict_mac(self, mac):
        pass

    def log_event(self, *a, **k):
        pass

    async def broadcast_timers(self):
        pass

    def record_flight(self, pilot_id, dur_ms):
        self.db.execute("INSERT INTO flights (pilot_id, duration_ms) VALUES (?, ?)",
                        (pilot_id, dur_ms))
        self.db.commit()
        return True

    def record_altitude(self, pilot_id, flight_no, alt_m):
        pass


class AckContractTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)
        self.db.execute("INSERT INTO pilots (name) VALUES ('Alice')")
        self.db.commit()
        self.pilot = self.db.execute("SELECT id FROM pilots").fetchone()["id"]
        self.client = srv.TimerClient(None, _Writer(), _Server(self.db))
        self.sent = []
        self.client.send = self._capture

    async def _capture(self, msg):
        self.sent.append(msg)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _dispatch(self, line):
        self.sent.clear()

        async def run():
            await self.client._dispatch(line)
            # _dispatch fires broadcasts via create_task; yield so they complete
            # rather than being destroyed pending when the loop closes.
            await asyncio.sleep(0)

        asyncio.run(run())
        return self.sent

    def _assert_acked(self, line):
        self.assertIn(f"ACK {line}", self._dispatch(line),
                      f"no ACK for: {line}")

    def test_ack_echoes_verbatim(self):
        """The timer matches by exact string, so the echo must be byte-for-byte."""
        line = f"FLIGHT pilot={self.pilot} dur=125430"
        self.assertIn(f"ACK {line}", self._dispatch(line))

    def test_all_retried_message_types_are_acked(self):
        for line in (f"FLIGHT pilot={self.pilot} dur=125430",
                     f"JUMPED pilot={self.pilot} dur=1500",
                     f"ALTITUDE pilot={self.pilot} flight=2 alt=47",
                     f"SELECT pilot={self.pilot}"):
            self._assert_acked(line)

    def test_no_pilot_messages_are_still_acked(self):
        """The base drops these deliberately — but silence would loop the timer.

        A timer that reconnects and loses its pilot selection really does send
        `pilot=0`. ACK means "received and decided", not "stored".
        """
        for line in ("FLIGHT pilot=0 dur=125430",
                     "JUMPED pilot=0 dur=1500",
                     "ALTITUDE pilot=0 flight=1 alt=47",
                     "SELECT pilot=0"):
            self._assert_acked(line)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM flights").fetchone()[0], 0,
            "a no-pilot FLIGHT must still not be recorded")

    def test_duplicate_flight_is_acked_again(self):
        """Retransmits arrive as duplicates; withholding the ACK would re-loop."""
        line = f"FLIGHT pilot={self.pilot} dur=125430"
        self._assert_acked(line)
        self._assert_acked(line)

    def test_ping_is_not_acked(self):
        """PING keeps its PONG reply — an ACK here would be a protocol change."""
        out = self._dispatch("PING")
        self.assertIn("PONG", out)
        self.assertFalse([m for m in out if m.startswith("ACK")])


if __name__ == "__main__":
    unittest.main()
