"""F3K Base Station — group draw.

Two tools:
- draw_round(): single-round seeded draw (SCORING_ENGINE_PROJECT.md Phase D1) —
  random for round 1, reverse-standings snake seeding for later rounds.
- draw_competition(): multi-round roster draw for the Rounds page Draw Wizard —
  randomised balanced groups optimised so every pilot meets every other pilot
  at least once, with optional avoidance of back-to-back flights (a pilot in
  the last group of one round and the first group of the next). Mirrors the
  options GliderScore encodes in f3kPilotsMin (Back2Back, GrpsInRnd, NbrRnds)
  and its DrawFreq pair-frequency weighting.
"""

from __future__ import annotations

import itertools
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

# ---------------------------------------------------------------------------
# Multi-round roster draw (Draw Wizard)
# ---------------------------------------------------------------------------

# Score weights: meeting a never-met pilot dominates; repeat meetings are
# mildly discouraged (GliderScore's DrawFreq idea); back-to-back violations
# sit between the two so they are avoided unless it costs pair coverage.
_W_NEW_PAIR = 100
_W_REPEAT = 1
_W_BACK_TO_BACK = 60


def _partition(pilots: list, groups: int) -> list:
    """Split an ordered pilot list into `groups` chunks, sizes differing by
    at most one (extras in the earlier groups)."""
    n = len(pilots)
    base, extra = divmod(n, groups)
    out, i = [], 0
    for g in range(groups):
        size = base + (1 if g < extra else 0)
        out.append(pilots[i:i + size])
        i += size
    return out


def _round_score(cand: list, meet: dict, prev_last: set, avoid_b2b: bool) -> int:
    score = 0
    for grp in cand:
        for a, b in itertools.combinations(grp, 2):
            k = (a, b) if a < b else (b, a)
            met = meet.get(k, 0)
            score += _W_NEW_PAIR if met == 0 else -_W_REPEAT * met
    if avoid_b2b and prev_last:
        score -= _W_BACK_TO_BACK * sum(1 for p in cand[0] if p in prev_last)
    return score


def draw_competition(pilot_ids: Sequence[int], num_rounds: int, groups: int, *,
                     avoid_back_to_back: bool = True,
                     history: Sequence[Sequence[Sequence[int]]] = (),
                     prev_last_group: Sequence[int] = (),
                     rng: random.Random | None = None,
                     candidates: int = 80, swap_iters: int = 300,
                     restarts: int = 3) -> dict:
    """Draw `num_rounds` rounds of `groups` groups for the given pilots.

    history: group lists of already-flown rounds (mid-competition redraw) —
    seeds the pair-meeting matrix so the new rounds prioritise pairs that have
    not met yet. prev_last_group: the final group of the round flown
    immediately before the new rounds (for the back-to-back check across the
    redraw boundary).

    Returns {"rounds": [[group pilot-id lists]], "stats": {...}}. Stats include
    timekeeper feasibility: every flying pilot can have a pilot-timekeeper only
    if the pilots NOT flying in a group are at least as many as those flying.
    """
    if groups < 1 or num_rounds < 1:
        raise ValueError("groups and num_rounds must be >= 1")
    pilots = list(pilot_ids)
    if len(pilots) < groups:
        raise ValueError("more groups than pilots")
    rng = rng or random.Random()

    def seed_meet() -> dict:
        meet: dict = {}
        for rnd in history:
            for grp in rnd:
                for a, b in itertools.combinations(grp, 2):
                    if a in pilots and b in pilots:
                        k = (a, b) if a < b else (b, a)
                        meet[k] = meet.get(k, 0) + 1
        return meet

    best_plan = None
    best_total = None
    for _ in range(restarts):
        meet = seed_meet()
        prev_last = set(prev_last_group) if avoid_back_to_back else set()
        plan = []
        total = 0
        for _r in range(num_rounds):
            # Best of K random balanced partitions...
            best_cand, best_score = None, None
            for _c in range(candidates):
                order = pilots[:]
                rng.shuffle(order)
                cand = _partition(order, groups)
                s = _round_score(cand, meet, prev_last, avoid_back_to_back)
                if best_score is None or s > best_score:
                    best_cand, best_score = cand, s
            # ...then greedy cross-group swap improvement
            cand = [list(g) for g in best_cand]
            for _i in range(swap_iters):
                g1, g2 = rng.randrange(groups), rng.randrange(groups)
                if g1 == g2 or not cand[g1] or not cand[g2]:
                    continue
                i1, i2 = rng.randrange(len(cand[g1])), rng.randrange(len(cand[g2]))
                cand[g1][i1], cand[g2][i2] = cand[g2][i2], cand[g1][i1]
                s = _round_score(cand, meet, prev_last, avoid_back_to_back)
                if s > best_score:
                    best_score = s
                else:
                    cand[g1][i1], cand[g2][i2] = cand[g2][i2], cand[g1][i1]
            # Group order within a round is arbitrary — put the group with the
            # fewest previous-last-group members first (free b2b reduction).
            if avoid_back_to_back and prev_last and groups > 1:
                g_min = min(range(groups),
                            key=lambda g: sum(1 for p in cand[g] if p in prev_last))
                if g_min != 0:
                    cand[0], cand[g_min] = cand[g_min], cand[0]
                    best_score = _round_score(cand, meet, prev_last, True)
            # Repair pass (3+ groups): back-to-back avoidance is effectively
            # hard — swap each remaining violator out of the first group for
            # the best-scoring non-violating pilot from another group, even at
            # a score cost. With 2 equal groups a zero-b2b draw would freeze
            # the group composition forever, so there it stays a soft penalty.
            if avoid_back_to_back and prev_last and groups > 2:
                for i0 in range(len(cand[0])):
                    if cand[0][i0] not in prev_last:
                        continue
                    best_swap, best_s = None, None
                    for g2 in range(1, groups):
                        for i2 in range(len(cand[g2])):
                            if cand[g2][i2] in prev_last:
                                continue
                            cand[0][i0], cand[g2][i2] = cand[g2][i2], cand[0][i0]
                            s = _round_score(cand, meet, prev_last, True)
                            if best_s is None or s > best_s:
                                best_swap, best_s = (g2, i2), s
                            cand[0][i0], cand[g2][i2] = cand[g2][i2], cand[0][i0]
                    if best_swap is not None:
                        g2, i2 = best_swap
                        cand[0][i0], cand[g2][i2] = cand[g2][i2], cand[0][i0]
                        best_score = best_s
            plan.append(cand)
            total += best_score
            for grp in cand:
                for a, b in itertools.combinations(grp, 2):
                    k = (a, b) if a < b else (b, a)
                    meet[k] = meet.get(k, 0) + 1
            prev_last = set(cand[-1]) if avoid_back_to_back else set()
        if best_total is None or total > best_total:
            best_plan, best_total = plan, total

    return {"rounds": best_plan,
            "stats": draw_stats(pilots, best_plan, history=history,
                                prev_last_group=prev_last_group)}


def draw_stats(pilots: Sequence[int], plan: Sequence, *,
               history: Sequence = (), prev_last_group: Sequence[int] = ()) -> dict:
    """Coverage / back-to-back / timekeeper stats for a draw plan."""
    pilots = list(pilots)
    meet: dict = {}
    for rnd in list(history) + list(plan):
        for grp in rnd:
            for a, b in itertools.combinations(grp, 2):
                if a in pilots and b in pilots:
                    k = (a, b) if a < b else (b, a)
                    meet[k] = meet.get(k, 0) + 1
    total_pairs = len(pilots) * (len(pilots) - 1) // 2
    met = sum(1 for v in meet.values() if v > 0)

    b2b = 0
    prev_last = set(prev_last_group)
    for rnd in plan:
        b2b += sum(1 for p in rnd[0] if p in prev_last)
        prev_last = set(rnd[-1])

    max_group = max((len(g) for rnd in plan for g in rnd), default=0)
    # Pilot timekeepers come from the groups not flying: need as many
    # non-flying pilots as flying ones for 1 timer per pilot.
    timers_ok = len(pilots) - max_group >= max_group and len(plan[0]) > 1 if plan else False
    return {
        "pilots": len(pilots),
        "total_pairs": total_pairs,
        "pairs_met": met,
        "pairs_unmet": total_pairs - met,
        "coverage_pct": round(met / total_pairs * 100, 1) if total_pairs else 100.0,
        "max_meetings": max(meet.values(), default=0),
        "back_to_back": b2b,
        "max_group_size": max_group,
        "timers_ok": timers_ok,
    }
