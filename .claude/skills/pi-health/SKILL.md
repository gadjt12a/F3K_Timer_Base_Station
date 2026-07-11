---
name: pi-health
description: Check whether the f3k-server is running and the HTTP API is responding on the Raspberry Pi. Use when the user asks "is the Pi running?", "is the server up?", "check Pi health", or to verify a deploy succeeded.
allowed-tools: [PowerShell]
---

# Pi Health Check

Verify the Raspberry Pi f3k-server service is active and the HTTP API is responding.

## Instructions

Run a single combined check:

```powershell
ssh -i "$env:USERPROFILE\.ssh\f3k_pi" pi@f3kpi "sudo systemctl is-active f3k-server && curl -s http://localhost:8080/health"
```

Report:
- Service state (`active` / `inactive` / `failed`)
- HTTP `/health` response body

If either check fails, automatically fetch the last 30 log lines to help diagnose:

```powershell
ssh -i "$env:USERPROFILE\.ssh\f3k_pi" pi@f3kpi "sudo journalctl -u f3k-server -n 30 --no-pager"
```
