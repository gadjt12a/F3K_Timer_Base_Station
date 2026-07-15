"""Integration tests: scoring engine DB glue against the real schema (db.py)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from base_station.frontend import scoring  # noqa: E402
from base_station.frontend.db import init_db  # noqa: E402


def _setup_comp(db, discipline="F3K", task="A", rounds=1):
    db.execute(
        "INSERT INTO competitions (name, discipline, date) VALUES ('T', ?, '2026-07-15')",
        (discipline if discipline in ("F3K", "F5K", "MIXED") else "F3K",))
    comp_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    ids = {"comp": comp_id, "rounds": [], "groups": []}
    for rn in range(1, rounds + 1):
        db.execute(
            "INSERT INTO rounds (competition_id, round_no, task, working_time_s, discipline)"
            " VALUES (?, ?, ?, 600, ?)",
            (comp_id, rn, task, discipline if discipline != "MIXED" else "F3K"))
        rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO groups (round_id, group_no) VALUES (?, 1)", (rid,))
        gid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        ids["rounds"].append(rid)
        ids["groups"].append(gid)
    db.commit()
    return ids


def _add_pilot(db, name, comp_id, group_ids):
    db.execute("INSERT INTO pilots (name) VALUES (?)", (name,))
    pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO competition_pilots (competition_id, pilot_id) VALUES (?, ?)",
               (comp_id, pid))
    for gid in group_ids:
        db.execute("INSERT INTO group_pilots (group_id, pilot_id) VALUES (?, ?)", (gid, pid))
    db.commit()
    return pid


def _fly(db, pid, gid, secs, alt=None, source=None):
    db.execute(
        "INSERT INTO flights (pilot_id, duration_ms, group_id, altitude_m, altitude_source)"
        " VALUES (?, ?, ?, ?, ?)", (pid, int(secs * 1000), gid, alt, source))
    db.commit()


class TestScoringDb(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = init_db(self.path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_migrations_added_config_columns(self):
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(competitions)")}
        for c in ("drop1_at_round", "f5k_ref_height", "f5k_min_time_for_bonus", "time_decimals"):
            self.assertIn(c, cols)
        self.assertIn("altitude_source",
                      {r[1] for r in self.db.execute("PRAGMA table_info(flights)")})
        self.assertIn("ref_height_m",
                      {r[1] for r in self.db.execute("PRAGMA table_info(rounds)")})
        self.assertIn("fai_number",
                      {r[1] for r in self.db.execute("PRAGMA table_info(pilots)")})

    def test_f3k_group_scoring_and_norm(self):
        ids = _setup_comp(self.db, "F3K", task="A")
        gid = ids["groups"][0]
        p1 = _add_pilot(self.db, "Alice", ids["comp"], [gid])
        p2 = _add_pilot(self.db, "Bob", ids["comp"], [gid])
        _fly(self.db, p1, gid, 100)
        _fly(self.db, p1, gid, 290)   # last flight counts: 290
        _fly(self.db, p2, gid, 145)   # last: 145
        res = scoring.score_group_db(self.db, gid)
        self.assertEqual(res["pilots"][p1]["raw_s"], 290)
        self.assertEqual(res["pilots"][p1]["norm"], 1000.0)
        self.assertEqual(res["pilots"][p2]["norm"], 500.0)
        self.assertEqual(res["pilots"][p2]["rank"], 2)

    def test_f5k_bonus_included(self):
        ids = _setup_comp(self.db, "F5K", task="B")
        gid = ids["groups"][0]
        p1 = _add_pilot(self.db, "Alice", ids["comp"], [gid])
        _fly(self.db, p1, gid, 200, alt=49, source="timer")  # 11m below ref 60 -> +5.5
        res = scoring.score_group_db(self.db, gid)
        self.assertEqual(res["pilots"][p1]["total"], 205.5)
        self.assertEqual(res["pilots"][p1]["flights"][0]["bonus"], 5.5)
        self.assertEqual(res["ref_height"], 60)

    def test_f5k_bonus_gated_by_min_time(self):
        ids = _setup_comp(self.db, "F5K", task="B")
        gid = ids["groups"][0]
        p1 = _add_pilot(self.db, "Alice", ids["comp"], [gid])
        _fly(self.db, p1, gid, 25, alt=40)  # under 30s -> no bonus
        res = scoring.score_group_db(self.db, gid)
        self.assertIsNone(res["pilots"][p1]["flights"][0]["bonus"])
        self.assertEqual(res["pilots"][p1]["total"], 25)

    def test_competition_standings_with_drop(self):
        ids = _setup_comp(self.db, "F3K", task="A", rounds=3)
        self.db.execute("UPDATE competitions SET drop1_at_round = 3 WHERE id = ?",
                        (ids["comp"],))
        self.db.commit()
        g1, g2, g3 = ids["groups"]
        p1 = _add_pilot(self.db, "Alice", ids["comp"], [g1, g2, g3])
        p2 = _add_pilot(self.db, "Bob", ids["comp"], [g1, g2, g3])
        _fly(self.db, p1, g1, 300); _fly(self.db, p2, g1, 150)  # A 1000 / B 500
        _fly(self.db, p1, g2, 100); _fly(self.db, p2, g2, 300)  # A 333.3 / B 1000
        _fly(self.db, p1, g3, 300); _fly(self.db, p2, g3, 240)  # A 1000 / B 800
        data = scoring.competition_standings(self.db, ids["comp"])
        rows = {r["pilot_id"]: r for r in data["standings"]}
        self.assertEqual(data["drops"], 1)
        # Alice drops 333.3 -> 2000; Bob drops 500 -> 1800
        self.assertEqual(rows[p1]["total"], 2000.0)
        self.assertEqual(rows[p2]["total"], 1800.0)
        self.assertEqual(rows[p1]["rank"], 1)
        self.assertTrue(rows[p1]["rounds"][2]["dropped"])
        self.assertEqual(rows[p1]["name"], "Alice")

    def test_custom_task_rule_loading(self):
        # Clone of F3K B with a 2:00 cap and 3 flights counting, code T1
        self.db.execute(
            """INSERT INTO custom_tasks (discipline, code, name, kind, n, cap_s, based_on)
               VALUES ('F3K', 'T1', 'Last 3 short', 'last_n', 3, 120, 'B')""")
        self.db.commit()
        scoring.load_custom_rules(self.db)
        try:
            r = scoring.score_task("F3K", "T1", [150_000, 90_000, 130_000, 60_000])
            self.assertEqual(r.flight_scores, [0, 90, 120, 60])
            # Reload after delete clears the rule; unknown task falls back to all-count
            self.db.execute("DELETE FROM custom_tasks")
            self.db.commit()
            scoring.load_custom_rules(self.db)
            r = scoring.score_task("F3K", "T1", [150_000])
            self.assertEqual(r.flight_scores, [150])
        finally:
            scoring.load_custom_rules(self.db)  # leave no stale registrations

    def test_uncontested_round_excluded(self):
        ids = _setup_comp(self.db, "F3K", task="A", rounds=2)
        gid = ids["groups"][0]
        p1 = _add_pilot(self.db, "Alice", ids["comp"], ids["groups"])
        _fly(self.db, p1, gid, 100)
        data = scoring.competition_standings(self.db, ids["comp"])
        self.assertEqual(len(data["standings"]), 1)
        contested = [r for r in data["rounds"] if r["contested"]]
        self.assertEqual(len(contested), 1)
        self.assertEqual(data["standings"][0]["total"], 1000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
