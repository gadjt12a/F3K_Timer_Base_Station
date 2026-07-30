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
import time
import wave
from pathlib import Path

from frontend import audio_control

log = logging.getLogger("f3k")

_DATA_DIR = Path(__file__).parent / "data"
_WAV_DIR = _DATA_DIR / "audio"

# GliderScore's own TimerSettings table names three wavs that do not exist in its
# Audio folder — dangling references in *its* data, not gaps in our extraction
# (verified against GliderScoreData.mdb; our vendored set is byte-identical to
# GliderScore's). gliderscore_timer_profiles.json is a verbatim extract, so the
# corrections live here rather than falsifying the extract. [I-23]
_WAV_ALIASES = {
    # "No flying allowed before the start." Timer 4 is the odd one out — every
    # other profile names NoFlyingAllowed.wav for the identical announcement, and
    # that file exists. 25 s of clear air either side, so nothing to clash with.
    "FlyingNotAllowed.wav": "NoFlyingAllowed.wav",
    # "10 seconds" opening the F5K landing countdown. In practice the last-10s
    # beep substitution catches this before playback, so it is belt-and-braces —
    # but that window is computed from the *unshifted* cue time while LT cues get
    # re-anchored to the competition's own landing length, so it is not worth
    # relying on. Costs nothing, and turns a silent gap into the right word.
    "10.wav": "10Secs.wav",
}

# Cues we deliberately drop. Silence here is a decision, not an accident, so they
# are listed rather than left to fail through the missing-wav warning — a warning
# that fires every round is one nobody reads, which is how the whole audio set
# went missing unnoticed for eight sessions.
_WAV_SUPPRESS = {
    # "to land", fired 1 s before the landing countdown beeps begin: the
    # clash-and-clip case that voice calls were pulled for. PilotsMustLand.wav
    # already makes the call 17 s earlier, and the timer shows the landing clock.
    "ToLand.wav",
}
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

    def __init__(self, raw: dict, span: tuple[int, int, int] | None = None) -> None:
        self.name: str = raw["name"]
        self.timer_no: int = raw.get("timerNo", 0)
        self.cues: list[dict] = raw.get("cues", [])
        self.generated: bool = bool(raw.get("generated"))

        # `span` is passed for generated schedules, whose names do not follow (and
        # should not have to follow) GliderScore's naming convention.
        span = span or _parse_profile_span(self.name)
        self.prep_s, self.work_s, self.land_s = span if span else (0, 0, 0)

        # Window boundaries, in the profile's own t-space. These anchor every
        # "seconds remaining" key, so getting them wrong shifts the whole countdown.
        #
        # They must come from the SCHEDULE, not from the name: GliderScore's t-space
        # does not agree with its own profile names. F3K-3m3m30s runs its working
        # cues to t=182 and puts the end horn at t=183, so trusting the name's 180
        # started the last-10 countdown 3 s early and left it saying "three" as the
        # horn sounded. F3K-1m3m30s is the same, and both F5K 4m profiles are 1 s out;
        # the other five happen to agree. The give-away is Remaining-2Mins.wav at
        # t=63 in F3K-3m3m30s — only 183-63 puts it at a whole 2:00. [I-24]
        wt_times = [c["t"] for c in self.cues if c["state"] == WT]
        lt_times = [c["t"] for c in self.cues if c["state"] == LT]

        # The working window closes on the end horn: the first StartEndHorn at t>0
        # (t==0 is the OPEN horn). Some profiles file it under WT, some under LT.
        end_horns = [c["t"] for c in self.cues
                     if c.get("wav") == "StartEndHorn.wav" and c["t"] > 0]
        if end_horns:
            self._wt_close = min(end_horns)
        elif lt_times:
            self._wt_close = min(lt_times)     # landing starts where working ended
        else:
            self._wt_close = max(wt_times) if wt_times else self.work_s
        # Landing ends on its last cue (the closing horn or long beep).
        self._lt_close = max(lt_times) if lt_times else self._wt_close + self.land_s

        # work_s/land_s stay NAME-derived: select_profile() matches them against the
        # competition's configured working time, which is the nominal 180 — not 183.
        if not span:
            self.work_s = self._wt_close
            self.land_s = self._lt_close - self._wt_close

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
                # Anything at or before the instant the window opens is not ours to
                # play: the engine fires the open horn there. The 3m F3K profiles
                # carry "1/2/3" at t=1..3 which land here once the close is anchored
                # correctly, and they would otherwise talk over the start.
                if key >= self.work_s:
                    continue
                self.working.setdefault(key, []).append(c)
            elif st == LT:
                key = self._lt_close - t       # seconds of landing time remaining
                self.landing.setdefault(key, []).append(c)



# Announcement marks for generated schedules, as (seconds-remaining, wav).
# A mark is used only when it falls strictly inside its window, so a 4-minute round
# calls 3/2/1 minutes and a 40-second one goes straight to 30/20.
_GEN_WORK_MARKS = [(m * 60, f"Remaining-{m}Mins.wav") for m in range(10, 1, -1)] + [
    (60, "Remaining-1Min.wav"),      # singular — the only irregular name
    (30, "Remaining-30Secs.wav"),
    (20, "Remaining-20Secs.wav"),
]
_GEN_PREP_MARKS = [(m * 60, f"TimeToStart-{m:02d}.00.wav") for m in range(5, 0, -1)] + [
    (30, "TimeToStart-00.30.wav"),
    (20, "TimeToStart-00.20.wav"),
]
_GEN_LAND_MARKS = [(20, "Remaining-20Secs.wav")]


def _countdown(span_s: int) -> list[tuple[int, str]]:
    """The final 10..1, for whatever fits. Voice here is notional — build_schedule
    substitutes 880Hz beeps across the last 10s of every phase, because the clips
    run longer than a second and clip against the horn."""
    out = []
    for rem in range(10, 0, -1):
        if rem < span_s:
            out.append((rem, "10Secs.wav" if rem == 10 else f"{rem}.wav"))
    return out


def _generate_profile(discipline: str, prep_s: int, work_s: int,
                      land_s: int) -> TimerProfile:
    """Synthesise a cue schedule for a working time no GliderScore profile covers.

    GliderScore ships profiles for a fixed set of working times, so a heat at any
    other length matched nothing and ran **silently** — a competition round with no
    audio at all, announced only by a warning in the log.

    Nothing about the cues is actually length-specific: every one is anchored to
    seconds *remaining*, and the final ten seconds of each phase are already beeps
    regardless of round length. The landing window was generalised long ago (see
    `lt_shift` in build_schedule); this is the same idea applied to working time.
    So the schedule is just "call each threshold that fits inside the window".

    A real profile always wins when one matches — it is the cadence pilots know,
    and it carries the test-flying calls (`TestFlyingStartsIn`, `TestFlyingEnding`,
    `NoFlyingAllowed`) that say when practice flying is permitted. Those are
    competition rules, not timing, so they are deliberately NOT invented here.
    """
    cues: list[dict] = []

    # Prep: keyed by seconds until the window opens, so t is negative.
    for secs, wav in _GEN_PREP_MARKS + _countdown(prep_s):
        if 0 < secs < prep_s:
            cues.append({"state": PT, "t": -secs, "wav": wav, "beepHz": 0, "beepMs": 0})

    # The window-OPEN signal, at the instant working time begins. GliderScore's own
    # profiles carry a 1 s 1000 Hz tone at WT t=0 and that is what pilots launch on
    # — the single most important cue in the round. Generated schedules had no open
    # horn at all: the round simply began in silence, with only the close horn 60 s
    # later to say anything had happened.
    #
    # It has to live in the cue list, not be fired by the engine at the phase
    # boundary: `horn()` exists for that and is called from nowhere, and
    # build_schedule() drives a heat from the RAW cues, not the bucketed tables.
    cues.append({"state": WT, "t": 0, "wav": "", "beepHz": 1000, "beepMs": 1000})

    # Working: t runs forward from the open, so remaining r sits at work_s - r.
    for secs, wav in _GEN_WORK_MARKS + _countdown(work_s):
        if 0 < secs < work_s:
            cues.append({"state": WT, "t": work_s - secs, "wav": wav,
                         "beepHz": 0, "beepMs": 0})

    # The close horn. The engine fires open/close itself and skips these, but the
    # boundary derivation reads it to find where the working window ends. [I-24]
    cues.append({"state": LT, "t": work_s, "wav": "StartEndHorn.wav",
                 "beepHz": 0, "beepMs": 0})

    lt_close = work_s + land_s
    for secs, wav in _GEN_LAND_MARKS + _countdown(land_s):
        if 0 < secs < land_s:
            cues.append({"state": LT, "t": lt_close - secs, "wav": wav,
                         "beepHz": 0, "beepMs": 0})
    # Closing long beep, and the anchor for _lt_close.
    cues.append({"state": LT, "t": lt_close, "wav": "", "beepHz": 1000, "beepMs": 1000})

    name = f"generated-{discipline}-{prep_s}s{work_s}s{land_s}s"
    return TimerProfile({"name": name, "cues": cues, "generated": True},
                        span=(prep_s, work_s, land_s))

# Seconds of idle after which the A2DP transport / SBC codec is considered cold.
# When cold, pre-silence is inserted into the next wav play so the codec reaches
# steady state before the audio content starts. Without this, announcements start
# quietly and ramp up over ~0.5s (SBC encoder startup artifact).
_TRANSPORT_IDLE_S = 2.0
_COLD_PRE_SILENCE_MS = 1200


class AudioEngine:
    def __init__(self) -> None:
        self._profiles: dict[str, TimerProfile] = {}
        self._active: TimerProfile | None = None
        self._queue: asyncio.Queue[dict] | None = None
        self._worker: asyncio.Task | None = None
        self._sched_task: asyncio.Task | None = None
        self._prep_offset: int = 0
        self._land_time_s: int | None = None
        self._current_proc: asyncio.subprocess.Process | None = None
        self._beep_cache: dict[tuple[int, int], str] = {}
        self._disabled = os.environ.get("F3K_AUDIO_DISABLE") == "1"
        self._loaded = False
        self._last_play_at: float = 0.0

    async def apply_saved_volume(self) -> None:
        """Re-apply the operator's saved volume (call once at startup)."""
        vol = audio_control.load_config().get("volume")
        if vol is not None:
            await audio_control.apply_volume(vol)

    def play_test(self) -> None:
        """Play a short sample (announcement + beep) to check output/volume.

        Drains any queued items and kills the current play first so every Test
        press is a clean restart — prevents rapid presses from stacking up items
        that play immediately after each other with no pre-silence on a cold transport.
        """
        if self._queue:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break
        self._kill_current()
        self._enqueue({"wav": "TimeToStart-00.30.wav", "pre_silence_ms": _COLD_PRE_SILENCE_MS})
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

    def select_profile(self, discipline: str, working_time_s: int,
                       prep_s: int | None = None,
                       land_s: int | None = None) -> str | None:
        """Pick the GliderScore profile matching discipline + working time.

        Returns the chosen profile name (or None). Prefers the discipline's
        standard prep length (F3K 3 min, F5K 5 min) when several work-time matches.

        Falls back to a generated schedule when nothing matches, so any working
        time gets audio. GliderScore only ships a handful of working times, and a
        heat at any other length used to run in total silence.
        """
        self._load_profiles()
        prefix = f"{discipline}-"
        candidates = [
            p for p in self._profiles.values()
            if p.name.startswith(prefix) and p.work_s == working_time_s
        ]
        if not candidates:
            if prep_s is None or land_s is None:
                self._active = None
                log.warning(
                    "[AUDIO] no %s profile for working_time=%ds and no prep/land "
                    "given to generate one — audio disabled for heat",
                    discipline, working_time_s,
                )
                return None
            self._active = _generate_profile(discipline, prep_s, working_time_s, land_s)
            log.info(
                "[AUDIO] no %s profile for working_time=%ds — generated '%s' "
                "(prep=%ds work=%ds land=%ds). Timing calls only; a heat that needs "
                "the test-flying announcements needs a real profile.",
                discipline, working_time_s, self._active.name,
                prep_s, working_time_s, land_s,
            )
            return self._active.name
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

    def build_schedule(self, prep_offset: int, land_time_s: int | None = None) -> list[tuple[float, dict]]:
        """Absolute cue schedule for the active profile, in seconds from sequence start.

        ``prep_offset`` is when the working window opens relative to sequence start
        (i.e. the competition's prep time). The profile's own cue times ``t`` are
        relative to the working-window open (negative during prep), so a cue plays at
        ``prep_offset + t`` seconds after the sequence starts. This anchors the
        window-open horn (t=0) exactly on the timer START broadcast, regardless of any
        difference between the competition prep length and the profile's own.

        ``land_time_s`` is the competition's actual landing window length. When it
        differs from the profile's own land_s, all LT cues are shifted by the
        difference so they land at the correct times within the actual window.
        """
        if not self._active:
            return []
        # How far to shift LT cues when competition landing time != profile landing time.
        lt_shift = (land_time_s - self._active.land_s) if land_time_s is not None else 0
        sched = []
        for c in self._active.cues:
            off = prep_offset + c["t"]
            # Re-anchor LT cues to the competition's actual landing window length.
            if lt_shift and c.get("state") == LT and c["t"] > self._active.work_s:
                off += lt_shift
            if off < -self._PREROLL_S:
                continue                      # genuinely before the sequence — drop
            # Replace per-second voice files (1.wav–10Secs.wav) in the last 10s of prep
            # with short beeps — the voice clips are longer than 1s and get clipped.
            if c.get("state") in (PT, TT, NF) and -10 <= c["t"] <= -1 and c.get("wav"):
                c = {"wav": "", "beepHz": 880, "beepMs": 150}
            # Same for the last 10s of working time — voice clips back up against the
            # close horn and fire late; short beeps land cleanly at each second mark.
            elif c.get("state") == WT and c.get("wav") and self._active.work_s - 10 <= c["t"] < self._active.work_s:
                c = {"wav": "", "beepHz": 880, "beepMs": 150}
            # Same for the last 10s of landing — voice clips during the landing window.
            elif c.get("state") == LT and c.get("wav") and self._active._lt_close - 10 <= c["t"] < self._active._lt_close:
                c = {"wav": "", "beepHz": 880, "beepMs": 150}
            sched.append((max(off, 0.0), c))  # clamp pre-roll cues to the start
        sched.sort(key=lambda x: x[0])
        return sched

    def start_schedule(self, prep_offset: int, land_time_s: int | None = None) -> None:
        """Begin lead-compensated playback of the active profile, anchored to now.

        Fires each cue ``lead_s`` seconds early so the *sound* — after fixed output
        latency (e.g. Bluetooth A2DP buffering) — emerges at the intended instant.
        """
        self.stop_schedule()
        self._prep_offset = prep_offset
        self._land_time_s = land_time_s
        sched = self.build_schedule(prep_offset, land_time_s)
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
        sched = self.build_schedule(self._prep_offset, self._land_time_s)
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
            # Timing instrumentation: intended vs actual, on the sequence clock.
            # "Does it sound right" cannot separate a late cue from a late round,
            # and the two phases drifted in opposite directions by ear — so log
            # numbers instead of asking. [audio-timing]
            actual = loop.time() - t0
            log.info("[AUDIO-T] off=%7.3f actual=%7.3f drift=%+6.0fms  %s",
                     offset, actual, (actual - offset) * 1000,
                     ",".join((c.get("wav") or f"beep{c.get('beepHz')}") for c in group))
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

    def _padded_wav(self, wav_name: str, pre_silence_ms: int) -> str | None:
        """Return path to a wav with pre_silence_ms of silence prepended.

        Keeps warmup and announcement in one aplay call so A2DP negotiates during
        the silence rather than between two separate processes.
        """
        cache_key = f"pad{pre_silence_ms}_{wav_name}"
        cached = self._beep_cache.get(cache_key)
        if cached and os.path.exists(cached):
            return cached
        src = _WAV_DIR / wav_name
        if not src.exists():
            return None
        with wave.open(str(src), "r") as r:
            nch, sw, rate = r.getnchannels(), r.getsampwidth(), r.getframerate()
            content = r.readframes(r.getnframes())
        silence = b"\x00" * (int(rate * pre_silence_ms / 1000) * nch * sw)
        out = os.path.join(tempfile.gettempdir(), f"f3k_pad{pre_silence_ms}_{wav_name}")
        with wave.open(out, "w") as w:
            w.setnchannels(nch)
            w.setsampwidth(sw)
            w.setframerate(rate)
            w.writeframes(silence + content)
        self._beep_cache[cache_key] = out
        return out

    async def _play(self, cue: dict) -> None:
        wav = cue.get("wav")
        if wav:
            if wav in _WAV_SUPPRESS:
                return
            wav = _WAV_ALIASES.get(wav, wav)
            pre = cue.get("pre_silence_ms", 0)
            if not pre and audio_control.load_config().get("bt_mac"):
                if time.monotonic() - self._last_play_at > _TRANSPORT_IDLE_S:
                    pre = _COLD_PRE_SILENCE_MS
            path = self._padded_wav(wav, pre) if pre else str(_WAV_DIR / wav)
            if not path or not os.path.exists(path):
                log.warning("[AUDIO] missing wav: %s", wav)
                return
            await self._aplay(path)
            self._last_play_at = time.monotonic()
        elif cue.get("beepMs"):
            await self._aplay(self._beep_wav(int(cue.get("beepHz", 0)), int(cue["beepMs"])))
            self._last_play_at = time.monotonic()

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
