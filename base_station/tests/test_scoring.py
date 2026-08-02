"""Unit tests for the pure scoring engine (scoring.py, Phases A1-A4).

Run:  python -m unittest base_station.tests.test_scoring -v
  or: python base_station/tests/test_scoring.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from base_station.frontend import scoring  # noqa: E402
from base_station.frontend.scoring import (  # noqa: E402
    f5k_bonus, normalise, parse_task, score_task, standings,
)

MS = 1000  # seconds -> ms


def s(*secs):
    return [int(x * MS) for x in secs]


class TestParseTask(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_task("A(1)"), ("A", 1))
        self.assertEqual(parse_task("C(3)"), ("C", 3))
        self.assertEqual(parse_task("D"), ("D", None))
        self.assertEqual(parse_task("U10"), ("U10", None))
        self.assertEqual(parse_task(" b(2) "), ("B", 2))


class TestF3KTasks(unittest.TestCase):
    def test_a_last_flight_capped(self):
        r = score_task("F3K", "A", s(120, 200, 340))
        self.assertEqual(r.flight_scores, [0, 0, 300])  # last only, 5:00 cap
        self.assertEqual(r.raw_s, 300)

    def test_a_single_flight(self):
        r = score_task("F3K", "A(1)", s(45.5))
        self.assertEqual(r.raw_s, 45.5)

    def test_b_last_two(self):
        r = score_task("F3K", "B", s(250, 100, 245))
        self.assertEqual(r.flight_scores, [0, 100, 240])  # 4:00 cap
        self.assertEqual(r.raw_s, 340)

    def test_b2_lower_cap(self):
        r = score_task("F3K", "B(2)", s(250, 100))
        self.assertEqual(r.flight_scores, [180, 100])  # 3:00 cap

    def test_c_all_up(self):
        r = score_task("F3K", "C(1)", s(200, 100, 150, 90))
        # 3 launches count; 4th ignored; 3:00 cap
        self.assertEqual(r.flight_scores, [180, 100, 150, 0])
        self.assertEqual(r.raw_s, 430)

    def test_c3_five_launches(self):
        r = score_task("F3K", "C(3)", s(60, 60, 60, 60, 60))
        self.assertEqual(r.raw_s, 300)

    def test_d_ladder(self):
        # targets 30, 45, 60...; each hit scores the target
        r = score_task("F3K", "D", s(35, 20, 50, 60))
        self.assertEqual(r.flight_scores, [30, 0, 45, 60])
        self.assertEqual(r.raw_s, 135)

    def test_d1_two_flights(self):
        r = score_task("F3K", "D(1)", s(320, 100, 50))
        self.assertEqual(r.flight_scores, [300, 100, 0])

    def test_e_poker_counts_three_targets(self):
        """FAI 2025 F3K.11.5: "up to three (3) target times". Was five, which is
        the 2011 rule and what GliderScore's base task text still says.

        ⚠ The count is the only part of Poker that is right yet. This still scores
        the FLOWN times of the N longest flights, where FAI credits the ANNOUNCED
        target — see [I-50]. Fixing that needs declared targets recorded per
        flight, which lands with the timer work.
        """
        r = score_task("F3K", "E", s(60, 120, 30, 90, 45, 180))
        self.assertEqual(r.flight_scores, [0, 120, 0, 90, 0, 180])
        self.assertEqual(r.raw_s, 390)

    def test_e1_three_scores(self):
        r = score_task("F3K", "E(1)", s(60, 120, 30, 90))
        self.assertEqual(r.raw_s, 270)  # 120+90+60

    def test_f_best3_of_6(self):
        r = score_task("F3K", "F", s(100, 190, 50, 170, 60, 80, 500))
        # 7th flight ignored (max 6); best 3 of first 6 with 3:00 cap
        self.assertEqual(r.flight_scores, [100, 180, 0, 170, 0, 0, 0])
        self.assertEqual(r.raw_s, 450)

    def test_g_best5_capped(self):
        r = score_task("F3K", "G", s(130, 110, 90, 121, 80, 70))
        # 2:00 cap; best 5 count
        self.assertEqual(r.raw_s, 120 + 110 + 90 + 120 + 80)

    def test_h_targets_any_order(self):
        # targets 1/2/3/4 min; longest->largest target
        r = score_task("F3K", "H", s(250, 65, 200, 130, 30))
        # 250->240, 200->180, 130->120, 65->60; 30 unused
        self.assertEqual(r.flight_scores, [240, 60, 180, 120, 0])
        self.assertEqual(r.raw_s, 600)

    def test_h_short_flights_not_wasted(self):
        r = score_task("F3K", "H", s(50, 40))
        # 50->240? min(50,240)=50, 40->180 => 40. Assignment maximises sum.
        self.assertEqual(r.raw_s, 90)

    def test_i_best3_320_cap(self):
        r = score_task("F3K", "I", s(210, 190, 205, 100))
        self.assertEqual(r.raw_s, 200 + 190 + 200)

    def test_j_last3(self):
        r = score_task("F3K", "J", s(200, 100, 110, 120))
        self.assertEqual(r.flight_scores, [0, 100, 110, 120])

    def test_k_big_ladder_advances_only_when_reached(self):
        """Rungs 1:00 1:30 2:00 2:30 3:00, and the rung rises ONLY on success — a
        missed rung is re-flown against the same time. Kris ruled on this 2026-08-02:
        Ladder and Big Ladder run the same way.

        The second flight here (85 s against the 90 s rung) is the whole difference:
        as a sequence it scored 85 and moved on; as a ladder it scores 0 and the
        pilot flies 1:30 again."""
        r = score_task("F3K", "K", s(75, 85, 130, 160, 200))
        self.assertEqual(r.flight_scores, [60, 0, 90, 120, 150])
        self.assertEqual(r.raw_s, 420)

    def test_k_big_ladder_stops_after_the_last_rung(self):
        """Five rungs and no more: a sixth flight scores nothing even if it is long."""
        r = score_task("F3K", "K", s(65, 95, 125, 155, 185, 300))
        self.assertEqual(r.flight_scores, [60, 90, 120, 150, 180, 0])

    def test_l_one_flight(self):
        r = score_task("F3K", "L", s(605, 300))
        self.assertEqual(r.flight_scores, [599, 0])

    def test_m_huge_ladder(self):
        """Rungs 3:00 / 5:00 / 7:00, advancing only when reached. The 400 s flight
        misses the 420 s rung and scores nothing — as a sequence it scored 400."""
        r = score_task("F3K", "M", s(190, 320, 400))
        self.assertEqual(r.flight_scores, [180, 300, 0])

    def test_n_best_flight(self):
        r = score_task("F3K", "N", s(300, 550, 200))
        self.assertEqual(r.flight_scores, [0, 550, 0])

    def test_u10_all_count(self):
        r = score_task("F3K", "U10", s(100, 200, 300))
        self.assertEqual(r.raw_s, 600)

    def test_truncation_not_rounding(self):
        r = score_task("F3K", "A", [59_990])  # 59.99s -> 59.9 at 1 decimal
        self.assertEqual(r.raw_s, 59.9)
        r = score_task("F3K", "A", [59_990], time_decimals=0)
        self.assertEqual(r.raw_s, 59.0)

    def test_no_flights(self):
        r = score_task("F3K", "A", [])
        self.assertEqual(r.raw_s, 0)
        self.assertEqual(r.flight_scores, [])

    def test_unknown_variant_falls_back(self):
        r = score_task("F3K", "A(9)", s(340))
        self.assertEqual(r.raw_s, 300)


class TestF5KTasks(unittest.TestCase):
    def test_a_four_targets(self):
        r = score_task("F5K", "A", s(250, 65, 200, 130))
        self.assertEqual(r.raw_s, 240 + 180 + 120 + 60)

    def test_a_max_four_flights(self):
        r = score_task("F5K", "A", s(10, 10, 10, 10, 300))
        self.assertEqual(r.flight_scores[4], 0)  # 5th launch ignored

    def test_b_last_of_three(self):
        r = score_task("F5K", "B", s(100, 200, 310, 290))
        # only first 3 launches allowed; last of those counts, 5:00 cap
        self.assertEqual(r.flight_scores, [0, 0, 300, 0])

    def test_c_all_up_4min(self):
        r = score_task("F5K", "C", s(250, 100))
        self.assertEqual(r.flight_scores, [240, 100])

    def test_d_334(self):
        r = score_task("F5K", "D", s(200, 250, 190))
        # targets 180,180,240: 250->240, 200->180, 190->180
        self.assertEqual(r.raw_s, 600)

    def test_e_poker3(self):
        r = score_task("F5K", "E", s(100, 200, 150))
        self.assertEqual(r.raw_s, 450)


class TestPokerDeclaredTargets(unittest.TestCase):
    """Poker credits the ANNOUNCED target, never the flown time. [I-50]

    FAI SC4 Vol. F3 F3K.11.5: "If the target is reached or exceeded, then the
    target time is credited."
    """

    def test_the_rulebooks_own_worked_example(self):
        """Straight from F3K.11.5. Announced / flown / scored:

            45 s   46 s   45
            50 s   48 s    0
                   52 s   50
            47 s   49 s   47      total 142
        """
        r = score_task("F3K", "E", s(46, 48, 52, 49),
                       targets_s=[45, 50, 50, 47])
        self.assertEqual(r.flight_scores, [45, 0, 50, 47])
        self.assertEqual(r.raw_s, 142)

    def test_overflying_the_call_credits_only_the_call(self):
        """The whole point of the task: 65 s flown against 60 s called is 60."""
        r = score_task("F3K", "E", s(65), targets_s=[60])
        self.assertEqual(r.raw_s, 60)

    def test_missing_the_call_scores_zero_not_the_flight(self):
        r = score_task("F3K", "E", s(57), targets_s=[60])
        self.assertEqual(r.raw_s, 0)

    def test_only_three_targets_score(self):
        """FAI 2025: up to three target times."""
        r = score_task("F3K", "E", s(30, 40, 50, 60), targets_s=[30, 40, 50, 60])
        self.assertEqual(r.flight_scores, [30, 40, 50, 0])
        self.assertEqual(r.raw_s, 120)

    def test_a_flight_with_no_call_scores_nothing(self):
        """You cannot score a target you never announced -- but the others still do."""
        r = score_task("F3K", "E", s(90, 90), targets_s=[0, 60])
        self.assertEqual(r.flight_scores, [0, 60])

    def test_heats_with_no_declarations_fall_back(self):
        """⚠ A wrong score, kept deliberately: rounds flown before the timer
        recorded targets, and CD-entered heats, would otherwise blank entirely.
        It cannot fire once ANY flight in the heat carries a call."""
        r = score_task("F3K", "E", s(60, 120, 30, 90))
        self.assertEqual(r.raw_s, 270)          # 120+90+60, the old N-longest rule

    def test_one_declaration_switches_the_whole_heat_to_the_real_rule(self):
        """The fallback is all-or-nothing on purpose — a heat that is half
        declared is a heat where the timer was working, so score it properly."""
        r = score_task("F3K", "E", s(200, 60), targets_s=[0, 60])
        self.assertEqual(r.flight_scores, [0, 60],
                         "the 200 s flight scores nothing: no call was made")


class TestTargetMode(unittest.TestCase):
    """What the timer is told to run. [TF-10]/[TF-11]

    The timer used to get only `wt` and `disc`, so it could not know it was flying
    Poker and had no way to offer a target. The base now names the mode; the rules
    stay here, beside the scoring that has to agree with them.
    """

    def test_poker_carries_its_target_count(self):
        m = scoring.target_mode("F3K", "E")
        self.assertEqual(m["mode"], "poker")
        self.assertEqual(m["targets"], 3)          # FAI 2025

    def test_ladder_carries_start_and_step(self):
        m = scoring.target_mode("F3K", "D")
        self.assertEqual(m, {"mode": "ladder", "start": 30, "step": 15})

    def test_variants_resolve(self):
        self.assertEqual(scoring.target_mode("F3K", "E(1)")["mode"], "poker")

    def test_f5k_poker_too(self):
        self.assertEqual(scoring.target_mode("F5K", "E")["mode"], "poker")

    def test_ordinary_tasks_are_plain(self):
        for task in ("A", "B", "C", "F", "G", "L", "N"):
            with self.subTest(task=task):
                self.assertEqual(scoring.target_mode("F3K", task), {"mode": "plain"})

    def test_big_and_huge_ladders_are_ladders(self):
        """Kris, 2026-08-02: "Ladder and Big ladder run the same way." The rung rises
        only when it is reached, so K and M are ladders — they were scored as
        sequences (advance every launch regardless) until that ruling. Sent as an
        explicit rung list, because they do not climb by a fixed step.

        ⚠ `rungs`, not `targets`: `targets` already carries Poker's target COUNT."""
        self.assertEqual(scoring.target_mode("F3K", "K"),
                         {"mode": "ladder", "rungs": "60,90,120,150,180"})
        self.assertEqual(scoring.target_mode("F3K", "M"),
                         {"mode": "ladder", "rungs": "180,300,420"})

    def test_matched_target_tasks_are_still_plain(self):
        """H matches flights to targets in any order, so there is no rung to show a
        pilot before a throw and nothing for the timer to count down to."""
        self.assertEqual(scoring.target_mode("F3K", "H"), {"mode": "plain"})

    def test_an_unknown_task_is_plain_not_a_guess(self):
        """A timer that guessed `poker` would demand a target that does not exist
        and block the round."""
        self.assertEqual(scoring.target_mode("F3K", "ZZZ"), {"mode": "plain"})
        self.assertEqual(scoring.target_mode("NOPE", "E"), {"mode": "plain"})


class TestF5KBonus(unittest.TestCase):
    def test_at_reference(self):
        self.assertEqual(f5k_bonus(60, 60), 0.0)

    def test_below_reference(self):
        self.assertEqual(f5k_bonus(49, 60), 5.5)   # 11m below -> +0.5/m
        self.assertEqual(f5k_bonus(59, 60), 0.5)

    def test_1_to_10_above(self):
        self.assertEqual(f5k_bonus(69, 60), -9.0)  # NZ Nats ground truth
        self.assertEqual(f5k_bonus(61, 60), -1.0)
        self.assertEqual(f5k_bonus(70, 60), -10.0)

    def test_11_plus_above(self):
        self.assertEqual(f5k_bonus(71, 60), -13.0)   # -10 + -3
        self.assertEqual(f5k_bonus(75, 60), -25.0)   # -10 + 5*-3


class TestNormalise(unittest.TestCase):
    def test_basic(self):
        n = normalise({1: 300.0, 2: 150.0, 3: 200.0})
        self.assertEqual(n[1], 1000.0)
        self.assertEqual(n[2], 500.0)
        self.assertEqual(n[3], 666.6)  # truncated, not rounded (666.66...)

    def test_all_zero(self):
        self.assertEqual(normalise({1: 0.0, 2: 0.0}), {1: 0.0, 2: 0.0})

    def test_tie_both_1000(self):
        n = normalise({1: 240.0, 2: 240.0, 3: 120.0})
        self.assertEqual(n[1], 1000.0)
        self.assertEqual(n[2], 1000.0)
        self.assertEqual(n[3], 500.0)


class TestStandings(unittest.TestCase):
    def test_totals_and_rank(self):
        rows = standings({
            1: {10: 1000.0, 11: 800.0},
            2: {10: 700.0, 11: 1000.0},
        })
        by_pid = {r["pilot_id"]: r for r in rows}
        self.assertEqual(by_pid[11]["total"], 1800.0)
        self.assertEqual(by_pid[11]["rank"], 1)
        self.assertEqual(by_pid[10]["rank"], 2)

    def test_drop_lowest_after_threshold(self):
        scores = {1: {10: 500.0}, 2: {10: 1000.0}, 3: {10: 800.0}}
        rows = standings(scores, drop_at=[3, 99, 99])
        row = rows[0]
        self.assertTrue(row["rounds"][1]["dropped"])
        self.assertEqual(row["total"], 1800.0)

    def test_drop_not_yet_active(self):
        scores = {1: {10: 500.0}, 2: {10: 1000.0}}
        rows = standings(scores, drop_at=[3, 99, 99])
        self.assertEqual(rows[0]["total"], 1500.0)
        self.assertFalse(any(r["dropped"] for r in rows[0]["rounds"].values()))

    def test_missing_round_scores_zero(self):
        rows = standings({1: {10: 1000.0, 11: 900.0}, 2: {10: 1000.0}})
        by_pid = {r["pilot_id"]: r for r in rows}
        self.assertEqual(by_pid[11]["rounds"][2]["score"], 0.0)
        self.assertEqual(by_pid[11]["total"], 900.0)

    def test_fai_tiebreak_last_round(self):
        rows = standings({
            1: {10: 1000.0, 11: 900.0},
            2: {10: 900.0, 11: 1000.0},
        })
        # equal totals; pilot 11 higher in last round -> rank 1
        self.assertEqual(rows[0]["pilot_id"], 11)
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[1]["rank"], 2)

    def test_exact_tie_same_rank(self):
        rows = standings({1: {10: 1000.0, 11: 1000.0, 12: 500.0}})
        ranks = {r["pilot_id"]: r["rank"] for r in rows}
        self.assertEqual(ranks[10], 1)
        self.assertEqual(ranks[11], 1)
        self.assertEqual(ranks[12], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
