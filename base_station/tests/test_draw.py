"""Unit tests for draw.py (Phase D1)."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from base_station.frontend.draw import draw_competition, draw_round, draw_stats  # noqa: E402


class TestDraw(unittest.TestCase):
    def test_seeded_snake_separates_leaders(self):
        # standings best-first: 1..9, 3 groups -> snake 1,2,3,3,2,1,1,2,3
        assignment = draw_round(range(1, 10), 3, standings_order=list(range(1, 10)))
        self.assertEqual([assignment[p] for p in range(1, 10)],
                         [1, 2, 3, 3, 2, 1, 1, 2, 3])
        # top 3 seeds all in different groups
        self.assertEqual({assignment[1], assignment[2], assignment[3]}, {1, 2, 3})

    def test_balanced_sizes(self):
        assignment = draw_round(range(1, 8), 3, standings_order=list(range(1, 8)))
        sizes = [list(assignment.values()).count(g) for g in (1, 2, 3)]
        self.assertLessEqual(max(sizes) - min(sizes), 1)

    def test_random_round1_covers_all_groups(self):
        assignment = draw_round(range(1, 9), 4, rng=random.Random(42))
        self.assertEqual(set(assignment.values()), {1, 2, 3, 4})
        sizes = [list(assignment.values()).count(g) for g in (1, 2, 3, 4)]
        self.assertEqual(sizes, [2, 2, 2, 2])

    def test_unranked_pilots_seed_last(self):
        # pilot 99 has no standing -> seeded after ranked pilots
        assignment = draw_round([1, 2, 99], 3, standings_order=[1, 2])
        self.assertEqual(assignment, {1: 1, 2: 2, 99: 3})

    def test_single_group(self):
        assignment = draw_round([1, 2, 3], 1, standings_order=[3, 2, 1])
        self.assertEqual(set(assignment.values()), {1})


class TestDrawCompetition(unittest.TestCase):
    def test_balanced_and_complete(self):
        res = draw_competition(range(1, 11), 4, 2, rng=random.Random(1))
        self.assertEqual(len(res["rounds"]), 4)
        for rnd in res["rounds"]:
            self.assertEqual(len(rnd), 2)
            flat = [p for g in rnd for p in g]
            self.assertEqual(sorted(flat), list(range(1, 11)))  # everyone flies once
            sizes = [len(g) for g in rnd]
            self.assertLessEqual(max(sizes) - min(sizes), 1)

    def test_full_pair_coverage_when_feasible(self):
        # 8 pilots, 2 groups of 4: each round meets 12 of 28 pairs; 5 rounds is
        # comfortably enough for the optimiser to cover all 28.
        res = draw_competition(range(1, 9), 5, 2, rng=random.Random(7))
        self.assertEqual(res["stats"]["pairs_unmet"], 0)
        self.assertEqual(res["stats"]["coverage_pct"], 100.0)

    def test_avoid_back_to_back(self):
        res = draw_competition(range(1, 13), 6, 3, avoid_back_to_back=True,
                               rng=random.Random(3))
        self.assertEqual(res["stats"]["back_to_back"], 0)

    def test_history_seeds_pair_matrix(self):
        # Rounds already flown: pilots 1-4 and 5-8 have met within their halves.
        history = [[[1, 2, 3, 4], [5, 6, 7, 8]]]
        res = draw_competition(range(1, 9), 1, 2, history=history,
                               rng=random.Random(2))
        # The one new round should maximise cross-half meetings: each group
        # should mix the halves rather than repeat them.
        for grp in res["rounds"][0]:
            halves = {p <= 4 for p in grp}
            self.assertEqual(halves, {True, False})
        # Stats include the history rounds in coverage
        self.assertGreater(res["stats"]["pairs_met"], 12)

    def test_timekeeper_feasibility(self):
        ok = draw_competition(range(1, 9), 1, 2, rng=random.Random(1))
        self.assertTrue(ok["stats"]["timers_ok"])       # 4 fly, 4 free to time
        single = draw_competition(range(1, 9), 1, 1, rng=random.Random(1))
        self.assertFalse(single["stats"]["timers_ok"])  # everyone flies at once

    def test_stats_standalone(self):
        stats = draw_stats([1, 2, 3, 4], [[[1, 2], [3, 4]]])
        self.assertEqual(stats["total_pairs"], 6)
        self.assertEqual(stats["pairs_met"], 2)
        self.assertEqual(stats["back_to_back"], 0)

    def test_pilot_pullout_redraw(self):
        # Pilot 9 pulled out; redraw ignores them even if present in history.
        history = [[[1, 2, 9], [3, 4, 5], [6, 7, 8]]]
        res = draw_competition(range(1, 9), 2, 2, history=history,
                               prev_last_group=[6, 7, 8], rng=random.Random(4))
        flat = [p for rnd in res["rounds"] for g in rnd for p in g]
        self.assertNotIn(9, flat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
