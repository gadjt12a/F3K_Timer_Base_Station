"""Unit tests for draw.py (Phase D1)."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from base_station.frontend.draw import draw_round  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
