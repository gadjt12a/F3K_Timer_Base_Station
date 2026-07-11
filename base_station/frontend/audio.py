"""F3K Base Station — GliderScore-driven audio engine (Task 8).

Replaces the MyBoysToys sound timer. Plays GliderScore's own cue schedules
(announcements + beeps) through the Pi's audio output via `aplay`.

Design:
- The competition state machine remains the master clock. Each second it calls
  ``engine.cue(phase, seconds_remaining)``; the engine looks up the GliderScore
  cue(s) for that instant in the *selected profile* and plays them.
- Playback is non-blocking and serialized: cues are pushed onto an asyncio queue
  and played one at a time by a background worker, so a multi-second announcement
  never stalls the 1 s tick loop and cues never overlap.
- Cue data comes from ``data/gliderscore_timer_profiles.json`` (extracted from
  GliderScoreData.mdb). Announcement wavs live in ``data/audio/``; beep tones are
  synthesized on demand with the stdlib ``wave`` module (no external deps).

Environment:
- ``F3K_AUDIO_DEVICE``  ALSA device for aplay (e.g. ``plughw:0,0``). Default: aplay default.
- ``F3K_AUDIO_DISABLE`` set to ``1`` to disable playback (log only) — for silent testing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import struct
import tempfile
import wave
from pathlib import Path

from frontend import audio_control

log = logging.getLogger("f3k")

_DATA_DIR = Path(__file__).parent / "data"
_WAV_DIR = _DATA_DIR / "audio"
_PROFILES_FILE = _DATA_DIR / "gliderscore_timer_profiles.json"

# GliderScore TimerState phase codes
PT, TT, NF, WT, LT = "PT", "TT", "NF", "WT", "LT"


def _parse_profile_span(name: str) -> tuple[int, int, int] | None:
    """Parse 'F3K-3m10m30s' -> (prep_s, work_s, land_s). Returns None if not parseable."""
    parts = name.split("-", 1)
    if len(parts) != 2:
        return None
    tokens = re.findall(r"(\d+)([ms])", parts[1])
    if len(tokens) != 3:
        return None
    secs = [int(v) * (60 if u == "m" else 1) for v, u in tokens]
    return secs[0], secs[1], secs[2]


class TimerProfile:
    """A GliderScore cue schedule, indexed by seconds-remaining within each phase."""

    def __init__(self, raw: dict) -> None:
        self.name: str = raw["name"]
        self.timer_no: int = raw.get("timerNo", 0)
        self.cues: list[dict] = raw.get("cues", [])

        span = _parse_profile_span(self.name)
        self.prep_s, self.work_s, self.land_s = span if span else (0, 0, 0)

        # Window boundaries. The working window CLOSES at t == work_s (the close horn
        # sits there), so "seconds remaining" during WT is work_s - t, not (max WT cue) - t.
        # Prefer the name-encoded span; fall back to the schedule if the name didn't parse.
        wt_times = [c["t"] for c in self.cues if c["state"] == WT]
        lt_times = [c["t"] for c in self.cues if c["state"] == LT]
        if not span:
            self.work_s = max(wt_times) if wt_times else 0
            self.land_s = (max(lt_times) - self.work_s) if lt_times else 0
        self._wt_close = self.work_s               # t at which the working window closes
        self._lt_close = self.work_s + self.land_s  # t at which landing ends

        # Bucket cues by (phase-group, seconds-remaining-in-phase).
        # phase-group: "prep" (PT/TT/NF), "working" (WT), "landing" (LT).
        self.prep: dict[int, list[dict]] = {}
        self.working: dict[int, list[dict]] = {}
        self.landing: dict[int, list[dict]] = {}
        for c in self.cues:
            st = c["state"]
            t = c["t"]
            # The window-open/close horns are fired explicitly by the engine at the
            # WORKING/LANDING phase boundaries (robust to configured land length), so
            # skip them here to avoid a double horn.
            if c.get("wav") == "StartEndHorn.wav":
                continue
            if st in (PT, TT, NF):
                key = -t                     # seconds until the window opens
                self.prep.setdefault(key, []).append(c)
            elif st == WT:
                key = self._wt_close - t       # seconds of working time remaining
                self.working.setdefault(key, []).append(c)
            elif st == LT:
                key = self._lt_close - t       # seconds of landing time remaining
                self.landing.setdefault(key, []).append(c)


class AudioEngine:
    def __init__(self) -> None:
        self._profiles: dict[str, TimerProfile] = {}
        self._active: TimerProfile | None = None
        self._queue: asyncio.Queue[dict] | None = None
        self._worker: asyncio.Task | None = None
        self._sched_task: asyncio.Task | None = None
        self._prep_offset: int = 0
        self._current_proc: asyncio.subprocess.Process | None = None
        self._beep_cache: dict[tuple[int, int], str] = {}
        self._disabled = os.environ.get("F3K_AUDIO_DISABLE") == "1"
        self._loaded = False

    async def apply_saved_volume(self) -> None:
        """Re-apply the operator's saved volume (call once at startup)."""
        vol = audio_control.load_config().get("volume")
        if vol is not None:
            await audio_control.apply_volume(vol)

    def play_test(self) -> None:
        """Play a short sample (announcement + beep) to check output/volume."""
        self._enqueue({"wav": "TimeToStart-00.30.wav", "beepHz": 0, "beepMs": 0})
        self._enqueue({"wav": "", "beepHz": 1000, "beepMs": 500})

    # ------------------------------------------------------------------
    # Loading / selection
    # ------------------------------------------------------------------

    def _load_profiles(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(_PROFILES_FILE.read_text())
            for raw in data.get("profiles", []):
                p = TimerProfile(raw)
                self._profiles[p.name] = p
            log.info("[AUDIO] loaded %d timer profiles", len(self._profiles))
        except Exception:
            log.exception("[AUDIO] failed to load timer profiles from %s", _PROFILES_FILE)

    def select_profile(self, discipline: str, working_time_s: int) -> str | None:
        """Pick the GliderScore profile matching discipline + working time.

        Returns the chosen profile name (or None). Prefers the discipline's
        standard prep length (F3K 3 min, F5K 5 min) when several work-time matches.
        """
        self._load_profiles()
        prefix = f"{discipline}-"
        candidates = [
            p for p in self._profiles.values()
            if p.name.startswith(prefix) and p.work_s == working_time_s
        ]
        if not candidates:
            self._active = None
            log.warning(
                "[AUDIO] no %s profile for working_time=%ds — audio disabled for heat",
                discipline, working_time_s,
            )
            return None
        std_prep = {"F3K": 180, "F5K": 300}.get(discipline)
        candidates.sort(key=lambda p: (p.prep_s != std_prep, p.name))
        self._active = candidates[0]
        log.info(
            "[AUDIO] selected profile '%s' (prep=%ds work=%ds land=%ds)",
            self._active.name, self._active.prep_s, self._active.work_s, self._active.land_s,
        )
        return self._active.name

    @property
    def active_profile(self) -> TimerProfile | None:
        return self._active

    # ------------------------------------------------------------------
    # Cue playback (called from the state-machine tick loop)
    # ------------------------------------------------------------------

    def cue(self, phase: str, seconds_remaining: int) -> None:
        """Play the GliderScore cue(s) for this instant, if the active profile has any.

        ``phase`` is one of "prep", "working", "landing" (our state groups).
        Non-blocking: cues are enqueued and played by the background worker.
        """
        if not self._active:
            return
        table = {
            "prep": self._active.prep,
            "working": self._active.working,
            "landing": self._active.landing,
        }.get(phase)
        if not table:
            return
        for c in table.get(seconds_remaining, []):
            self._enqueue(c)

    def horn(self) -> None:
        """Play the start/end working-window horn."""
        self._enqueue({"wav": "StartEndHorn.wav", "beepHz": 0, "beepMs": 0})

    # ------------------------------------------------------------------
    # Lead-compensated schedule (drives a whole heat's audio)
    # ------------------------------------------------------------------

    # Pre-roll window: GliderScore starts an announcement a few seconds before its
    # mark so it *finishes* on the mark. A cue up to this far before sequence start is
    # clamped to play AT the start (e.g. "3 minutes to start" on a 3-minute prep)
    # rather than being dropped.
    _PREROLL_S = 5

    def build_schedule(self, prep_offset: int) -> list[tuple[float, dict]]:
        """Absolute cue schedule for the active profile, in seconds from sequence start.

        ``prep_offset`` is when the working window opens relative to sequence start
        (i.e. the competition's prep time). The profile's own cue times ``t`` are
        relative to the working-window open (negative during prep), so a cue plays at
        ``prep_offset + t`` seconds after the sequence starts. This anchors the
        window-open horn (t=0) exactly on the timer START broadcast, regardless of any
        difference between the competition prep length and the profile's own.
        """
        if not self._active:
            return []
        sched = []
        for c in self._active.cues:
            off = prep_offset + c["t"]
            if off < -self._PREROLL_S:
                continue                      # genuinely before the sequence — drop
            # Replace per-second voice files (1.wav–10Secs.wav) in the last 10s of prep
            # with short beeps — the voice clips are longer than 1s and get clipped.
            if c.get("state") in (PT, TT, NF) and -10 <= c["t"] <= -1 and c.get("wav"):
                c = {"wav": "", "beepHz": 880, "beepMs": 150}
            sched.append((max(off, 0.0), c))  # clamp pre-roll cues to the start
        sched.sort(key=lambda x: x[0])
        return sched

    def start_schedule(self, prep_offset: int) -> None:
        """Begin lead-compensated playback of the active profile, anchored to now.

        Fires each cue ``lead_s`` seconds early so the *sound* — after fixed output
        latency (e.g. Bluetooth A2DP buffering) — emerges at the intended instant.
        """
        self.stop_schedule()
        self._prep_offset = prep_offset
        sched = self.build_schedule(prep_offset)
        if not sched:
            return
        lead = audio_control.get_lead()
        self._sched_task = asyncio.create_task(self._run_schedule(sched, lead))
        log.info("[AUDIO] schedule started: %d cues, prep_offset=%ds, lead=%.1fs",
                 len(sched), prep_offset, lead)

    def reanchor(self, elapsed: float) -> None:
        """Fast-forward the running schedule so 'now' == ``elapsed`` seconds into the
        sequence (used when the CD skips the prep countdown ahead). Cues already passed
        are not replayed; pending queued cues are dropped so nothing stale plays."""
        if self._sched_task is None or self._active is None:
            return
        self.stop_schedule()
        self._drain_queue()
        sched = self.build_schedule(self._prep_offset)
        lead = audio_control.get_lead()
        self._sched_task = asyncio.create_task(self._run_schedule(sched, lead, elapsed))
        log.info("[AUDIO] schedule reanchored to elapsed=%.0fs", elapsed)

    def stop_schedule(self) -> None:
        if self._sched_task and not self._sched_task.done():
            self._sched_task.cancel()
        self._sched_task = None
        self._kill_current()

    def _kill_current(self) -> None:
        """Kill the currently playing aplay subprocess immediately (preempt)."""
        proc = self._current_proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _drain_queue(self) -> None:
        if self._queue is None:
            return
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except asyncio.QueueEmpty:
            pass

    async def _run_schedule(self, sched: list[tuple[float, dict]], lead: float,
                            elapsed: float = 0.0) -> None:
        """Preemptive scheduler: each cue group fires at its exact scheduled time,
        killing whatever is currently playing rather than waiting for it to finish.

        Cues sharing the same offset (e.g. a tone + announcement at t=−60) are
        grouped and played sequentially within the group — only groups preempt
        each other.
        """
        loop = asyncio.get_event_loop()
        t0 = loop.time() - elapsed         # virtual sequence-start time
        i = 0
        while i < len(sched):
            offset, _ = sched[i]
            if offset < elapsed - 0.5:
                i += 1
                continue                   # already passed (skip-ahead) — don't replay
            delay = (t0 + offset - lead) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            # Collect all cues at this offset (float precision: within 50ms)
            group: list[dict] = []
            while i < len(sched) and abs(sched[i][0] - offset) < 0.05:
                group.append(sched[i][1])
                i += 1
            # Preempt whatever is currently playing and fire this group
            self._kill_current()
            asyncio.create_task(self._play_group(group))

    async def _play_group(self, cues: list[dict]) -> None:
        """Play a list of cues sequentially (used for same-offset cue groups)."""
        for cue in cues:
            try:
                await self._play(cue)
            except Exception:
                log.exception("[AUDIO] playback error for cue %s", cue)

    def _enqueue(self, cue: dict) -> None:
        if self._disabled:
            log.info("[AUDIO] (disabled) cue %s", cue.get("wav") or f"beep {cue.get('beepHz')}Hz")
            return
        self._ensure_worker()
        assert self._queue is not None
        try:
            self._queue.put_nowait(cue)
        except asyncio.QueueFull:
            log.warning("[AUDIO] queue full — dropping cue %s", cue.get("wav"))

    # ------------------------------------------------------------------
    # Background playback worker
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=32)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker())

    async def _run_worker(self) -> None:
        assert self._queue is not None
        while True:
            cue = await self._queue.get()
            try:
                await self._play(cue)
            except Exception:
                log.exception("[AUDIO] playback error for cue %s", cue)
            finally:
                self._queue.task_done()

    async def _play(self, cue: dict) -> None:
        wav = cue.get("wav")
        if wav:
            path = _WAV_DIR / wav
            if not path.exists():
                log.warning("[AUDIO] missing wav: %s", wav)
                return
            await self._aplay(str(path))
        elif cue.get("beepHz") and cue.get("beepMs"):
            await self._aplay(self._beep_wav(int(cue["beepHz"]), int(cue["beepMs"])))

    async def _aplay(self, path: str) -> None:
        args = ["aplay", "-q", "-D", audio_control.output_device(), path]
        # Serialize with volume changes (amixer on the same bluealsa device can cause
        # A2DP renegotiation if run concurrently with aplay). Cues preempt each other
        # via _kill_current() / proc.kill(), which makes proc.communicate() return
        # immediately so this lock is released without waiting for the full clip.
        async with audio_control.bluealsa_lock:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._current_proc = proc
            err = b""
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=8.0)
            except asyncio.TimeoutError:
                log.warning("[AUDIO] aplay timed out (killed) for %s", path)
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            except BaseException:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            finally:
                if self._current_proc is proc:
                    self._current_proc = None
        rc = proc.returncode
        # rc == -9 (SIGKILL) is expected when this clip was preempted by a later cue
        if rc not in (0, -9) and rc is not None and err:
            log.warning("[AUDIO] aplay rc=%s for %s: %s",
                        rc, path, err.decode(errors="replace").strip())

    def _beep_wav(self, hz: int, ms: int) -> str:
        key = (hz, ms)
        cached = self._beep_cache.get(key)
        if cached and os.path.exists(cached):
            return cached
        rate = 44100
        n = int(rate * ms / 1000)
        amp = 22000
        path = os.path.join(tempfile.gettempdir(), f"f3k_beep_{hz}_{ms}.wav")
        with wave.open(path, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            frames = bytearray()
            for i in range(n):
                sample = int(amp * math.sin(2 * math.pi * hz * i / rate))
                frames += struct.pack("<h", sample)
            w.writeframes(bytes(frames))
        self._beep_cache[key] = path
        return path


# Singleton engine used across the app.
engine = AudioEngine()


async def play_cue(name: str) -> None:
    """Back-compat shim for the old named-cue API (logs; superseded by engine.cue)."""
    log.info("[AUDIO] cue-name %s", name)
