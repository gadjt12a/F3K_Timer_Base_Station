import csv
import datetime
import io
import json
import logging
import os
import re
import tempfile
import urllib.parse
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from frontend import audio_control, draw, scoring
from frontend.audio import engine


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict) -> None:
        msg = json.dumps(data)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


log = logging.getLogger("f3k")

manager = ConnectionManager()

# Interactive API docs are off: this app is served on the public OPS AP that every
# pilot at the field joins, and /docs is a ready-made console for POSTing to the
# run-control endpoints mid-competition. Nothing here consumes the schema. [I-21]
app = FastAPI(title="Glide Base", docs_url=None, redoc_url=None, openapi_url=None)
app.state.ws_manager = manager


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    """Missing/invalid form fields must not dump raw JSON at the CD. [I-12]

    Clicking Assign with no pilots ticked used to render
    `{"detail":[{"type":"missing"...}]}` as a bare page. Browser form posts go
    back where they came from with a readable message; fetch() callers still get
    JSON, since that's what their error paths already expect.
    """
    fields = ", ".join(
        str(err["loc"][-1]) for err in exc.errors() if err.get("loc")) or "input"
    msg = f"Missing or invalid: {fields}"
    accepts_html = "text/html" in request.headers.get("accept", "")
    if request.method == "POST" and accepts_html:
        base = (request.headers.get("referer") or "/").split("?")[0]
        # Pages surface their banner under different names — /setup reads `msg`,
        # /rounds and /results read `error`. Sending both means one handler works
        # for every form on the site without a per-page lookup table.
        q = urllib.parse.urlencode({"error": msg, "msg": msg})
        return RedirectResponse(f"{base}?{q}", status_code=303)
    return JSONResponse({"ok": False, "error": msg, "detail": exc.errors()},
                        status_code=422)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Tailwind + Alpine are vendored — the field networks have no internet, so CDN
# scripts would leave every page unstyled and dead on an uncached device.
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def _fmt_date(value: str) -> str:
    """YYYY-MM-DD → 22 Jul 2026; passes through anything that doesn't parse."""
    try:
        d = datetime.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value) if value else ""
    # Built by hand rather than with strftime("%-d") — that flag is glibc-only, so
    # on Windows it raises and the bare except used to hide it, silently returning
    # raw ISO dates instead of failing loudly. [I-15]
    return f"{d.day} {d.strftime('%b %Y')}"


templates.env.filters["fmt_date"] = _fmt_date


def _fmt_ms(ms) -> str:
    if ms is None:
        return "—"
    total_s = ms // 1000
    centis = (ms % 1000) // 10
    return f"{total_s // 60}:{total_s % 60:02d}.{centis:02d}"


def _gs_time(ms: int) -> str:
    """GliderScore mmss.sss format: 83400ms → '123.400', 600000ms → '1000.000'."""
    total_s = ms // 1000
    millis = ms % 1000
    return f"{total_s // 60}{total_s % 60:02d}.{millis:03d}"


# A flight is a hand-launched glider in a working window that is never longer than
# 30 minutes, and motor-cut heights are tens of metres. These ceilings exist to turn
# a CD's typo into an error rather than into plausible-looking data. [I-02] [I-03]
MAX_DURATION_MS = 60 * 60 * 1000     # 1 hour — well past any real working window
MAX_ALTITUDE_M = 1000.0              # F5K motor-cut heights are 30–100 m

DURATION_HELP = "Invalid time — use M:SS or M:SS.HH (e.g. 3:00 or 3:00.55)"
ALTITUDE_HELP = f"Invalid altitude — enter metres between 0 and {MAX_ALTITUDE_M:.0f}"


def _parse_duration(s: str) -> int:
    """Parse 'M:SS' or 'M:SS.HH' to milliseconds. Raises ValueError on bad input."""
    s = s.strip()
    if ':' not in s:
        raise ValueError("Expected M:SS format")
    m, rest = s.split(':', 1)
    if '.' in rest:
        sec, frac = rest.split('.', 1)
        centis = frac.ljust(2, '0')[:2]
    else:
        sec, centis = rest, '00'
    mins, secs, cs = int(m), int(sec), int(centis)
    # Without this, '1:99' parses as 2:39 — a wrong time that looks entirely
    # plausible on the results page and is never questioned again. [I-02]
    if not 0 <= secs < 60:
        raise ValueError("Seconds must be 0–59")
    if mins < 0 or cs < 0:
        raise ValueError("Duration must be positive")
    total_ms = (mins * 60 + secs) * 1000 + cs * 10
    if total_ms <= 0:
        raise ValueError("Duration must be positive")
    if total_ms > MAX_DURATION_MS:
        raise ValueError("Duration is longer than any working window")
    return total_ms


def _parse_altitude(s) -> float | None:
    """Parse an altitude field to metres, or None if blank. Raises ValueError.

    Guards the whole float domain, not just non-numeric text: 'inf' and '1e400'
    store as inf and poison F5K bonus scoring, and 'nan' is coerced to NULL by
    SQLite, silently discarding the entry. [I-03]
    """
    if s is None or not str(s).strip():
        return None
    alt = float(str(s).strip())          # ValueError on anything non-numeric
    if alt != alt or alt in (float("inf"), float("-inf")):
        raise ValueError("Altitude must be a finite number")
    if not 0 <= alt <= MAX_ALTITUDE_M:
        raise ValueError(f"Altitude must be between 0 and {MAX_ALTITUDE_M:.0f} m")
    return alt


def _working_time_ms(db, group_id: int) -> int | None:
    """The working window of the round this group belongs to, in ms."""
    row = db.execute(
        "SELECT r.working_time_s FROM groups g JOIN rounds r ON r.id = g.round_id"
        " WHERE g.id = ?", (group_id,)).fetchone()
    if row is None or row["working_time_s"] is None:
        return None
    return int(row["working_time_s"]) * 1000


def _duration_fits_round(db, group_id: int, dur_ms: int) -> str | None:
    """Error message if the flight is longer than its round's working time. [I-04]

    The ceiling is the working time itself: FAI SC4 Vol. F3 5.7.7 stops the clock
    at a landing *or the expiry of working time*, whichever comes first.

    DO NOT add the landing window to this ceiling. It looks like you should — a
    glider is plainly still flying after the horn — but 5.7.9.3 is a validity
    rule, not a duration one: land later than the 30 s window and the flight
    scores 0. The clock already stopped. (Manual CD entry only; the timer's own
    record_flight() is not gated on this.)
    """
    wt_ms = _working_time_ms(db, group_id)
    if wt_ms and dur_ms > wt_ms:
        return (f"Flight time {_fmt_ms(dur_ms)} is longer than the round's "
                f"working time ({_fmt_ms(wt_ms)}) — timing stops at the end of "
                f"working time (FAI 5.7.7)")
    return None


templates.env.filters["fmt_ms"] = _fmt_ms

GS_UPLOAD_PATH = str(Path(__file__).resolve().parent.parent / "gs_upload.mdb")


def _db():
    return app.state.server.db


def _gs_locked(db, comp_id: int) -> bool:
    """Return True if the competition was imported from GliderScore (structure is locked)."""
    row = db.execute(
        "SELECT gliderscore_comp_no FROM competitions WHERE id = ?", (comp_id,)
    ).fetchone()
    return bool(row and row["gliderscore_comp_no"])


_GS_LOCK_MSG = urllib.parse.quote("GS Locked — structure is managed in GliderScore. Re-import to update.")


def _comp_id_of_round(db, round_id: int) -> int | None:
    row = db.execute("SELECT competition_id FROM rounds WHERE id = ?", (round_id,)).fetchone()
    return row["competition_id"] if row else None


def _comp_id_of_group(db, group_id: int) -> int | None:
    row = db.execute(
        "SELECT r.competition_id FROM groups g JOIN rounds r ON r.id = g.round_id WHERE g.id = ?",
        (group_id,),
    ).fetchone()
    return row["competition_id"] if row else None


def _comp_pilots(db, comp_id: int) -> list:
    """Pilots entered in a competition (id, name), ordered by name."""
    return db.execute(
        """SELECT p.id, p.name FROM pilots p
           JOIN competition_pilots cp ON cp.pilot_id = p.id
           WHERE cp.competition_id = ? ORDER BY p.name""",
        (comp_id,),
    ).fetchall()


@app.get("/health")
async def health():
    return {"status": "ok", "timers_connected": len(app.state.server._clients)}


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@app.get("/setup")
async def setup_get(request: Request, msg: str = None):
    db = _db()
    pilots = db.execute("SELECT * FROM pilots ORDER BY name").fetchall()
    competitions = db.execute("SELECT * FROM competitions ORDER BY id DESC").fetchall()

    # Full card data for every comp (newest first) — archived ones render the
    # same card inside the Archived section so their data stays viewable.
    comp_data = [{"comp": comp, "pilots": _comp_pilots(db, comp["id"])}
                 for comp in competitions]

    return templates.TemplateResponse(request, "setup.html", {
        "active": "setup",
        "all_pilots": pilots,
        "comp_data": comp_data,
        "msg": msg,
    })


DISCIPLINES = ("F3K", "F5K", "MIXED")   # must match the CHECK constraint in db.py


@app.post("/setup/competition/new")
async def competition_new(
    name: str = Form(...),
    discipline: str = Form(...),
    date: str = Form(...),
    location: str = Form(""),
    gliderscore_comp_no: str = Form(""),
    prep_time_s: int = Form(180),
    land_time_s: int = Form(30),
    heat_gap_s: int = Form(30),
    round_gap_s: int = Form(30),
    focus_time_s: int = Form(45),
    count_last_s: int = Form(15),
):
    db = _db()
    # The DB has a CHECK on discipline, so anything else arrived as a raw
    # "CHECK constraint failed" 500 rather than a message. [I-08]
    if discipline not in DISCIPLINES:
        return RedirectResponse(
            f"/setup?msg={urllib.parse.quote('Discipline must be F3K, F5K or MIXED')}",
            status_code=303)
    if not name.strip():
        return RedirectResponse(
            f"/setup?msg={urllib.parse.quote('Competition name cannot be empty')}",
            status_code=303)
    try:
        comp_no = int(gliderscore_comp_no) if gliderscore_comp_no.strip() else None
    except (ValueError, TypeError):
        return RedirectResponse(
            f"/setup?msg={urllib.parse.quote('GliderScore comp number must be a whole number')}",
            status_code=303)
    db.execute(
        """INSERT INTO competitions
           (name, discipline, date, location, gliderscore_comp_no,
            prep_time_s, land_time_s, heat_gap_s, round_gap_s, focus_time_s, count_last_s)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, discipline, date, location.strip(), comp_no,
         prep_time_s, land_time_s, heat_gap_s, round_gap_s, focus_time_s, count_last_s),
    )
    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/competition/{comp_id}/delete")
async def competition_delete(comp_id: int):
    db = _db()
    db.execute("""
        DELETE FROM group_pilots WHERE group_id IN (
            SELECT g.id FROM groups g
            JOIN rounds r ON g.round_id = r.id
            WHERE r.competition_id = ?
        )
    """, (comp_id,))
    db.execute("""
        DELETE FROM flights WHERE group_id IN (
            SELECT g.id FROM groups g
            JOIN rounds r ON g.round_id = r.id
            WHERE r.competition_id = ?
        )
    """, (comp_id,))
    db.execute("""
        DELETE FROM groups WHERE round_id IN (
            SELECT id FROM rounds WHERE competition_id = ?
        )
    """, (comp_id,))
    db.execute("DELETE FROM rounds WHERE competition_id = ?", (comp_id,))
    db.execute("DELETE FROM competition_pilots WHERE competition_id = ?", (comp_id,))
    db.execute("DELETE FROM competitions WHERE id = ?", (comp_id,))
    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/competition/{comp_id}/pilot/add")
async def competition_pilot_add(comp_id: int, pilot_id: int = Form(...)):
    db = _db()
    if _gs_locked(db, comp_id):
        return RedirectResponse(f"/setup?msg={_GS_LOCK_MSG}", status_code=303)
    # A stale tab can post against a competition or pilot that has since been
    # deleted; INSERT OR IGNORE doesn't cover an FK violation. [I-08]
    if db.execute("SELECT 1 FROM competitions WHERE id = ?", (comp_id,)).fetchone() is None:
        return RedirectResponse(
            f"/setup?msg={urllib.parse.quote('That competition no longer exists')}",
            status_code=303)
    if db.execute("SELECT 1 FROM pilots WHERE id = ?", (pilot_id,)).fetchone() is None:
        return RedirectResponse(
            f"/setup?msg={urllib.parse.quote('That pilot no longer exists')}",
            status_code=303)
    db.execute(
        "INSERT OR IGNORE INTO competition_pilots (competition_id, pilot_id) VALUES (?, ?)",
        (comp_id, pilot_id),
    )
    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/competition/{comp_id}/pilot/{pilot_id}/remove")
async def competition_pilot_remove(comp_id: int, pilot_id: int):
    db = _db()
    if _gs_locked(db, comp_id):
        return RedirectResponse(f"/setup?msg={_GS_LOCK_MSG}", status_code=303)
    db.execute(
        "DELETE FROM competition_pilots WHERE competition_id = ? AND pilot_id = ?",
        (comp_id, pilot_id),
    )
    db.commit()
    return RedirectResponse("/setup", status_code=303)


# ---------------------------------------------------------------------------
# Global pilot registry
# ---------------------------------------------------------------------------

@app.post("/setup/pilot/add")
async def pilot_add(name: str = Form(...)):
    db = _db()
    db.execute("INSERT INTO pilots (name) VALUES (?)", (name.strip(),))
    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/pilot/{pilot_id}/delete")
async def pilot_delete(pilot_id: int):
    db = _db()
    db.execute("DELETE FROM competition_pilots WHERE pilot_id = ?", (pilot_id,))
    db.execute("DELETE FROM group_pilots WHERE pilot_id = ?", (pilot_id,))
    db.execute("UPDATE flights SET pilot_id = NULL WHERE pilot_id = ?", (pilot_id,))
    db.execute("DELETE FROM pilots WHERE id = ?", (pilot_id,))
    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/pilot/{pilot_id}/rename")
async def pilot_rename(pilot_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name cannot be empty")
    db = _db()
    db.execute("UPDATE pilots SET name = ? WHERE id = ?", (name, pilot_id))
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Rounds
# ---------------------------------------------------------------------------

# AUTHORITATIVE task catalogues — sourced verbatim from GliderScore's own task audio
# descriptions (GliderScoreData.mdb → AudioSettings, F3KTask/F5KTask rows, 2026-07-10).
# GliderScore is the competition/deployment tool, so operators see these same labels.
# IMPORTANT: F3K and F5K reuse the same LETTERS with DIFFERENT meanings — keep separate.
# Base letters use the primary variant; GliderScore variants (A(1), C(3)...) are captured
# in base_station/frontend/data/gliderscore_audio_library.json for when we build import.
# Each entry: letter -> {"name": short label, "desc": objective for the run screen};
# "wt_min" only where the usual 10-minute window doesn't apply (Draw Wizard default).
F3K_TASKS = {
    "A": {"name": "Last flight",      "desc": "Only your last flight counts — 5:00 max; unlimited flights in the window."},
    "B": {"name": "Last 2 flights",   "desc": "Your last two flights count — 4:00 max each; unlimited flights in 10 min."},
    "C": {"name": "All up",           "desc": "All gliders launch together on the horn — 3:00 max; 3–5 flights per round."},
    "D": {"name": "Ladder",           "desc": "First target 0:30, +0:15 each time you reach it; unlimited flights in 10 min."},
    "E": {"name": "Poker",            "desc": "Call your own target times; best 5 timed flights count; unlimited flights in 10 min."},
    "F": {"name": "Best 3 of 6",      "desc": "Your 3 longest flights count — 3:00 max each; max 6 flights in 10 min."},
    "G": {"name": "Best 5",           "desc": "Your 5 longest flights count — 2:00 max each; unlimited flights in 10 min."},
    "H": {"name": "Best 4 (1-2-3-4)", "desc": "Best 4 flights against 1, 2, 3 and 4 min targets (any order); unlimited flights in 10 min."},
    "I": {"name": "Best 3",           "desc": "Your 3 longest flights count — 3:20 max each; unlimited flights in 10 min."},
    "J": {"name": "Last 3",           "desc": "Your last 3 flights count — 3:00 max each; unlimited flights in 10 min."},
    "K": {"name": "Big Ladder",       "desc": "5 flights in order — 1:00, 1:30, 2:00, 2:30, 3:00 — each scored up to its target."},
    "L": {"name": "One flight",       "desc": "A single flight, up to 9:59, in a 10-minute window."},
    "M": {"name": "Huge Ladder",      "desc": "3 flights of 3:00, 5:00, 7:00 in order; 15-minute window; 3 flights max.", "wt_min": 15},
    "N": {"name": "Best flight",      "desc": "Your single best flight counts; 10-minute window."},
}
# F5K reuses letters with DIFFERENT tasks (GliderScore class F5K2024). Scoring also adds a
# motor-cut height bonus vs the round's reference height — see GLIDERSCORE.md.
F5K_TASKS = {
    "A": {"name": "1-2-3-4",          "desc": "4 flights in 10 min — targets 1, 2, 3 and 4 minutes in any order."},
    "B": {"name": "Last flight",      "desc": "Only your last flight counts — 5:00 max; max 3 flights in 7 min.", "wt_min": 7},
    "C": {"name": "All up",           "desc": "All gliders launch together — 4:00 max; 3 flights per round."},
    "D": {"name": "3 flights (3-3-4)","desc": "Three flights of 3:00, 3:00 and 4:00 in any order; max 3 flights in 10 min."},
    "E": {"name": "Poker",            "desc": "Call your own target times; best 3 timed flights count; max 3 flights in 10 min."},
}
TASKS = {"F3K": F3K_TASKS, "F5K": F5K_TASKS}

RULE_KINDS = {
    "last_n": "Last N flights count",
    "best_n": "N longest flights count",
    "first_n": "First N flights count (all-up)",
    "ladder": "Ladder (target grows each time it is reached)",
    "targets": "Fixed targets, any order",
    "sequence": "Fixed targets, in order",
    "poker": "Poker (N longest, no cap)",
    "all": "All flights count",
}


def merged_tasks(db) -> dict:
    """Built-in catalogue + user-defined custom tasks (for dropdowns/labels)."""
    out = {d: dict(t) for d, t in TASKS.items()}
    for r in db.execute(
            "SELECT * FROM custom_tasks ORDER BY discipline, code").fetchall():
        out.setdefault(r["discipline"], {})[r["code"]] = {
            "name": r["name"], "desc": r["descr"], "custom": True,
            "wt_min": r["wt_min"], "id": r["id"], "based_on": r["based_on"],
        }
    return out


def task_label(discipline: str, letter: str) -> str:
    t = TASKS.get(discipline, {}).get(letter)
    if t:
        return t["name"]
    row = _db().execute(
        "SELECT name FROM custom_tasks WHERE discipline = ? AND code = ?",
        (discipline, letter)).fetchone()
    return row["name"] if row else letter


@app.get("/rounds")
async def rounds_get(request: Request, error: str = None):
    db = _db()
    competitions = db.execute("SELECT * FROM competitions WHERE archived = 0 ORDER BY id DESC").fetchall()

    comp_data = []
    for comp in competitions:
        rounds = db.execute(
            "SELECT * FROM rounds WHERE competition_id = ? ORDER BY round_no",
            (comp["id"],),
        ).fetchall()

        round_data = []
        for rnd in rounds:
            groups = db.execute(
                "SELECT * FROM groups WHERE round_id = ? ORDER BY group_no",
                (rnd["id"],),
            ).fetchall()
            group_data = []
            rnd_has_flights = False
            for grp in groups:
                gpilots = db.execute(
                    """SELECT p.name FROM pilots p
                       JOIN group_pilots gp ON gp.pilot_id = p.id
                       WHERE gp.group_id = ? ORDER BY p.name""",
                    (grp["id"],),
                ).fetchall()
                group_data.append({"group": grp, "pilots": gpilots})
                n = db.execute("SELECT COUNT(*) FROM flights WHERE group_id = ?",
                               (grp["id"],)).fetchone()[0]
                if n > 0:
                    rnd_has_flights = True
            round_data.append({"round": rnd, "groups": group_data, "has_flights": rnd_has_flights})

        comp_data.append({
            "comp": comp,
            "rounds": round_data,
            "comp_pilots": _comp_pilots(db, comp["id"]),
        })

    return templates.TemplateResponse(request, "rounds.html", {
        "active": "rounds",
        "comp_data": comp_data,
        "tasks": merged_tasks(db),
        "custom_tasks": db.execute(
            "SELECT * FROM custom_tasks ORDER BY discipline, code").fetchall(),
        "rule_kinds": RULE_KINDS,
        "error": error,
    })


MAX_WORKING_TIME_M = 60


def _validate_round(db, task: str, working_time_m: int, comp_discipline: str) -> str | None:
    """Error message if a round's task or working time is nonsense. [I-17]

    A zero-minute working time made a round that ends the instant it starts, and
    an unknown task letter scored as nothing at all — both accepted silently.
    """
    if not 1 <= working_time_m <= MAX_WORKING_TIME_M:
        return f"Working time must be between 1 and {MAX_WORKING_TIME_M} minutes"
    discipline, letter = (task.split(":", 1) if ":" in task else (comp_discipline, task))
    if discipline not in ("F3K", "F5K"):
        return "Round discipline must be F3K or F5K"
    if letter not in merged_tasks(db).get(discipline, {}):
        return f"Unknown {discipline} task '{letter}'"
    return None


@app.post("/rounds/{comp_id}/add")
async def round_add(comp_id: int, task: str = Form(...), working_time_m: int = Form(10)):
    db = _db()
    if _gs_locked(db, comp_id):
        return RedirectResponse(f"/rounds?error={_GS_LOCK_MSG}", status_code=303)
    comp = db.execute("SELECT discipline FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    if not comp:
        return RedirectResponse("/rounds", status_code=303)
    err = _validate_round(db, task, working_time_m, comp["discipline"])
    if err:
        return RedirectResponse(f"/rounds?error={urllib.parse.quote(err)}", status_code=303)
    max_no = db.execute(
        "SELECT MAX(round_no) FROM rounds WHERE competition_id = ?", (comp_id,)
    ).fetchone()[0]
    round_no = (max_no or 0) + 1
    # MIXED comps submit the task as "F3K:A" / "F5K:B" (discipline per round);
    # pure comps submit the bare letter and inherit the comp discipline.
    if ":" in task:
        discipline, task = task.split(":", 1)
    else:
        discipline = comp["discipline"]
    db.execute(
        """INSERT INTO rounds (competition_id, round_no, task, working_time_s, discipline)
           VALUES (?, ?, ?, ?, ?)""",
        (comp_id, round_no, task, working_time_m * 60, discipline),
    )
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.post("/rounds/round/{round_id}/edit")
async def round_edit(round_id: int, task: str = Form(...), working_time_m: int = Form(...)):
    db = _db()
    comp_id = _comp_id_of_round(db, round_id)
    if comp_id is None:
        return RedirectResponse("/rounds", status_code=303)
    if _gs_locked(db, comp_id):
        return RedirectResponse(f"/rounds?error={_GS_LOCK_MSG}", status_code=303)
    has_flights = db.execute(
        """SELECT COUNT(*) FROM flights f
           JOIN groups g ON g.id = f.group_id
           WHERE g.round_id = ?""", (round_id,)
    ).fetchone()[0]
    if has_flights:
        return RedirectResponse(
            f"/rounds?error={urllib.parse.quote('Cannot edit a round that already has flights recorded')}",
            status_code=303)
    rnd = db.execute("SELECT discipline FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not rnd:
        return RedirectResponse("/rounds", status_code=303)
    err = _validate_round(db, task, working_time_m, rnd["discipline"])   # [I-17]
    if err:
        return RedirectResponse(f"/rounds?error={urllib.parse.quote(err)}", status_code=303)
    discipline = rnd["discipline"]
    if ":" in task:
        discipline, task = task.split(":", 1)
    db.execute(
        "UPDATE rounds SET task = ?, working_time_s = ?, discipline = ? WHERE id = ?",
        (task, working_time_m * 60, discipline, round_id),
    )
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.post("/rounds/round/{round_id}/delete")
async def round_delete(round_id: int):
    db = _db()
    if _gs_locked(db, _comp_id_of_round(db, round_id)):
        return RedirectResponse(f"/rounds?error={_GS_LOCK_MSG}", status_code=303)
    groups = db.execute("SELECT id FROM groups WHERE round_id = ?", (round_id,)).fetchall()
    for grp in groups:
        db.execute("DELETE FROM group_pilots WHERE group_id = ?", (grp["id"],))
    db.execute("DELETE FROM groups WHERE round_id = ?", (round_id,))
    db.execute("DELETE FROM rounds WHERE id = ?", (round_id,))
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.get("/reports/flight_cards/{comp_id}")
async def flight_cards_get(comp_id: int, request: Request):
    db = _db()
    comp = db.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    if not comp:
        return RedirectResponse("/rounds", status_code=303)
    pilots = db.execute(
        """SELECT p.id, p.name, p.gliderscore_pilot_no, p.fai_number
           FROM pilots p
           JOIN competition_pilots cp ON cp.pilot_id = p.id
           WHERE cp.competition_id = ? ORDER BY p.name""",
        (comp_id,),
    ).fetchall()
    all_tasks = merged_tasks(db)
    pilot_cards = []
    for p in pilots:
        rounds = db.execute(
            """SELECT r.round_no, r.task, r.discipline, r.working_time_s, g.group_no
               FROM rounds r
               JOIN groups g ON g.round_id = r.id
               JOIN group_pilots gp ON gp.group_id = g.id
               WHERE r.competition_id = ? AND gp.pilot_id = ?
               ORDER BY r.round_no""",
            (comp_id, p["id"]),
        ).fetchall()
        by_disc: dict[str, list] = {}
        for r in rounds:
            rd = dict(r)
            rd["task_name"] = all_tasks.get(r["discipline"], {}).get(r["task"], {}).get("name", r["task"])
            by_disc.setdefault(r["discipline"], []).append(rd)
        for disc, disc_rounds in by_disc.items():
            pilot_cards.append({"pilot": p, "rounds": disc_rounds, "is_f5k": disc == "F5K"})
        if not rounds:
            pilot_cards.append({"pilot": p, "rounds": [], "is_f5k": False})
    return templates.TemplateResponse(request, "flight_cards.html", {
        "comp": comp,
        "pilot_cards": pilot_cards,
    })


@app.post("/rounds/round/{round_id}/group/add")
async def group_add(round_id: int, request: Request):
    db = _db()
    if _gs_locked(db, _comp_id_of_round(db, round_id)):
        return RedirectResponse(f"/rounds?error={_GS_LOCK_MSG}", status_code=303)
    form = await request.form()
    pilot_ids = form.getlist("pilot_ids")
    dummy_count = int(form.get("dummy_count", 0) or 0)
    max_no = db.execute(
        "SELECT MAX(group_no) FROM groups WHERE round_id = ?", (round_id,)
    ).fetchone()[0]
    group_no = (max_no or 0) + 1
    cur = db.execute(
        "INSERT INTO groups (round_id, group_no, dummy_count) VALUES (?, ?, ?)",
        (round_id, group_no, dummy_count),
    )
    group_id = cur.lastrowid
    for pid in pilot_ids:
        db.execute(
            "INSERT OR IGNORE INTO group_pilots (group_id, pilot_id) VALUES (?, ?)",
            (group_id, int(pid)),
        )
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.post("/rounds/group/{group_id}/dummy/add")
async def group_dummy_add(group_id: int):
    db = _db()
    if _gs_locked(db, _comp_id_of_group(db, group_id)):
        return RedirectResponse(f"/rounds?error={_GS_LOCK_MSG}", status_code=303)
    db.execute("UPDATE groups SET dummy_count = dummy_count + 1 WHERE id = ?", (group_id,))
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.post("/rounds/group/{group_id}/dummy/remove")
async def group_dummy_remove(group_id: int):
    db = _db()
    if _gs_locked(db, _comp_id_of_group(db, group_id)):
        return RedirectResponse(f"/rounds?error={_GS_LOCK_MSG}", status_code=303)
    db.execute(
        "UPDATE groups SET dummy_count = MAX(0, dummy_count - 1) WHERE id = ?", (group_id,)
    )
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.post("/rounds/group/{group_id}/delete")
async def group_delete(group_id: int):
    db = _db()
    if _gs_locked(db, _comp_id_of_group(db, group_id)):
        return RedirectResponse(f"/rounds?error={_GS_LOCK_MSG}", status_code=303)
    db.execute("DELETE FROM group_pilots WHERE group_id = ?", (group_id,))
    db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@app.get("/results")
async def results_get(request: Request, error: str = None, comp_id: int = None):
    db = _db()
    all_comps = db.execute("SELECT id, name, discipline FROM competitions WHERE archived = 0 ORDER BY id DESC").fetchall()
    if comp_id is not None:
        competitions = [c for c in all_comps if c["id"] == comp_id]
    else:
        competitions = list(all_comps)

    comp_data = []
    for comp in competitions:
        rounds = db.execute(
            "SELECT * FROM rounds WHERE competition_id = ? ORDER BY round_no",
            (comp["id"],),
        ).fetchall()

        round_data = []
        for rnd in rounds:
            groups = db.execute(
                "SELECT * FROM groups WHERE round_id = ? ORDER BY group_no",
                (rnd["id"],),
            ).fetchall()

            heat_data = []
            for grp in groups:
                rows = db.execute(
                    """SELECT p.id AS pilot_id, p.name AS pilot_name,
                              f.id AS flight_id, f.duration_ms, f.altitude_m, f.recorded_at
                       FROM group_pilots gp
                       JOIN pilots p ON p.id = gp.pilot_id
                       LEFT JOIN flights f ON f.pilot_id = p.id AND f.group_id = ?
                       WHERE gp.group_id = ?
                       ORDER BY p.name, f.flight_no NULLS LAST, f.recorded_at""",
                    (grp["id"], grp["id"]),
                ).fetchall()

                pilot_flights: dict = {}
                pilot_order: list = []
                any_altitudes = False
                for row in rows:
                    pid = row["pilot_id"]
                    if pid not in pilot_flights:
                        pilot_flights[pid] = {
                            "id": pid,
                            "name": row["pilot_name"],
                            "flights": [],
                        }
                        pilot_order.append(pid)
                    if row["flight_id"] is not None:
                        alt = row["altitude_m"]
                        if alt is not None:
                            any_altitudes = True
                        pilot_flights[pid]["flights"].append(
                            {"id": row["flight_id"], "duration_ms": row["duration_ms"], "altitude_m": alt}
                        )

                pilots = [pilot_flights[pid] for pid in pilot_order]
                max_flights = max((len(p["flights"]) for p in pilots), default=0)

                # Computed scores (scoring engine): raw / normalised / rank per
                # pilot, per-flight scored time + F5K bonus. On-demand, not stored.
                any_scores = False
                if max_flights > 0:
                    scored = scoring.score_group_db(db, grp["id"])
                    for p in pilots:
                        sp = scored["pilots"].get(p["id"])
                        if not sp:
                            continue
                        p["raw_s"] = sp["total"]
                        p["norm"] = sp["norm"]
                        p["rank"] = sp["rank"]
                        any_scores = True
                        by_fid = {f["id"]: f for f in sp["flights"]}
                        for fl in p["flights"]:
                            sf = by_fid.get(fl["id"])
                            fl["scored_s"] = sf["scored_s"] if sf else 0.0
                            fl["bonus"] = sf["bonus"] if sf else None
                            fl["altitude_source"] = sf["altitude_source"] if sf else None

                heat_data.append({
                    "group": grp,
                    "heat": chr(64 + grp["group_no"]),
                    "pilots": pilots,
                    "max_flights": max_flights,
                    "any_altitudes": any_altitudes,
                    "any_scores": any_scores,
                })

            round_data.append({
                "round": rnd,
                "task_name": task_label(rnd["discipline"], rnd["task"]),
                "heats": heat_data,
            })

        comp_data.append({"comp": comp, "rounds": round_data})

    return templates.TemplateResponse(request, "results.html", {
        "active": "results",
        "comp_data": comp_data,
        "all_comps": all_comps,
        "active_comp_id": comp_id,
        "error": error,
    })


@app.post("/results/flight/add")
async def results_flight_add(
    group_id: int = Form(...),
    pilot_id: int = Form(...),
    duration: str = Form(...),
    altitude_m: str = Form(""),
    flight_no: str = Form(""),
):
    def bad(msg: str):
        return RedirectResponse(f"/results?error={urllib.parse.quote(msg)}", status_code=303)

    try:
        dur_ms = _parse_duration(duration)
    except (ValueError, TypeError):
        return bad(DURATION_HELP)
    # Altitude and flight number were parsed outside the guard above, so one stray
    # letter in either box was a 500 rather than a message. [I-05] [I-06]
    try:
        alt = _parse_altitude(altitude_m)
    except (ValueError, TypeError):
        return bad(ALTITUDE_HELP)
    try:
        fno = int(flight_no) if flight_no.strip() else None
    except (ValueError, TypeError):
        return bad("Invalid flight number")
    if fno is not None and not 1 <= fno <= 50:
        return bad("Flight number must be between 1 and 50")

    db = _db()
    # An unknown pilot or group reaches SQLite as a FOREIGN KEY violation, i.e. a
    # 500 with no explanation. Check first and say which one is missing. [I-08]
    if db.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone() is None:
        return bad("That heat no longer exists")
    if db.execute("SELECT 1 FROM pilots WHERE id = ?", (pilot_id,)).fetchone() is None:
        return bad("That pilot no longer exists")
    err = _duration_fits_round(db, group_id, dur_ms)
    if err:
        return bad(err)
    db.execute(
        "INSERT INTO flights (pilot_id, duration_ms, group_id, altitude_m, flight_no, altitude_source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (pilot_id, dur_ms, group_id, alt, fno, "cd_entry" if alt is not None else None),
    )
    db.commit()
    return RedirectResponse("/results", status_code=303)


@app.post("/results/flight/delete")
async def results_flight_delete(flight_id: int = Form(...)):
    db = _db()
    db.execute("DELETE FROM flights WHERE id = ?", (flight_id,))
    db.commit()
    return RedirectResponse("/results", status_code=303)


# ---------------------------------------------------------------------------
# Import — GliderScore .mdb via mdbtools
# ---------------------------------------------------------------------------

@app.get("/import")
async def import_get(request: Request, error: str = None):
    from frontend import gs_import as gsi
    competitions: list = []
    upload_exists = os.path.exists(GS_UPLOAD_PATH)
    read_error: str | None = None
    if upload_exists and not error:
        try:
            competitions = gsi.list_competitions(GS_UPLOAD_PATH)
        except Exception as exc:
            read_error = str(exc)
    return templates.TemplateResponse(request, "import.html", {
        "active": "import",
        "competitions": competitions,
        "upload_exists": upload_exists,
        "error": error or read_error,
    })


@app.post("/import/upload")
async def import_upload(file: UploadFile = File(...)):
    content = await file.read()
    with open(GS_UPLOAD_PATH, "wb") as f:
        f.write(content)
    return RedirectResponse("/import", status_code=303)


@app.post("/import/create")
async def import_create(comp_no: int = Form(...)):
    from frontend import gs_import as gsi
    try:
        gsi.import_competition(GS_UPLOAD_PATH, comp_no, _db())
    except Exception as exc:
        return RedirectResponse(
            f"/import?error={urllib.parse.quote(str(exc))}", status_code=303
        )
    return RedirectResponse("/setup", status_code=303)


# ---------------------------------------------------------------------------
# Export — GliderScore CSV (15-field External Scoring System format)
# ---------------------------------------------------------------------------

_TASK_NO = {"F3K": 5, "F5K": 6}


@app.get("/export")
async def export_get(request: Request):
    db = _db()
    competitions = db.execute("SELECT * FROM competitions WHERE archived = 0 ORDER BY id DESC").fetchall()
    comp_data = []
    for comp in competitions:
        flight_count = db.execute(
            """SELECT COUNT(*) FROM flights f
               JOIN groups g ON g.id = f.group_id
               JOIN rounds r ON r.id = g.round_id
               WHERE r.competition_id = ?""",
            (comp["id"],),
        ).fetchone()[0]
        round_count = db.execute(
            "SELECT COUNT(*) FROM rounds WHERE competition_id = ?", (comp["id"],)
        ).fetchone()[0]
        comp_data.append({
            "comp": comp,
            "round_count": round_count,
            "flight_count": flight_count,
        })
    return templates.TemplateResponse(request, "export.html", {
        "active": "export",
        "comp_data": comp_data,
    })


@app.get("/export/{comp_id}/csv")
async def export_csv(comp_id: int):
    db = _db()
    comp = db.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    if not comp:
        return RedirectResponse("/export", status_code=303)

    comp_no = comp["gliderscore_comp_no"] or comp_id

    buf = io.StringIO()
    writer = csv.writer(buf)

    rounds = db.execute(
        "SELECT * FROM rounds WHERE competition_id = ? ORDER BY round_no", (comp_id,)
    ).fetchall()

    for rnd in rounds:
        task_no = _TASK_NO.get(rnd["discipline"], 5)
        groups = db.execute(
            "SELECT * FROM groups WHERE round_id = ? ORDER BY group_no", (rnd["id"],)
        ).fetchall()
        for grp in groups:
            pilots = db.execute(
                """SELECT p.id, p.name, p.gliderscore_pilot_no FROM pilots p
                   JOIN group_pilots gp ON gp.pilot_id = p.id
                   WHERE gp.group_id = ? ORDER BY p.name""",
                (grp["id"],),
            ).fetchall()
            for pilot in pilots:
                flights = db.execute(
                    """SELECT duration_ms FROM flights
                       WHERE pilot_id = ? AND group_id = ?
                       ORDER BY COALESCE(flight_no, 9999), recorded_at""",
                    (pilot["id"], grp["id"]),
                ).fetchall()
                times = [f["duration_ms"] for f in flights[:7]]
                data = [_gs_time(t) for t in times] + ["0"] * (7 - len(times))
                pilot_no = pilot["gliderscore_pilot_no"] or pilot["id"]
                writer.writerow([
                    comp_no, task_no,
                    rnd["round_no"], grp["group_no"], 0,
                    pilot_no,
                    *data,
                    0, pilot["name"],
                ])

    name_safe = comp["name"].replace(" ", "_")
    filename = f"{name_safe}_{comp['date']}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/{comp_id}/json")
async def export_json(comp_id: int):
    """JSON export for the Windows gs_sync bridge — includes all flight/altitude data."""
    db = _db()
    comp = db.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    if not comp:
        return {"error": "not found"}

    rounds_out = []
    for rnd in db.execute(
        "SELECT * FROM rounds WHERE competition_id = ? ORDER BY round_no", (comp_id,)
    ).fetchall():
        groups_out = []
        for grp in db.execute(
            "SELECT * FROM groups WHERE round_id = ? ORDER BY group_no", (rnd["id"],)
        ).fetchall():
            pilots_out = []
            for pilot in db.execute(
                """SELECT p.id, p.name, p.gliderscore_pilot_no FROM pilots p
                   JOIN group_pilots gp ON gp.pilot_id = p.id
                   WHERE gp.group_id = ? ORDER BY p.name""",
                (grp["id"],),
            ).fetchall():
                flights_out = [
                    {"duration_ms": f["duration_ms"], "altitude_m": f["altitude_m"]}
                    for f in db.execute(
                        """SELECT duration_ms, altitude_m FROM flights
                           WHERE pilot_id = ? AND group_id = ?
                           ORDER BY COALESCE(flight_no, 9999), recorded_at""",
                        (pilot["id"], grp["id"]),
                    ).fetchall()
                ]
                pilots_out.append({
                    "name": pilot["name"],
                    "gliderscore_pilot_no": pilot["gliderscore_pilot_no"],
                    "flights": flights_out,
                })
            groups_out.append({"group_no": grp["group_no"], "pilots": pilots_out})
        rounds_out.append({
            "round_no": rnd["round_no"],
            "task": rnd["task"],
            "discipline": rnd["discipline"],
            "working_time_s": rnd["working_time_s"],
            "groups": groups_out,
        })

    return {
        "name": comp["name"],
        "discipline": comp["discipline"],
        "date": comp["date"],
        "gliderscore_comp_no": comp["gliderscore_comp_no"],
        "rounds": rounds_out,
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except Exception:
        pass
    finally:
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Runner UI
# ---------------------------------------------------------------------------

def _disc_color(discipline: str) -> str:
    return {"F3K": "orange", "F5K": "fuchsia"}.get(discipline, "amber")


@app.get("/run")
async def run_get(request: Request, comps: str = None):
    """Operator screen. ?comps=1,2 shows only those competitions (max 2,
    side-by-side columns); no selection shows everything (discipline split)."""
    db = _db()
    all_comps = db.execute("SELECT * FROM competitions WHERE archived = 0 ORDER BY id DESC").fetchall()
    valid_ids = {c["id"] for c in all_comps}
    sel_ids: list = []
    if comps:
        for tok in comps.split(","):
            try:
                cid = int(tok)
            except ValueError:
                continue
            if cid in valid_ids and cid not in sel_ids:
                sel_ids.append(cid)
        sel_ids = sel_ids[:2]

    heats = []
    for comp in all_comps:
        if sel_ids and comp["id"] not in sel_ids:
            continue
        rounds = db.execute(
            "SELECT * FROM rounds WHERE competition_id = ? ORDER BY round_no",
            (comp["id"],),
        ).fetchall()
        for rnd in rounds:
            groups = db.execute(
                "SELECT * FROM groups WHERE round_id = ? ORDER BY group_no",
                (rnd["id"],),
            ).fetchall()
            for grp in groups:
                pilots = db.execute(
                    """SELECT p.name FROM pilots p
                       JOIN group_pilots gp ON gp.pilot_id = p.id
                       WHERE gp.group_id = ? ORDER BY p.name""",
                    (grp["id"],),
                ).fetchall()
                names = [r["name"] for r in pilots] + ["— TBD —"] * grp["dummy_count"]
                heats.append({
                    "comp_id": comp["id"],
                    "comp_name": comp["name"],
                    "discipline": rnd["discipline"],
                    "round_id": rnd["id"],
                    "round_no": rnd["round_no"],
                    "task": rnd["task"],
                    "working_time_s": rnd["working_time_s"],
                    "group_id": grp["id"],
                    "group_no": grp["group_no"],
                    "heat": chr(64 + grp["group_no"]),
                    "pilots": names,
                    "completed": bool(grp["completed"]),
                })

    # Queue columns: two selected comps -> one column each; otherwise split by
    # discipline when both are present (original behaviour), else one column.
    if len(sel_ids) == 2:
        by_comp = {c["id"]: c for c in all_comps}
        queue_cols = [{
            "label": by_comp[cid]["name"],
            "color": _disc_color(by_comp[cid]["discipline"]),
            "heats": [h for h in heats if h["comp_id"] == cid],
        } for cid in sel_ids]
    else:
        f3k = [h for h in heats if h["discipline"] == "F3K"]
        f5k = [h for h in heats if h["discipline"] == "F5K"]
        if f3k and f5k:
            queue_cols = [{"label": "F3K", "color": "orange", "heats": f3k},
                          {"label": "F5K", "color": "fuchsia", "heats": f5k}]
        else:
            queue_cols = [{"label": None, "color": _disc_color(
                heats[0]["discipline"] if heats else "F3K"), "heats": heats}]

    # Completed heats sink to the bottom of each column (stable sort keeps
    # round/heat order within each half) so the CD isn't scrolling past them.
    for col in queue_cols:
        col["heats"] = sorted(col["heats"], key=lambda h: h["completed"])

    sm = app.state.state_machine
    return templates.TemplateResponse(request, "run.html", {
        "active": "run",
        "heats": heats,
        "queue_cols": queue_cols,
        "comp_options": [
            {"id": c["id"], "name": c["name"], "discipline": c["discipline"]}
            for c in all_comps
        ],
        "sel_ids": sel_ids,
        "initial_state": json.dumps(sm.get_status()),
        "tasks": merged_tasks(db),
    })


@app.post("/api/run/load")
async def api_run_load(round_id: int, group_id: int, response: Response):
    """Load a heat. Refuses (409) unless the state machine is IDLE.

    The client gate is `:disabled="state !== 'IDLE'"` on websocket-derived state,
    so a tab whose socket dropped sees a permanent IDLE and the lock never
    engages. Loading a different heat mid-run swapped `_loaded` out from under the
    running sequence, so the round finished under the wrong pilots. The server has
    to be the one that refuses. [I-01]
    """
    sm = app.state.state_machine
    if sm.state != "IDLE":
        response.status_code = 409
        return {"ok": False, "error": f"A heat is running ({sm.state}) — abort it first",
                "status": sm.get_status()}
    if not await sm.load_heat(round_id, group_id):
        response.status_code = 404
        return {"ok": False, "error": "Heat not found", "status": sm.get_status()}
    return {"ok": True, "status": sm.get_status()}


@app.post("/api/run/start")
async def api_run_start(response: Response):
    ok, reason = await app.state.state_machine.start()
    if not ok:
        response.status_code = 409
    return {"ok": ok, "error": reason}


@app.post("/api/run/abort")
async def api_run_abort():
    await app.state.state_machine.abort()
    return {"ok": True}


@app.post("/api/run/skip")
async def api_run_skip(to: int = 60):
    """CD control: during PREP, skip the countdown to `to` seconds remaining."""
    ok = app.state.state_machine.skip_prep_to(to)
    return {"ok": ok}


async def _set_completed(group_id: int, completed: int, response: Response) -> dict:
    """Mark a heat done/not-done, and tell the leaderboard.

    The leaderboard has always listened for a 'complete' message that nothing
    broadcast, so marking a heat done left every scoreboard permanently stale
    until someone reloaded it by hand. [I-11]
    """
    db = _db()
    cur = db.execute("UPDATE groups SET completed = ? WHERE id = ?", (completed, group_id))
    db.commit()
    if not cur.rowcount:                                   # [I-10]
        response.status_code = 404
        return {"ok": False, "error": "That heat no longer exists"}
    await manager.broadcast({"type": "complete", "group_id": group_id,
                             "completed": bool(completed)})
    return {"ok": True}


@app.post("/api/run/complete")
async def api_run_complete(group_id: int, response: Response):
    return await _set_completed(group_id, 1, response)


@app.post("/api/run/uncomplete")
async def api_run_uncomplete(group_id: int, response: Response):
    return await _set_completed(group_id, 0, response)


@app.get("/api/run/state")
async def api_run_state():
    return app.state.state_machine.get_status()


@app.post("/api/run/flight/add")
async def api_run_flight_add(
    pilot_id: int = Form(...),
    duration: str = Form(...),
    altitude_m: str = Form(""),
):
    sm = app.state.state_machine
    if not sm._loaded:
        return {"ok": False, "error": "No heat loaded"}
    try:
        dur_ms = _parse_duration(duration)
    except (ValueError, TypeError):
        return {"ok": False, "error": "Invalid time — use M:SS or M:SS.HH"}
    try:
        alt = _parse_altitude(altitude_m)          # was unguarded → 500 [I-05]
    except (ValueError, TypeError):
        return {"ok": False, "error": ALTITUDE_HELP}
    d = sm._loaded
    valid_ids = [pid for pid, _ in d["pilot_id_names"]]
    if pilot_id not in valid_ids:
        return {"ok": False, "error": "Pilot not in this heat"}
    db = _db()
    group_id = d["group_id"]
    err = _duration_fits_round(db, group_id, dur_ms)   # [I-04]
    if err:
        return {"ok": False, "error": err}
    next_no = db.execute(
        "SELECT COALESCE(MAX(flight_no), 0) + 1 FROM flights WHERE pilot_id = ? AND group_id IS ?",
        (pilot_id, group_id),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO flights (pilot_id, duration_ms, group_id, flight_no, altitude_m, altitude_source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (pilot_id, dur_ms, group_id, next_no, alt, "cd_entry" if alt is not None else None),
    )
    db.commit()
    row = db.execute("SELECT name FROM pilots WHERE id = ?", (pilot_id,)).fetchone()
    pilot_name = row["name"] if row else f"Pilot {pilot_id}"
    await manager.broadcast({
        "type": "flight",
        "pilot_id": pilot_id,
        "pilot_name": pilot_name,
        "duration_ms": dur_ms,
        "round_no": d["round_no"],
        "heat": d["heat"],
    })
    return {"ok": True}


# ---------------------------------------------------------------------------
# Settings — audio output / Bluetooth speaker + timer diagnostics
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _apply_saved_volume():
    await engine.apply_saved_volume()


@app.get("/settings")
async def settings_get(request: Request):
    return templates.TemplateResponse(request, "settings.html", {"active": "settings"})


@app.get("/api/audio/status")
async def api_audio_status():
    # The whole settings audio panel is driven off this. If the bluetooth stack is
    # missing or wedged it should degrade to "unknown", not 500 the panel away —
    # the volume slider is how the CD fixes a silent field. [I-16]
    try:
        status = await audio_control.bt_status()
        status["volume"] = await audio_control.get_volume()
        status["saved_volume"] = audio_control.load_config().get("volume")
        status["lead_s"] = audio_control.get_lead()
        status["ok"] = True
        return status
    except Exception as exc:
        log.exception("audio status failed")
        return {"ok": False, "error": str(exc), "connected": False,
                "device": None, "volume": None, "saved_volume": None,
                "lead_s": audio_control.get_lead()}   # reads the config file, can't fail


@app.post("/api/audio/volume")
async def api_audio_volume(level: int):
    return await audio_control.set_volume(level)


@app.post("/api/audio/lead")
async def api_audio_lead(seconds: float):
    """Set the audio lead (seconds cues fire early to offset output latency)."""
    return audio_control.set_lead(seconds)


@app.post("/api/audio/test")
async def api_audio_test():
    engine.play_test()
    return {"ok": True}


@app.post("/api/bt/scan")
async def api_bt_scan(seconds: int = 8):
    return {"devices": await audio_control.bt_scan(seconds)}


@app.post("/api/bt/connect")
async def api_bt_connect(mac: str):
    return await audio_control.bt_connect(mac)


@app.post("/api/bt/disconnect")
async def api_bt_disconnect(mac: str):
    result = await audio_control.bt_disconnect(mac)
    if result["ok"]:
        cfg = audio_control.load_config()
        if cfg.get("bt_mac") == mac:
            cfg["bt_mac"] = None
            audio_control.save_config(cfg)
    return result


@app.get("/api/competitions")
async def api_competitions():
    """List all competitions — used by the Windows gs_sync GUI dropdown."""
    rows = _db().execute(
        "SELECT id, name, discipline, gliderscore_comp_no FROM competitions ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/downloads/{filename}")
async def downloads(filename: str):
    """Serve files from base_station/downloads/ (e.g. F3KSync.exe)."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    path = Path(__file__).resolve().parent.parent / "downloads" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)


@app.get("/api/timers")
async def api_timers():
    srv = app.state.server
    return {"timers": srv.timers_info(), "events": srv.recent_events()}


@app.get("/api/db/backup")
async def api_db_backup():
    import sqlite3
    db = _db()
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dest = sqlite3.connect(tmp_path)
        db.backup(dest)
        dest.close()
        data = Path(tmp_path).read_bytes()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    today = datetime.date.today().isoformat()
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="f3k_backup_{today}.db"'},
    )


@app.post("/api/db/restore")
async def api_db_restore(file: UploadFile = File(...)):
    import sqlite3
    from frontend.db import (_add_flight_columns, _migrate_competitions,
                             _migrate_groups, _migrate_pilots, _migrate_rounds)
    content = await file.read()
    if not content.startswith(b"SQLite format 3"):
        return {"ok": False, "error": "Not a valid SQLite database file"}
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    try:
        os.write(fd, content)
        os.close(fd)
        source = sqlite3.connect(tmp_path)
        target = _db()
        source.backup(target)
        source.close()
        _add_flight_columns(target)
        _migrate_groups(target)
        _migrate_pilots(target)
        _migrate_competitions(target)
        _migrate_rounds(target)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Scoring engine — leaderboard, public results, auto-draw, scoring config
# (SCORING_ENGINE_PROJECT.md Phases A/C/D/F/G — GliderScore now optional)
# ---------------------------------------------------------------------------

@app.get("/leaderboard")
async def leaderboard_get(request: Request, comp_id: int = None, discipline: str = None, kiosk: bool = False):
    db = _db()
    comps = db.execute("SELECT * FROM competitions WHERE archived = 0 ORDER BY id DESC").fetchall()
    # Comps are newest-first; default to the most recent active competition
    comp = next((c for c in comps if c["id"] == comp_id), comps[0] if comps else None)
    data = scoring.competition_standings(db, comp["id"], discipline or None) if comp else None
    return templates.TemplateResponse(request, "leaderboard.html", {
        "active": "leaderboard",
        "comps": comps,
        "comp": comp,
        "discipline": discipline or "",
        "data": data,
        "kiosk": kiosk,
    })


@app.get("/api/results/{comp_id}/public")
async def api_results_public(comp_id: int, discipline: str = None):
    """Unauthenticated JSON standings — proxied by glidetime.pawson.co.nz."""
    db = _db()
    comp = db.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    if comp is None:
        return {"error": "competition not found"}
    data = scoring.competition_standings(db, comp_id, discipline or None)
    return {
        "competition": {"id": comp["id"], "name": comp["name"],
                        "discipline": comp["discipline"], "date": comp["date"]},
        "rounds": data["rounds"],
        "drops_active": data["drops"],
        "standings": [
            {"rank": r["rank"], "pilot": r["name"], "total": r["total"],
             "rounds": {rn: rr for rn, rr in r["rounds"].items()}}
            for r in data["standings"]
        ],
    }


@app.post("/rounds/round/{round_id}/autodraw")
async def round_autodraw(round_id: int):
    """FAI draw: round 1 random, later rounds reverse-standings snake seeding."""
    db = _db()
    comp_id = _comp_id_of_round(db, round_id)
    if comp_id is None:
        return RedirectResponse("/rounds", status_code=303)
    if _gs_locked(db, comp_id):
        return RedirectResponse(f"/rounds?error={_GS_LOCK_MSG}", status_code=303)
    rnd = db.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
    groups = db.execute(
        "SELECT * FROM groups WHERE round_id = ? ORDER BY group_no", (round_id,)
    ).fetchall()
    if not groups:
        db.execute("INSERT INTO groups (round_id, group_no) VALUES (?, 1)", (round_id,))
        db.commit()
        groups = db.execute(
            "SELECT * FROM groups WHERE round_id = ? ORDER BY group_no", (round_id,)
        ).fetchall()
    pilots = [r["id"] for r in _comp_pilots(db, comp_id)]
    if not pilots:
        return RedirectResponse("/rounds", status_code=303)

    # Standings from contested rounds before this one (best-first); empty -> random
    prior = db.execute(
        "SELECT id, round_no FROM rounds WHERE competition_id = ? AND round_no < ? ORDER BY round_no",
        (comp_id, rnd["round_no"])).fetchall()
    round_scores = {}
    for pr in prior:
        scores = scoring.round_norm_scores(db, pr["id"])
        if scores:
            round_scores[pr["round_no"]] = scores
    order = None
    if round_scores:
        comp = db.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,)).fetchone()
        drop_at = [comp["drop1_at_round"], comp["drop2_at_round"], comp["drop3_at_round"]]
        order = [r["pilot_id"] for r in scoring.standings(round_scores, drop_at)]

    assignment = draw.draw_round(pilots, len(groups), standings_order=order)
    group_by_no = {i + 1: g["id"] for i, g in enumerate(groups)}
    for g in groups:
        db.execute("DELETE FROM group_pilots WHERE group_id = ?", (g["id"],))
    for pid, gno in assignment.items():
        db.execute("INSERT INTO group_pilots (group_id, pilot_id) VALUES (?, ?)",
                   (group_by_no[gno], pid))
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


def _flight_ordinal(db, flight_id: int) -> tuple:
    """(pilot_id, ordinal-within-group) for a flight — ordinal matches the run
    page flight log numbering (COALESCE(flight_no), recorded_at order)."""
    row = db.execute("SELECT pilot_id, group_id FROM flights WHERE id = ?",
                     (flight_id,)).fetchone()
    if row is None:
        return None, None
    ids = [r["id"] for r in db.execute(
        """SELECT id FROM flights WHERE pilot_id = ? AND group_id IS ?
           ORDER BY COALESCE(flight_no, 9999), recorded_at""",
        (row["pilot_id"], row["group_id"])).fetchall()]
    return row["pilot_id"], ids.index(flight_id) + 1


@app.get("/api/run/altitudes")
async def api_run_altitudes(group_id: int = None):
    """Flights of a heat for the F5K CD altitude entry panel (B1). The client
    passes its group_id explicitly so the panel keeps working after the round
    ends (the state machine clears its loaded heat at IDLE)."""
    db = _db()
    if group_id is None:
        sm = app.state.server.state_machine
        loaded = sm._loaded if sm else None
        if not loaded or not loaded.get("group_id"):
            return {"ok": False, "error": "No heat loaded"}
        group_id = loaded["group_id"]
    rnd = db.execute(
        "SELECT r.* FROM rounds r JOIN groups g ON g.round_id = r.id WHERE g.id = ?",
        (group_id,)).fetchone()
    comp = db.execute("SELECT * FROM competitions WHERE id = ?",
                      (rnd["competition_id"],)).fetchone()
    ref = rnd["ref_height_m"] if rnd["ref_height_m"] is not None else comp["f5k_ref_height"]
    flights = db.execute(
        """SELECT f.id, f.pilot_id, p.name AS pilot_name, f.duration_ms,
                  f.altitude_m, f.altitude_source
           FROM flights f JOIN pilots p ON p.id = f.pilot_id
           WHERE f.group_id = ?
           ORDER BY p.name, COALESCE(f.flight_no, 9999), f.recorded_at""",
        (group_id,)).fetchall()
    return {"ok": True, "ref_height": ref,
            "min_time_s": comp["f5k_min_time_for_bonus"],
            "flights": [dict(f) for f in flights]}


@app.post("/api/run/altitude/set")
async def api_run_altitude_set(flight_id: int = Form(...), altitude_m: str = Form("")):
    """CD altitude entry/correction (B1/E1) — tagged 'cd_entry' for the audit trail."""
    db = _db()
    try:
        alt = _parse_altitude(altitude_m)          # was unguarded → 500 [I-05]
    except (ValueError, TypeError):
        return {"ok": False, "error": ALTITUDE_HELP}
    if db.execute("SELECT 1 FROM flights WHERE id = ?", (flight_id,)).fetchone() is None:
        return {"ok": False, "error": "That flight no longer exists"}
    if alt is None:
        db.execute("UPDATE flights SET altitude_m = NULL, altitude_source = NULL WHERE id = ?",
                   (flight_id,))
    else:
        db.execute("UPDATE flights SET altitude_m = ?, altitude_source = 'cd_entry' WHERE id = ?",
                   (alt, flight_id))
    db.commit()
    pilot_id, ordinal = _flight_ordinal(db, flight_id)
    if pilot_id is not None and alt is not None:
        await manager.broadcast({"type": "altitude", "pilot_id": pilot_id,
                                 "flight_no": ordinal, "altitude_m": alt})
    return {"ok": True}


@app.get("/api/run/scores")
async def api_run_scores(group_id: int):
    """Raw/normalised score preview for a heat (C3) — computed on demand."""
    db = _db()
    scored = scoring.score_group_db(db, group_id)
    return {"ok": True, "scores": {
        pid: {"raw": p["total"], "norm": p["norm"], "rank": p["rank"]}
        for pid, p in scored["pilots"].items()
    }}


@app.post("/setup/pilots/assign")
async def setup_pilots_assign(comp_id: int = Form(...), pilot_ids: list[int] = Form(...)):
    """Bind pilots selected in the registry panel to a competition."""
    db = _db()
    if _gs_locked(db, comp_id):
        return RedirectResponse(
            f"/setup?msg={urllib.parse.quote('That competition is GS-locked — pilots are managed in GliderScore')}",
            status_code=303)
    for pid in pilot_ids:
        db.execute(
            "INSERT OR IGNORE INTO competition_pilots (competition_id, pilot_id) VALUES (?, ?)",
            (comp_id, pid))
    db.commit()
    comp = db.execute("SELECT name FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    msg = f"{len(pilot_ids)} pilot(s) added to {comp['name'] if comp else 'competition'}"
    return RedirectResponse(f"/setup?msg={urllib.parse.quote(msg)}", status_code=303)


@app.post("/setup/competition/{comp_id}/archive")
async def competition_archive(comp_id: int):
    db = _db()
    db.execute("UPDATE competitions SET archived = 1 WHERE id = ?", (comp_id,))
    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/competition/{comp_id}/unarchive")
async def competition_unarchive(comp_id: int):
    db = _db()
    db.execute("UPDATE competitions SET archived = 0 WHERE id = ?", (comp_id,))
    db.commit()
    return RedirectResponse("/setup", status_code=303)


# ---------------------------------------------------------------------------
# Custom tasks — clone a catalogue task and adjust its rule settings
# ---------------------------------------------------------------------------

def _parse_targets(s: str) -> list:
    """'1:00, 90, 2:30' -> [60.0, 90.0, 150.0]."""
    out = []
    for tok in (s or "").replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            m, sec = tok.split(":", 1)
            out.append(float(m) * 60 + float(sec))
        else:
            out.append(float(tok))
    return out


@app.get("/api/tasks/rule")
async def api_task_rule(discipline: str, task: str):
    """Rule parameters of a task — prefills the clone form."""
    letter, variant = scoring.parse_task(task)
    rules = scoring.DISCIPLINE_RULES.get(discipline, {})
    rule = rules.get((letter, variant)) or rules.get((letter, None))
    if rule is None:
        return {"ok": False, "error": "Unknown task"}
    info = merged_tasks(_db()).get(discipline, {}).get(letter, {})
    return {"ok": True, "kind": rule.kind, "n": rule.n, "cap_s": rule.cap_s,
            "targets": list(rule.targets_s), "start_s": rule.start_s,
            "step_s": rule.step_s, "max_flights": rule.max_flights,
            "name": info.get("name", ""), "desc": info.get("desc", ""),
            "wt_min": info.get("wt_min", 10)}


@app.post("/tasks/custom/add")
async def custom_task_add(
    discipline: str = Form(...), code: str = Form(...), name: str = Form(...),
    descr: str = Form(""), kind: str = Form(...), n: int = Form(0),
    cap_s: str = Form("0"), targets: str = Form(""), start_s: str = Form("0"),
    step_s: str = Form("0"), max_flights: int = Form(0), wt_min: int = Form(10),
    based_on: str = Form(""),
):
    def fail(msg):
        return RedirectResponse(f"/rounds?error={urllib.parse.quote(msg)}", status_code=303)

    code = code.strip().upper()
    db = _db()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,3}", code):
        return fail("Task code must be 1-4 characters, letters/digits, starting with a letter")
    if code in TASKS.get(discipline, {}) or scoring.parse_task(code)[0] in TASKS.get(discipline, {}):
        return fail(f"Code {code} clashes with a built-in {discipline} task")
    if kind not in RULE_KINDS:
        return fail("Unknown rule type")
    try:
        cap = _parse_targets(cap_s)[0] if cap_s.strip() else 0.0
        start = _parse_targets(start_s)[0] if start_s.strip() else 0.0
        step = _parse_targets(step_s)[0] if step_s.strip() else 0.0
        tgts = _parse_targets(targets)
    except ValueError:
        return fail("Times must be seconds or M:SS (e.g. 180 or 3:00)")
    if kind in ("targets", "sequence") and not tgts:
        return fail("This rule type needs at least one target time")
    if kind in ("last_n", "best_n", "first_n", "poker") and n < 1:
        return fail("This rule type needs N of at least 1")
    try:
        db.execute(
            """INSERT INTO custom_tasks (discipline, code, name, descr, kind, n,
               cap_s, targets, start_s, step_s, max_flights, wt_min, based_on)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (discipline, code, name.strip(), descr.strip(), kind, n, cap,
             json.dumps(tgts), start, step, max_flights, wt_min,
             based_on.strip() or None))
        db.commit()
    except Exception:
        return fail(f"A {discipline} custom task with code {code} already exists")
    scoring.load_custom_rules(db)
    return RedirectResponse("/rounds", status_code=303)


@app.post("/tasks/custom/{task_id}/delete")
async def custom_task_delete(task_id: int):
    db = _db()
    row = db.execute("SELECT * FROM custom_tasks WHERE id = ?", (task_id,)).fetchone()
    if row:
        used = db.execute(
            "SELECT COUNT(*) FROM rounds WHERE task = ? AND discipline = ?",
            (row["code"], row["discipline"])).fetchone()[0]
        if used:
            msg = (f"Task {row['code']} is used by {used} round(s) — "
                   "delete or redraw those rounds first")
            return RedirectResponse(
                f"/rounds?error={urllib.parse.quote(msg)}", status_code=303)
        db.execute("DELETE FROM custom_tasks WHERE id = ?", (task_id,))
        db.commit()
        scoring.load_custom_rules(db)
    return RedirectResponse("/rounds", status_code=303)


@app.post("/tasks/custom/{task_id}/edit")
async def custom_task_edit(
    task_id: int,
    name: str = Form(...), descr: str = Form(""), n: int = Form(0),
    cap_s: str = Form("0"), targets: str = Form(""), start_s: str = Form("0"),
    step_s: str = Form("0"), max_flights: int = Form(0), wt_min: int = Form(10),
):
    def fail(msg):
        return RedirectResponse(f"/rounds?error={urllib.parse.quote(msg)}", status_code=303)

    db = _db()
    row = db.execute("SELECT * FROM custom_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return fail("Custom task not found")
    kind = row["kind"]
    try:
        cap = _parse_targets(cap_s)[0] if cap_s.strip() else 0.0
        start = _parse_targets(start_s)[0] if start_s.strip() else 0.0
        step = _parse_targets(step_s)[0] if step_s.strip() else 0.0
        tgts = _parse_targets(targets)
    except ValueError:
        return fail("Times must be seconds or M:SS (e.g. 180 or 3:00)")
    if kind in ("targets", "sequence") and not tgts:
        return fail("This rule type needs at least one target time")
    if kind in ("last_n", "best_n", "first_n", "poker") and n < 1:
        return fail("This rule type needs N of at least 1")
    db.execute(
        """UPDATE custom_tasks SET name=?, descr=?, n=?, cap_s=?, targets=?,
           start_s=?, step_s=?, max_flights=?, wt_min=? WHERE id=?""",
        (name.strip(), descr.strip(), n, cap, json.dumps(tgts),
         start, step, max_flights, wt_min, task_id))
    db.commit()
    scoring.load_custom_rules(db)
    return RedirectResponse("/rounds", status_code=303)


@app.on_event("startup")
async def _load_custom_task_rules():
    try:
        scoring.load_custom_rules(_db())
    except Exception:
        pass  # table exists after init_db; never block startup


SYSTEM_CONFIG_VERSION_FILE = Path("/var/lib/f3k/system-config.version")


def _system_config_versions():
    """(target, applied) system-config versions, or (None, None) if unavailable."""
    root = _git_root()
    if root is None:
        return None, None
    script = root / "setup" / "apply-system-config.sh"
    if not script.is_file():
        return None, None
    target = None
    try:
        for line in script.read_text().splitlines():
            if line.startswith("CONFIG_VERSION="):
                target = line.split("=", 1)[1].strip()
                break
    except Exception:
        return None, None
    try:
        applied = SYSTEM_CONFIG_VERSION_FILE.read_text().strip()
    except Exception:
        applied = None
    return target, applied


@app.on_event("startup")
async def _apply_system_config_if_stale():
    """Apply OS-level config when the version on disk is behind the repo.

    This is what makes a single "Update from GitHub" click enough. The running
    server is the process that performs the git pull, so config applied only
    from the update endpoint would always be one release behind — the operator
    would have to click Update twice, and the second click is exactly the one a
    remote tester would never think to make. Checking here covers that, plus
    reboots and restored images.

    Skipped entirely when already at the target version, so a healthy Pi does no
    work and restarts nothing on boot.
    """
    import asyncio
    import subprocess

    target, applied = _system_config_versions()
    if target is None or target == applied:
        return

    root = _git_root()
    script = root / "setup" / "apply-system-config.sh"

    def _run():
        try:
            proc = subprocess.run(
                ["sudo", "-n", "bash", str(script)],
                capture_output=True, text=True, timeout=180,
            )
            status = "ok" if proc.returncode == 0 else "FAILED (rolled back)"
            print(f"[startup] system config v{applied or 'none'} -> v{target}: {status}",
                  flush=True)
            if proc.returncode != 0:
                print((proc.stdout or proc.stderr or "").strip()[-2000:], flush=True)
        except Exception as exc:
            print(f"[startup] system config apply error: {exc}", flush=True)

    # Off the event loop: subprocess.run blocks, and this must never delay the
    # server becoming reachable — losing the web UI is how a remote Pi becomes
    # unrecoverable.
    asyncio.get_running_loop().run_in_executor(None, _run)


# ---------------------------------------------------------------------------
# Draw Wizard — multi-round roster draw with preview / accept
# ---------------------------------------------------------------------------

def _comp_round_groups(db, comp_id: int) -> list:
    """Existing rounds with their group pilot lists and flight status."""
    out = []
    for rnd in db.execute(
            "SELECT * FROM rounds WHERE competition_id = ? ORDER BY round_no",
            (comp_id,)).fetchall():
        groups = []
        has_flights = False
        for grp in db.execute(
                "SELECT * FROM groups WHERE round_id = ? ORDER BY group_no",
                (rnd["id"],)).fetchall():
            pids = [r["pilot_id"] for r in db.execute(
                "SELECT pilot_id FROM group_pilots WHERE group_id = ?",
                (grp["id"],)).fetchall()]
            n = db.execute("SELECT COUNT(*) FROM flights WHERE group_id = ?",
                           (grp["id"],)).fetchone()[0]
            has_flights = has_flights or n > 0
            groups.append({"group_no": grp["group_no"], "pilot_ids": pids,
                           "completed": bool(grp["completed"])})
        out.append({"round_id": rnd["id"], "round_no": rnd["round_no"],
                    "task": rnd["task"], "discipline": rnd["discipline"],
                    "wt_min": rnd["working_time_s"] // 60,
                    "groups": groups, "has_flights": has_flights})
    return out


@app.get("/api/draw/context")
async def api_draw_context(comp_id: int):
    """Everything the Draw Wizard needs: pilots, existing rounds with flight
    status, and the first round that can safely be redrawn."""
    db = _db()
    comp = db.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    if comp is None:
        return {"ok": False, "error": "Competition not found"}
    pilots = [{"id": r["id"], "name": r["name"]} for r in _comp_pilots(db, comp_id)]
    rounds = _comp_round_groups(db, comp_id)
    # Earliest safe redraw point: after the last round with flights recorded
    suggested = max((r["round_no"] for r in rounds if r["has_flights"]), default=0) + 1
    return {"ok": True, "locked": _gs_locked(db, comp_id),
            "discipline": comp["discipline"], "pilots": pilots,
            "rounds": rounds, "suggested_start": suggested}


@app.post("/api/draw/preview")
async def api_draw_preview(request: Request):
    """Generate a draw proposal. Body: {comp_id, start_round, num_rounds,
    groups_per_round, avoid_back_to_back}. Rounds before start_round are kept
    and seed the pair-meeting matrix; the round immediately before feeds the
    back-to-back check."""
    try:
        body = await request.json()
        comp_id = int(body["comp_id"])
        start_round = int(body.get("start_round", 1))
        num_rounds = int(body["num_rounds"])
        groups_n = int(body["groups_per_round"])
        avoid = bool(body.get("avoid_back_to_back", True))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"Bad draw request: {exc}"}   # [I-07]
    if num_rounds < 1 or groups_n < 1 or start_round < 1:
        return {"ok": False, "error": "Rounds, groups and start round must be 1 or more"}

    db = _db()
    pilots = [r["id"] for r in _comp_pilots(db, comp_id)]
    if len(pilots) < groups_n:
        return {"ok": False, "error": "More groups than pilots"}

    kept = [r for r in _comp_round_groups(db, comp_id) if r["round_no"] < start_round]
    history = [[g["pilot_ids"] for g in r["groups"]] for r in kept]
    prev_last = (history[-1][-1] if history and history[-1] else [])

    try:
        res = draw.draw_competition(
            pilots, num_rounds, groups_n, avoid_back_to_back=avoid,
            history=history, prev_last_group=prev_last)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    names = {r["id"]: r["name"] for r in db.execute(
        "SELECT id, name FROM pilots").fetchall()}
    return {"ok": True, "stats": res["stats"], "rounds": [
        [[{"id": pid, "name": names.get(pid, f"Pilot {pid}")} for pid in grp]
         for grp in rnd]
        for rnd in res["rounds"]
    ]}


@app.post("/api/draw/accept")
async def api_draw_accept(request: Request):
    """Write an accepted draw. Body: {comp_id, start_round, rounds: [{task,
    discipline, wt_min, groups: [[pilot_id, ...], ...]}, ...]}. Replaces all
    rounds from start_round onward; refuses if any of those have flights."""
    try:
        body = await request.json()
        comp_id = int(body["comp_id"])
        start_round = int(body["start_round"])
        new_rounds = body["rounds"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"Bad draw request: {exc}"}   # [I-07]
    if start_round < 1:
        return {"ok": False, "error": "Start round must be 1 or more"}
    if not isinstance(new_rounds, list) or not new_rounds:
        return {"ok": False, "error": "No rounds in the accepted draw"}

    db = _db()
    comp = db.execute("SELECT * FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    if comp is None or _gs_locked(db, comp_id):
        return {"ok": False, "error": "Competition locked or not found"}
    for rd in new_rounds:
        if not isinstance(rd, dict) or rd.get("discipline") not in ("F3K", "F5K") \
                or not str(rd.get("task", "")).strip():
            return {"ok": False, "error": "Each round needs a discipline and task"}
        if not isinstance(rd.get("groups"), list) or not rd["groups"]:
            return {"ok": False, "error": "Each round needs at least one group"}
        try:
            wt_min = int(rd.get("wt_min", 10))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Working time must be a whole number of minutes"}
        if not 1 <= wt_min <= 60:
            return {"ok": False, "error": "Working time must be between 1 and 60 minutes"}
        try:
            [int(pid) for grp in rd["groups"] for pid in grp]
        except (TypeError, ValueError):
            return {"ok": False, "error": "Group pilot IDs must be numeric"}

    n_flights = db.execute(
        """SELECT COUNT(*) FROM flights f
           JOIN groups g ON g.id = f.group_id
           JOIN rounds r ON r.id = g.round_id
           WHERE r.competition_id = ? AND r.round_no >= ?""",
        (comp_id, start_round)).fetchone()[0]
    if n_flights:
        return {"ok": False,
                "error": f"Rounds from {start_round} already have {n_flights} "
                         "flight(s) recorded — pick a later start round"}

    old = db.execute(
        "SELECT id FROM rounds WHERE competition_id = ? AND round_no >= ?",
        (comp_id, start_round)).fetchall()
    for r in old:
        db.execute("DELETE FROM group_pilots WHERE group_id IN "
                   "(SELECT id FROM groups WHERE round_id = ?)", (r["id"],))
        db.execute("DELETE FROM groups WHERE round_id = ?", (r["id"],))
        db.execute("DELETE FROM rounds WHERE id = ?", (r["id"],))

    for i, rd in enumerate(new_rounds):
        db.execute(
            "INSERT INTO rounds (competition_id, round_no, task, working_time_s, discipline)"
            " VALUES (?, ?, ?, ?, ?)",
            (comp_id, start_round + i, rd["task"],
             int(rd.get("wt_min", 10)) * 60, rd["discipline"]))
        rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        for gno, pids in enumerate(rd["groups"], start=1):
            db.execute("INSERT INTO groups (round_id, group_no) VALUES (?, ?)", (rid, gno))
            gid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for pid in pids:
                db.execute("INSERT INTO group_pilots (group_id, pilot_id) VALUES (?, ?)",
                           (gid, int(pid)))
    db.commit()
    return {"ok": True, "rounds_written": len(new_rounds)}


@app.post("/setup/competition/{comp_id}/scoring")
async def setup_scoring_config(
    comp_id: int,
    drop1_at_round: int = Form(99),
    drop2_at_round: int = Form(99),
    drop3_at_round: int = Form(99),
    f5k_ref_height: float = Form(60),
    f5k_min_time_for_bonus: int = Form(30),
):
    db = _db()
    db.execute(
        """UPDATE competitions SET drop1_at_round = ?, drop2_at_round = ?,
           drop3_at_round = ?, f5k_ref_height = ?, f5k_min_time_for_bonus = ?
           WHERE id = ?""",
        (drop1_at_round or 99, drop2_at_round or 99, drop3_at_round or 99,
         f5k_ref_height, f5k_min_time_for_bonus, comp_id),
    )
    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/pilots/import")
async def setup_pilots_import(file: UploadFile = File(...)):
    """Bulk pilot import from a CSV roster (Phase G2).

    Accepts either one name per line, or FirstName,LastName[,FAINumber,...]
    columns (standard entry-form export). Skips duplicates by FAI number when
    present, otherwise by exact name.
    """
    raw = await file.read()
    # An .xlsx picked by mistake is binary: NUL bytes make csv.reader raise, so
    # the CD got a 500 instead of "that's not a CSV". [I-09]
    if b"\x00" in raw[:4096]:
        return RedirectResponse(
            f"/setup?msg={urllib.parse.quote('That file is not a CSV — export the roster as CSV and try again')}",
            status_code=303)
    content = raw.decode("utf-8-sig", errors="replace")
    db = _db()
    existing_names = {r["name"].strip().lower()
                      for r in db.execute("SELECT name FROM pilots").fetchall()}
    existing_fai = {str(r["fai_number"])
                    for r in db.execute(
                        "SELECT fai_number FROM pilots WHERE fai_number IS NOT NULL"
                    ).fetchall()}
    added = skipped = 0
    for row in csv.reader(io.StringIO(content)):
        cells = [c.strip() for c in row if c.strip()]
        if not cells:
            continue
        low = [c.lower() for c in cells]
        if "name" in low or "firstname" in low or "first name" in low:
            continue  # header row
        if len(cells) == 1:
            name, fai = cells[0], None
        else:
            name = f"{cells[0]} {cells[1]}"
            fai = cells[2] if len(cells) > 2 and cells[2] else None
        if (fai and fai in existing_fai) or name.lower() in existing_names:
            skipped += 1
            continue
        db.execute("INSERT INTO pilots (name, fai_number) VALUES (?, ?)", (name, fai))
        existing_names.add(name.lower())
        if fai:
            existing_fai.add(fai)
        added += 1
    db.commit()
    return RedirectResponse(
        f"/setup?msg={urllib.parse.quote(f'Imported {added} pilots ({skipped} duplicates skipped)')}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Pilot view — read-only phone page for pilots on the F3K_OPS WiFi
# ---------------------------------------------------------------------------

@app.get("/pilot")
async def pilot_get(request: Request):
    return templates.TemplateResponse(request, "pilot.html", {
        "tasks": merged_tasks(_db()),
    })


# ---------------------------------------------------------------------------
# System info, base-station git update, and timer OTA firmware server
# ---------------------------------------------------------------------------

OTA_DIR = Path.home() / "f3k_timer_ota"
FW_REPO = "gadjt12a/F3K_Timer"     # firmware lives in its own repo, not this one


def _github_head_sha(repo: str, branch: str = "main") -> str:
    """Current commit of `branch`, resolved through the API rather than raw.

    Fetching `raw.githubusercontent.com/<repo>/main/...` is what this used to do,
    and it is served through a CDN that holds a stale copy for several minutes
    after a push. Clicking "Update from GitHub" straight after a firmware release
    therefore downloaded the **previous** firmware and reported success — an
    entirely silent wrong-version install. Observed on 2026-07-27: raw still
    served fw-v15 while the API already had fw-v16.

    Commit-pinned raw URLs are immutable, so they are never stale. The API call
    that resolves the sha is not CDN-cached.

    Falls back to the branch name so a rate-limited or unreachable API degrades
    to the old behaviour rather than blocking the update entirely.
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/commits/{branch}",
            headers={"Accept": "application/vnd.github.sha",
                     "User-Agent": "f3k-base-station"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            sha = resp.read().decode().strip()
        # Accept only something that actually looks like a sha — an error body
        # pinned into a URL would fail confusingly much later.
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
            return sha
        log.warning("[OTA] unexpected sha response for %s: %.60s", repo, sha)
    except Exception as exc:
        log.warning("[OTA] could not resolve %s@%s via API (%s) — "
                    "falling back to the branch, which may be CDN-stale", repo, branch, exc)
    return branch


def _git_root():
    import subprocess
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=Path(__file__).parent,
        )
        return Path(r.stdout.strip()) if r.returncode == 0 else None
    except FileNotFoundError:
        return None


def _ota_version() -> str | None:
    ver_path = OTA_DIR / "version.json"
    try:
        return json.loads(ver_path.read_text()).get("version") if ver_path.exists() else None
    except Exception:
        return None


@app.get("/api/system/info")
async def api_system_info():
    import subprocess
    root = _git_root()
    ota_version = _ota_version()
    if root is None:
        return {"git": False, "ota_version": ota_version}
    r = subprocess.run(
        ["git", "log", "-1", "--format=%h|%s|%ci"],
        capture_output=True, text=True, cwd=root,
    )
    if r.returncode != 0:
        return {"git": False, "ota_version": ota_version}
    parts = r.stdout.strip().split("|", 2)
    rev = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, cwd=root,
    )
    build_no = rev.stdout.strip() if rev.returncode == 0 else None
    return {"git": True, "commit": parts[0], "message": parts[1],
            "date": parts[2][:10] if len(parts) > 2 else "",
            "build_no": build_no,
            "ota_version": ota_version}


@app.post("/api/system/update")
async def api_system_update(background_tasks: BackgroundTasks):
    import subprocess
    root = _git_root()
    if root is None:
        return {"ok": False, "error": "Not a git repository — run the migration script first."}

    # Fetch + hard reset so locally-modified tracked files never block the update
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=root,
    ).stdout.strip()
    fetch = subprocess.run(
        ["git", "fetch", "origin", "main"], capture_output=True, text=True, cwd=root,
    )
    if fetch.returncode != 0:
        return {"ok": False, "error": (fetch.stderr or fetch.stdout).strip()}
    reset = subprocess.run(
        ["git", "reset", "--hard", "origin/main"], capture_output=True, text=True, cwd=root,
    )
    if reset.returncode != 0:
        return {"ok": False, "error": (reset.stderr or reset.stdout).strip()}
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=root,
    ).stdout.strip()
    changed = before != after
    if changed:
        for py in (root / "base_station").rglob("*.py"):
            py.touch()

    # Bring the Pi's OS-level config (hostapd, dnsmasq, watchdog, modprobe) up to
    # spec. Without this an update only ever ships app code, and every Pi-side fix
    # has to be applied by hand over SSH to each unit — which is how the fleet
    # drifted apart in the first place.
    #
    # Runs on every update rather than only when the repo moved: the script is
    # idempotent and a no-op on a box already at spec, so it also self-heals a Pi
    # that drifted for any other reason. Non-fatal — the app code is already
    # updated, and a failed apply rolls its own config back, so report it rather
    # than failing the whole update.
    sys_cfg = {"ran": False, "ok": None, "output": "", "error": None}
    applier = root / "setup" / "apply-system-config.sh"
    if applier.is_file():
        try:
            proc = subprocess.run(
                ["sudo", "-n", "bash", str(applier)],
                capture_output=True, text=True, timeout=180,
            )
            sys_cfg["ran"] = True
            sys_cfg["ok"] = proc.returncode == 0
            sys_cfg["output"] = (proc.stdout or "").strip()[-4000:]
            if proc.returncode != 0:
                sys_cfg["error"] = (proc.stderr or proc.stdout or "").strip()[-1000:]
        except Exception as exc:
            sys_cfg["error"] = str(exc)

    # Sync timer OTA firmware files (non-fatal if no internet at the field)
    ota_version = None
    ota_error = None
    ota_ref = None
    try:
        OTA_DIR.mkdir(exist_ok=True)
        ota_ref = _github_head_sha(FW_REPO, "main")
        for fname in ("firmware.bin", "version.json"):
            # Download beside the target, then rename. wget -O writes straight to
            # the destination, so a transfer that died halfway used to leave a
            # truncated firmware.bin in place — which a timer would then happily
            # flash. rename(2) within one directory is atomic.
            tmp = OTA_DIR / f"{fname}.new"
            r = subprocess.run(
                ["wget", "-q", "-O", str(tmp),
                 f"https://raw.githubusercontent.com/{FW_REPO}/{ota_ref}/firmware/ota/{fname}"],
                capture_output=True, timeout=120,
            )
            if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"download of {fname} failed (no internet?)")
            tmp.replace(OTA_DIR / fname)
        ota_version = _ota_version()
    except Exception as exc:
        ota_error = str(exc)

    if changed:
        background_tasks.add_task(_restart_after_update)

    return {
        "ok": True, "changed": changed, "output": reset.stdout.strip(),
        "ota": {"version": ota_version, "error": ota_error, "ref": ota_ref},
        "system_config": sys_cfg,
    }


async def _restart_after_update():
    import asyncio, subprocess
    await asyncio.sleep(2)
    subprocess.run(["sudo", "systemctl", "restart", "f3k-server"])


@app.get("/ota/version.json")
async def ota_version_json():
    path = OTA_DIR / "version.json"
    if not path.exists():
        raise HTTPException(404, "No firmware cached — run an update first")
    return Response(content=path.read_bytes(), media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@app.get("/ota/firmware.bin")
async def ota_firmware_bin():
    path = OTA_DIR / "firmware.bin"
    if not path.exists():
        raise HTTPException(404, "No firmware cached — run an update first")
    return FileResponse(str(path), media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Captive portal — F3K_OPS network (wlan0, 192.168.20.0/24)
# dnsmasq resolves all DNS to 192.168.20.1; nftables redirects :80 → :8080.
# OS captive-portal probes land here as unrecognised paths → redirect to the
# read-only /pilot page (phones can't drive /run, and it exposes CD controls).
# All named routes above take priority; only truly unknown GET paths reach this.
# ---------------------------------------------------------------------------

@app.get("/ws")
async def ws_http_probe(request: Request):
    # A websocket handshake only lands here as plain HTTP when something between the
    # browser and uvicorn dropped the Upgrade header — in practice a reverse proxy
    # without websocket support. Without this route it falls through to the captive
    # portal catch-all and the browser sees a meaningless 302 to /pilot.
    import logging
    logging.getLogger("uvicorn.error").warning(
        "[WS] handshake arrived as plain HTTP (Upgrade stripped) from host=%s via=%s "
        "— reverse proxy needs websocket support",
        request.headers.get("host"), request.headers.get("x-forwarded-for"))
    return Response(status_code=426)


@app.get("/favicon.ico")
async def favicon():
    # Without this the browser's favicon probe falls through to the captive-portal
    # catch-all and gets redirected to /pilot on every page load — pure log noise.
    return Response(status_code=204)


@app.get("/{path:path}")
async def captive_portal_catchall(path: str, request: Request):
    # OPS WiFi captive portal: redirect OS probes to the pilot page via the AP's
    # address. All other clients (ethernet, home network, external proxy) get a
    # plain relative redirect.
    client = request.client.host if request.client else ""
    if client.startswith("192.168.20."):
        return RedirectResponse(url="http://192.168.20.1:8080/pilot", status_code=302)
    return RedirectResponse(url="/pilot", status_code=302)
