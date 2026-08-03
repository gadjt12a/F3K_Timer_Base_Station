"""Input validation and state guards — the ISSUES.md register, pinned.

Every test here names the issue it locks down. These are all cases where the app
previously accepted nonsense silently or returned a 500 from ordinary UI use, so
a regression is a competition-day failure, not a cosmetic one.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

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


class StartupTests(unittest.TestCase):
    """⚠ `TestClient(app)` without a `with` block never runs the startup hooks, so
    every test in this file could pass against an app that dies on boot. It did
    exactly that once: the OTA auto-push task was added with no `import asyncio`,
    the suite went green, and the service crash-looped on the Pi.

    Entering the context manager runs them. Anything registered with
    `@app.on_event("startup")` is covered by this one test."""

    def test_the_app_actually_starts(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = init_db(path)
        _seed(db)
        server = _FakeServer(db)
        app.state.server = server
        app.state.state_machine = CompetitionStateMachine(server)
        server.state_machine = app.state.state_machine
        try:
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/run/state").status_code, 200)
        finally:
            db.close()
            os.unlink(path)


class RunControlContractTests(unittest.TestCase):
    """The [I-01] / [I-13] family, pinned as a rule rather than case by case.

    Three bugs now share one shape: the page believed something the server never
    confirmed. [I-01] and [I-13] were a DEAD socket leaving client gates reading
    IDLE for ever. The 2026-08-03 callup bug was the mirror — a HEALTHY socket, so
    the fallback poll never ran and any field the tick stream does not carry went
    stale instead.

    Both halves reduce to the same rule, which these tests enforce:

      A run-control endpoint that changes what the page must display returns the
      authoritative status in its response. The client never re-derives it.
    """

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

    def test_state_carries_every_field_the_page_cannot_derive(self):
        """The tick stream carries state, seconds and a rebuilt `loaded` — nothing
        else. Anything the page shows beyond that must be here, or it is only
        correct while the websocket is broken."""
        s = self.client.get("/api/run/state").json()
        for field in ("state", "loaded", "flights", "callup", "callup_overridden"):
            with self.subTest(field=field):
                self.assertIn(field, s)

    def test_mutations_return_the_new_status(self):
        """load and unload both change what the page must show. Returning `ok`
        alone forces the client to guess, and it guessed wrong for a whole
        afternoon."""
        r = self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                             f"&group_id={self.ids['group']}").json()
        self.assertIn("status", r)
        self.assertIsNotNone(r["status"]["loaded"])
        u = self.client.post("/api/run/unload").json()
        self.assertIn("status", u)
        self.assertIsNone(u["status"]["loaded"])

    def test_refusals_say_why_and_do_not_pretend_to_succeed(self):
        """[I-01]: client-side gating cannot be the lock, so the server refuses
        with 409 and a reason the CD can act on — never a silent {"ok": true}."""
        self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                         f"&group_id={self.ids['group']}")
        self.client.post("/api/run/start")
        for path in ("/api/run/unload",
                     f"/api/run/load?round_id={self.ids['round']}&group_id={self.ids['group']}",
                     "/api/run/callup?on=true"):
            with self.subTest(path=path):
                r = self.client.post(path)
                self.assertEqual(r.status_code, 409)
                self.assertFalse(r.json()["ok"])
                self.assertTrue(r.json().get("error"), "a refusal must say why")


class UnloadTests(unittest.TestCase):
    """Clearing a loaded heat. Distinct from abort, which keeps it loaded so a CD
    can restart a round that went wrong (Kris, 2026-08-02). Until this existed the
    only way to put a heat down was to load a different one — and a loaded heat
    holds off firmware pushes, because `_loaded` is otherwise cleared only by a
    round running to completion."""

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

    def _load(self):
        return self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                                f"&group_id={self.ids['group']}")

    def test_unload_clears_the_heat(self):
        self._load()
        self.assertIsNotNone(self.client.get("/api/run/state").json()["loaded"])
        r = self.client.post("/api/run/unload")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.client.get("/api/run/state").json()["loaded"])

    def test_unload_with_nothing_loaded_is_not_an_error(self):
        self.assertEqual(self.client.post("/api/run/unload").status_code, 200)

    def test_load_and_unload_report_the_callup_state(self):
        """⚠ The Run page CANNOT re-derive this. pollState() returns early while
        the websocket is healthy, so callup is refreshed only by what these two
        responses carry. Without it the button kept whatever the last manual
        toggle set for ever, and the Settings default looked like it did nothing —
        reported from the field, same family as [I-01]/[I-13].
        """
        r = self._load().json()
        self.assertIn("callup", r["status"])
        self.assertIn("callup_overridden", r["status"])
        u = self.client.post("/api/run/unload").json()
        self.assertIn("callup", u["status"])
        self.assertIn("callup_overridden", u["status"])

    def test_a_per_heat_callup_override_dies_with_the_heat(self):
        """Kris: whichever way the default is set, loading a different heat returns
        to it — whether or not the last heat was actually run."""
        self._load()
        base = self.client.get("/api/run/state").json()["callup"]
        r = self.client.post(f"/api/run/callup?on={str(not base).lower()}").json()
        self.assertEqual(r["callup"], not base)
        self.assertTrue(r["overridden"])
        # A different heat, without running the first one
        after = self._load().json()["status"]
        self.assertEqual(after["callup"], base)
        self.assertFalse(after["callup_overridden"])

    def test_unload_refused_while_a_round_runs(self):
        """Same 409-with-a-reason contract as every other run control [I-01]."""
        self._load()
        self.client.post("/api/run/start")
        r = self.client.post("/api/run/unload")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["error"])
        self.assertIsNotNone(self.client.get("/api/run/state").json()["loaded"])


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

    def test_run_state_carries_the_recorded_flights(self):
        """[I-47] The Run page built its flight log from live websocket pushes
        alone, so every recorded time vanished on any page load — a refresh, or
        going to /results to correct an earlier heat and coming back, mid-heat.

        get_status() feeds both the server-side page render and /api/run/state,
        so seeding it there covers the reload and the socket-down poll at once.
        """
        self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                         f"&group_id={self.ids['group']}")
        pilot = self.ids["pilot"]
        for dur, scratched in ((12000, 0), (34000, 1)):
            self.db.execute(
                "INSERT INTO flights (pilot_id, group_id, duration_ms, scratched)"
                " VALUES (?, ?, ?, ?)",
                (pilot, self.ids["group"], dur, scratched))
        self.db.commit()

        s = self.client.get("/api/run/state").json()
        self.assertEqual(len(s["flights"]), 2,
                         "a reloaded Run page must see both recorded flights")
        self.assertEqual(s["flights"][0]["duration_ms"], 12000)
        self.assertFalse(s["flights"][0]["scratched"])
        self.assertTrue(s["flights"][1]["scratched"],
                        "and must know which was scratched, or it shows green")
        # Shaped like the live `flight` event so the page can use one code path.
        for f in s["flights"]:
            self.assertEqual(f["type"], "flight")
            self.assertIn("pilot_name", f)

    def test_run_state_has_no_flights_when_nothing_is_loaded(self):
        """[I-47] No heat loaded is a normal state, not an error."""
        s = self.client.get("/api/run/state").json()
        self.assertEqual(s["flights"], [])

    def test_run_state_flights_are_scoped_to_the_loaded_heat(self):
        """[I-47] Hydration must not drag in another heat's flights — the CD
        would see times against pilots who have not flown this round."""
        other = self.db.execute(
            "INSERT INTO groups (round_id, group_no) VALUES (?, 2)",
            (self.ids["round"],)).lastrowid
        self.db.execute(
            "INSERT INTO flights (pilot_id, group_id, duration_ms) VALUES (?, ?, ?)",
            (self.ids["pilot"], other, 99000))
        self.db.commit()
        self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                         f"&group_id={self.ids['group']}")
        s = self.client.get("/api/run/state").json()
        self.assertEqual(s["flights"], [])

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

    # ── Task mode + WTSYNC on the wire [TF-10]/[TF-11]/[I-51] ─────────

    def test_task_line_names_the_mode_and_its_parameters(self):
        """[TF-10] The timer had no task letter at all, so it could not know it
        was flying Poker. Params are APPENDED — wt and disc keep their places so a
        pre-v31 timer reads exactly what it always did."""
        self.sm._loaded = {"working_time_s": 600, "discipline": "F3K", "task": "D"}
        line = self.sm._task_line(600)
        self.assertTrue(line.startswith("TASK wt=600 disc=F3K "), line)
        self.assertIn("task=D", line)
        self.assertIn("mode=ladder", line)
        self.assertIn("start=30", line)
        self.assertIn("step=15", line)

    def test_task_line_for_a_plain_task_carries_no_target_params(self):
        self.sm._loaded = {"working_time_s": 600, "discipline": "F3K", "task": "A"}
        line = self.sm._task_line(600)
        self.assertIn("mode=plain", line)
        for junk in ("start=", "step=", "targets="):
            self.assertNotIn(junk, line)

    def test_task_line_for_poker_carries_the_target_count(self):
        self.sm._loaded = {"working_time_s": 600, "discipline": "F3K", "task": "E"}
        self.assertIn("mode=poker", self.sm._task_line(600))
        self.assertIn("targets=3", self.sm._task_line(600))

    def test_catchup_syncs_the_working_clock_in_seconds(self):
        """[I-51] This used to send `TASK wt=<rem>` + START. The firmware does
        `g_wtMinutes = seconds / 60`, so a timer rejoining with 45 s left was told
        ZERO, and one with 8:30 left was told 8:00."""
        self.sm._loaded = {"working_time_s": 600, "discipline": "F3K", "task": "A",
                           "pilot_id_names": [], "land_time_s": 30}
        self.sm._state = "WORKING"
        self.sm._wt_remaining = 45
        sent = []

        async def send(line):
            sent.append(line)

        import asyncio
        asyncio.run(self.sm.send_catchup(send))
        self.assertIn("WTSYNC t=45", sent)
        # And the full working time on TASK, not the remainder — TASK configures
        # the round, WTSYNC steers the clock.
        self.assertTrue(any(x.startswith("TASK wt=600 ") for x in sent), sent)

    # ── Test mode [TF-16] ─────────────────────────────────────────────

    def _set_test_mode(self, on):
        cfg = app_mod.audio_control.load_config()
        cfg["test_mode"] = on
        app_mod.audio_control.save_config(cfg)
        self.addCleanup(self._clear_test_mode)

    def _clear_test_mode(self):
        cfg = app_mod.audio_control.load_config()
        cfg.pop("test_mode", None)
        app_mod.audio_control.save_config(cfg)

    def test_fast_forward_is_refused_when_test_mode_is_off(self):
        """[TF-16] Cutting a working window short falsifies the round, so this must
        never be reachable by accident. The server refuses; hiding the button is
        only so a CD never sees it."""
        self._set_test_mode(False)
        self.client.post(f"/api/run/load?round_id={self.ids['round']}"
                         f"&group_id={self.ids['group']}")
        self.sm._state = "WORKING"
        r = self.client.post("/api/run/fast-forward?to=15")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.json()["ok"])
        self.assertIsNone(self.sm._skip_to, "the clock must not have moved")

    def test_fast_forward_works_in_every_running_phase_with_test_mode_on(self):
        self._set_test_mode(True)
        for phase in ("PREP", "WORKING", "LANDING"):
            with self.subTest(phase=phase):
                self.sm._state = phase
                self.sm._skip_to = None
                r = self.client.post("/api/run/fast-forward?to=15")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self.sm._skip_to, 15)

    def test_fast_forward_refuses_when_nothing_is_running(self):
        """[I-10]'s rule: report the refusal rather than claiming success."""
        self._set_test_mode(True)
        self.sm._state = "IDLE"
        r = self.client.post("/api/run/fast-forward?to=15")
        self.assertEqual(r.status_code, 409)
        self.assertIn("IDLE", r.json()["error"])

    def test_test_mode_needs_the_passcode(self):
        r = self.client.post("/testmode", data={"passcode": "wrong", "enable": "true"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(app_mod.test_mode_on())

    def test_the_passcode_turns_it_on_and_off_again(self):
        self.addCleanup(self._clear_test_mode)
        pc = app_mod._TEST_PASSCODE
        self.assertTrue(self.client.post(
            "/testmode", data={"passcode": pc, "enable": "true"}).json()["test_mode"])
        self.assertTrue(app_mod.test_mode_on())
        self.client.post("/testmode", data={"passcode": pc, "enable": "false"})
        self.assertFalse(app_mod.test_mode_on(), "must be switchable back off")

    def test_the_run_page_shows_no_trace_of_test_mode_when_it_is_off(self):
        """[TF-16] "Invisible until activated" is stronger than "password
        protected" — a greyed-out control on a competition screen is still one a
        CD can find and ask about."""
        self._set_test_mode(False)
        body = self.client.get("/run").text
        self.assertNotIn("TEST MODE", body)
        self.assertNotIn("fast-forward", body)

    def test_the_run_page_warns_loudly_when_test_mode_is_on(self):
        """A round that ends early must never leave a CD wondering why."""
        self._set_test_mode(True)
        body = self.client.get("/run").text
        self.assertIn("TEST MODE", body)

    def test_poker_exports_the_called_time_not_the_flown_one(self):
        """[I-50] Kris: "GS will only take the first 3 called flight lengths."

        FAI credits the announced target, so 46 s flown against a 45 s call is
        worth 45 — and a call that was missed is worth nothing, even though the
        glider was plainly in the air.
        """
        self.db.execute("UPDATE rounds SET task = 'E' WHERE id = ?",
                        (self.ids["round"],))
        for dur, target in ((46000, 45000), (48000, 50000)):
            self.db.execute(
                "INSERT INTO flights (pilot_id, group_id, duration_ms,"
                " declared_target_ms) VALUES (?, ?, ?, ?)",
                (self.ids["pilot"], self.ids["group"], dur, target))
        self.db.commit()

        body = self.client.get(f"/export/{self.ids['comp']}/csv").text
        row = [ln for ln in body.splitlines() if "045.000" in ln]
        self.assertTrue(row, f"called time not in export:\n{body}")
        fields = row[0].split(",")
        self.assertEqual(fields[6], "045.000", "achieved call exports as the CALL")
        self.assertEqual(fields[7], "000.000", "a missed call is worth nothing")

    def test_a_non_poker_task_still_exports_the_flown_time(self):
        """Only Poker is special. Everything else sends raw times and lets
        GliderScore apply the task rule, as it always has."""
        self.db.execute(
            "INSERT INTO flights (pilot_id, group_id, duration_ms,"
            " declared_target_ms) VALUES (?, ?, ?, ?)",
            (self.ids["pilot"], self.ids["group"], 46000, 45000))
        self.db.commit()
        body = self.client.get(f"/export/{self.ids['comp']}/csv").text
        self.assertIn("046.000", body)

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


class UiChecksTests(unittest.TestCase):
    """The T8 check sheet (/checks). Its own JSON store, never the competition DB.

    ⚠ Route ORDER is the thing worth pinning here. `/api/ui-checks/reset` and
    `/api/ui-checks/{check_id}` both match "reset", and FastAPI takes the first
    declared — so with them the wrong way round, Reset answers 404 "no such
    check" and silently does nothing. It did exactly that until it was tested.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        from frontend import ui_checks
        self.mod = ui_checks
        self._orig = ui_checks._STORE
        ui_checks._STORE = Path(self.tmp) / "ui_checks.json"
        self.client = TestClient(app)

    def tearDown(self):
        self.mod._STORE = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reset_is_not_swallowed_by_the_id_route(self):
        first = [c["id"] for s in self.mod.SECTIONS for c in s["checks"]][0]
        self.client.post(f"/api/ui-checks/{first}", json={"status": "pass"})
        self.assertEqual(self.client.get("/api/ui-checks").json()["counts"]["pass"], 1)
        r = self.client.post("/api/ui-checks/reset")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/ui-checks").json()["counts"]["pass"], 0)

    def test_a_tick_survives_and_carries_its_note(self):
        r = self.client.post("/api/ui-checks/i63-stale-fields",
                             json={"status": "fail", "note": "leaderboard tab stale"})
        self.assertEqual(r.status_code, 200)
        state = self.client.get("/api/ui-checks").json()["state"]
        self.assertEqual(state["i63-stale-fields"]["status"], "fail")
        self.assertEqual(state["i63-stale-fields"]["note"], "leaderboard tab stale")

    def test_unknown_check_and_bad_status_are_refused(self):
        self.assertEqual(
            self.client.post("/api/ui-checks/nope", json={"status": "pass"}).status_code, 404)
        first = [c["id"] for s in self.mod.SECTIONS for c in s["checks"]][0]
        self.assertEqual(
            self.client.post(f"/api/ui-checks/{first}", json={"status": "banana"}).status_code,
            400)

    def test_every_check_is_complete_and_uniquely_identified(self):
        """A check with no `why` becomes a ritual: click it, see something, tick it."""
        ids = [c["id"] for s in self.mod.SECTIONS for c in s["checks"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate check id")
        for sec in self.mod.SECTIONS:
            for c in sec["checks"]:
                for key in ("title", "how", "expect", "why"):
                    with self.subTest(check=c["id"], key=key):
                        self.assertTrue(c.get(key), f"{c['id']} missing {key}")
