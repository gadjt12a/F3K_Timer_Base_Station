---
name: serial-monitor
description: Start, read, or stop the background serial logger for the ESP32 timer (COM4). Use when the user says "serial monitor", "check serial", "read the log", "watch serial", or "stop serial". The logger runs as a detached process and writes to tools/serial_log.txt which Claude can read with the Read tool.
argument-hint: [start|read|stop] [PORT|lines]
allowed-tools: [PowerShell, Read]
---

# Serial Monitor (Background Logger)

Runs `tools\serial_log.py` as a detached Windows process. Serial output is written to
`C:\Kris\Projects\F3K_Timer_1\tools\serial_log.txt` — Claude reads that file at any time with the Read tool.

## Arguments

`$ARGUMENTS`

- `start [PORT]` — start the logger (default COM4). Kills any existing instance first.
- `read [N]`     — show last N lines from the log (default 80).
- `stop`         — kill the background logger process.
- *(no args)*    — same as `start`.

---

## Instructions

### `start` (or no args)

**Step 1 — kill any existing logger**

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*serial_log*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
```

**Step 2 — clear the old log**

```powershell
"" | Set-Content "C:\Kris\Projects\F3K_Timer_1\tools\serial_log.txt" -Encoding utf8
```

**Step 3 — start the logger in background**

Determine PORT: if the user passed a port argument (e.g. `COM5`), use it; otherwise use `COM4`.

```powershell
$port   = "COM4"      # override if argument was given
$script = "C:\Kris\Projects\F3K_Timer_1\tools\serial_log.py"
$proc   = Start-Process python -ArgumentList "$script $port" -WindowStyle Hidden -PassThru
"Logger PID: $($proc.Id)"
```

**Step 4 — wait 2 seconds, then read first lines to confirm**

Wait 2 seconds (use `Start-Sleep 2`), then use the **Read tool** to read `C:\Kris\Projects\F3K_Timer_1\tools\serial_log.txt`.

If the first line contains `ERROR` or if the file is empty, report the error to the user (likely port not found or pyserial missing).

If the first line contains `Serial logger started`, report: "Logger running on PORT — reading serial_log.txt".

---

### `read [N]`

Use the **Read tool** to read `C:\Kris\Projects\F3K_Timer_1\tools\serial_log.txt`.

Default: show last 80 lines. If the user gave a number (e.g. `/serial-monitor read 200`), use that as the `limit`.

Call out any lines containing `[COMMS] PING send failed`, `[COMMS] TCP dropped`, `ERROR`, or `reconnect` so the user can spot connectivity events quickly.

---

### `stop`

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*serial_log*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "Stopped PID $($_.ProcessId)" }
```

Report how many processes were stopped.

---

## Log file path

`C:\Kris\Projects\F3K_Timer_1\tools\serial_log.txt`

This path is absolute so the skill works from either the Timer_1 or Timer_Project session.
