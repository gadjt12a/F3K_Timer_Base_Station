"""Cue-timing tests for the vendored GliderScore profiles.

The audio set was silently dead for eight sessions because a missing wav only
logs a warning. These tests fail loudly instead — both for a wav that cannot be
resolved and for a countdown that fires at the wrong second.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from frontend import audio  # noqa: E402


def _profile(name: str) -> audio.TimerProfile:
    eng = audio.AudioEngine()
    eng._load_profiles()
    p = eng._profiles.get(name)
    if p is None:
        raise unittest.SkipTest(f"profile {name} not vendored")
    return p


def _spoken(table, key):
    """Wavs at this key, alias-resolved the way the engine resolves them at play."""
    return sorted(audio._WAV_ALIASES.get(c.get("wav") or "", c.get("wav") or "")
                  for c in table.get(key, []))


class WindowBoundaryTests(unittest.TestCase):
    """GliderScore's t-space does not agree with its own profile names. [I-24]"""

    # name -> t of the end horn, as it actually appears in the schedule
    EXPECTED_CLOSE = {
        "F3K-3m3m30s": 183,
        "F3K-1m3m30s": 183,
        "F3K-3m7m30s": 420,
        "F3K-3m10m30s": 600,
        "F3K-3m15m30s": 900,
        "F5K-5m10m15s": 600,
        "F5K-5m7m15s": 420,
        "F5K-5m4m15s": 241,
        "F5K-15s4m15s": 241,
    }

    def test_close_anchored_to_end_horn_not_name(self):
        for name, close in self.EXPECTED_CLOSE.items():
            with self.subTest(name=name):
                self.assertEqual(_profile(name)._wt_close, close)

    def test_selection_span_stays_nominal(self):
        # work_s feeds select_profile(), which matches the competition's configured
        # working time. It must stay 180, even though the window closes at 183.
        p = _profile("F3K-3m3m30s")
        self.assertEqual(p.work_s, 180)
        self.assertEqual(p.land_s, 30)


class CountdownTimingTests(unittest.TestCase):

    def test_number_calls_match_seconds_remaining(self):
        """A spoken number must land on its own second.

        Asserted over whatever numbers a profile actually schedules rather than a
        fixed 10..1 — F3K-3m15m30s legitimately counts 14..10 then 5..1, a gap in
        GliderScore's data, not a skew. The invariant that matters is that "N" is
        spoken with N seconds left, which is exactly what the boundary bug broke.
        """
        for name in WindowBoundaryTests.EXPECTED_CLOSE:
            for phase in ("working", "landing"):
                with self.subTest(name=name, phase=phase):
                    table = getattr(_profile(name), phase)
                    seen = 0
                    for key in table:
                        for wav in _spoken(table, key):
                            stem = wav[:-4] if wav.endswith(".wav") else wav
                            if stem.isdigit():
                                self.assertEqual(int(stem), key)
                                seen += 1
                    self.assertGreater(seen, 0, "no number cues at all")

    def test_ten_second_call_is_at_ten(self):
        for name in WindowBoundaryTests.EXPECTED_CLOSE:
            for phase in ("working", "landing"):
                with self.subTest(name=name, phase=phase):
                    table = getattr(_profile(name), phase)
                    self.assertIn("10Secs.wav", _spoken(table, 10))

    def test_remaining_calls_land_on_whole_times(self):
        p = _profile("F3K-3m3m30s")
        self.assertIn("Remaining-2Mins.wav", _spoken(p.working, 120))
        self.assertIn("Remaining-1Min.wav", _spoken(p.working, 60))
        self.assertIn("Remaining-30Secs.wav", _spoken(p.working, 30))
        self.assertIn("Remaining-20Secs.wav", _spoken(p.working, 20))

    def test_nothing_fires_as_the_window_opens(self):
        """The engine owns the open horn; a profile cue there talks over it."""
        for name in WindowBoundaryTests.EXPECTED_CLOSE:
            with self.subTest(name=name):
                p = _profile(name)
                for key in p.working:
                    self.assertLess(key, p.work_s)


class WavResolutionTests(unittest.TestCase):

    def test_every_f3k_f5k_cue_resolves_to_a_file(self):
        eng = audio.AudioEngine()
        eng._load_profiles()
        missing = []
        for name, p in eng._profiles.items():
            if not (name.startswith("F3K-") or name.startswith("F5K-")):
                continue          # F3B/F3J/F5J profiles are unreachable by selection
            for c in p.cues:
                wav = c.get("wav") or ""
                if not wav or wav in audio._WAV_SUPPRESS:
                    continue
                resolved = audio._WAV_ALIASES.get(wav, wav)
                if not (audio._WAV_DIR / resolved).is_file():
                    missing.append(f"{name}: {resolved}")
        self.assertEqual(missing, [], f"missing wavs: {missing}")

    def test_selection_cannot_reach_other_disciplines(self):
        eng = audio.AudioEngine()
        for disc in ("F3K", "F5K"):
            for work_s in (180, 240, 420, 600, 900):
                chosen = eng.select_profile(disc, work_s)
                if chosen:
                    self.assertTrue(chosen.startswith(f"{disc}-"))


if __name__ == "__main__":
    unittest.main()
