---
name: pi-logs
description: Show recent f3k-server service logs from the Raspberry Pi. Use when the user asks for "Pi logs", "server logs", "what does the log say", "check the logs", or when debugging any base station issue after a deploy.
argument-hint: [lines]
allowed-tools: [PowerShell]
---

# Pi Service Logs

Fetch recent `f3k-server` logs from the Raspberry Pi.

## Arguments

`$ARGUMENTS`

If a number is given, use it as the line count. Default: 50 lines.

## Instructions

Run:

```powershell
ssh -i "$env:USERPROFILE\.ssh\f3k_pi" pi@f3kpi "sudo journalctl -u f3k-server -n <N> --no-pager"
```

Where `<N>` is the line count (default 50).

Display the output as-is. Call out any lines containing `ERROR`, `Exception`, `Traceback`, or `CRITICAL` so the user can spot them quickly.
