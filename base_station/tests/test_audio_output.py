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
        # Never let a test touch the developer's real ~/.asoundrc.
        self._home = tempfile.mkdtemp()
        self._orig_rc = ac._ASOUNDRC
        ac._ASOUNDRC = Path(self._home) / ".asoundrc"
        self._patch_aplay(_APLAY_L)

    def tearDown(self):
        ac._ASOUNDRC = self._orig_rc
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
            return_value=mock.Mock(stdout=output, stderr=""))
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
        # A named PCM, not plughw: USB needs a forced slave rate — see UsbRateTests.
        self.assertEqual(ac.output_device(), "f3k_out")
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


class MixerScaleTests(_Base):
    """Percentages must be MAPPED on hardware mixers, not raw. [I-31]"""

    def _mixer_for(self, mode):
        self._write({"output": mode, "bt_mac": "AA:BB:CC:DD:EE:FF"})
        calls = []

        async def fake_run(args, timeout=20.0):
            calls.append(args)
            return 0, "Simple mixer control 'PCM',0", ""

        async def go():
            with mock.patch.object(ac, "_run", fake_run):
                return await ac._active_mixer()

        import asyncio
        return asyncio.run(go())

    def test_hardware_mixers_use_mapped_percentages(self):
        """The Pi jack runs -102.39dB..+4dB, so a raw 20% is -81dB — playing, and
        completely inaudible. That is how a "working" test produced silence."""
        for mode in ("jack", "usb"):
            with self.subTest(mode=mode):
                dev, _ = self._mixer_for(mode)
                self.assertIn("-M", dev)

    def test_bluealsa_is_left_alone(self):
        """bluealsa softvol is already linear; -M would change tuned levels."""
        dev, _ = self._mixer_for("bt")
        self.assertEqual(dev, ["-D", "bluealsa"])


class UsbRateTests(_Base):
    """A USB device that lies about its rate must still play at the right speed."""

    def _rates(self, lo, hi):
        return mock.patch.object(
            ac, "_device_rates", return_value=(lo, hi))

    def test_usb_gets_a_named_pcm_that_forces_resampling(self):
        """plughw: cannot say "resample even though the device claims this rate",
        and the Jabra claims 16k then runs at 48k — so cues played 3x fast, which
        is worse than silence because it sounds like something is working."""
        self._write({"output": "usb"})
        with self._rates(8000, 48000):
            self.assertEqual(ac.output_device(), "f3k_out")
        conf = ac._ASOUNDRC.read_text()
        self.assertIn('pcm "hw:3,0"', conf)
        self.assertIn("rate 48000", conf)

    def test_rate_falls_back_when_48k_is_out_of_range(self):
        self._write({"output": "usb"})
        with self._rates(8000, 44100):
            ac.output_device()
        self.assertIn("rate 44100", ac._ASOUNDRC.read_text())

    def test_regenerating_does_not_duplicate_or_grow(self):
        self._write({"output": "usb"})
        with self._rates(8000, 48000):
            ac.output_device()
            first = ac._ASOUNDRC.read_text()
            ac.output_device()
        self.assertEqual(ac._ASOUNDRC.read_text(), first)
        self.assertEqual(first.count("pcm.f3k_out"), 1)

    def test_operator_content_in_asoundrc_is_preserved(self):
        """Never silently eat something the operator put there by hand."""
        ac._ASOUNDRC.write_text("pcm.mything { type null }\n")
        self._write({"output": "usb"})
        with self._rates(8000, 48000):
            ac.output_device()
        conf = ac._ASOUNDRC.read_text()
        self.assertIn("pcm.mything", conf)
        self.assertIn("pcm.f3k_out", conf)

    def test_unwritable_asoundrc_still_makes_a_noise(self):
        """Degrade to plughw rather than returning nothing at all."""
        self._write({"output": "usb"})
        with self._rates(8000, 48000),              mock.patch.object(ac.Path, "write_text", side_effect=OSError):
            self.assertEqual(ac.output_device(), "plughw:3,0")


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
        self.assertEqual(ac.output_device(), "f3k_out")


if __name__ == "__main__":
    unittest.main()
