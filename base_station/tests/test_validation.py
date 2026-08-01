"""Input validation and state guards — the ISSUES.md register, pinned.

Every test here names the issue it locks down. These are all cases where the app
previously accepted nonsense silently or returned a 500 from ordinary UI use, so
a regression is a competition-day failure, not a cosmetic one.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# TestClient needs an HTTP client library that the server itself does not, so it is
# absent from the Pi venv. Skip rather than error: an ImportError here fails the
# whole discover run, and a red suite on the Pi reads as "the deploy broke
# something" when the production code is fine. Install with `pip install httpx2`
# in base_station/venv to get this coverage on the Pi too.
try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as exc:  # RuntimeError: starlette's own guard
    raise unittest.SkipTest(f"TestClient unavailable: {exc}") from None

from frontend import app as app_mod  # noqa: E402
from frontend.app import (  # noqa: E402
    MAX_ALTITUDE_M, _parse_altitude, _parse_duration, app,
)
from frontend.db import init_db  # noqa: E402
from frontend.state_machine import CompetitionStateMachine  # noqa: E402


class _FakeServer:
    """Stands in for the TCP server: holds the DB, swallows timer broadcasts."""

    def __init__(self, db):
        self.db = db
        self.sent = []

    async def broadcast(self, line):
        self.sent.append(line)


def _seed(db):
    db.execute("INSERT INTO competitions (name, discipline, date)"
               " VALUES ('Test', 'F3K', '2026-07-27')")
    comp_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO rounds (competition_id, round_no, task, working_time_s,"
               " discipline) VALUES (?, 1, 'A', 600, 'F3K')", (comp_id,))
    round_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO groups (round_id, group_no) VALUES (?, 1)", (round_id,))
    group_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO pilots (name) VALUES ('Alice')")
    pilot_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO competition_pilots (competition_id, pilot_id) VALUES (?, ?)",
               (comp_id, pilot_id))
    db.execute("INSERT INTO group_pilots (group_id, pilot_id) VALUES (?, ?)",
               (group_id, pilot_id))
    db.commit()
    return {"comp": comp_id, "round": round_id, "group": group_id, "pilot": pilot_id}


class ParserTests(unittest.TestCase):
    """[I-02] [I-03] — the two parsers that guard every entered number."""

    def test_seconds_must_be_under_60(self):
        # '1:99' used to parse as 2:39 — a wrong time that looks entirely plausible
        with self.assertRaises(ValueError):
            _parse_duration("1:99")

    def test_absurd_duration_rejected(self):
        with self.assertRaises(ValueError):
            _parse_duration("99999:99")

    def test_valid_durations_still_parse(self):
        self.assertEqual(_parse_duration("3:00"), 180_000)
        self.assertEqual(_parse_duration("3:00.55"), 180_550)
        self.assertEqual(_parse_duration("0:01"), 1_000)

    def test_altitude_rejects_infinity_and_nan(self):
        # inf poisons F5K bonus scoring; nan is coerced to NULL by SQLite, which
        # silently discards the entry
        for bad in ("inf", "-inf", "1e400", "nan"):
            with self.assertRaises(ValueError, msg=bad):
                _parse_altitude(bad)

    def test_altitude_rejects_out_of_range(self):
        for bad in ("-10", str(MAX_ALTITUDE_M + 1)):
            with self.assertRaises(ValueError, msg=bad):
                _parse_altitude(bad)

    def test_altitude_blank_is_none(self):
        self.assertIsNone(_parse_altitude(""))
        self.assertIsNone(_parse_altitude(None))
        self.assertEqual(_parse_altitude(" 47 "), 47.0)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)
        self.ids = _seed(self.db)
        self.server = _FakeServer(self.db)
        self.sm = CompetitionStateMachine(self.server)
        app.state.server = self.server
        app.state.state_machine = self.sm
        self.server.state_machine = self.sm
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    # ── P1 ────────────────────────────────────────────────────────────

    def test_load_refused_while_a_heat_is_running(self):
        """[I-01] Loading a different heat mid-run swapped the running heat out."""
        r = self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                             f"&group_id={self.ids['group']}")
        self.assertTrue(r.json()["ok"])
        self.sm._state = "PREP"
        r = self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                             f"&group_id={self.ids['group']}")
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.json()["ok"])

    def test_load_reports_a_missing_heat(self):
        """[I-10] A nonexistent group used to return ok:true."""
        r = self.client.post("/api/run/load?round_id=9999&group_id=9999")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["ok"])

    def test_start_reports_refusal(self):
        """[I-10] start() refuses internally; the API used to claim success."""
        r = self.client.post("/api/run/start")     # nothing loaded
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.json()["ok"])

    def test_flight_longer_than_working_time_rejected(self):
        """[I-04] A 69-day flight was accepted into a 10-minute round."""
        self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                         f"&group_id={self.ids['group']}")
        r = self.client.post("/api/run/flight/add", data={
            "pilot_id": self.ids["pilot"], "duration": "20:00"})
        self.assertFalse(r.json()["ok"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM flights").fetchone()[0], 0)

    # ── P2 — reachable 500s ───────────────────────────────────────────

    def test_bad_altitude_does_not_500(self):
        """[I-05] float(altitude_m) sat outside the try/except in three handlers."""
        self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                         f"&group_id={self.ids['group']}")
        r = self.client.post("/api/run/flight/add", data={
            "pilot_id": self.ids["pilot"], "duration": "3:00", "altitude_m": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])

        r = self.client.post("/api/run/altitude/set",
                             data={"flight_id": 1, "altitude_m": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])

        r = self.client.post("/results/flight/add", follow_redirects=False, data={
            "group_id": self.ids["group"], "pilot_id": self.ids["pilot"],
            "duration": "3:00", "altitude_m": "x"})
        self.assertEqual(r.status_code, 303)

    def test_bad_flight_no_does_not_500(self):
        """[I-06] int(flight_no) was unguarded."""
        r = self.client.post("/results/flight/add", follow_redirects=False, data={
            "group_id": self.ids["group"], "pilot_id": self.ids["pilot"],
            "duration": "3:00", "flight_no": "abc"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM flights").fetchone()[0], 0)

    def test_draw_endpoints_survive_malformed_bodies(self):
        """[I-07] Direct subscripts and bare int() — reproduced on the Pi."""
        for path in ("/api/draw/preview", "/api/draw/accept"):
            for body in ({}, {"comp_id": "x"}, {"comp_id": 1}):
                r = self.client.post(path, json=body)
                self.assertEqual(r.status_code, 200, f"{path} {body}")
                self.assertFalse(r.json()["ok"], f"{path} {body}")
            r = self.client.post(path, content=b"not json",
                                 headers={"Content-Type": "application/json"})
            self.assertEqual(r.status_code, 200, path)

    def test_unknown_foreign_keys_do_not_500(self):
        """[I-08] FK/CHECK violations surfaced raw."""
        r = self.client.post("/setup/competition/9999/pilot/add",
                             follow_redirects=False, data={"pilot_id": self.ids["pilot"]})
        self.assertEqual(r.status_code, 303)

        r = self.client.post("/setup/competition/new", follow_redirects=False, data={
            "name": "X", "discipline": "F9K", "date": "2026-07-27"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM competitions WHERE name='X'")
            .fetchone()[0], 0)

        r = self.client.post("/results/flight/add", follow_redirects=False, data={
            "group_id": self.ids["group"], "pilot_id": 9999, "duration": "3:00"})
        self.assertEqual(r.status_code, 303)

    def test_binary_upload_is_not_a_csv(self):
        """[I-09] An .xlsx picked by mistake raised inside csv.reader."""
        r = self.client.post("/setup/pilots/import", follow_redirects=False,
                             files={"file": ("roster.xlsx", b"PK\x03\x04\x00\x00binary")})
        self.assertEqual(r.status_code, 303)

    # ── P3 ────────────────────────────────────────────────────────────

    def test_complete_broadcasts_and_reports_honestly(self):
        """[I-10] [I-11] Always-true ok, and a leaderboard event nothing sent."""
        sent = []

        async def capture(data):
            sent.append(data)

        original, app_mod.manager.broadcast = app_mod.manager.broadcast, capture
        try:
            r = self.client.post(f"/api/run/complete?group_id={self.ids['group']}")
            self.assertTrue(r.json()["ok"])
            self.assertIn("complete", [m["type"] for m in sent])

            r = self.client.post("/api/run/complete?group_id=9999")
            self.assertEqual(r.status_code, 404)
            self.assertFalse(r.json()["ok"])
        finally:
            app_mod.manager.broadcast = original

    def test_missing_form_field_is_not_raw_json(self):
        """[I-12] Assign with nothing ticked rendered a bare validation dump."""
        r = self.client.post("/setup/pilots/assign", follow_redirects=False,
                             data={"comp_id": self.ids["comp"]},
                             headers={"Accept": "text/html", "Referer": "http://x/setup"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=", r.headers["location"])

    # ── P4 ────────────────────────────────────────────────────────────

    def test_round_creation_rejects_nonsense(self):
        """[I-17] Zero-minute working times and unknown task codes were accepted."""
        before = self.db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        for data in ({"task": "A", "working_time_m": 0},
                     {"task": "A", "working_time_m": -5},
                     {"task": "Z", "working_time_m": 10}):
            r = self.client.post(f"/rounds/{self.ids['comp']}/add",
                                 follow_redirects=False, data=data)
            self.assertEqual(r.status_code, 303, data)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], before)

    def test_custom_task_saves_without_the_inapplicable_fields(self):
        """Save did nothing for ladder/targets/sequence clones (F3K D, H, K, M).

        The N input carries min="1" but those kinds clone with n=0 and the field
        is hidden, so the browser blocked submission on an unfocusable invalid
        control — no request ever left the page. Those fields are now disabled as
        well as hidden, which means they are absent from the POST entirely; this
        pins that the server accepts that shape.
        """
        r = self.client.post("/tasks/custom/add", follow_redirects=False, data={
            "discipline": "F3K", "code": "T1", "name": "Custom targets",
            "kind": "targets", "targets": "1:00, 2:00, 3:00", "wt_min": 10})
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("error=", r.headers["location"])
        row = self.db.execute(
            "SELECT * FROM custom_tasks WHERE code = 'T1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "targets")

        # ...and a sequence clone, the other shape that used to be unsaveable
        r = self.client.post("/tasks/custom/add", follow_redirects=False, data={
            "discipline": "F3K", "code": "T2", "name": "Custom ladder",
            "kind": "ladder", "start_s": "0:30", "step_s": "0:15", "wt_min": 10})
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("error=", r.headers["location"])

    def test_a_scratched_flight_exports_as_a_zero_in_its_own_slot(self):
        """[I-46] The CSV is the competition result GliderScore imports.

        A scratch is a land-out, so it must appear as a zero — not be omitted.
        Omitting it shifts every later flight up a slot, so GliderScore would
        score a different flight from us and neither system would look wrong.
        """
        pilot = self.ids["pilot"]
        group = self.ids["group"]
        for dur, scratched in ((12000, 0), (34000, 1), (56000, 0)):
            self.db.execute(
                "INSERT INTO flights (pilot_id, group_id, duration_ms, scratched)"
                " VALUES (?, ?, ?, ?)", (pilot, group, dur, scratched))
        self.db.commit()

        body = self.client.get(f"/export/{self.ids['comp']}/csv").text
        row = [ln for ln in body.splitlines() if "012.000" in ln]
        self.assertTrue(row, f"pilot row not found in export:\n{body}")
        fields = row[0].split(",")
        # Data1-3. The scratched flight holds slot 2 as a zero, and the 56 s
        # flight stays in slot 3 rather than being promoted into slot 2.
        self.assertEqual(fields[6], "012.000")
        self.assertEqual(fields[7], "000.000", "the scratch must export as a zero time")
        self.assertEqual(fields[8], "056.000", "later flights must not shift up a slot")

    def test_api_docs_are_not_exposed(self):
        """[I-21] /docs is a POST console for run control on the public AP.

        These paths still answer 200 — the captive-portal catch-all claims every
        unrouted path — so assert on the body, not the status. What must be gone
        is the schema and the Swagger/ReDoc bundle that drives it.
        """
        for path in ("/docs", "/redoc", "/openapi.json"):
            body = self.client.get(path).text.lower()
            self.assertNotIn("swagger", body, path)
            self.assertNotIn("redoc", body, path)
            self.assertNotIn("openapi", body, path)


if __name__ == "__main__":
    unittest.main()
