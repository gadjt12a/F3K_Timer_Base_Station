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
from pathlib import Path

log = logging.getLogger("f3k")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "audio_config.json"
_DEFAULTS = {"bt_mac": None, "volume": 45}


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


def output_device() -> str:
    """ALSA device string the engine should play to (config BT speaker > env > jack)."""
    mac = load_config().get("bt_mac")
    if mac:
        return f"bluealsa:DEV={mac},PROFILE=a2dp"
    return os.environ.get("F3K_AUDIO_DEVICE") or "plughw:0,0"


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


async def _device_list(subcmd: str = "Paired") -> list[dict]:
    rc, out, _ = await _run(["bluetoothctl", "devices", subcmd])
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
    }


async def bt_scan(seconds: int = 8) -> list[dict]:
    await _run(["bluetoothctl", "--timeout", str(seconds), "scan", "on"], timeout=seconds + 5)
    known = {d["mac"] for d in await _device_list("Paired")}
    unpaired = [d for d in await _device_list() if d["mac"] not in known]
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


async def get_volume() -> int | None:
    ctrl = await _mixer_control()
    if not ctrl:
        return None
    _, out, _ = await _run(["amixer", "-D", "bluealsa", "sget", ctrl])
    m = re.search(r"\[(\d+)%\]", out)
    return int(m.group(1)) if m else None


async def apply_volume(pct: int) -> bool:
    """Set the speaker volume (0–100). Returns False if no BT mixer is present."""
    pct = max(0, min(100, int(pct)))
    ctrl = await _mixer_control()
    if not ctrl:
        return False
    rc, _, _ = await _run(["amixer", "-D", "bluealsa", "sset", ctrl, f"{pct}%"])
    return rc == 0


async def set_volume(pct: int) -> dict:
    ok = await apply_volume(pct)
    cfg = load_config()
    cfg["volume"] = max(0, min(100, int(pct)))
    save_config(cfg)
    return {"ok": ok, "volume": cfg["volume"],
            "error": None if ok else "no Bluetooth speaker connected"}
