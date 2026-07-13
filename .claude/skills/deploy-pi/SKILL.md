---
name: deploy-pi
description: Deploy base station files to the Raspberry Pi and restart f3k-server. Use when the user asks to "deploy to Pi", "push to Pi", "update the Pi", "send files to Pi", or after editing any file under base_station/.
argument-hint: [file ...]
allowed-tools: [PowerShell, Glob, Read]
---

# Deploy to Pi

Copy changed base station files to the Raspberry Pi at `f3kpi` and restart the service.

## Arguments

`$ARGUMENTS`

If file paths are given, deploy only those files (paths relative to `C:\Kris\Projects\F3K_Timer_Project\base_station\`, e.g. `frontend/app.py frontend/templates/run.html`).

If no arguments are given, look at which base_station files were modified in this conversation and deploy those. If that is ambiguous, deploy all Python and HTML files in base_station/.

## File Mapping (local → remote)

| Local (`base_station/`) | Remote (`~/f3k_base/`) |
|---|---|
| `server.py` | `server.py` |
| `frontend/*.py` | `frontend/*.py` |
| `frontend/templates/*.html` | `frontend/templates/*.html` |
| `frontend/data/*.json` | `frontend/data/*.json` |

## Instructions

1. For each file to deploy, run:
   ```powershell
   scp -i "$env:USERPROFILE\.ssh\f3k_pi" "C:\Kris\Projects\F3K_Timer_Project\base_station\<local-path>" "pi@f3kpi:~/f3k_base/<remote-path>"
   ```
   You can scp multiple files in one command by listing them before the destination — but the destination must be a directory (e.g. `pi@f3kpi:~/f3k_base/frontend/templates/`), not a file path, when deploying multiple files to the same directory.

2. After all files are copied, touch each deployed .py file on the Pi to ensure its mtime is newer than any cached .pyc (scp preserves source timestamps, which can match an existing .pyc and cause Python to use stale bytecode):
   ```powershell
   ssh -i "$env:USERPROFILE\.ssh\f3k_pi" pi@f3kpi "touch ~/f3k_base/<remote-path> [...]"
   ```
   Touch only the files that were actually deployed. Use separate paths per file, or a glob if all are in the same directory.

3. After touching, restart and confirm:
   ```powershell
   ssh -i "$env:USERPROFILE\.ssh\f3k_pi" pi@f3kpi "sudo systemctl restart f3k-server && sleep 2 && sudo systemctl is-active f3k-server"
   ```

3. Report which files were deployed and whether the service came back `active`.

## Connection Details

- SSH key: `$env:USERPROFILE\.ssh\f3k_pi`
- Host alias: `f3kpi` → `pi@10.0.1.12`
- Pi must be reachable on the eth0 cable (PC set to 10.0.1.1/24) or via the F3K_BASE WiFi AP
