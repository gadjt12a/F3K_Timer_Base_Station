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
import types
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
    _loaded = None

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


class _DedupServer(_Server):
    """Delegates to the real record_flight so dedup is actually exercised.

    The plain _Server stub inserts unconditionally, which would make any
    reconciliation test pass regardless of whether dedup works.
    """

    def record_flight(self, pilot_id, dur_ms):
        return srv.F3KServer.record_flight(self, pilot_id, dur_ms)


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
        """Unattributable data is dropped — but silence would loop the timer.

        A timer that reconnects and loses its pilot selection really does send
        `pilot=0`. ACK means "received and decided", not "stored". This client has
        never had a pilot bound, so there is nothing to attribute to; see
        `PilotAttributionTests` for the case where there is. [I-25]
        """
        self.client.last_pilot_id = None
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

    def test_join_reports_firmware_version(self):
        """fw-v17+ appends `fw=` to JOIN so the CD can spot stale timers."""
        self._dispatch("JOIN mac=aa:bb:cc:dd:ee:ff fw=fw-v17")
        self.assertEqual(self.client.fw, "fw-v17")

    def test_join_without_firmware_is_none_not_a_guess(self):
        """fw-v16 and earlier send no `fw=`.

        None must survive to the UI as "older than the field", rather than being
        defaulted to some version the timer never claimed.
        """
        self._dispatch("JOIN mac=aa:bb:cc:dd:ee:ff")
        self.assertIsNone(self.client.fw)

    def test_unknown_join_params_are_ignored(self):
        """Old bases must tolerate new JOIN fields, and vice versa."""
        self._dispatch("JOIN mac=aa:bb:cc:dd:ee:ff fw=fw-v17 somethingnew=1")
        self.assertEqual(self.client.fw, "fw-v17")
        self.assertEqual(self.client.mac, "aa:bb:cc:dd:ee:ff")

    def test_ping_is_not_acked(self):
        """PING keeps its PONG reply — an ACK here would be a protocol change."""
        out = self._dispatch("PING")
        self.assertIn("PONG", out)
        self.assertFalse([m for m in out if m.startswith("ACK")])


class PilotAttributionTests(unittest.TestCase):
    """pilot=0 must fall back to the timer's last binding, not vanish. [I-25]"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)
        self.db.execute("INSERT INTO pilots (name) VALUES ('Alice')")
        self.db.commit()
        self.pilot = self.db.execute("SELECT id FROM pilots").fetchone()["id"]
        self.client = srv.TimerClient(None, _Writer(), _Server(self.db))
        self.client.timer_id = 1
        self.sent = []

        async def capture(msg):
            self.sent.append(msg)

        self.client.send = capture

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _dispatch(self, line):
        async def run():
            await self.client._dispatch(line)
            await asyncio.sleep(0)

        asyncio.run(run())

    def _flights(self):
        return self.db.execute(
            "SELECT pilot_id, duration_ms FROM flights ORDER BY id").fetchall()

    def test_flight_with_no_pilot_uses_the_last_binding(self):
        """The case that silently lost flights: reconnect clears the timer's
        selection, so every later report arrives as pilot=0 and used to be ACKed
        and binned. The base still knows the binding — it is restored by MAC."""
        self.client.last_pilot_id = self.pilot
        self._dispatch("FLIGHT pilot=0 dur=125430")
        rows = self._flights()
        self.assertEqual(len(rows), 1, "the flight must not be dropped")
        self.assertEqual(rows[0]["pilot_id"], self.pilot)

    def test_flight_with_no_pilot_and_no_binding_is_still_dropped(self):
        """Attribution is a fallback, not a guess. With nothing to fall back to
        an orphan row would be worse than a loud error."""
        self.client.last_pilot_id = None
        self._dispatch("FLIGHT pilot=0 dur=125430")
        self.assertEqual(len(self._flights()), 0)

    def test_attribution_still_acks(self):
        self.client.last_pilot_id = self.pilot
        self.sent.clear()
        self._dispatch("FLIGHT pilot=0 dur=125430")
        self.assertIn("ACK FLIGHT pilot=0 dur=125430", self.sent,
                      "the ACK must echo what the timer sent, not what we resolved")

    def test_explicit_pilot_always_wins(self):
        self.client.last_pilot_id = 999
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430")
        self.assertEqual(self._flights()[0]["pilot_id"], self.pilot)


class ReconciliationTests(unittest.TestCase):
    """End-of-round resend: rc=1 copies fill gaps without creating duplicates."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)
        self.db.execute("INSERT INTO pilots (name) VALUES ('Alice')")
        self.db.commit()
        self.pilot = self.db.execute("SELECT id FROM pilots").fetchone()["id"]
        self.client = srv.TimerClient(None, _Writer(), _DedupServer(self.db))
        self.client.timer_id = 1
        self.sent = []

        async def capture(msg):
            self.sent.append(msg)

        self.client.send = capture

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _dispatch(self, line):
        async def run():
            await self.client._dispatch(line)
            await asyncio.sleep(0)

        asyncio.run(run())

    def _count(self):
        return self.db.execute("SELECT COUNT(*) FROM flights").fetchone()[0]

    def test_resend_of_a_known_flight_adds_nothing(self):
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430")
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430 rc=1")
        self.assertEqual(self._count(), 1, "the resend must dedup, not duplicate")

    def test_resend_of_a_lost_flight_records_it(self):
        """Nothing arrived during the round; the resend is the only copy."""
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430 rc=1")
        self.assertEqual(self._count(), 1)

    def test_resend_is_acked_like_any_other_message(self):
        line = f"FLIGHT pilot={self.pilot} dur=125430 rc=1"
        self.sent.clear()
        self._dispatch(line)
        self.assertIn(f"ACK {line}", self.sent)

    def test_rc_marker_does_not_change_dedup_identity(self):
        """Dedup keys on (pilot, group, duration) — the marker must not leak in."""
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430 rc=1")
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430")
        self.assertEqual(self._count(), 1)


class AltitudeTargetingTests(unittest.TestCase):
    """Altitudes must land on the flight they name. [I-26]"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)
        self.db.execute("INSERT INTO pilots (name) VALUES ('Alice')")
        self.db.commit()
        self.pilot = self.db.execute("SELECT id FROM pilots").fetchone()["id"]
        for n, dur in enumerate([10000, 20000, 30000], start=1):
            self.db.execute(
                "INSERT INTO flights (pilot_id, duration_ms, flight_no)"
                " VALUES (?, ?, ?)", (self.pilot, dur, n))
        self.db.commit()

        class _Loaded:
            _loaded = None

        self.server = types.SimpleNamespace(db=self.db, state_machine=_Loaded())

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _alt(self, flight_no):
        return self.db.execute(
            "SELECT altitude_m FROM flights WHERE pilot_id = ? AND flight_no = ?",
            (self.pilot, flight_no)).fetchone()["altitude_m"]

    def test_each_altitude_lands_on_its_own_flight(self):
        """The whole round used to collapse onto the last row: F5K altitudes are
        entered after the flights exist, so every UPDATE hit the newest one."""
        for n, alt in ((1, 45), (2, 60), (3, 55)):
            srv.F3KServer.record_altitude(self.server, self.pilot, n, alt)
        self.assertEqual((self._alt(1), self._alt(2), self._alt(3)), (45, 60, 55))

    def test_changed_flag_reports_whether_it_moved(self):
        self.assertTrue(srv.F3KServer.record_altitude(self.server, self.pilot, 1, 45))
        self.assertFalse(srv.F3KServer.record_altitude(self.server, self.pilot, 1, 45),
                         "an unchanged resend must not be reported as a recovery")

    def test_unknown_flight_no_falls_back_rather_than_dropping(self):
        srv.F3KServer.record_altitude(self.server, self.pilot, 99, 70)
        self.assertEqual(self._alt(3), 70, "fall back to the most recent flight")


class TimerNumberingTests(unittest.TestCase):
    """Timer numbers are printed on the timer's own screen and called out loud,
    so they must survive a restart. They used to live only in memory."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _server(self):
        """A fresh F3KServer bound to the same DB — i.e. a service restart."""
        s = srv.F3KServer.__new__(srv.F3KServer)
        s.db = self.db
        s._clients = {}
        s._mac_to_id = {}
        s._mac_to_pilot = {}
        return s

    def test_numbers_survive_a_restart(self):
        a = self._server()
        self.assertEqual(a.assign_id("aa:aa"), 1)
        self.assertEqual(a.assign_id("bb:bb"), 2)

        b = self._server()          # restart: in-memory map is empty again
        self.assertEqual(b.assign_id("bb:bb"), 2, "timer was renumbered by a restart")
        self.assertEqual(b.assign_id("aa:aa"), 1)

    def test_same_mac_is_stable_within_a_session(self):
        s = self._server()
        self.assertEqual(s.assign_id("aa:aa"), 1)
        self.assertEqual(s.assign_id("aa:aa"), 1)
        self.assertEqual(s.assign_id("bb:bb"), 2)

    def test_renumber_frees_the_numbers_again(self):
        s = self._server()
        s.assign_id("aa:aa")
        s.assign_id("bb:bb")
        self.assertEqual(s.renumber_timers(), 2)
        # A different timer now gets T1 — the point of the escape hatch
        self.assertEqual(s.assign_id("cc:cc"), 1)

    def test_lowest_free_number_is_reused(self):
        """A running counter would leave a permanent gap after a renumber."""
        s = self._server()
        s.assign_id("aa:aa")            # 1
        s.assign_id("bb:bb")            # 2
        self.db.execute("DELETE FROM timer_ids WHERE mac = 'aa:aa'")
        self.db.commit()
        s._mac_to_id.pop("aa:aa")
        self.assertEqual(s.assign_id("cc:cc"), 1, "should fill the gap, not go to 3")


if __name__ == "__main__":
    unittest.main()
