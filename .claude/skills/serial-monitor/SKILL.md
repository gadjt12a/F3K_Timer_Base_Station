---
name: serial-monitor
description: Start, read, or stop the background serial logger for the ESP32 timer. Use when the user says "serial monitor", "check serial", "read the log", "watch serial", or "stop serial". The logger runs as a detached process and writes to tools/serial_log.txt which Claude can read with the Read tool.
argument-hint: [start|read|stop] [PORT|lines]
allowed-tools: [Bash, Read]
---

# Serial Monitor (Background Logger)

Runs `tools/serial_log.py` as a detached process. Serial output is written to
`~/Projects/Repos/F3K_Timer_1/tools/serial_log.txt` — Claude reads that file at
any time with the Read tool.

## Which port

`serial_log.py` **autodetects** when given no argument, so normally you pass
nothing. It picks the single attached USB-serial device, and if there are none
or several it says so and lists them rather than guessing.

The Waveshare ESP32-S3 presents native USB CDC, so it usually appears as
`/dev/ttyACM0`. A board behind a CP2102/CH340 adapter appears as
`/dev/ttyUSB0`. To see what is attached:

```bash
ls -l /dev/serial/by-id/
```

Prefer a `/dev/serial/by-id/...` path when two boards are connected — those
names are stable across reboots and distinguish otherwise identical devices.

**If you get `Permission denied` on the port**, the user is not in the
`dialout` group *in this login session*. Group membership is established at
login, so adding it requires a logout/login (or `newgrp dialout`). Do not
suggest `sudo` as the fix.

## Arguments

`$ARGUMENTS`

- `start [PORT]` — start the logger (autodetects if PORT omitted). Kills any existing instance first.
- `read [N]`     — show last N lines from the log (default 80).
- `stop`         — kill the background logger process.
- *(no args)*    — same as `start`.

---

## Instructions

### `start` (or no args)

**Steps 1-3 — kill any existing logger, clear the log, start detached**

Run as one command. `pkill -f` matches the script name in the command line, the
same way the old PowerShell version matched on `CommandLine`:

```bash
pkill -f serial_log.py 2>/dev/null
: > ~/Projects/Repos/F3K_Timer_1/tools/serial_log.txt
nohup python3 ~/Projects/Repos/F3K_Timer_1/tools/serial_log.py PORT_ARG \
  > /dev/null 2>&1 &
echo "Logger PID: $!"
sleep 2
```

Replace `PORT_ARG` with the port if the user gave one, or **omit it entirely**
to let the script autodetect. The `sleep 2` is part of the same command so the
log has content before the next step reads it.

**Step 4 — confirm it started**

Use the **Read tool** to read
`~/Projects/Repos/F3K_Timer_1/tools/serial_log.txt`.

If the first line contains `ERROR` or the file is empty, report the error to the
user (likely no device attached, the wrong port, missing `pyserial`, or the
`dialout` group issue above).

If the first line contains `Serial logger started`, report: "Logger running on
PORT — reading serial_log.txt".

---

### `read [N]`

Use the **Read tool** to read
`~/Projects/Repos/F3K_Timer_1/tools/serial_log.txt`.

Default: show last 80 lines. If the user gave a number (e.g.
`/serial-monitor read 200`), use that as the `limit`.

Call out any lines containing `[COMMS] PING send failed`, `[COMMS] TCP dropped`,
`ERROR`, or `reconnect` so the user can spot connectivity events quickly.

---

### `stop`

```bash
pkill -ef serial_log.py
```

`-e` echoes what was killed, so the output names each PID. If nothing matched,
`pkill` prints nothing and exits 1 — report that no logger was running rather
than treating it as a failure.

---

## Log file path

`~/Projects/Repos/F3K_Timer_1/tools/serial_log.txt`

Written as a `~` path rather than a bare relative one so the skill works from
either the Timer_1 or Timer_Project session, which was the point of the
absolute path it replaced.
