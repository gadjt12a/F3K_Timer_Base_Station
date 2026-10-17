---
name: flash-firmware
description: Build and upload firmware to the ESP32 timer device. Use when the user asks to "flash the timer", "flash firmware", "upload to the device", or after making changes to firmware source files.
allowed-tools: [Bash]
---

# Flash Timer Firmware

Build and upload firmware to the Waveshare ESP32-S3 timer device.

## Instructions

Run this command and show the last 20 lines of output:

```bash
pio run -e waveshare --target upload --project-dir ~/Projects/Repos/F3K_Timer_1 2>&1 | tail -20
```

`--upload-port` is omitted on purpose: PlatformIO autodetects a single attached
board, which is right more often than a hardcoded port. Pass
`--upload-port /dev/ttyACM0` (or a stable `/dev/serial/by-id/...` path) only if
two boards are connected and it picks the wrong one. `ls -l /dev/serial/by-id/`
shows what is attached.

A successful flash ends with `Hard resetting via RTS pin...` and `[SUCCESS]`.

If the port is not found, tell the user to check the USB cable is connected and the device is on (short-press the PWR/left button to wake it). Do not go hunting through other `/dev/tty*` devices unless the user confirms the autodetected one is wrong.

If it fails with `Permission denied` on the port, the user is not in the `dialout` group *in this login session*. Group membership is established at login, so it needs a logout/login (or `newgrp dialout`) — not `sudo`.

After flashing, do NOT open the serial monitor. If the user wants serial output, tell them to run:
```
! pio device monitor --environment waveshare --baud 115200 --project-dir ~/Projects/Repos/F3K_Timer_1
```

## Release vs. Development Flash

If this is a milestone flash (end of a session, feature complete, ready to test in the field),
run the `/release-firmware` skill first. It builds, writes `fw_version.h`, snapshots binaries
to `firmware/releases/fw-vN/`, updates `firmware/ota/`, commits, and tags `fw-vN`.
Then flash normally. Skip this for quick iterative dev flashes during active coding.

## Device Details

- Board: Waveshare ESP32-S3-Touch-LCD-1.28 (CO5300 QSPI display)
- Port: autodetected — `/dev/ttyACM0` typically, since the S3 presents native USB CDC
- MAC: `28:84:85:55:1e:b0`
- Project dir: `~/Projects/Repos/F3K_Timer_1`
- Environment: `waveshare`
