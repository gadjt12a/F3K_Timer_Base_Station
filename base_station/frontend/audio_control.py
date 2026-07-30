"""F3K Base Station — audio output + Bluetooth speaker control.

Thin async wrappers around `bluetoothctl` and `amixer` (bluez-alsa), plus a small
persisted config so the operator can pick a BT speaker and volume from the web UI.

All operations run as the `pi` user without sudo:
- Volume:            amixer -D bluealsa (the A2DP soft-volume control)
- Connect/disconnect/scan of already-paired devices: bluetoothctl (via polkit)

The selected speaker MAC + volume live in ``audio_config.json`` (next to the DB).
``output_device()`` turns the config into the ALSA device string the engine plays to.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger("f3k")

# Serializes all access to the bluealsa device (playback via aplay + volume via
# amixer). Changing volume while a cue is playing otherwise makes bluealsa
# renegotiate and the in-flight aplay can hang, wedging the whole audio worker.
bluealsa_lock = asyncio.Lock()

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "audio_config.json"
# lead_s: seconds to fire cues EARLY, to compensate for fixed output latency
# (Bluetooth A2DP buffering). The operator measures the observed lag and sets it here.
_DEFAULTS = {"bt_mac": None, "volume": 45, "lead_s": 0.4}


# ---------------------------------------------------------------------------
# Persisted config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(_DEFAULTS)
    try:
        cfg.update(json.loads(_CONFIG_PATH.read_text()))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("[AUDIO] bad audio_config.json — using defaults")
    return cfg


def save_config(cfg: dict) -> None:
    try:
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception:
        log.exception("[AUDIO] failed to write audio_config.json")


def get_lead() -> float:
    """Seconds to fire audio cues early to compensate for output latency."""
    try:
        return max(0.0, float(load_config().get("lead_s", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def set_lead(seconds: float) -> dict:
    seconds = max(0.0, min(30.0, float(seconds)))
    cfg = load_config()
    cfg["lead_s"] = seconds
    save_config(cfg)
    return {"ok": True, "lead_s": seconds}


# How the operator chose to get sound out. Stored in audio_config.json as
# "output"; everything else about the audio path follows from it.
OUTPUT_MODES = ("jack", "usb", "bt")


def _alsa_cards() -> list[tuple[int, str]]:
    """[(card_number, description)] from `aplay -l`, in the order it lists them."""
    try:
        out = subprocess.run(["aplay", "-l"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return []
    return [(int(m.group(1)), m.group(2))
            for m in re.finditer(r"^card (\d+): (.+?),", out, re.M)]


def _find_card(*needles: str) -> int | None:
    """First card whose description mentions any needle. Matched by NAME, never by
    a fixed index: a USB device changes card number as things are plugged in, and
    on this Pi the HDMI outputs sit between the headphone jack and USB audio."""
    for num, desc in _alsa_cards():
        low = desc.lower()
        if any(n.lower() in low for n in needles):
            return num
    return None


def jack_card() -> int | None:
    return _find_card("headphones", "bcm2835")


def usb_card() -> int | None:
    return _find_card("usb")


def output_mode() -> str:
    """Selected output. Falls back to the pre-selector behaviour for old configs:
    a saved bt_mac used to be the only way Bluetooth got chosen."""
    cfg = load_config()
    mode = cfg.get("output")
    if mode in OUTPUT_MODES:
        return mode
    return "bt" if cfg.get("bt_mac") else "jack"


def output_device() -> str:
    """ALSA device string the engine should play to.

    ⚠ F3K_AUDIO_DEVICE overrides everything and is a developer escape hatch only.
    A hand-added `Environment="F3K_AUDIO_DEVICE=bluealsa:DEV=…"` line in the
    systemd unit on the field Pi silently pinned output to one specific speaker's
    MAC, so changing the device in the app did nothing at all and the config on
    disk was a lie. If it is set, the settings page says so rather than pretending
    the selection took effect.
    """
    env = os.environ.get("F3K_AUDIO_DEVICE")
    if env:
        return env

    mode = output_mode()
    if mode == "bt":
        mac = load_config().get("bt_mac")
        if mac:
            return f"bluealsa:DEV={mac},PROFILE=a2dp"
    elif mode == "usb":
        card = usb_card()
        if card is not None:
            return f"plughw:{card},0"
    # Jack, and the safety net for "BT selected but nothing paired" or "USB
    # selected but unplugged" — silence is the worst outcome, so fall back to the
    # output that is always physically present.
    card = jack_card()
    return f"plughw:{card if card is not None else 0},0"


def set_output(mode: str) -> dict:
    if mode not in OUTPUT_MODES:
        return {"ok": False, "error": f"unknown output {mode!r}"}
    cfg = load_config()
    cfg["output"] = mode
    save_config(cfg)
    return {"ok": True, "output": mode, "device": output_device()}


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

async def _run(args: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


# ---------------------------------------------------------------------------
# Bluetooth
# ---------------------------------------------------------------------------

_DEV_RE = re.compile(r"^Device ([0-9A-F:]{17}) (.+)$", re.MULTILINE)


async def _device_list(subcmd: str | None = "Paired") -> list[dict]:
    cmd = ["bluetoothctl", "devices"]
    if subcmd:
        cmd.append(subcmd)
    rc, out, _ = await _run(cmd)
    if rc != 0:
        return []
    return [{"mac": m.group(1), "name": m.group(2)} for m in _DEV_RE.finditer(out)]


async def _is_connected(mac: str) -> bool:
    _, out, _ = await _run(["bluetoothctl", "info", mac])
    return bool(re.search(r"Connected:\s*yes", out))


async def bt_status() -> dict:
    _, show, _ = await _run(["bluetoothctl", "show"])
    powered = bool(re.search(r"Powered:\s*yes", show))
    paired = await _device_list("Paired")
    connected_mac = None
    for d in paired:
        d["connected"] = await _is_connected(d["mac"])
        if d["connected"]:
            connected_mac = d["mac"]
    return {
        "powered": powered,
        "paired": paired,
        "connected_mac": connected_mac,
        "active_device": output_device(),
        "output": output_mode(),
        "jack_card": jack_card(),
        "usb_card": usb_card(),
        "usb_name": next((d for n, d in _alsa_cards() if n == usb_card()), None),
        # Truthy only when the developer escape hatch is pinning output, in which
        # case the selector below it is not actually in charge.
        "device_override": os.environ.get("F3K_AUDIO_DEVICE"),
    }


async def bt_scan(seconds: int = 8) -> list[dict]:
    await _run(["bluetoothctl", "--timeout", str(seconds), "scan", "on"], timeout=seconds + 5)
    known = {d["mac"] for d in await _device_list("Paired")}
    unpaired = [d for d in await _device_list(None) if d["mac"] not in known]
    # Devices advertising a real name (not just their MAC) sort first.
    def named(d: dict) -> bool:
        return d["name"] != d["mac"].replace(":", "-")
    return sorted(unpaired, key=lambda d: (not named(d), d["name"]))


async def bt_connect(mac: str) -> dict:
    """Pair (if needed), trust, connect; set as the active output speaker."""
    info_rc, info, _ = await _run(["bluetoothctl", "info", mac])
    if not re.search(r"Paired:\s*yes", info):
        prc, _, perr = await _run(["bluetoothctl", "pair", mac], timeout=30)
        if prc != 0:
            return {"ok": False, "error": f"pair failed: {perr.strip() or 'see logs'}"}
        await _run(["bluetoothctl", "trust", mac])
    rc, out, err = await _run(["bluetoothctl", "connect", mac], timeout=30)
    ok = (rc == 0 and "Connection successful" in out) or await _is_connected(mac)
    if ok:
        cfg = load_config()
        cfg["bt_mac"] = mac
        save_config(cfg)
        await apply_volume(cfg.get("volume", _DEFAULTS["volume"]))
    return {"ok": bool(ok), "error": None if ok else (err.strip() or "connect failed")}


async def bt_disconnect(mac: str) -> dict:
    rc, out, err = await _run(["bluetoothctl", "disconnect", mac])
    return {"ok": rc == 0, "error": None if rc == 0 else err.strip()}


# ---------------------------------------------------------------------------
# Volume (bluez-alsa soft-volume control for the active A2DP device)
# ---------------------------------------------------------------------------

async def _mixer_control() -> str | None:
    """Name of the bluealsa simple mixer control, e.g. 'WONDERBOOM A2DP'."""
    rc, out, _ = await _run(["amixer", "-D", "bluealsa", "scontrols"])
    if rc != 0:
        return None
    m = re.search(r"Simple mixer control '([^']+)'", out)
    return m.group(1) if m else None


async def _card_control(card: int) -> str | None:
    """First simple mixer control on an ALSA card. Name varies by device — the
    Pi's jack calls it 'PCM', a USB speakerphone might call it 'Speaker' or
    'Headset' — so it is discovered rather than assumed."""
    rc, out, _ = await _run(["amixer", "-c", str(card), "scontrols"])
    if rc != 0:
        return None
    m = re.search(r"Simple mixer control '([^']+)'", out)
    return m.group(1) if m else None


async def _active_mixer() -> tuple[list[str], str] | None:
    """(amixer device args, control name) for whatever output is selected.

    Volume has to follow the output: `amixer -D bluealsa` only exists while an
    A2DP transport is up, so it silently did nothing whenever sound was coming
    out of the jack.
    """
    mode = output_mode()
    if mode == "bt":
        ctrl = await _mixer_control()
        return (["-D", "bluealsa"], ctrl) if ctrl else None
    card = usb_card() if mode == "usb" else jack_card()
    if card is None:
        return None
    ctrl = await _card_control(card)
    # -M (mapped) is essential, not cosmetic. A hardware mixer's percentage is
    # linear across its RAW range, and the Pi's jack control runs -102.39dB..+4dB
    # — so a plain "20%" lands at -81dB, which is running but inaudible. That is
    # exactly what happened: Bluetooth softvol is linear so 20 sounded fine, and
    # the identical saved number was silence on the jack. -M maps percentages
    # perceptually, so the slider means roughly the same thing on every output.
    return (["-M", "-c", str(card)], ctrl) if ctrl else None


async def pcm_alive() -> bool:
    """True if the bluealsa A2DP PCM is really available (soft-volume control present).

    bluetoothctl can report a speaker 'connected' while the A2DP transport/PCM has
    idle-died, in which case aplay fails with 'No such device'. The presence of the
    bluealsa mixer control is a reliable signal that the PCM is actually there.
    Returns True for non-Bluetooth output (nothing to check).
    """
    if not load_config().get("bt_mac"):
        return True
    async with bluealsa_lock:
        return (await _mixer_control()) is not None


async def get_volume() -> int | None:
    async with bluealsa_lock:
        mixer = await _active_mixer()
        if not mixer:
            return None
        dev, ctrl = mixer
        _, out, _ = await _run(["amixer", *dev, "sget", ctrl])
    m = re.search(r"\[(\d+)%\]", out)
    return int(m.group(1)) if m else None


async def apply_volume(pct: int) -> bool:
    """Set the speaker volume (0–100). Returns False if no BT mixer is present.

    Serialized against playback (bluealsa_lock) so a volume change never collides
    with an in-flight aplay on the same device.
    """
    pct = max(0, min(100, int(pct)))
    async with bluealsa_lock:
        mixer = await _active_mixer()
        if not mixer:
            return False
        dev, ctrl = mixer
        rc, _, _ = await _run(["amixer", *dev, "sset", ctrl, f"{pct}%"])
    return rc == 0


async def set_volume(pct: int) -> dict:
    ok = await apply_volume(pct)
    cfg = load_config()
    cfg["volume"] = max(0, min(100, int(pct)))
    save_config(cfg)
    return {"ok": ok, "volume": cfg["volume"],
            "error": None if ok else f"no mixer for the selected output ({output_mode()})"}
