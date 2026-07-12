import csv
import io
import json
import os
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from frontend import audio_control
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


manager = ConnectionManager()

app = FastAPI(title="Glide Base")
app.state.ws_manager = manager
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


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
    total_ms = (int(m) * 60 + int(sec)) * 1000 + int(centis) * 10
    if total_ms <= 0:
        raise ValueError("Duration must be positive")
    return total_ms


templates.env.filters["fmt_ms"] = _fmt_ms

GS_UPLOAD_PATH = os.path.expanduser("~/f3k_base/gs_upload.mdb")


def _db():
    return app.state.server.db


@app.get("/health")
async def health():
    return {"status": "ok", "timers_connected": len(app.state.server._clients)}


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@app.get("/setup")
async def setup_get(request: Request):
    db = _db()
    pilots = db.execute("SELECT * FROM pilots ORDER BY name").fetchall()
    competitions = db.execute("SELECT * FROM competitions ORDER BY id").fetchall()

    # For each competition, attach its pilot list
    comp_data = []
    for comp in competitions:
        comp_pilots = db.execute(
            """SELECT p.id, p.name FROM pilots p
               JOIN competition_pilots cp ON cp.pilot_id = p.id
               WHERE cp.competition_id = ? ORDER BY p.name""",
            (comp["id"],),
        ).fetchall()
        # Pilots not yet in this competition (available to add)
        in_ids = {r["id"] for r in comp_pilots}
        available = [p for p in pilots if p["id"] not in in_ids]
        comp_data.append({"comp": comp, "pilots": comp_pilots, "available": available})

    return templates.TemplateResponse(request, "setup.html", {
        "active": "setup",
        "all_pilots": pilots,
        "comp_data": comp_data,
    })


@app.post("/setup/competition/new")
async def competition_new(
    name: str = Form(...),
    discipline: str = Form(...),
    date: str = Form(...),
    gliderscore_comp_no: str = Form(""),
    prep_time_s: int = Form(180),
    land_time_s: int = Form(30),
    heat_gap_s: int = Form(30),
    round_gap_s: int = Form(30),
    focus_time_s: int = Form(45),
    count_last_s: int = Form(15),
):
    db = _db()
    comp_no = int(gliderscore_comp_no) if gliderscore_comp_no.strip() else None
    db.execute(
        """INSERT INTO competitions
           (name, discipline, date, gliderscore_comp_no,
            prep_time_s, land_time_s, heat_gap_s, round_gap_s, focus_time_s, count_last_s)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, discipline, date, comp_no,
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
    db.execute(
        "INSERT OR IGNORE INTO competition_pilots (competition_id, pilot_id) VALUES (?, ?)",
        (comp_id, pilot_id),
    )
    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/competition/{comp_id}/pilot/{pilot_id}/remove")
async def competition_pilot_remove(comp_id: int, pilot_id: int):
    db = _db()
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


# ---------------------------------------------------------------------------
# Rounds
# ---------------------------------------------------------------------------

# AUTHORITATIVE task catalogues — sourced verbatim from GliderScore's own task audio
# descriptions (GliderScoreData.mdb → AudioSettings, F3KTask/F5KTask rows, 2026-07-10).
# GliderScore is the competition/deployment tool, so operators see these same labels.
# IMPORTANT: F3K and F5K reuse the same LETTERS with DIFFERENT meanings — keep separate.
# Base letters use the primary variant; GliderScore variants (A(1), C(3)...) are captured
# in base_station/frontend/data/gliderscore_audio_library.json for when we build import.
# Each entry: letter -> {"name": short label, "desc": objective for the run screen}.
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
    "M": {"name": "Huge Ladder",      "desc": "3 flights of 3:00, 5:00, 7:00 in order; 15-minute window; 3 flights max."},
    "N": {"name": "Best flight",      "desc": "Your single best flight counts; 10-minute window."},
}
# F5K reuses letters with DIFFERENT tasks (GliderScore class F5K2024). Scoring also adds a
# motor-cut height bonus vs the round's reference height — see GLIDERSCORE.md.
F5K_TASKS = {
    "A": {"name": "1-2-3-4",          "desc": "4 flights in 10 min — targets 1, 2, 3 and 4 minutes in any order."},
    "B": {"name": "Last flight",      "desc": "Only your last flight counts — 5:00 max; max 3 flights in 7 min."},
    "C": {"name": "All up",           "desc": "All gliders launch together — 4:00 max; 3 flights per round."},
    "D": {"name": "3 flights (3-3-4)","desc": "Three flights of 3:00, 3:00 and 4:00 in any order; max 3 flights in 10 min."},
    "E": {"name": "Poker",            "desc": "Call your own target times; best 3 timed flights count; max 3 flights in 10 min."},
}
TASKS = {"F3K": F3K_TASKS, "F5K": F5K_TASKS}


def task_label(discipline: str, letter: str) -> str:
    t = TASKS.get(discipline, {}).get(letter)
    return t["name"] if t else letter


@app.get("/rounds")
async def rounds_get(request: Request):
    db = _db()
    competitions = db.execute("SELECT * FROM competitions ORDER BY id").fetchall()

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
            for grp in groups:
                gpilots = db.execute(
                    """SELECT p.name FROM pilots p
                       JOIN group_pilots gp ON gp.pilot_id = p.id
                       WHERE gp.group_id = ? ORDER BY p.name""",
                    (grp["id"],),
                ).fetchall()
                group_data.append({"group": grp, "pilots": gpilots})
            round_data.append({"round": rnd, "groups": group_data})

        comp_pilots = db.execute(
            """SELECT p.id, p.name FROM pilots p
               JOIN competition_pilots cp ON cp.pilot_id = p.id
               WHERE cp.competition_id = ? ORDER BY p.name""",
            (comp["id"],),
        ).fetchall()

        comp_data.append({
            "comp": comp,
            "rounds": round_data,
            "comp_pilots": comp_pilots,
        })

    return templates.TemplateResponse(request, "rounds.html", {
        "active": "rounds",
        "comp_data": comp_data,
        "tasks": TASKS,
    })


@app.post("/rounds/{comp_id}/add")
async def round_add(comp_id: int, task: str = Form(...), working_time_m: int = Form(10)):
    db = _db()
    comp = db.execute("SELECT discipline FROM competitions WHERE id = ?", (comp_id,)).fetchone()
    if not comp:
        return RedirectResponse("/rounds", status_code=303)
    max_no = db.execute(
        "SELECT MAX(round_no) FROM rounds WHERE competition_id = ?", (comp_id,)
    ).fetchone()[0]
    round_no = (max_no or 0) + 1
    db.execute(
        """INSERT INTO rounds (competition_id, round_no, task, working_time_s, discipline)
           VALUES (?, ?, ?, ?, ?)""",
        (comp_id, round_no, task, working_time_m * 60, comp["discipline"]),
    )
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.post("/rounds/round/{round_id}/delete")
async def round_delete(round_id: int):
    db = _db()
    groups = db.execute("SELECT id FROM groups WHERE round_id = ?", (round_id,)).fetchall()
    for grp in groups:
        db.execute("DELETE FROM group_pilots WHERE group_id = ?", (grp["id"],))
    db.execute("DELETE FROM groups WHERE round_id = ?", (round_id,))
    db.execute("DELETE FROM rounds WHERE id = ?", (round_id,))
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.post("/rounds/round/{round_id}/group/add")
async def group_add(round_id: int, request: Request):
    form = await request.form()
    pilot_ids = form.getlist("pilot_ids")
    dummy_count = int(form.get("dummy_count", 0) or 0)
    db = _db()
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
    db.execute("UPDATE groups SET dummy_count = dummy_count + 1 WHERE id = ?", (group_id,))
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.post("/rounds/group/{group_id}/dummy/remove")
async def group_dummy_remove(group_id: int):
    db = _db()
    db.execute(
        "UPDATE groups SET dummy_count = MAX(0, dummy_count - 1) WHERE id = ?", (group_id,)
    )
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


@app.post("/rounds/group/{group_id}/delete")
async def group_delete(group_id: int):
    db = _db()
    db.execute("DELETE FROM group_pilots WHERE group_id = ?", (group_id,))
    db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    db.commit()
    return RedirectResponse("/rounds", status_code=303)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@app.get("/results")
async def results_get(request: Request, error: str = None):
    db = _db()
    competitions = db.execute("SELECT * FROM competitions ORDER BY id").fetchall()

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
                heat_data.append({
                    "group": grp,
                    "heat": chr(64 + grp["group_no"]),
                    "pilots": pilots,
                    "max_flights": max_flights,
                    "any_altitudes": any_altitudes,
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
    try:
        dur_ms = _parse_duration(duration)
    except (ValueError, TypeError):
        return RedirectResponse(
            f"/results?error={urllib.parse.quote('Invalid time — use M:SS or M:SS.HH (e.g. 3:00 or 3:00.55)')}",
            status_code=303,
        )
    alt = float(altitude_m) if altitude_m.strip() else None
    fno = int(flight_no) if flight_no.strip() else None
    db = _db()
    db.execute(
        "INSERT INTO flights (pilot_id, duration_ms, group_id, altitude_m, flight_no) VALUES (?, ?, ?, ?, ?)",
        (pilot_id, dur_ms, group_id, alt, fno),
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
    competitions = db.execute("SELECT * FROM competitions ORDER BY id").fetchall()
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
    task_no = _TASK_NO.get(comp["discipline"], 5)

    buf = io.StringIO()
    writer = csv.writer(buf)

    rounds = db.execute(
        "SELECT * FROM rounds WHERE competition_id = ? ORDER BY round_no", (comp_id,)
    ).fetchall()

    for rnd in rounds:
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
                       ORDER BY recorded_at""",
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

@app.get("/run")
async def run_get(request: Request):
    db = _db()
    heats = []
    comps = db.execute("SELECT * FROM competitions ORDER BY id").fetchall()
    for comp in comps:
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
                    "comp_name": comp["name"],
                    "discipline": comp["discipline"],
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
    sm = app.state.state_machine
    return templates.TemplateResponse(request, "run.html", {
        "active": "run",
        "heats": heats,
        "initial_state": json.dumps(sm.get_status()),
        "tasks": TASKS,
    })


@app.post("/api/run/load")
async def api_run_load(round_id: int, group_id: int):
    sm = app.state.state_machine
    await sm.load_heat(round_id, group_id)
    return {"ok": True, "status": sm.get_status()}


@app.post("/api/run/start")
async def api_run_start():
    await app.state.state_machine.start()
    return {"ok": True}


@app.post("/api/run/abort")
async def api_run_abort():
    await app.state.state_machine.abort()
    return {"ok": True}


@app.post("/api/run/skip")
async def api_run_skip(to: int = 60):
    """CD control: during PREP, skip the countdown to `to` seconds remaining."""
    ok = app.state.state_machine.skip_prep_to(to)
    return {"ok": ok}


@app.post("/api/run/complete")
async def api_run_complete(group_id: int):
    db = _db()
    db.execute("UPDATE groups SET completed = 1 WHERE id = ?", (group_id,))
    db.commit()
    return {"ok": True}


@app.post("/api/run/uncomplete")
async def api_run_uncomplete(group_id: int):
    db = _db()
    db.execute("UPDATE groups SET completed = 0 WHERE id = ?", (group_id,))
    db.commit()
    return {"ok": True}


@app.get("/api/run/state")
async def api_run_state():
    return app.state.state_machine.get_status()


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
    status = await audio_control.bt_status()
    status["volume"] = await audio_control.get_volume()
    status["saved_volume"] = audio_control.load_config().get("volume")
    status["lead_s"] = audio_control.get_lead()
    return status


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
    return await audio_control.bt_disconnect(mac)


@app.get("/api/timers")
async def api_timers():
    srv = app.state.server
    return {"timers": srv.timers_info(), "events": srv.recent_events()}
