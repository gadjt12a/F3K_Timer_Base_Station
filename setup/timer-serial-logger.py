#!/usr/bin/env python3
"""Continuously log a USB-attached timer's serial output on the base station.

Why this exists: the base station lives in a building that blocks the field
Wi-Fi, so the timer and a laptop are rarely in the same place. Cabling the timer
to one of the Pi's USB ports turns the base station into a remote lab — the
firmware's serial output becomes readable over SSH/Tailscale from anywhere.

Design notes:

- **Holds the port open.** Opening the port asserts DTR and resets the ESP32, so
  a tool that opens and closes on each look would reboot the timer every time you
  glanced at it. One long-lived reader means exactly one reset, at start.
- **Survives the device disappearing.** The port vanishes and re-enumerates on
  every reboot, flash, or cable nudge. A plain `cat` dies there; this reopens.
- **Prefers the by-id path.** /dev/ttyACM0 is allocation order — plug in anything
  else and it can move. The by-id symlink is tied to the device itself.
- Lines are timestamped on arrival, so ordering against base station logs is
  possible even though the timer has no clock.

Not enabled by install.sh: this is a development aid, not part of running a
competition.
"""

import argparse
import glob
import os
import sys
import time
from datetime import datetime

try:
    import serial
except ImportError:
    sys.exit("pyserial missing — install with: sudo apt install -y python3-serial")

DEFAULT_LOG = os.path.expanduser("~/f3k_timer_serial.log")
BAUD = 115200
MAX_BYTES = 4 * 1024 * 1024      # rotate at 4 MB, keep one previous
REOPEN_DELAY_S = 2.0


def find_port(explicit=None):
    """Locate the timer's serial device, preferring the stable by-id symlink."""
    if explicit:
        return explicit if os.path.exists(explicit) else None
    # by-id is tied to the device; ttyACM0 is just allocation order
    for pattern in ("/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


def rotate(path):
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", help="serial device (default: auto-detect)")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--baud", type=int, default=BAUD)
    args = ap.parse_args()

    print(f"[logger] writing to {args.log}", flush=True)
    waiting_logged = False

    while True:
        port = find_port(args.port)
        if not port:
            if not waiting_logged:
                # Log the wait once, not every 2s — an unplugged timer overnight
                # would otherwise bury everything else in the journal.
                print("[logger] no serial device — waiting for the timer", flush=True)
                waiting_logged = True
            time.sleep(REOPEN_DELAY_S)
            continue
        waiting_logged = False

        try:
            with serial.Serial(port, args.baud, timeout=1) as ser:
                print(f"[logger] attached to {port}", flush=True)
                rotate(args.log)
                with open(args.log, "a", encoding="utf-8", errors="replace") as fh:
                    stamp = datetime.now().isoformat(timespec="seconds")
                    fh.write(f"\n===== attached {stamp} ({port}) =====\n")
                    fh.flush()
                    while True:
                        raw = ser.readline()
                        if not raw:
                            continue
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        fh.write(f"{ts}  {line}\n")
                        # Unbuffered: the whole point is reading this live from
                        # another machine while something is going wrong.
                        fh.flush()
                        rotate(args.log)
        except (serial.SerialException, OSError) as exc:
            print(f"[logger] {port} went away ({exc}) — reopening", flush=True)
            time.sleep(REOPEN_DELAY_S)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
