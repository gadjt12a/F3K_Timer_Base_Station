---
name: flash-firmware
description: Build and upload firmware to the ESP32 timer device. Use when the user asks to "flash the timer", "flash firmware", "upload to the device", or after making changes to source files under C:\Kris\Projects\F3K_Timer_1.
allowed-tools: [PowerShell]
---

# Flash Timer Firmware

Build and upload firmware to the Waveshare ESP32-S3 timer device on COM4.

## Instructions

Run this PowerShell command and show the last 20 lines of output:

```powershell
& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run -e waveshare --target upload --upload-port COM4 --project-dir "C:\Kris\Projects\F3K_Timer_1" 2>&1 | Select-Object -Last 20
```

A successful flash ends with `Hard resetting via RTS pin...` and `[SUCCESS]`.

If the port is not found, tell the user to check the USB cable is connected and the device is on (short-press the PWR/left button to wake it). Do not suggest other COM ports unless the user confirms COM4 is wrong.

After flashing, do NOT open the serial monitor. If the user wants serial output, tell them to run:
```
! pio device monitor --environment waveshare --baud 115200 --project-dir "C:\Kris\Projects\F3K_Timer_1"
```

## Device Details

- Board: Waveshare ESP32-S3-Touch-LCD-1.28 (CO5300 QSPI display)
- Port: COM4
- MAC: `28:84:85:55:1e:b0`
- Project dir: `C:\Kris\Projects\F3K_Timer_1`
- Environment: `waveshare`
