"""Read GliderScore .mdb files via mdbtools and import into the Pi DB."""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import subprocess
from datetime import datetime

log = logging.getLogger("f3k")

# GliderScore class string → our discipline code
_GS_CLASS = {
    "F3K": "F3K",
    "F5K2024": "F5K",
    "F5K": "F5K",
}

# Working time in seconds for F3K task strings that aren't 10 min
_F3K_WT_S: dict[str, int] = {
    "A(2)": 420, "B(2)": 420,   # 7-min variants
    "E(2)": 900, "M": 900,       # 15-min tasks
}
# F5K: B = 7 min, all others 10 min
_F5K_WT_S: dict[str, int] = {"B": 420}


def _parse_date(gs_date: str) -> str:
    """Convert GliderScore MM/DD/YY (or MM/DD/YYYY) date to YYYY-MM-DD."""
    raw = gs_date.split(" ")[0]
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw  # fallback: return as-is


def _mdb_export(mdb_path: str, table: str) -> list[dict]:
    """Return all rows of a table as a list of dicts using mdb-export."""
    result = subprocess.run(
        ["mdb-export", mdb_path, table],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mdb-export {table}: {result.stderr.strip()}")
    return list(csv.DictReader(io.StringIO(result.stdout)))


def _working_time_s(discipline: str, task: str) -> int:
    if discipline == "F3K":
        return _F3K_WT_S.get(task, 600)
    return _F5K_WT_S.get(task, 600)


def _task_letter(task_str: str) -> str:
    """Strip variant: 'A(1)' → 'A', 'C(3)' → 'C', 'G' → 'G'."""
    return task_str[0] if task_str else "A"


def list_competitions(mdb_path: str) -> list[dict]:
    """Return F3K/F5K competitions from the .mdb with pilot and round counts."""
    comps = _mdb_export(mdb_path, "Comps")
    comp_pilots = _mdb_export(mdb_path, "CompPilots")
    f3k_rounds = _mdb_export(mdb_path, "F3KTaskByRound")
    f5k_rounds = _mdb_export(mdb_path, "F5KTaskandRefHeightByRound")

    pilot_counts: dict[int, int] = {}
    for r in comp_pilots:
        cn = int(r["CompNo"])
        pilot_counts[cn] = pilot_counts.get(cn, 0) + 1

    round_counts: dict[int, int] = {}
    for r in f3k_rounds + f5k_rounds:
        if r.get("Task", "X") not in ("X", ""):
            cn = int(r["CompNo"])
            round_counts[cn] = round_counts.get(cn, 0) + 1

    result = []
    for row in comps:
        discipline = _GS_CLASS.get(row.get("GSCompClass", ""))
        if not discipline:
            continue
        comp_no = int(row["CompNo"])
        result.append({
            "comp_no": comp_no,
            "name": row["CompName"],
            "discipline": discipline,
            "date": _parse_date(row["CompDate"]),
            "venue": row.get("CompVenue", ""),
            "pilot_count": pilot_counts.get(comp_no, 0),
            "round_count": round_counts.get(comp_no, 0),
        })

    return sorted(result, key=lambda c: c["comp_no"])


def import_competition(mdb_path: str, gs_comp_no: int, db: sqlite3.Connection) -> int:
    """
    Import a GliderScore competition into the Pi DB.
    Pilots are upserted by gliderscore_pilot_no.
    Returns the new internal competition id.
    Raises ValueError if already imported or comp not found.
    """
    # Guard: don't re-import
    existing = db.execute(
        "SELECT id FROM competitions WHERE gliderscore_comp_no = ?", (gs_comp_no,)
    ).fetchone()
    if existing:
        raise ValueError(
            f"GliderScore CompNo {gs_comp_no} is already imported "
            f"(competition id {existing['id']})."
        )

    # Load tables
    comps = _mdb_export(mdb_path, "Comps")
    gs_pilots = {int(r["PilotNo"]): r for r in _mdb_export(mdb_path, "Pilots")}
    comp_pilots = _mdb_export(mdb_path, "CompPilots")
    f3k_rounds = _mdb_export(mdb_path, "F3KTaskByRound")
    f5k_rounds = _mdb_export(mdb_path, "F5KTaskandRefHeightByRound")
    scores = _mdb_export(mdb_path, "Scores")

    # Find competition row
    comp_row = next((r for r in comps if int(r["CompNo"]) == gs_comp_no), None)
    if not comp_row:
        raise ValueError(f"CompNo {gs_comp_no} not found in .mdb")

    discipline = _GS_CLASS.get(comp_row["GSCompClass"], "F3K")
    comp_date = _parse_date(comp_row["CompDate"])

    # Pilots entered in this comp
    gs_pilot_nos = [
        int(r["PilotNo"]) for r in comp_pilots if int(r["CompNo"]) == gs_comp_no
    ]

    # Upsert pilots — match by GS pilot no, fall back to name, else insert
    gs_to_internal: dict[int, int] = {}
    for gs_pno in gs_pilot_nos:
        p = gs_pilots.get(gs_pno)
        if not p:
            continue
        name = f"{p['FirstName']} {p['LastName']}".strip()

        row = db.execute(
            "SELECT id FROM pilots WHERE gliderscore_pilot_no = ?", (gs_pno,)
        ).fetchone()
        if row:
            internal_id = row["id"]
            db.execute("UPDATE pilots SET name = ? WHERE id = ?", (name, internal_id))
        else:
            row = db.execute("SELECT id FROM pilots WHERE name = ?", (name,)).fetchone()
            if row:
                internal_id = row["id"]
                db.execute(
                    "UPDATE pilots SET gliderscore_pilot_no = ? WHERE id = ?",
                    (gs_pno, internal_id),
                )
            else:
                cur = db.execute(
                    "INSERT INTO pilots (name, gliderscore_pilot_no) VALUES (?, ?)",
                    (name, gs_pno),
                )
                internal_id = cur.lastrowid

        gs_to_internal[gs_pno] = internal_id

    db.commit()

    # Create competition
    cur = db.execute(
        """INSERT INTO competitions (name, discipline, date, gliderscore_comp_no)
           VALUES (?, ?, ?, ?)""",
        (comp_row["CompName"], discipline, comp_date, gs_comp_no),
    )
    comp_id = cur.lastrowid

    for internal_id in gs_to_internal.values():
        db.execute(
            "INSERT OR IGNORE INTO competition_pilots (competition_id, pilot_id) VALUES (?, ?)",
            (comp_id, internal_id),
        )

    db.commit()

    # Build round task table
    if discipline == "F3K":
        round_task = {
            int(r["RoundNo"]): r["Task"]
            for r in f3k_rounds
            if int(r["CompNo"]) == gs_comp_no and r.get("Task", "X") not in ("X", "")
        }
    else:
        round_task = {
            int(r["RoundNo"]): r["Task"]
            for r in f5k_rounds
            if int(r["CompNo"]) == gs_comp_no and r.get("Task", "X") not in ("X", "")
        }

    # Build draw from Scores table (may be empty if draw not yet done)
    draw = [r for r in scores if int(r["CompNo"]) == gs_comp_no]
    draw_round_nos = sorted({int(r["RoundNo"]) for r in draw})

    # Rounds = union of task table + draw rounds
    all_round_nos = sorted(set(list(round_task.keys()) + draw_round_nos))

    for round_no in all_round_nos:
        task_str = round_task.get(round_no, "A")
        cur = db.execute(
            """INSERT INTO rounds (competition_id, round_no, task, working_time_s, discipline)
               VALUES (?, ?, ?, ?, ?)""",
            (
                comp_id, round_no,
                _task_letter(task_str),
                _working_time_s(discipline, task_str),
                discipline,
            ),
        )
        round_id = cur.lastrowid

        # Groups in this round from the draw
        round_draw = [r for r in draw if int(r["RoundNo"]) == round_no]
        group_nos = sorted({int(r["GroupNo"]) for r in round_draw})

        for group_no in group_nos:
            cur = db.execute(
                "INSERT INTO groups (round_id, group_no) VALUES (?, ?)",
                (round_id, group_no),
            )
            group_id = cur.lastrowid

            group_pilots = sorted(
                [r for r in round_draw if int(r["GroupNo"]) == group_no],
                key=lambda r: int(r["SeqNo"]),
            )
            for gp in group_pilots:
                internal_id = gs_to_internal.get(int(gp["PilotNo"]))
                if internal_id:
                    db.execute(
                        "INSERT OR IGNORE INTO group_pilots (group_id, pilot_id) VALUES (?, ?)",
                        (group_id, internal_id),
                    )

    db.commit()
    log.info(
        "Imported GS CompNo=%d → internal comp_id=%d (%d pilots, %d rounds)",
        gs_comp_no, comp_id, len(gs_to_internal), len(all_round_nos),
    )
    return comp_id
