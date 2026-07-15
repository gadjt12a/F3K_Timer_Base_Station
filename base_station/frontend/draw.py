"""F3K Base Station — group draw (SCORING_ENGINE_PROJECT.md Phase D1).

Round 1: random draw. Round 2+: reverse-standings snake seeding so the
leaders are separated across groups (standard FAI F3K practice: seed from
the bottom of the standings, top pilots land in different groups).
"""

from __future__ import annotations

import random
from typing import Sequence


def draw_round(pilot_ids: Sequence[int], groups: int,
               standings_order: Sequence[int] | None = None,
               rng: random.Random | None = None) -> dict:
    """Return {pilot_id: group_no} (group_no starting at 1).

    standings_order: pilot ids best-first (from scoring.competition_standings).
    Pilots not present in standings_order are treated as unranked and seeded
    last. When standings_order is None/empty the draw is random (round 1).
    Group sizes differ by at most one; the earlier groups get the extras.
    """
    if groups < 1:
        raise ValueError("groups must be >= 1")
    pilots = list(pilot_ids)

    if standings_order:
        pos = {pid: i for i, pid in enumerate(standings_order)}
        # Best pilot first, unranked pilots at the end
        seeded = sorted(pilots, key=lambda pid: pos.get(pid, len(pos)))
    else:
        seeded = pilots[:]
        (rng or random).shuffle(seeded)

    # Snake through groups so consecutive seeds land in different groups:
    # seeds 1..G go to groups 1..G, seeds G+1..2G go to groups G..1, etc.
    assignment: dict = {}
    direction = 1
    g = 0
    for pid in seeded:
        assignment[pid] = g + 1
        if direction == 1 and g == groups - 1:
            direction = -1
        elif direction == -1 and g == 0:
            direction = 1
        else:
            g += direction
    return assignment
