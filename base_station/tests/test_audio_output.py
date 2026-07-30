"""Audio output selection: 3.5mm jack / USB / Bluetooth.

Output used to be implied by whether a bt_mac happened to be saved, with the real
decision hidden in an F3K_AUDIO_DEVICE line hand-added to the systemd unit on the
field Pi. Changing the device in the app did nothing and the config on disk was a
lie. These tests pin the selection, the fallbacks, and the override.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from frontend import audio_control as ac  # noqa: E402

# Realistic `aplay -l` from the field Pi: jack on 0, HDMI on 1 and 2, USB on 3.
# The gap matters — anything that assumed "USB is card 1" would pass on a simpler
# machine and send every cue to an unplugged HDMI port here.
_APLAY_L = """**** List of PLAYBACK Hardware Devices ****
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
card 1: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
card 2: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
card 3: USB [Jabra SPEAK 510 USB], device 0: USB Audio [USB Audio]
"""

_APLAY_NO_USB = "\n".join(_APLAY_L.splitlines()[:4]) + "\n"


class _Base(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        Path(self.path).write_text("{}")   # mkstemp leaves it empty, which is not valid JSON
        self._orig_path = ac._CONFIG_PATH
        ac._CONFIG_PATH = Path(self.path)
        self._orig_env = os.environ.pop("F3K_AUDIO_DEVICE", None)
        self._patch_aplay(_APLAY_L)

    def tearDown(self):
        ac._CONFIG_PATH = self._orig_path
        self._aplay.stop()
        os.unlink(self.path)
        if self._orig_env is not None:
            os.environ["F3K_AUDIO_DEVICE"] = self._orig_env
        else:
            os.environ.pop("F3K_AUDIO_DEVICE", None)

    def _patch_aplay(self, output):
        if hasattr(self, "_aplay"):
            self._aplay.stop()
        self._aplay = mock.patch.object(
            ac.subprocess, "run",
            return_value=mock.Mock(stdout=output))
        self._aplay.start()

    def _write(self, cfg):
        Path(self.path).write_text(json.dumps(cfg))


class CardResolutionTests(_Base):
    def test_cards_are_found_by_name_not_index(self):
        self.assertEqual(ac.jack_card(), 0)
        self.assertEqual(ac.usb_card(), 3)

    def test_missing_usb_reports_none(self):
        self._patch_aplay(_APLAY_NO_USB)
        self.assertIsNone(ac.usb_card())
        self.assertEqual(ac.jack_card(), 0)

    def test_no_sound_cards_at_all_does_not_raise(self):
        self._patch_aplay("**** List of PLAYBACK Hardware Devices ****\n")
        self.assertIsNone(ac.jack_card())
        self.assertEqual(ac.output_device(), "plughw:0,0")


class OutputSelectionTests(_Base):
    def test_each_mode_maps_to_its_device(self):
        self._write({"output": "jack"})
        self.assertEqual(ac.output_device(), "plughw:0,0")
        self._write({"output": "usb"})
        self.assertEqual(ac.output_device(), "plughw:3,0")
        self._write({"output": "bt", "bt_mac": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(ac.output_device(),
                         "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")

    def test_bt_without_a_speaker_falls_back_to_the_jack(self):
        """Silence is the worst outcome, so fall back to the output that is always
        physically present rather than playing to a device that is not there."""
        self._write({"output": "bt", "bt_mac": None})
        self.assertEqual(ac.output_device(), "plughw:0,0")

    def test_usb_unplugged_falls_back_to_the_jack(self):
        self._patch_aplay(_APLAY_NO_USB)
        self._write({"output": "usb"})
        self.assertEqual(ac.output_device(), "plughw:0,0")

    def test_set_output_rejects_nonsense(self):
        self.assertFalse(ac.set_output("hdmi")["ok"])
        self._write({"output": "jack"})
        self.assertEqual(ac.output_mode(), "jack")

    def test_set_output_persists(self):
        ac.set_output("usb")
        self.assertEqual(ac.output_mode(), "usb")
        self.assertEqual(json.loads(Path(self.path).read_text())["output"], "usb")


class BackCompatTests(_Base):
    def test_old_config_with_a_saved_mac_still_means_bluetooth(self):
        """Before the selector, a saved bt_mac was the only way BT got chosen.
        An upgrade must not silently move a competition to the wrong output."""
        self._write({"bt_mac": "AA:BB:CC:DD:EE:FF", "volume": 20})
        self.assertEqual(ac.output_mode(), "bt")
        self.assertEqual(ac.output_device(),
                         "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")

    def test_old_config_without_a_mac_means_the_jack(self):
        self._write({"volume": 20})
        self.assertEqual(ac.output_mode(), "jack")

    def test_an_explicit_choice_beats_a_leftover_mac(self):
        self._write({"output": "jack", "bt_mac": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(ac.output_device(), "plughw:0,0")


class EnvOverrideTests(_Base):
    def test_env_override_wins_and_is_reported(self):
        """The trap that cost an evening: a hand-added Environment= line in the
        systemd unit pinned output to one speaker's MAC, so the app's own setting
        did nothing. It still wins — it is a developer escape hatch — but the
        status must expose it so the UI can say the selector is not in charge."""
        os.environ["F3K_AUDIO_DEVICE"] = "bluealsa:DEV=11:22:33:44:55:66,PROFILE=a2dp"
        self._write({"output": "jack"})
        self.assertEqual(ac.output_device(),
                         "bluealsa:DEV=11:22:33:44:55:66,PROFILE=a2dp")
        self.assertEqual(os.environ.get("F3K_AUDIO_DEVICE"),
                         "bluealsa:DEV=11:22:33:44:55:66,PROFILE=a2dp")

    def test_no_override_leaves_the_selection_in_charge(self):
        self._write({"output": "usb"})
        self.assertEqual(ac.output_device(), "plughw:3,0")


if __name__ == "__main__":
    unittest.main()
