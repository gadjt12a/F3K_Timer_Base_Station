"""F3K Base Station — self-contained scoring engine (Phase A of SCORING_ENGINE_PROJECT.md).

Pure-Python task scoring, F5K altitude bonus, group normalisation and cumulative
standings with drop scores. Rules verified against GliderScore 6.78
(Knowledge_Base/GLIDERSCORE_FULL_REFERENCE.md sections 5-7).

Disciplines are registered in DISCIPLINE_RULES — adding F3J/F5J/F3B later means
adding a rule table (or a custom scorer via register_discipline), no schema changes.

Layering: everything above the "DB glue" marker is pure and unit-tested
(base_station/tests/test_scoring.py); the glue functions at the bottom read the
SQLite schema from db.py and are exercised through the web UI.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

# ---------------------------------------------------------------------------
# Task rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """Declarative description of which flights count for a task.

    kind:
      last_n    — last n flights count, each capped at cap_s
      best_n    — n longest flights count (after capping), each capped at cap_s
      first_n   — first n flights count (all-up / launch-limited tasks)
      ladder    — chronological; flight >= target scores the target, then the target
                  advances. Rungs come from start_s/step_s (D, unbounded) or from an
                  explicit targets_s list (K "Big Ladder", M "Huge Ladder"). ⚠ The
                  target advances ONLY on success — a missed rung is re-flown against
                  the same time. Kris, 2026-08-02: Ladder and Big Ladder run the same
                  way. This is the whole difference between `ladder` and `sequence`.
      targets   — len(targets_s) flights matched to targets (any order), scored min(flight, target)
      sequence  — i-th flight capped at targets_s[i]; the target advances every launch
                  whether or not it was reached. No FAI task uses this any more — it is
                  kept for custom tasks, which offer it as "Fixed targets, in order".
      poker     — n longest flights count, no cap (declared targets are not recorded;
                  the achieved/CD-entered times are taken as the scored times)
      all       — every flight counts, no cap
    max_flights — launches allowed in the window (0 = unlimited); extra recorded
                  flights beyond this are ignored chronologically.
    """
    kind: str
    n: int = 0
    cap_s: float = 0.0
    targets_s: tuple = ()
    start_s: float = 0.0
    step_s: float = 0.0
    max_flights: int = 0


# Keyed by (letter, variant); variant None = base task. Lookup falls back to
# (letter, None) so unknown variants score like the base task.
F3K_RULES: dict[tuple, Rule] = {
    ("A", None): Rule("last_n", n=1, cap_s=300),            # A(1)/A(2) differ only in window
    ("B", None): Rule("last_n", n=2, cap_s=240),
    ("B", 2):    Rule("last_n", n=2, cap_s=180),
    ("C", None): Rule("first_n", n=3, cap_s=180),           # all-up, launches all count
    ("C", 2):    Rule("first_n", n=4, cap_s=180),
    ("C", 3):    Rule("first_n", n=5, cap_s=180),
    ("D", None): Rule("ladder", start_s=30, step_s=15),
    ("D", 1):    Rule("first_n", n=2, cap_s=300),           # D(1): two flights, 5:00 max
    # Three targets, per FAI 2025 (F3K.11.5): "an unlimited number of flights to
    # achieve or exceed up to three (3) target times". Was 5, which is the 2011
    # rule and what GliderScore's base task text still says. Kris's call
    # 2026-08-01: follow FAI 2025. The E(1)/E(2) variants already said 3.
    #
    # ⚠ `n` is the only part of Poker that is right yet — the rule still scores the
    # FLOWN time of the N longest flights, where FAI credits the ANNOUNCED target.
    # See [I-50]; the fix needs declared targets recorded per flight, which lands
    # with the timer work.
    ("E", None): Rule("poker", n=3),
    ("E", 1):    Rule("poker", n=3),
    ("E", 2):    Rule("poker", n=3),
    ("F", None): Rule("best_n", n=3, cap_s=180, max_flights=6),
    ("G", None): Rule("best_n", n=5, cap_s=120),
    ("H", None): Rule("targets", targets_s=(60, 120, 180, 240)),
    ("I", None): Rule("best_n", n=3, cap_s=200),
    ("J", None): Rule("last_n", n=3, cap_s=180),
    # ⚠ K and M are LADDERS, not sequences: the rung rises only when it is reached,
    # and a missed rung is re-flown against the same time. They were scored as
    # sequences (advance every launch regardless) until Kris ruled on it 2026-08-02:
    # "Ladder and Big ladder run the same way." Raised for the rules moderators as
    # R-19b, because the two readings give different scores for the same flights.
    ("K", None): Rule("ladder", targets_s=(60, 90, 120, 150, 180)),
    ("L", None): Rule("first_n", n=1, cap_s=599),
    ("M", None): Rule("ladder", targets_s=(180, 300, 420)),   # Huge Ladder — see K
    ("N", None): Rule("best_n", n=1, cap_s=600),
    ("U10", None): Rule("all"),
    ("U15", None): Rule("all"),
}

# F5K reuses letters with different tasks (GliderScore class F5K2024).
F5K_RULES: dict[tuple, Rule] = {
    ("A", None): Rule("targets", targets_s=(60, 120, 180, 240), max_flights=4),
    ("B", None): Rule("last_n", n=1, cap_s=300, max_flights=3),
    ("C", None): Rule("first_n", n=3, cap_s=240),
    ("D", None): Rule("targets", targets_s=(180, 180, 240), max_flights=3),
    ("E", None): Rule("poker", n=3, max_flights=3),
}

DISCIPLINE_RULES: dict[str, dict[tuple, Rule]] = {
    "F3K": F3K_RULES,
    "F5K": F5K_RULES,
}

# Future disciplines whose scoring is not a flight-time rule table (e.g. F3B
# distance = laps) can register a callable instead: (task, variant, flights_ms,
# time_decimals) -> TaskResult.
CUSTOM_SCORERS: dict[str, Callable] = {}


def register_discipline(code: str, rules: dict[tuple, Rule] | None = None,
                        scorer: Callable | None = None) -> None:
    if rules is not None:
        DISCIPLINE_RULES[code] = rules
    if scorer is not None:
        CUSTOM_SCORERS[code] = scorer


# User-defined tasks (custom_tasks table) registered at runtime; tracked so a
# reload can clear stale entries after an edit/delete.
_CUSTOM_TASK_KEYS: set = set()


def load_custom_rules(db) -> None:
    """(Re)register user-defined tasks from the custom_tasks table."""
    for disc, code in _CUSTOM_TASK_KEYS:
        DISCIPLINE_RULES.get(disc, {}).pop((code, None), None)
    _CUSTOM_TASK_KEYS.clear()
    for r in db.execute("SELECT * FROM custom_tasks").fetchall():
        code = r["code"].upper()
        DISCIPLINE_RULES.setdefault(r["discipline"], {})[(code, None)] = Rule(
            kind=r["kind"], n=r["n"] or 0, cap_s=r["cap_s"] or 0,
            targets_s=tuple(json.loads(r["targets"] or "[]")),
            start_s=r["start_s"] or 0, step_s=r["step_s"] or 0,
            max_flights=r["max_flights"] or 0)
        _CUSTOM_TASK_KEYS.add((r["discipline"], code))


_TASK_RE = re.compile(r"^\s*([A-Z]+[0-9]*)\s*(?:\((\d+)\))?\s*$", re.IGNORECASE)


def parse_task(task: str) -> tuple[str, int | None]:
    """'A(1)' -> ('A', 1); 'C' -> ('C', None)."""
    m = _TASK_RE.match(task or "")
    if not m:
        return (task or "").strip().upper(), None
    return m.group(1).upper(), int(m.group(2)) if m.group(2) else None


def target_mode(discipline: str, task: str) -> dict:
    """How the TIMER should run this task: `plain`, `poker` or `ladder`.

    The timer used to be told only `wt` and `disc`, so it could not know it was
    flying Poker and had no way to offer a target at all. Rather than teach it the
    task catalogue, the base sends the mode and its parameters and the timer just
    executes them — which keeps the rules here, next to the scoring that has to
    agree with them, and means a custom task works without a firmware change.

    ⚠ Anything unrecognised is `plain`. A timer that guessed `poker` for a task
    that is not would demand a target that does not exist and block the round.
    """
    letter, variant = parse_task(task)
    rules = DISCIPLINE_RULES.get(discipline) or {}
    rule = rules.get((letter, variant)) or rules.get((letter, None))
    if rule is None:
        return {"mode": "plain"}
    if rule.kind == "poker":
        # `targets` is the count that can be scored; the timer only needs it to
        # stop offering a picker once they are all achieved.
        return {"mode": "poker", "targets": rule.n}
    if rule.kind == "ladder":
        if rule.targets_s:
            # Big/Huge Ladder: explicit rungs, so the timer walks a list instead of
            # adding a step. ⚠ Sent as `rungs=`, NOT `targets=` — `targets=` already
            # means "how many targets Poker can score", and one key cannot be a count
            # in one mode and a list in another.
            return {"mode": "ladder",
                    "rungs": ",".join(str(int(t)) for t in rule.targets_s)}
        return {"mode": "ladder", "start": rule.start_s, "step": rule.step_s}
    # ⚠ `sequence` advances its target every launch whether or not it was reached, so
    # it is not a ladder and the timer has no mode for it. Custom tasks can still be
    # built on it, and they run `plain` — the timer times them, the CD scores them.
    return {"mode": "plain"}


def _trunc(value: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(value * f) / f


@dataclass
class TaskResult:
    """Per-flight scored seconds aligned with the input flight list.

    flight_scores[i] is what flight i contributes (0.0 if it does not count).
    raw_s is the task raw score (sum of flight_scores).
    """
    flight_scores: list = field(default_factory=list)
    raw_s: float = 0.0


def score_task(discipline: str, task: str, flights_ms: Sequence[int],
               time_decimals: int = 1,
               targets_s: Sequence[float] | None = None) -> TaskResult:
    """Apply a task rule to flights in chronological order (ms). Times are
    truncated (not rounded) to time_decimals before capping, per GliderScore.

    ``targets_s`` is the ANNOUNCED target per flight, in seconds, aligned with
    ``flights_ms``; 0 or None where nothing was declared. Only Poker reads it, and
    only Poker can be scored without it being wrong — see the rule body. [I-50]
    """
    letter, variant = parse_task(task)
    if discipline in CUSTOM_SCORERS:
        return CUSTOM_SCORERS[discipline](task, variant, flights_ms, time_decimals)
    rules = DISCIPLINE_RULES.get(discipline)
    if rules is None:
        raise ValueError(f"Unknown discipline: {discipline}")
    rule = rules.get((letter, variant)) or rules.get((letter, None))
    if rule is None:
        # Unknown task (e.g. a deleted custom task still referenced by a
        # round): count every flight rather than crash results mid-competition.
        rule = Rule("all")

    times = [_trunc(max(ms, 0) / 1000.0, time_decimals) for ms in flights_ms]
    scores = [0.0] * len(times)
    idx = list(range(len(times)))
    if rule.max_flights and len(idx) > rule.max_flights:
        idx = idx[: rule.max_flights]

    cap = rule.cap_s or float("inf")

    if rule.kind == "last_n":
        for i in idx[-rule.n:]:
            scores[i] = min(times[i], cap)
    elif rule.kind == "first_n":
        for i in idx[: rule.n]:
            scores[i] = min(times[i], cap)
    elif rule.kind == "best_n":
        ranked = sorted(idx, key=lambda i: (-min(times[i], cap), i))
        for i in ranked[: rule.n]:
            scores[i] = min(times[i], cap)
    elif rule.kind == "poker":
        # FAI SC4 Vol. F3 F3K.11.5: "If the target is reached or exceeded, then the
        # TARGET TIME is credited." Never the flown time — the rulebook's own
        # example scores 45 s for a 46 s flight against a 45 s call. [I-50]
        #
        # Judged strictly (>=), with no tolerance: the timer's picker reaches every
        # second, so any legal call can be entered exactly. A tolerance would only
        # ever help, and since we export the CALLED time and GliderScore sums the
        # calls, this decision IS the score.
        declared = [(targets_s[i] if targets_s and i < len(targets_s) else 0) or 0
                    for i in range(len(times))]
        if any(declared):
            achieved = 0
            for i in idx:
                if declared[i] and times[i] >= declared[i] and achieved < rule.n:
                    scores[i] = float(declared[i])
                    achieved += 1
        else:
            # ⚠ Nothing was declared for any flight in this heat. That is either a
            # round flown before the timer recorded targets, or one entered by hand
            # on the Results page. Scoring it all as zero would be defensible by the
            # rules and useless in practice — it would blank historical comps and
            # any CD-entered heat — so fall back to the pre-[I-50] behaviour.
            #
            # This is a WRONG score, kept only because no-data is worse. It cannot
            # fire once a heat has any declaration.
            ranked = sorted(idx, key=lambda i: (-times[i], i))
            for i in ranked[: rule.n]:
                scores[i] = times[i]
    elif rule.kind == "all":
        for i in idx:
            scores[i] = times[i]
    elif rule.kind == "ladder":
        # Two shapes of ladder, one behaviour: the rung rises only when it is reached.
        # D climbs by a fixed step forever; K and M walk an explicit list and stop
        # scoring once it is exhausted (five rungs on K, three on M).
        rungs = list(rule.targets_s)
        target = rungs[0] if rungs else rule.start_s
        rung_i = 0
        for i in idx:
            if target is None:
                break                       # explicit ladder finished; nothing left to score
            if times[i] >= target:
                scores[i] = target
                if rungs:
                    rung_i += 1
                    target = rungs[rung_i] if rung_i < len(rungs) else None
                else:
                    target += rule.step_s
    elif rule.kind == "sequence":
        for slot, i in enumerate(idx[: len(rule.targets_s)]):
            scores[i] = min(times[i], rule.targets_s[slot])
    elif rule.kind == "targets":
        # Longest k flights matched to targets, longest flight -> largest target.
        # min(f, t) with both sequences sorted descending maximises the sum.
        k = len(rule.targets_s)
        ranked = sorted(idx, key=lambda i: (-times[i], i))[:k]
        for i, target in zip(ranked, sorted(rule.targets_s, reverse=True)):
            scores[i] = min(times[i], target)
    else:
        raise ValueError(f"Unknown rule kind: {rule.kind}")

    return TaskResult(flight_scores=scores, raw_s=round(sum(scores), time_decimals))


# ---------------------------------------------------------------------------
# F5K altitude bonus (A2)
# ---------------------------------------------------------------------------

# BP Table 2020-10 (GliderScore F5KBonusData BonusNo=1), confirmed against the
# NZ Nats 2026 database. Rows of (metres_offset_threshold, pts_per_metre):
# the rate applies from that signed offset upward until the next threshold.
BP_TABLE_2020_10: tuple = ((-1, 0.5), (0, 0.0), (1, -1.0), (11, -3.0))


def f5k_bonus(altitude_m: float, ref_height_m: float,
              table: tuple = BP_TABLE_2020_10) -> float:
    """Height bonus points (positive = bonus, negative = penalty).

    diff < 0  -> +0.5/m below reference (flat)
    1..10 above -> -1/m; 11+ above -> -1/m for first 10 then -3/m beyond.
    Caller is responsible for the min-flight-time gate (f5k_min_time_for_bonus).
    """
    diff = altitude_m - ref_height_m
    if diff == 0:
        return 0.0
    if diff < 0:
        below_rate = next((r for m, r in table if m < 0), 0.5)
        return round(-diff * below_rate, 1)
    # Above reference: integrate the stepped per-metre rates
    above = sorted(((m, r) for m, r in table if m >= 1))
    pts = 0.0
    for j, (start_m, rate) in enumerate(above):
        end_m = above[j + 1][0] - 1 if j + 1 < len(above) else float("inf")
        if diff < start_m:
            break
        metres_in_band = min(diff, end_m) - start_m + 1
        pts += metres_in_band * rate
    return round(pts, 1)


# ---------------------------------------------------------------------------
# Group normalisation (A3)
# ---------------------------------------------------------------------------

def normalise(raw_scores: dict) -> dict:
    """Best raw score in group = 1000; others floor(own/best*10000)/10.
    All-zero group -> all 0.0 (not 1000)."""
    best = max(raw_scores.values(), default=0.0)
    if best <= 0:
        return {pid: 0.0 for pid in raw_scores}
    out = {}
    for pid, raw in raw_scores.items():
        out[pid] = 1000.0 if raw == best else math.floor(raw / best * 10000) / 10
    return out


# ---------------------------------------------------------------------------
# Cumulative standings with drop scores (A4)
# ---------------------------------------------------------------------------

def active_drops(drop_at: Sequence[int], rounds_completed: int) -> int:
    return sum(1 for d in drop_at if d and d != 99 and rounds_completed >= d)


def standings(round_scores: dict, drop_at: Sequence[int] = ()) -> list:
    """round_scores: {round_no: {pilot_id: normalised_score}} for contested rounds.
    Returns rows sorted by rank: {pilot_id, total, rank, rounds: {round_no:
    {score, dropped}}}. Ties broken by highest score in the last contested
    round, then working backwards (FAI)."""
    round_nos = sorted(round_scores)
    pilots = set()
    for scores in round_scores.values():
        pilots.update(scores)

    drops = active_drops(drop_at, len(round_nos))
    rows = []
    for pid in pilots:
        per_round = {rn: float(round_scores[rn].get(pid, 0.0)) for rn in round_nos}
        # Drop the N lowest scores (earliest round first on ties)
        dropped = set()
        if drops:
            order = sorted(round_nos, key=lambda rn: (per_round[rn], rn))
            dropped = set(order[:drops])
        total = round(sum(s for rn, s in per_round.items() if rn not in dropped), 1)
        tiebreak = tuple(-per_round[rn] for rn in reversed(round_nos))
        rows.append({
            "pilot_id": pid,
            "total": total,
            "rounds": {rn: {"score": per_round[rn], "dropped": rn in dropped}
                       for rn in round_nos},
            "_key": (-total,) + tiebreak,
        })

    rows.sort(key=lambda r: r["_key"])
    rank = 0
    prev_key = None
    for pos, row in enumerate(rows, start=1):
        if row["_key"] != prev_key:
            rank = pos
            prev_key = row["_key"]
        row["rank"] = rank
        del row["_key"]
    return rows


# ---------------------------------------------------------------------------
# DB glue — reads the schema from db.py; used by app.py
# ---------------------------------------------------------------------------

def _comp_config(db, comp) -> dict:
    return {
        "drop_at": [comp["drop1_at_round"], comp["drop2_at_round"], comp["drop3_at_round"]],
        "f5k_ref_height": comp["f5k_ref_height"] if comp["f5k_ref_height"] is not None else 60,
        "f5k_min_time_for_bonus": comp["f5k_min_time_for_bonus"]
            if comp["f5k_min_time_for_bonus"] is not None else 30,
        "time_decimals": comp["time_decimals"] if comp["time_decimals"] is not None else 1,
    }


def score_group_db(db, group_id: int) -> dict:
    """Score one group: task rule + F5K bonus + normalisation.

    Returns {"pilots": {pilot_id: {name, flights: [{id, duration_ms, altitude_m,
    scored_s, bonus, fpt}], raw_s, bonus_pts, total, norm, rank}}, "discipline",
    "task", "ref_height"}.
    """
    grp = db.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    rnd = db.execute("SELECT * FROM rounds WHERE id = ?", (grp["round_id"],)).fetchone()
    comp = db.execute("SELECT * FROM competitions WHERE id = ?",
                      (rnd["competition_id"],)).fetchone()
    cfg = _comp_config(db, comp)
    discipline = rnd["discipline"]
    is_f5k = discipline == "F5K"
    ref_height = rnd["ref_height_m"] if rnd["ref_height_m"] is not None else cfg["f5k_ref_height"]

    pilot_rows = db.execute(
        """SELECT p.id, p.name FROM pilots p
           JOIN group_pilots gp ON gp.pilot_id = p.id
           WHERE gp.group_id = ? ORDER BY p.name""", (group_id,)).fetchall()

    pilots: dict = {}
    raw: dict = {}
    for p in pilot_rows:
        flights = db.execute(
            # Scratched flights ARE scored — as zero. A scratch is a land-out:
            # the launch happened, so it consumes a launch and scores nothing.
            # [I-46], which revises [I-42]'s "exclude entirely".
            #
            # ⚠ They must stay in this list, in order, because `max_flights` is a
            # limit on LAUNCHES. Filtering them out frees the slot, and on any
            # launch-limited task (F3K F = 6, F5K A = 4, B = 3, D = 3, E = 3) the
            # pilot simply launches again — scratching the weak ones until six
            # good flights remain.
            """SELECT id, duration_ms, altitude_m, altitude_source, scratched,
                      declared_target_ms, declared_window
               FROM flights
               WHERE pilot_id = ? AND group_id = ?
               ORDER BY COALESCE(flight_no, 9999), recorded_at""",
            (p["id"], group_id)).fetchall()
        result = score_task(discipline, rnd["task"],
                            [0 if f["scratched"] else (f["duration_ms"] or 0)
                             for f in flights],
                            cfg["time_decimals"],
                            # A voided launch cannot have achieved its call, so its
                            # target goes with it — otherwise a scratched flight
                            # could still credit a Poker target. [I-46]/[I-50]
                            [0 if f["scratched"]
                             else (f["declared_target_ms"] or 0) / 1000.0
                             for f in flights])
        bonus_total = 0.0
        fl_out = []
        for i, f in enumerate(flights):
            scored = result.flight_scores[i]
            bonus = None
            if (is_f5k and scored > 0 and f["altitude_m"] is not None
                    and (f["duration_ms"] or 0) / 1000.0 >= cfg["f5k_min_time_for_bonus"]):
                bonus = f5k_bonus(f["altitude_m"], ref_height)
                bonus_total += bonus
            fl_out.append({
                "id": f["id"], "duration_ms": f["duration_ms"],
                "altitude_m": f["altitude_m"], "altitude_source": f["altitude_source"],
                "scored_s": scored, "bonus": bonus,
                # Carried so the UI can show the flown time struck through next to
                # the zero it scored — the CD needs to see the launch happened.
                "scratched": bool(f["scratched"]),
                # The announced call, so Results can show what was flown FOR next
                # to what was flown — the two are different numbers in Poker and
                # the score is the former. [I-50]
                "declared_target_s": (f["declared_target_ms"] or 0) / 1000.0 or None,
                "declared_window": bool(f["declared_window"]),
                "fpt": round(scored + bonus, 1) if bonus is not None else scored,
            })
        total = round(result.raw_s + bonus_total, 1)
        pilots[p["id"]] = {
            "id": p["id"], "name": p["name"], "flights": fl_out,
            "raw_s": result.raw_s, "bonus_pts": round(bonus_total, 1), "total": total,
        }
        raw[p["id"]] = total

    norms = normalise(raw)
    ranked = sorted(pilots, key=lambda pid: -norms[pid])
    rank = 0
    prev = None
    for pos, pid in enumerate(ranked, start=1):
        if norms[pid] != prev:
            rank, prev = pos, norms[pid]
        pilots[pid]["norm"] = norms[pid]
        pilots[pid]["rank"] = rank
    return {"pilots": pilots, "discipline": discipline, "task": rnd["task"],
            "ref_height": ref_height if is_f5k else None}


def round_norm_scores(db, round_id: int) -> dict:
    """{pilot_id: normalised_score} across all groups in a round.
    Empty dict if the round has no flights yet (not contested)."""
    groups = db.execute("SELECT id FROM groups WHERE round_id = ?", (round_id,)).fetchall()
    out: dict = {}
    contested = False
    for g in groups:
        scored = score_group_db(db, g["id"])
        if any(p["flights"] for p in scored["pilots"].values()):
            contested = True
        for pid, p in scored["pilots"].items():
            out[pid] = p["norm"]
    return out if contested else {}


def competition_standings(db, comp_id: int, discipline: str | None = None) -> dict:
    """Full leaderboard for a competition: per-round normalised scores +
    cumulative standings with drops. Optionally filter rounds by discipline
    (for MIXED F3K+F5K days)."""
    comp = db.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    if comp is None:
        return {"rounds": [], "standings": [], "drops": 0}
    cfg = _comp_config(db, comp)
    q = "SELECT * FROM rounds WHERE competition_id = ?"
    args: list = [comp_id]
    if discipline:
        q += " AND discipline = ?"
        args.append(discipline)
    rounds = db.execute(q + " ORDER BY round_no", args).fetchall()

    round_scores: dict = {}
    round_meta = []
    for rnd in rounds:
        scores = round_norm_scores(db, rnd["id"])
        if scores:
            round_scores[rnd["round_no"]] = scores
        round_meta.append({"round_no": rnd["round_no"], "task": rnd["task"],
                           "discipline": rnd["discipline"], "contested": bool(scores)})

    rows = standings(round_scores, cfg["drop_at"])
    names = {r["id"]: r["name"] for r in db.execute(
        "SELECT id, name FROM pilots").fetchall()}
    for row in rows:
        row["name"] = names.get(row["pilot_id"], f"Pilot {row['pilot_id']}")
    return {
        "rounds": round_meta,
        "standings": rows,
        "drops": active_drops(cfg["drop_at"], len(round_scores)),
    }
