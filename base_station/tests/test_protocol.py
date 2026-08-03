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
import collections
import os
import sys
import tempfile
import time
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
        self._mac_to_group = {}
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

    def current_group_id(self):
        loaded = self.state_machine._loaded
        return loaded.get("group_id") if loaded else None

    def scratch_flight(self, pilot_id, dur_ms, group_id=None):
        return srv.F3KServer.scratch_flight(self, pilot_id, dur_ms, group_id)

    def record_flight(self, pilot_id, dur_ms, group_id=None,
                      target_s=0, target_window=False):
        self.db.execute("INSERT INTO flights (pilot_id, duration_ms) VALUES (?, ?)",
                        (pilot_id, dur_ms))
        self.db.commit()
        return True

    def record_altitude(self, pilot_id, flight_no, alt_m, group_id=None):
        pass

    def record_jumped(self, pilot_id, dur_ms, group_id=None):
        return srv.F3KServer.record_jumped(self, pilot_id, dur_ms, group_id)


class _DedupServer(_Server):
    """Delegates to the real record_flight so dedup is actually exercised.

    The plain _Server stub inserts unconditionally, which would make any
    reconciliation test pass regardless of whether dedup works.
    """

    GROUP = 37

    def __init__(self, db):
        super().__init__(db)
        self.state_machine._loaded = {"group_id": self.GROUP}

    def close_heat(self):
        """What the base does when the round ends — the state the resend meets."""
        self.state_machine._loaded = None

    def current_group_id(self):
        return srv.F3KServer.current_group_id(self)

    def record_flight(self, pilot_id, dur_ms, group_id=None,
                      target_s=0, target_window=False):
        return srv.F3KServer.record_flight(self, pilot_id, dur_ms, group_id,
                                           target_s, target_window)

    def record_altitude(self, pilot_id, flight_no, alt_m, group_id=None):
        return srv.F3KServer.record_altitude(self, pilot_id, flight_no, alt_m, group_id)

    def scratch_flight(self, pilot_id, dur_ms, group_id=None):
        return srv.F3KServer.scratch_flight(self, pilot_id, dur_ms, group_id)

    def record_jumped(self, pilot_id, dur_ms, group_id=None):
        return srv.F3KServer.record_jumped(self, pilot_id, dur_ms, group_id)


class JumpedStartTests(unittest.TestCase):
    """A jumped start is a launch. It counts, and it scores zero. [I-49]

    It used to be a CD note and nothing else — never written to the database — so
    the pilot did not lose the launch at all. On any launch-limited task they
    could jump the start and simply throw again for free.
    """

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)
        self.db.execute("INSERT INTO pilots (name) VALUES ('Jumper')")
        self.db.commit()
        self.pilot = self.db.execute("SELECT id FROM pilots").fetchone()["id"]
        self.srv = _DedupServer(self.db)
        self.client = srv.TimerClient(None, _Writer(), self.srv)
        self.client.timer_id = 1

        async def capture(msg):
            pass

        self.client.send = capture

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _rows(self):
        return self.db.execute(
            "SELECT duration_ms, scratched, void_reason, flight_no"
            " FROM flights ORDER BY id").fetchall()

    def _send(self, line):
        async def run():
            await self.client._dispatch(line)
            await asyncio.sleep(0)      # let create_task broadcasts complete

        asyncio.run(run())

    def test_a_jumped_start_is_recorded_as_a_voided_launch(self):
        self._send(f"JUMPED pilot={self.pilot} dur=9000")
        rows = self._rows()
        self.assertEqual(len(rows), 1, "the launch happened — it must be stored")
        self.assertEqual(rows[0]["duration_ms"], 9000)
        self.assertEqual(rows[0]["scratched"], 1, "and it must score zero")
        self.assertEqual(rows[0]["void_reason"], "jumped")

    def test_a_jumped_start_consumes_a_flight_number(self):
        """The hole: it must cost the pilot the launch, not be free."""
        self._send(f"FLIGHT pilot={self.pilot} dur=30000")
        self._send(f"JUMPED pilot={self.pilot} dur=4000")
        self._send(f"FLIGHT pilot={self.pilot} dur=50000")
        self.assertEqual([r["flight_no"] for r in self._rows()], [1, 2, 3])

    def test_the_reason_distinguishes_it_from_a_scratch(self):
        """Both score zero, but a dispute turns on which happened — and R-10 asks
        whether a jumped start should carry a penalty, which is unanswerable if
        the reason was never recorded."""
        self._send(f"FLIGHT pilot={self.pilot} dur=30000")
        self._send(f"SCRATCH pilot={self.pilot} dur=30000")
        self._send(f"JUMPED pilot={self.pilot} dur=4000")
        self.assertEqual([r["void_reason"] for r in self._rows()],
                         ["scratch", "jumped"])

    def test_a_repeated_jumped_is_not_double_counted(self):
        self._send(f"JUMPED pilot={self.pilot} dur=9000")
        self._send(f"JUMPED pilot={self.pilot} dur=9000")
        self.assertEqual(len(self._rows()), 1)

    def test_a_jumped_start_with_no_pilot_stores_nothing(self):
        """[I-25]'s rule still applies: an unattributable message is dropped, not
        guessed at."""
        self.client.pilot_id = 0
        self._send("JUMPED pilot=0 dur=9000")
        self.assertEqual(self._rows(), [])


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
        self.srv = _DedupServer(self.db)
        self.client = srv.TimerClient(None, _Writer(), self.srv)
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

    def test_resend_after_the_heat_closes_does_not_duplicate(self):
        """The case that actually happened on hardware. [I-27]

        The timer reconciles at the results screen, which is always AFTER the base
        has sent STOP/LAND and unloaded the heat. With `_loaded` gone the group was
        None, the dedup looked under group NULL, missed the originals sitting under
        the real group, and inserted a full set of orphan duplicates — the exact
        outcome the resend was built to prevent.
        """
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=19454")
        self.srv.close_heat()
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=19454 rc=1")
        self.assertEqual(self._count(), 1, "the resend duplicated after heat close")
        row = self.db.execute("SELECT group_id FROM flights").fetchone()
        self.assertEqual(row["group_id"], _DedupServer.GROUP,
                         "the flight must not be orphaned under group NULL")

    def test_resend_after_close_still_recovers_a_genuinely_lost_flight(self):
        """The fallback must not become a blanket suppressor: a flight that never
        arrived still has to land, and under the right heat."""
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=19454")
        self.srv.close_heat()
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=5256 rc=1")
        self.assertEqual(self._count(), 2)
        groups = [r["group_id"] for r in
                  self.db.execute("SELECT group_id FROM flights").fetchall()]
        self.assertEqual(groups, [_DedupServer.GROUP, _DedupServer.GROUP])

    def test_live_report_after_close_is_not_back_dated(self):
        """Only reconciliation copies get the fallback. A live report arriving with
        no heat loaded is a different situation and must not be filed into the
        previous round."""
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=19454")
        self.srv.close_heat()
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=5256")
        row = self.db.execute(
            "SELECT group_id FROM flights WHERE duration_ms = 5256").fetchone()
        self.assertIsNone(row["group_id"])

    def test_live_altitude_after_close_still_lands(self):
        """F5K heights are entered AFTER the round ends, so a live ALTITUDE always
        arrives with the heat closed. Gating the group fallback on rc=1 dropped
        every one of them and left the resend to recover each as a "loss". [I-29]
        """
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=13770")
        self.srv.close_heat()
        self._dispatch(f"ALTITUDE pilot={self.pilot} flight=1 alt=25")
        row = self.db.execute(
            "SELECT altitude_m FROM flights WHERE flight_no = 1").fetchone()
        self.assertEqual(row["altitude_m"], 25)

    def test_altitude_resend_of_an_applied_value_is_not_a_recovery(self):
        """Once the live report lands, the rc=1 copy is a no-op and must not raise
        a recovery alert — a warning that fires every round is one nobody reads."""
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=13770")
        self.srv.close_heat()
        self._dispatch(f"ALTITUDE pilot={self.pilot} flight=1 alt=25")
        self.assertFalse(
            self.srv.record_altitude(self.pilot, 1, 25, _DedupServer.GROUP),
            "an unchanged altitude must report changed=False")

    def test_live_flight_fallback_stays_off(self):
        """The altitude relaxation must not leak into FLIGHT: a live flight with no
        heat loaded still must not be back-dated into the previous round."""
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=13770")
        self.srv.close_heat()
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=9135")
        row = self.db.execute(
            "SELECT group_id FROM flights WHERE duration_ms = 9135").fetchone()
        self.assertIsNone(row["group_id"])

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

        self.server = types.SimpleNamespace(
            db=self.db, state_machine=_Loaded(),
            current_group_id=lambda: None)

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


class SilentTimerEvictionTests(unittest.TestCase):
    """A timer that stops answering must be announced, not just removed. [I-48]

    "If watch turned off run screen not updating." A powered-off watch sends no
    FIN, so the socket stays open and only the missing PINGs give it away. It was
    evicted at 90 s and then simply vanished from /api/timers — and a pill
    disappearing is not a notification. Connect and JOIN both broadcast; eviction
    did not.
    """

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)
        self.s = srv.F3KServer.__new__(srv.F3KServer)
        self.s.db = self.db
        self.s._clients = {}
        self.s._mac_to_id = {}
        self.s._mac_to_pilot = {}
        self.s.events = collections.deque(maxlen=40)
        self.broadcasts = 0

        async def counting_broadcast():
            self.broadcasts += 1

        self.s.broadcast_timers = counting_broadcast

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _client(self, timer_id, ping_age_s, rx_age_s=None):
        now = time.monotonic()
        c = types.SimpleNamespace(
            timer_id=timer_id, mac=f"aa:{timer_id}", addr=("10.0.0.9", 5000),
            last_ping_at=now - ping_age_s,
            last_rx_at=now - (ping_age_s if rx_age_s is None else rx_age_s),
            last_pilot_id=None, closed=False,
        )
        c.close = lambda: setattr(c, "closed", True)
        self.s._clients[timer_id] = c
        return c

    def test_a_silent_timer_is_evicted_and_announced(self):
        self._client(1, srv.PING_TIMEOUT_S + 10)
        evicted = asyncio.run(self.s.evict_silent_timers())
        self.assertEqual(len(evicted), 1)
        self.assertEqual(self.s._clients, {})
        self.assertEqual(self.broadcasts, 1,
                         "eviction must tell the clients, or the CD is never told")

    def test_a_healthy_timer_is_left_alone_and_nothing_is_broadcast(self):
        """No spurious broadcast every 30s — it would fight the poll for no gain."""
        self._client(1, 5)
        self.assertEqual(asyncio.run(self.s.evict_silent_timers()), [])
        self.assertEqual(self.broadcasts, 0)

    def test_a_timer_one_ping_short_is_not_evicted(self):
        """Timers ping every 30s and are evicted at 90s, so a single missed ping
        must not drop a working timer off the field."""
        self._client(1, 40)
        self.assertEqual(asyncio.run(self.s.evict_silent_timers()), [])
        self.assertIn(1, self.s._clients)

    def test_eviction_closes_the_socket(self):
        """A powered-off watch never sends FIN, so the socket leaks otherwise."""
        c = self._client(1, srv.PING_TIMEOUT_S + 10)
        asyncio.run(self.s.evict_silent_timers())
        self.assertTrue(c.closed)

    def test_rx_age_is_not_reset_by_our_own_keepalive_sends(self):
        """The heart of [I-48], and the thing that made it invisible.

        A powered-off watch never sends FIN, so the socket stays open and our
        writes keep succeeding into the kernel buffer for minutes. `_keepalive()`
        resets `last_ping_at` on every successful send, so a dead watch read as
        healthy indefinitely — measured on the Pi as 150 s of total silence with
        `last_ping_age_s` stuck between 7 s and 8 s, so the amber-at-45 s pill
        could never fire.

        A successful write proves nothing about the far end. `last_rx_age_s` moves
        only when the timer actually speaks, which is why the UI judges on it.
        """
        c = self._client(1, 5)
        c.last_rx_at = time.monotonic() - 300      # silent for five minutes
        c.last_ping_at = time.monotonic()          # ...but our sends kept "working"
        c.fw, c.connected_at = "fw-v30", time.time()
        row = self.s.timers_info()[0]
        self.assertLess(row["last_ping_age_s"], 45,
                        "precondition: the keepalive makes this look healthy")
        self.assertGreater(row["last_rx_age_s"], 290,
                           "rx age must expose the silence the ping clock hides")

    def test_a_silent_timer_is_evicted_even_while_our_sends_succeed(self):
        """The residual half of [I-48]: with the ping clock reset by keepalives, a
        powered-off watch was NEVER evicted — it held its timer ID and read as
        bound to its pilot indefinitely. Measured live at 120 s of silence with the
        ping age still under 8 s."""
        self._client(1, ping_age_s=5, rx_age_s=srv.RX_TIMEOUT_S + 10)
        self.assertEqual(len(asyncio.run(self.s.evict_silent_timers())), 1)
        self.assertEqual(self.broadcasts, 1)

    def test_rx_eviction_is_generous_enough_not_to_drop_a_working_timer(self):
        """Six missed PINGs at the firmware's 30 s interval. Evicting mid-round
        forces pilot re-selection, so the bias is toward waiting."""
        self.assertGreaterEqual(srv.RX_TIMEOUT_S, 180)
        self._client(1, ping_age_s=5, rx_age_s=120)
        self.assertEqual(asyncio.run(self.s.evict_silent_timers()), [])

    def test_one_broadcast_covers_several_evictions(self):
        for i in (1, 2, 3):
            self._client(i, srv.PING_TIMEOUT_S + 10)
        self.assertEqual(len(asyncio.run(self.s.evict_silent_timers())), 3)
        self.assertEqual(self.broadcasts, 1)


class ScratchTests(unittest.TestCase):
    """SCRATCH: a flight the caller discarded on the timer. [I-42]

    The flight is reported and stored the instant it is flown, so scratching it
    on the timer alone left it valid at the base — and in the CSV that goes to
    GliderScore. The row is flagged rather than deleted, because the timer
    re-reports the whole round from NVS when it ends and dedup matches on
    (pilot, group, duration): a deleted row would match nothing and be
    re-inserted seconds later, silently undoing the scratch.
    """

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)
        self.db.execute("INSERT INTO pilots (name) VALUES ('Alice')")
        self.db.commit()
        self.pilot = self.db.execute("SELECT id FROM pilots").fetchone()["id"]
        self.srv = _DedupServer(self.db)
        self.client = srv.TimerClient(None, _Writer(), self.srv)
        self.client.timer_id = 1
        self.sent = []

        async def capture(msg):
            self.sent.append(msg)

        self.client.send = capture

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _dispatch(self, line):
        self.sent.clear()

        async def run():
            await self.client._dispatch(line)
            await asyncio.sleep(0)

        asyncio.run(run())
        return self.sent

    def _rows(self):
        return self.db.execute(
            "SELECT duration_ms, scratched FROM flights ORDER BY id").fetchall()

    def test_scratch_flags_the_flight(self):
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430")
        self._dispatch(f"SCRATCH pilot={self.pilot} dur=125430")
        rows = self._rows()
        self.assertEqual(len(rows), 1, "the row must be kept, not deleted")
        self.assertEqual(rows[0]["scratched"], 1)

    def test_scratch_is_acked_verbatim(self):
        """ACK-gated like FLIGHT — the timer retries until the echo matches."""
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430")
        line = f"SCRATCH pilot={self.pilot} dur=125430"
        self.assertIn(f"ACK {line}", self._dispatch(line))

    def test_scratch_is_acked_even_when_nothing_matches(self):
        """Withholding the ACK would put the timer in an unbreakable retry loop."""
        line = f"SCRATCH pilot={self.pilot} dur=999999"
        self.assertIn(f"ACK {line}", self._dispatch(line))

    def test_only_the_named_flight_is_scratched(self):
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=100000")
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430")
        self._dispatch(f"SCRATCH pilot={self.pilot} dur=125430")
        rows = self._rows()
        self.assertEqual([r["scratched"] for r in rows], [0, 1])

    def test_end_of_round_resend_does_not_resurrect_a_scratch(self):
        """The reason the row is flagged and not deleted.

        A deleted row would find no dedup match here and be inserted again.
        """
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430")
        self._dispatch(f"SCRATCH pilot={self.pilot} dur=125430")
        self.srv.close_heat()
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430 rc=1")
        rows = self._rows()
        self.assertEqual(len(rows), 1, "the resend must not re-add the flight")
        self.assertEqual(rows[0]["scratched"], 1,
                         "and it must still be scratched afterwards")

    def test_repeated_scratch_is_harmless(self):
        """The ACK queue can retry, so the same SCRATCH may arrive twice."""
        self._dispatch(f"FLIGHT pilot={self.pilot} dur=125430")
        self._dispatch(f"SCRATCH pilot={self.pilot} dur=125430")
        self._dispatch(f"SCRATCH pilot={self.pilot} dur=125430")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scratched"], 1)


if __name__ == "__main__":
    unittest.main()


class PilotNameAudioKeyTests(unittest.TestCase):
    """Matching a pilot to the .wav GliderScore generated for them.

    GliderScore writes `Surname_Firstname.wav` from the name as stored, so the key
    is derived, not held. ⚠ /api/audio/pilot-coverage MUST use these same two
    functions: if the report and the callup key differently, the report says a
    pilot is covered and the callup silently skips them — a lie that only shows up
    at the ready box.
    """

    def test_matches_with_the_zz_marker_on_either_side(self):
        """ZZ prefixes test and duplicate roster entries; it is nobody's name. A
        roster cleaned up to 'Chris Barrenger' must still find ZZBarrenger_Chris."""
        from frontend.state_machine import audio_name_key, pilot_name_key
        for name, stem in (
            ("David ZZPratley", "ZZPratley_David"),   # marker on both
            ("David Pratley", "ZZPratley_David"),     # marker on the file only
            ("David ZZPratley", "Pratley_David"),     # marker on the pilot only
            ("Mike ZZO'Reilly", "ZZO'Reilly_Mike"),   # punctuation survives
        ):
            with self.subTest(name=name, stem=stem):
                self.assertEqual(pilot_name_key(name), audio_name_key(stem))

    def test_a_real_name_starting_zz_is_not_eaten(self):
        """Requires literal ZZ + a CAPITAL, which is how the marker is written.
        An earlier case-insensitive version turned 'Zzap' into 'ap'."""
        from frontend.state_machine import pilot_name_key
        self.assertEqual(pilot_name_key("Jo Zzap"), "zzap_jo")

    def test_multi_word_first_names_keep_all_their_parts(self):
        from frontend.state_machine import pilot_name_key
        self.assertEqual(pilot_name_key("Mary Jane Smith"), "smith_mary_jane")

    def test_a_single_word_name_has_no_key(self):
        """No surname means no GliderScore filename to match — reported as missing
        rather than guessed at."""
        from frontend.state_machine import pilot_name_key
        self.assertEqual(pilot_name_key("Madonna"), "")
        self.assertEqual(pilot_name_key(""), "")
