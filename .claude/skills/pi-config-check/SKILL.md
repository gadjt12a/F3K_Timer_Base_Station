---
name: pi-config-check
description: Check that Pi OS-level config is captured in the repo before committing. Use before any commit that touched setup/ or Pi config, when the user says "commit", "check pi config", "check for drift", or after any change applied to the Pi over SSH.
allowed-tools: [Bash, Read, Edit, Grep]
---

# Pi Config Check

Catch Pi OS-level changes that exist on a running Pi but not in the repo, **before** the
commit that should have carried them.

## Why this exists

Every Pi-side fix in this project was originally applied by hand over SSH: the hostapd
watchdog, `disable_usb_sg=1`, `ctrl_interface=`, `bind-dynamic`, the `wlan1-setup` poll
loop. Each one worked, each one was never written into a script, and each was then
silently reverted the next time `upgrade-to-dual-ap.sh` ran — because that script rewrites
those files wholesale. One of them (`bind-dynamic`) was even recorded in notes as "fixed
and verified" while the Pi had long since lost it.

The failure is always the same shape: **the fix is on the Pi, not in the repo, and nobody
notices until a Pi is re-imaged or a tester's unit behaves differently.**

## Run it

```bash
ssh -i ~/.ssh/f3k_pi pi@100.115.187.90 'sudo bash ~/f3k_repo/setup/apply-system-config.sh --check'
```

Use the Tailscale IP `100.115.187.90` — the direct-cable address `10.0.1.12` only works
when the PC is cabled to the Pi and set to `10.0.1.1/24`. If the Pi's repo is older than
the working copy, scp the local script to `/tmp` and run that instead so you are checking
against the version about to be committed:

```bash
scp -i ~/.ssh/f3k_pi setup/apply-system-config.sh pi@100.115.187.90:/tmp/apply.sh
ssh -i ~/.ssh/f3k_pi pi@100.115.187.90 'sed -i "s/\r$//" /tmp/apply.sh && sudo bash /tmp/apply.sh --check'
```

`--check` is read-only: it reports what *would* change and restarts nothing.
**Exit 0 = in sync. Exit 1 = something needs attention.**

## Step 1 — CONFIG_VERSION guard (do this even if the Pi is unreachable)

If `setup/apply-system-config.sh` is modified in this commit, `CONFIG_VERSION` **must** be
bumped. Deployed Pis skip the applier when their recorded version already matches, so an
unbumped change reaches nobody — it will sit in the repo looking applied and silently do
nothing on every unit in the field.

```bash
git diff HEAD --stat -- setup/apply-system-config.sh
git diff HEAD -- setup/apply-system-config.sh | grep -E '^[+-]CONFIG_VERSION='
```

- Script changed **and** `CONFIG_VERSION` changed → fine.
- Script changed, `CONFIG_VERSION` unchanged → **stop and tell the user.** Offer to bump it.
- Script unchanged → nothing to check.

Only a change to what gets *applied* needs a bump. Editing a comment or the `--check`
reporting does not, but bumping anyway is harmless — the applier is idempotent.

## Step 2 — Read the drift report

**"IN SYNC"** — nothing to do.

**`[WOULD CHANGE]` items** — the Pi is *behind* the repo. Usually harmless (that Pi just
hasn't been updated), but confirm the change is intentional and not a fix someone made on
the Pi that the repo is about to stomp.

**`[UNMANAGED]` items** — this is the one that matters. Config exists on the Pi that no
script owns. For each, decide:

| Finding | Action |
|---|---|
| A fix we made over SSH | Fold into `apply-system-config.sh`, bump `CONFIG_VERSION`. This is the whole point. |
| A stray `.bak`/`.save` in `/etc/dnsmasq.d` | **Live config** — dnsmasq's `CONFIG_DIR` excludes only `.dpkg-*`, so it is being parsed. Delete it. This once took DHCP down on both networks. |
| A stray backup elsewhere | Harmless clutter (hostapd is given explicit file paths and never scans its directory). Suggest deleting. |
| An OS package file | Add to `MANAGED_PATHS` with a comment saying who owns it, so it stops being reported. |

Do not "fix" an unmanaged file on the Pi to make the report clean. The report is asking
where the config should *live*, not what the Pi should look like.

## Step 3 — Report

State plainly whether the commit is safe:

- `CONFIG_VERSION` correct (or not applicable)
- Pi in sync, or what differs
- Anything unmanaged, and what you propose doing about it

If nothing needs changing, say so in one line and let the commit proceed. Do not block a
commit over leftover `.bak` clutter — mention it and move on.

## When the Pi is unreachable

Step 1 still runs and is the higher-value check. Say clearly that the live drift scan was
skipped, so the user knows the commit went in unverified against real hardware.

## Rules

- **Never** run the applier without `--check` from this skill. Applying config is a
  deliberate act, not a side effect of committing.
- Use the **Bash tool** for ssh (PowerShell mangles remote quoting).
- Pi OS config is not in git; the scripts that produce it are. That distinction is the
  entire reason this check exists.
