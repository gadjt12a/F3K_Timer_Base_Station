import json
from pathlib import Path

from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates


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

app = FastAPI(title="F3K Base Station")
app.state.ws_manager = manager
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


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
    prep_time_s: int = Form(120),
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

TASK_NAMES = {
    "A": "Last flight",
    "B": "Two flights of 3:00",
    "C": "All up last down",
    "D": "Two last flights",
    "E": "Poker",
    "F": "Three flights",
    "G": "Four flights",
    "H": "Five flights",
    "I": "Last two flights",
    "J": "All up last down",
    "K": "Best two of five",
    "L": "Best flight × 2:30",
    "M": "One flight of 10:00",
    "N": "Best two of six",
}


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
        "task_names": TASK_NAMES,
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
                })
    sm = app.state.state_machine
    return templates.TemplateResponse(request, "run.html", {
        "active": "run",
        "heats": heats,
        "initial_state": json.dumps(sm.get_status()),
        "task_names": TASK_NAMES,
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


@app.get("/api/run/state")
async def api_run_state():
    return app.state.state_machine.get_status()
