"""F3K Base Station — Competition state machine.

States: IDLE → PREP → WORKING → LANDING → IDLE
"""

from __future__ import annotations

import asyncio
import logging

from frontend.audio import engine

log = logging.getLogger("f3k")


class CompetitionStateMachine:
    def __init__(self, server) -> None:
        self._server = server
        self._state: str = "IDLE"
        self._loaded: dict | None = None
        self._task: asyncio.Task | None = None
        self._skip_to: int | None = None   # CD requested prep jump to N seconds remaining
        self._wt_remaining: int = 0        # live seconds remaining during WORKING (for reconnect)
        self._prep_remaining: int = 0      # live seconds remaining during PREP (for reconnect)
        self._land_remaining: int = 0      # live seconds remaining during LANDING (for reconnect)

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
                "group_id": d["group_id"],
                "pilot_id_names": d["pilot_id_names"],
            } if d else None,
            "flights": self._loaded_flights(),
        }

    def _loaded_flights(self) -> list[dict]:
        """Flights already recorded for the loaded heat, shaped like the `flight`
        websocket event so the Run page can seed its log from them. [I-47]

        The Run page used to build its flight log purely by accumulating live
        websocket pushes, so every recorded time vanished from the screen on any
        page load — a refresh, or simply visiting /results to correct an earlier
        heat and coming back. The data was never lost, but the CD was blinded by
        an entirely ordinary action, mid-heat.

        Same lesson as [I-01]/[I-13]: a client must not assume it saw everything.
        Anything the page needs has to be fetchable, not only broadcast.

        Jumped starts are included: since [I-49] they are real voided rows that
        consume a launch and score zero, so they survive a reload like any other
        flight and keep their flight number. That numbering matters beyond
        display — F5K altitudes are matched to flights by index.
        """
        d = self._loaded
        if not d:
            return []
        rows = self._server.db.execute(
            """SELECT f.pilot_id, f.duration_ms, f.scratched, f.void_reason,
                      p.name AS pilot_name
               FROM flights f
               JOIN pilots p ON p.id = f.pilot_id
               WHERE f.group_id = ?
               ORDER BY COALESCE(f.flight_no, 9999), f.recorded_at""",
            (d["group_id"],),
        ).fetchall()
        return [{
            "type": "flight",
            "pilot_id": r["pilot_id"],
            "pilot_name": r["pilot_name"],
            "duration_ms": r["duration_ms"],
            "scratched": bool(r["scratched"]),
            "void_reason": r["void_reason"],
            "round_no": d["round_no"],
            "heat": d["heat"],
        } for r in rows]

    async def load_heat(self, round_id: int, group_id: int) -> bool:
        """Load a heat ready to run. Returns False (and changes nothing) if the
        heat doesn't exist. Callers must refuse to call this outside IDLE — see
        the state guard in ``/api/run/load``. [I-01]"""
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
            return False

        grp = db.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not grp:
            log.warning("load_heat: group_id=%d not found", group_id)
            return False

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
        engine.select_profile(rnd["discipline"], rnd["working_time_s"],
                              rnd["prep_time_s"], rnd["land_time_s"])

        # Push pilot list to timers immediately so selection is available before PREP starts
        pilots_str = ",".join(f"{pid}:{name}" for pid, name in self._loaded["pilot_id_names"])
        if pilots_str:
            await self._server.broadcast(f"PILOTS {pilots_str}")

        log.info(
            "Heat loaded: round=%d heat=%s pilots=%s",
            rnd["round_no"], heat_letter, pilot_names,
        )
        return True

    async def start(self) -> tuple[bool, str]:
        """Begin the prep→working→landing sequence. Returns (ok, reason) so the
        API can report a refusal instead of always claiming success. [I-10]"""
        if self._state != "IDLE":
            log.warning("start() called but state=%s (expected IDLE)", self._state)
            return False, f"A heat is already running ({self._state})"
        if not self._loaded:
            log.warning("start() called but no heat loaded")
            return False, "No heat loaded"
        self._task = asyncio.create_task(self._run_sequence_safe())
        return True, ""

    def skip_prep_to(self, seconds: int) -> bool:
        """CD control: during PREP, jump the countdown to ``seconds`` remaining
        (e.g. 60 = "1 minute to start" when everyone is ready). No-op outside PREP
        or if the countdown is already at/below that point.

        ⚠ Legitimate competition control, and deliberately NOT gated behind test
        mode — shortening prep when everyone is ready changes nothing about the
        round. Skipping WORKING or LANDING is a different matter entirely and goes
        through ``skip_phase_to`` below.
        """
        if self._state != "PREP":
            return False
        self._skip_to = max(0, int(seconds))
        log.info("Prep skip requested → %ds remaining", self._skip_to)
        return True

    # Phases whose clock the test-mode fast-forward may move.
    _SKIPPABLE = ("PREP", "WORKING", "LANDING")

    def skip_phase_to(self, seconds: int) -> tuple[bool, str]:
        """TEST ONLY: jump the *current* phase to ``seconds`` remaining. [TF-16]

        Exists because verifying end-of-phase behaviour meant sitting through the
        phase. A 10-minute working window costs ten minutes to reach its last ten
        seconds, every time, and that is where the countdown, the horn and the
        phase transition all live. The whole `ZZ-ACK-TEST` competition (30s prep /
        60s WT / 15s land) exists only to make that wait shorter.

        ⚠ **Cutting WORKING short falsifies the round**, so unlike `skip_prep_to`
        this is refused unless test mode is on — see `/api/run/skip`.

        Returns (ok, reason) so the caller can say why, rather than claiming
        success. [I-10]
        """
        if self._state not in self._SKIPPABLE:
            return False, f"Nothing to skip — state is {self._state}"
        self._skip_to = max(0, int(seconds))
        log.warning("[TEST] Fast-forward requested: %s → %ds remaining",
                    self._state, self._skip_to)
        return True, ""

    async def abort(self) -> None:
        engine.stop_schedule()
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
        if self._state == "PREP":
            if self._prep_remaining > 0:
                await send_fn(f"PREP t={self._prep_remaining}")
        elif self._state == "WORKING":
            rem = self._wt_remaining if self._wt_remaining > 0 else d["working_time_s"]
            await send_fn(f"TASK wt={rem} disc={d['discipline']}")
            await send_fn("START")
        elif self._state == "LANDING":
            if self._land_remaining > 0:
                await send_fn(f"LAND t={self._land_remaining}")

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
            "pilot_id_names": d["pilot_id_names"],
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

    @staticmethod
    async def _tick_sleep(deadline: float) -> float:
        """Sleep until deadline (monotonic), return next deadline (+1s).
        Uses asyncio's internal clock so sleep drift is self-correcting."""
        loop = asyncio.get_event_loop()
        sleep_s = deadline - loop.time()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        return deadline + 1.0

    async def _run_sequence(self) -> None:
        d = self._loaded
        loop = asyncio.get_event_loop()

        # Audio is driven by a single lead-compensated schedule anchored to *now*
        # (the start of PREP), so cues fire early enough to overcome fixed output
        # latency and land on the beat. The tick loop below stays the master clock
        # for the display and the timers (TCP), independent of audio.
        engine.start_schedule(d["prep_time_s"], d["land_time_s"])
        seq_t0 = loop.time()   # same anchor the audio schedule uses

        # ── PREP ─────────────────────────────────────────────────────
        self._state = "PREP"
        self._skip_to = None
        # PILOTS already broadcast in load_heat(); don't re-send here — a second
        # PILOTS message at PREP start resets the pilot selection on connected timers.

        remaining = d["prep_time_s"]
        self._prep_remaining = remaining
        # Timers run the prep countdown locally from this; COUNT re-syncs the last 10s
        await self._server.broadcast(f"PREP t={remaining}")
        deadline = loop.time() + 1.0
        while remaining > 0:
            # CD may jump the countdown ahead ("everyone ready — skip to 1:00").
            if self._skip_to is not None:
                if remaining > self._skip_to:
                    remaining = self._skip_to
                    engine.reanchor(d["prep_time_s"] - remaining)  # fast-forward audio
                    await self._server.broadcast(f"PREP t={remaining}")  # re-sync timers
                self._skip_to = None
                deadline = loop.time() + 1.0  # re-anchor after skip
            self._prep_remaining = remaining
            await self._broadcast_tick(remaining)
            if remaining <= 10:
                await self._server.broadcast(f"COUNT {remaining}")
            deadline = await self._tick_sleep(deadline)
            remaining -= 1

        log.info("[AUDIO-T] prep loop ended at %.3f (expected %.3f)",
                 loop.time() - seq_t0, float(d["prep_time_s"]))
        await self._server.broadcast(f"TASK wt={d['working_time_s']} disc={d['discipline']}")

        # ── WORKING ──────────────────────────────────────────────────
        self._state = "WORKING"
        await self._server.broadcast("START")

        # The audio schedule assumes WORKING begins exactly at prep_time_s. This
        # loop re-anchors to now, AFTER the TASK and START broadcasts — so any gap
        # here is one the audio timeline does not know about.
        log.info("[AUDIO-T] START sent at %.3f — working clock anchored here",
                 loop.time() - seq_t0)
        deadline = loop.time() + 1.0
        self._skip_to = None
        remaining = d["working_time_s"]
        while remaining > 0:
            # Test-mode fast-forward. [TF-16]
            #
            # ⚠ The timer is NOT re-synced here, deliberately. `TASK wt=` is the
            # only message that carries a working time, and on the firmware it
            # sets `g_wtMinutes` — whole minutes, used to configure a round, not
            # to steer a running clock. Sending it mid-working would corrupt that
            # value (15s becomes 0 minutes) without moving the display. The base's
            # STOP is authoritative and still lands correctly, so a fast-forwarded
            # working window ends properly; the timer's own display just will not
            # have followed the jump. Acceptable, and test-only.
            if self._skip_to is not None:
                if remaining > self._skip_to:
                    remaining = self._skip_to
                    engine.reanchor(d["prep_time_s"] + d["working_time_s"] - remaining)
                self._skip_to = None
                deadline = loop.time() + 1.0
            self._wt_remaining = remaining
            await self._broadcast_tick(remaining)
            deadline = await self._tick_sleep(deadline)
            remaining -= 1
        self._wt_remaining = 0

        await self._server.broadcast("STOP")

        # ── LANDING ──────────────────────────────────────────────────
        self._state = "LANDING"
        # Timers show the landing window countdown (STOP above precedes this on the wire)
        await self._server.broadcast(f"LAND t={d['land_time_s']}")

        deadline = loop.time() + 1.0
        self._skip_to = None
        remaining = d["land_time_s"]
        while remaining > 0:
            # Test-mode fast-forward. [TF-16]. Unlike WORKING, the timer CAN be
            # re-synced here: `LAND t=` is a pure display countdown, the same
            # message the loop opened with and the one send_catchup() uses.
            if self._skip_to is not None:
                if remaining > self._skip_to:
                    remaining = self._skip_to
                    engine.reanchor(d["prep_time_s"] + d["working_time_s"]
                                    + d["land_time_s"] - remaining)
                    await self._server.broadcast(f"LAND t={remaining}")
                self._skip_to = None
                deadline = loop.time() + 1.0
            self._land_remaining = remaining
            await self._broadcast_tick(remaining)
            deadline = await self._tick_sleep(deadline)
            remaining -= 1
        self._land_remaining = 0

        # ── Done ─────────────────────────────────────────────────────
        self._state = "IDLE"
        self._loaded = None
        self._task = None
        await self._broadcast_ws({"type": "state_change", "state": "IDLE"})
        log.info("Heat complete → IDLE")
