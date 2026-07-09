"""F3K Base Station — Competition state machine.

States: IDLE → PREP → WORKING → LANDING → IDLE
"""

from __future__ import annotations

import asyncio
import logging

from frontend.audio import play_cue

log = logging.getLogger("f3k")

# Seconds-remaining values that trigger an audio cue (milestone + per-second below 10)
_PREP_CUES = frozenset({30, 20, 15, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1})
_LANDING_CUES = frozenset({30, 20, 15, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1})


class CompetitionStateMachine:
    def __init__(self, server) -> None:
        self._server = server
        self._state: str = "IDLE"
        self._loaded: dict | None = None
        self._task: asyncio.Task | None = None

    @property
    def state(self) -> str:
        return self._state

    def get_status(self) -> dict:
        d = self._loaded
        return {
            "state": self._state,
            "loaded": {
                "comp_name": d["comp_name"],
                "discipline": d["discipline"],
                "round_no": d["round_no"],
                "heat": d["heat"],
                "task": d["task"],
                "working_time_s": d["working_time_s"],
                "pilots": d["pilots"],
            } if d else None,
        }

    async def load_heat(self, round_id: int, group_id: int) -> None:
        db = self._server.db
        rnd = db.execute(
            """SELECT r.*, c.name AS comp_name,
                      c.prep_time_s, c.land_time_s, c.focus_time_s, c.count_last_s
               FROM rounds r
               JOIN competitions c ON c.id = r.competition_id
               WHERE r.id = ?""",
            (round_id,),
        ).fetchone()
        if not rnd:
            log.warning("load_heat: round_id=%d not found", round_id)
            return

        grp = db.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not grp:
            log.warning("load_heat: group_id=%d not found", group_id)
            return

        real_pilots = db.execute(
            """SELECT p.id, p.name FROM pilots p
               JOIN group_pilots gp ON gp.pilot_id = p.id
               WHERE gp.group_id = ? ORDER BY p.name""",
            (group_id,),
        ).fetchall()

        pilot_names = [r["name"] for r in real_pilots] + ["— TBD —"] * grp["dummy_count"]
        heat_letter = chr(64 + grp["group_no"])

        self._loaded = {
            "round_id": round_id,
            "group_id": group_id,
            "round_no": rnd["round_no"],
            "task": rnd["task"],
            "working_time_s": rnd["working_time_s"],
            "discipline": rnd["discipline"],
            "comp_name": rnd["comp_name"],
            "prep_time_s": rnd["prep_time_s"],
            "land_time_s": rnd["land_time_s"],
            "focus_time_s": rnd["focus_time_s"],
            "count_last_s": rnd["count_last_s"],
            "group_no": grp["group_no"],
            "heat": heat_letter,
            "pilots": pilot_names,
            "pilot_id_names": [(r["id"], r["name"]) for r in real_pilots],
        }
        log.info(
            "Heat loaded: round=%d heat=%s pilots=%s",
            rnd["round_no"], heat_letter, pilot_names,
        )

    async def start(self) -> None:
        if self._state != "IDLE":
            log.warning("start() called but state=%s (expected IDLE)", self._state)
            return
        if not self._loaded:
            log.warning("start() called but no heat loaded")
            return
        self._task = asyncio.create_task(self._run_sequence_safe())

    async def abort(self) -> None:
        task = self._task
        self._task = None
        self._state = "IDLE"
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._server.broadcast("STOP")
        await self._broadcast_ws({"type": "state_change", "state": "IDLE"})
        log.info("Heat aborted → IDLE")

    async def send_catchup(self, send_fn) -> None:
        """Resend protocol state to a timer that just reconnected mid-round."""
        d = self._loaded
        if not d or self._state == "IDLE":
            return
        pilots_str = ",".join(f"{pid}:{name}" for pid, name in d["pilot_id_names"])
        if pilots_str:
            await send_fn(f"PILOTS {pilots_str}")
        if self._state == "WORKING":
            await send_fn(f"TASK wt={d['working_time_s']}")
            await send_fn("START")

    async def on_flight(self, pilot_id: int, dur_ms: int) -> None:
        if self._state not in ("WORKING", "LANDING") or not self._loaded:
            return
        d = self._loaded
        row = self._server.db.execute(
            "SELECT name FROM pilots WHERE id = ?", (pilot_id,)
        ).fetchone()
        pilot_name = row["name"] if row else f"Pilot {pilot_id}"
        await self._broadcast_ws({
            "type": "flight",
            "pilot_id": pilot_id,
            "pilot_name": pilot_name,
            "duration_ms": dur_ms,
            "round_no": d["round_no"],
            "heat": d["heat"],
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _broadcast_ws(self, data: dict) -> None:
        from frontend.app import manager
        await manager.broadcast(data)

    async def _broadcast_tick(self, remaining: int) -> None:
        d = self._loaded
        await self._broadcast_ws({
            "type": "tick",
            "state": self._state,
            "seconds_remaining": remaining,
            "competition": d["comp_name"],
            "discipline": d["discipline"],
            "round_no": d["round_no"],
            "heat": d["heat"],
            "task": d["task"],
            "working_time_s": d["working_time_s"],
            "pilots": d["pilots"],
        })

    async def _run_sequence_safe(self) -> None:
        try:
            await self._run_sequence()
        except asyncio.CancelledError:
            log.info("State machine sequence cancelled")
            raise
        except Exception:
            log.exception("State machine sequence error")
            self._state = "IDLE"
            await self._broadcast_ws({"type": "state_change", "state": "IDLE"})

    async def _run_sequence(self) -> None:
        d = self._loaded

        # ── PREP ─────────────────────────────────────────────────────
        self._state = "PREP"
        pilots_str = ",".join(f"{pid}:{name}" for pid, name in d["pilot_id_names"])
        if pilots_str:
            await self._server.broadcast(f"PILOTS {pilots_str}")
        await play_cue(f"announce_round_{d['round_no']}_heat_{d['heat']}")

        for remaining in range(d["prep_time_s"], 0, -1):
            await self._broadcast_tick(remaining)
            if remaining == d["focus_time_s"]:
                await play_cue("focus_bell")
            if remaining in _PREP_CUES:
                await play_cue(f"prep_{remaining}s")
            if remaining <= 10:
                await self._server.broadcast(f"COUNT {remaining}")
            await asyncio.sleep(1)

        await self._server.broadcast(f"TASK wt={d['working_time_s']}")

        # ── WORKING ──────────────────────────────────────────────────
        self._state = "WORKING"
        await self._server.broadcast("START")
        await play_cue("window_open")

        for remaining in range(d["working_time_s"], 0, -1):
            await self._broadcast_tick(remaining)
            if remaining <= d["count_last_s"]:
                await play_cue(f"working_{remaining}s")
            await asyncio.sleep(1)

        await self._server.broadcast("STOP")
        await play_cue("window_close")

        # ── LANDING ──────────────────────────────────────────────────
        self._state = "LANDING"

        for remaining in range(d["land_time_s"], 0, -1):
            await self._broadcast_tick(remaining)
            if remaining in _LANDING_CUES:
                await play_cue(f"landing_{remaining}s")
            await asyncio.sleep(1)

        # ── Done ─────────────────────────────────────────────────────
        self._state = "IDLE"
        self._loaded = None
        self._task = None
        await self._broadcast_ws({"type": "state_change", "state": "IDLE"})
        log.info("Heat complete → IDLE")
