"""Firmware version ordering — the rule that stops a downgrade being offered.

The timer's OTA check originally treated *any* difference from the running build
as "an update is available", so a base station holding an old firmware.bin would
offer to take a timer backwards. The failure mode is a CD who updates the timers,
forgets the Pi, and then "updates" a timer straight back onto an older build in
the middle of a competition.

Two things have to hold, and both are easy to get wrong:

- Ordering is **numeric**. Comparing the strings is worse than the equality test
  it replaces, because "fw-v9" sorts above "fw-v28" lexically.
- A timer *ahead* of the base is not an error for the timer. It means the base
  station is stale, and the base has to say so — see `base_firmware_stale`.

The same comparison exists in the firmware (`_fwNum` in OtaUpdater.cpp). These
tests pin the Python half; the two must agree.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from frontend.app import _fw_num  # noqa: E402


class FwNumTests(unittest.TestCase):
    def test_parses_release_number(self):
        self.assertEqual(_fw_num("fw-v28"), 28)
        self.assertEqual(_fw_num("fw-v1"), 1)
        self.assertEqual(_fw_num("fw-v100"), 100)

    def test_tolerates_surrounding_whitespace(self):
        self.assertEqual(_fw_num("  fw-v28\n"), 28)

    def test_rejects_anything_not_a_release(self):
        for bad in (None, "", "v28", "fw-v", "fw-vX", "fw-v28-dirty",
                    "fw-v2.8", "28", "FW-V28"):
            with self.subTest(bad=bad):
                self.assertIsNone(_fw_num(bad))

    def test_ordering_is_numeric_not_lexical(self):
        """The whole point: "fw-v9" > "fw-v28" as strings, but 9 < 28."""
        self.assertLess("fw-v28", "fw-v9")            # the trap, as strings
        self.assertGreater(_fw_num("fw-v28"), _fw_num("fw-v9"))

    def test_double_digit_boundary(self):
        self.assertGreater(_fw_num("fw-v10"), _fw_num("fw-v9"))


class FwStateTests(unittest.TestCase):
    """The current/behind/ahead/unknown verdict served to every client."""

    @staticmethod
    def _state(timer_fw, cached_fw):
        """Mirror of the classification in api_timers()."""
        ota_n = _fw_num(cached_fw)
        t_n = _fw_num(timer_fw)
        if ota_n is None:
            return "unknown"
        if t_n is None:
            return "behind"
        if t_n > ota_n:
            return "ahead"
        if t_n < ota_n:
            return "behind"
        return "current"

    def test_equal_is_current(self):
        self.assertEqual(self._state("fw-v28", "fw-v28"), "current")

    def test_older_timer_is_behind(self):
        self.assertEqual(self._state("fw-v24", "fw-v28"), "behind")

    def test_newer_timer_is_ahead(self):
        """This is the case that means WE are out of date, not the timer."""
        self.assertEqual(self._state("fw-v28", "fw-v24"), "ahead")

    def test_no_cached_firmware_is_unknown(self):
        self.assertEqual(self._state("fw-v28", None), "unknown")

    def test_timer_without_fw_field_is_behind_not_unknown(self):
        """Pre-fw-v17 timers send no fw= — that is knowledge, not an unknown."""
        self.assertEqual(self._state(None, "fw-v28"), "behind")

    def test_lexical_comparison_would_get_this_wrong(self):
        """fw-v9 timer against a fw-v28 base: behind, despite sorting above it."""
        self.assertEqual(self._state("fw-v9", "fw-v28"), "behind")


if __name__ == "__main__":
    unittest.main()
