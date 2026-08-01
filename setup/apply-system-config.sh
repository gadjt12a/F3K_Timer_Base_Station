#!/bin/bash
# apply-system-config.sh — bring this Pi's OS-level config up to the current spec.
#
# Run automatically by "Update from GitHub" (POST /api/system/update) after the
# git pull, so Pi-side fixes reach every deployed unit instead of living only on
# whichever box someone SSH'd into. Safe to run by hand:
#
#     sudo bash ~/f3k_repo/setup/apply-system-config.sh
#
# ── Design rules, all of which exist because a fielded Pi may be in another city
#    with no out-of-band access (no Tailscale, no console, no remote hands) ──
#
#   1. NEVER touch eth0, NetworkManager, or anything carrying the admin session.
#      upgrade-to-dual-ap.sh resets eth0 to DHCP — fine for a one-time in-person
#      bootstrap, fatal for an unattended update. If a wlan change goes wrong the
#      operator must still be able to SSH in over the wire and undo it.
#   2. Surgical edits only — ensure/replace individual lines, never `cat >` a
#      whole config. A wholesale rewrite would force interface=wlan1 onto a
#      single-AP Pi that serves F3K_BASE from wlan0 and take its AP down. The
#      only files written whole are ones we own outright (the watchdog).
#   3. Only manage files that already exist (except our own watchdog), so this
#      never imposes a topology the box wasn't already running.
#   4. Restart a service only if its own config actually changed, then verify it
#      came back healthy — and if it didn't, restore the backup and restart
#      again. A failed apply must leave the box no worse than it started.
#
# Idempotent: a Pi already at this spec makes no changes and restarts nothing.

#
# --check runs as a read-only dry run: reports what *would* change, plus any
# F3K-looking config on the box that no script manages, and exits 1 if it finds
# either. The /pi-config-check skill uses it before a commit so a fix applied by
# hand over SSH cannot quietly fail to make it into the repo — which is how the
# watchdog, disable_usb_sg and bind-dynamic all went missing.

set -uo pipefail   # deliberately NOT -e: failures are handled with rollback

CONFIG_VERSION=3
STATE_FILE=/var/lib/f3k/system-config.version
BACKUP_DIR=/var/backups/f3k-system-config

HOSTAPD_CONFS=(/etc/hostapd/hostapd.conf /etc/hostapd/hostapd-ops.conf)
DNSMASQ_CONFS=(/etc/dnsmasq.d/f3k-timer.conf /etc/dnsmasq.d/f3k-ops.conf)

DRY_RUN=0
[ "${1:-}" = "--check" ] && DRY_RUN=1

# Every OS path this project owns, across both this script and
# upgrade-to-dual-ap.sh. Anything F3K-shaped outside this list is unmanaged and
# will be reported by --check.
MANAGED_PATHS=(
    /etc/modprobe.d/mt76_usb.conf
    /etc/hostapd/hostapd.conf
    /etc/hostapd/hostapd-ops.conf
    /etc/dnsmasq.d/f3k-timer.conf
    /etc/dnsmasq.d/f3k-ops.conf
    /etc/systemd/system/wlan0-setup.service
    /etc/systemd/system/wlan1-setup.service
    /etc/systemd/system/hostapd.service.d/override.conf
    /etc/nftables.d/f3k-captive.conf
    /etc/NetworkManager/conf.d/99-unmanaged-wlan.conf
    /usr/local/bin/hostapd-watchdog.sh
    /etc/cron.d/hostapd-watchdog
    /etc/systemd/system/f3k-server.service   # written by migrate-to-git.sh
    /etc/hostapd/ifupdown.sh                 # shipped by the hostapd package
    # Listed as known-ours but DELIBERATELY not applied: the serial logger
    # (setup/timer-serial-logger.py) is a bench aid for a Pi with a timer cabled
    # to it, not part of a competition install. Without this line the drop-in
    # scan reports it on every single run, and a check that always cries wolf is
    # one everybody learns to skim past.
    /etc/systemd/system/f3k-timer-serial.service
)
WATCH_DIRS=(
    /etc/hostapd /etc/dnsmasq.d /etc/nftables.d /etc/NetworkManager/conf.d
    /etc/modprobe.d /etc/cron.d /usr/local/bin /etc/systemd/system
)

CHANGES=()
hostapd_dirty=0
dnsmasq_dirty=0
reboot_needed=0

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (use sudo)." >&2
    exit 1
fi

if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$BACKUP_DIR" "$(dirname "$STATE_FILE")"
fi

note() {
    CHANGES+=("$1")
    if [ "$DRY_RUN" -eq 1 ]; then echo "  [WOULD CHANGE] $1"; else echo "  [CHANGED] $1"; fi
}
skip()  { echo "  [ok]      $1"; }

# Back up a file once per run, outside its own config directory.
# dnsmasq and hostapd parse *every* file in their conf dirs regardless of
# extension, so a .bak left in place becomes live config — that mistake put
# bind-interfaces and bind-dynamic in scope simultaneously and dnsmasq refused
# to start, taking DHCP down on both networks.
backup() {
    local f=$1 dest
    dest="$BACKUP_DIR/$(echo "${f#/}" | tr '/' '_')"
    [ -f "$dest" ] || cp -a "$f" "$dest" 2>/dev/null
}

# Append a line if no line matching the given key is present.
ensure_line() {
    local file=$1 key=$2 line=$3
    [ -f "$file" ] || return 0
    if grep -qE "$key" "$file"; then
        skip "$file: $line"
    else
        if [ "$DRY_RUN" -eq 0 ]; then
            backup "$file"
            printf '%s\n' "$line" >> "$file"
        fi
        note "$file: added $line"
        return 1
    fi
}

# Replace an exact line with another.
replace_line() {
    local file=$1 from=$2 to=$3
    [ -f "$file" ] || return 0
    if grep -qxF "$to" "$file"; then
        skip "$file: $to"
    elif grep -qxF "$from" "$file"; then
        if [ "$DRY_RUN" -eq 0 ]; then
            backup "$file"
            sed -i "s|^${from}\$|${to}|" "$file"
        fi
        note "$file: $from -> $to"
        return 1
    fi
}

# Write a file we own outright, only if the content differs.
# Compared with cmp rather than "$(cat f)" = "$content": command substitution
# strips trailing newlines from one side but not the other, so a file that was
# already correct would report as changed on every single run.
write_file() {
    local path=$1 mode=$2 content=$3
    if [ -f "$path" ] && printf '%s' "$content" | cmp -s - "$path"; then
        skip "$path"
        return 0
    fi
    if [ "$DRY_RUN" -eq 0 ]; then
        [ -f "$path" ] && backup "$path"
        printf '%s' "$content" > "$path"
        chmod "$mode" "$path"
    fi
    note "$path"
    return 1
}

echo "=========================================="
echo " F3K system config — target version $CONFIG_VERSION"
echo "=========================================="
echo "Applied version: $(cat "$STATE_FILE" 2>/dev/null || echo "none")"
echo

# ── 1. MT7612U USB scatter-gather crash fix ───────────────────────────────
# The Pi 4's VL805 USB3 controller has a scatter-gather bug with mt76x2u: under
# AP traffic the MCU TX queue deadlocks and the AP hangs while still looking
# alive to systemd. Takes effect on module load, so a reboot is required.
echo "1. MT7612U USB scatter-gather"
if write_file /etc/modprobe.d/mt76_usb.conf 644 'options mt76_usb disable_usb_sg=1
'; then :; else reboot_needed=1; fi

# ── 2. hostapd control interface ──────────────────────────────────────────
# Without a control socket hostapd_cli cannot report AP state, so the watchdog
# below has nothing to probe and can never confirm the AP is healthy.
echo "2. hostapd control interface"
for conf in "${HOSTAPD_CONFS[@]}"; do
    ensure_line "$conf" '^ctrl_interface=' 'ctrl_interface=/var/run/hostapd' || hostapd_dirty=1
    ensure_line "$conf" '^ctrl_interface_group=' 'ctrl_interface_group=0'    || hostapd_dirty=1
done

# ── 3. dnsmasq bind-dynamic ───────────────────────────────────────────────
# bind-interfaces aborts dnsmasq outright if a listed interface is missing at
# startup. The MT7612U enumerates slowly, so a slow USB probe took DHCP down on
# BOTH networks. bind-dynamic binds interfaces as they appear.
echo "3. dnsmasq bind mode"
for conf in "${DNSMASQ_CONFS[@]}"; do
    replace_line "$conf" 'bind-interfaces' 'bind-dynamic' || dnsmasq_dirty=1
done

# ── 4. wlan1-setup poll loop ──────────────────────────────────────────────
# Only on Pis already running the dual-AP layout. Requires=sys-subsystem-net-
# devices-wlan1.device raced the USB adapter's enumeration and the service died
# with "Cannot find device wlan1", leaving hostapd and dnsmasq no interface.
echo "4. wlan1-setup service"
if [ -f /etc/systemd/system/wlan1-setup.service ]; then
    if grep -q "seq 1 30" /etc/systemd/system/wlan1-setup.service; then
        skip "/etc/systemd/system/wlan1-setup.service"
    elif [ "$DRY_RUN" -eq 1 ]; then
        note "/etc/systemd/system/wlan1-setup.service (poll loop)"
    else
        backup /etc/systemd/system/wlan1-setup.service
        cat > /etc/systemd/system/wlan1-setup.service << 'UNITEOF'
[Unit]
Description=F3K timer network interface setup (wlan1 - MT7612U)
Before=hostapd.service dnsmasq.service
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'for i in $(seq 1 30); do ip link show wlan1 >/dev/null 2>&1 && break; echo "wlan1-setup: waiting for wlan1 ($i/30)..."; sleep 2; done; ip link set wlan1 up; ip addr show dev wlan1 | grep -q 192.168.10.1 || ip addr add 192.168.10.1/24 dev wlan1; echo "wlan1-setup: wlan1 ready at 192.168.10.1"'

[Install]
WantedBy=multi-user.target
UNITEOF
        systemctl daemon-reload
        note "/etc/systemd/system/wlan1-setup.service (poll loop)"
    fi
else
    skip "wlan1-setup.service not present (single-AP Pi) — skipping"
fi

# ── 5. hostapd watchdog ───────────────────────────────────────────────────
# Recovers a wedged MT7612U. The interface is read from hostapd.conf at run
# time rather than hardcoded, so this works on a single-AP Pi (F3K_BASE on
# wlan0) as well as the dual-AP layout.
echo "5. hostapd watchdog"
write_file /usr/local/bin/hostapd-watchdog.sh 755 '#!/bin/bash
# Restart hostapd only if its AP has genuinely stopped serving.
#
# hostapd_cli is called by ABSOLUTE PATH: cron`s built-in PATH is /usr/bin:/bin
# and the binary lives in /usr/sbin. An earlier version used the bare name with
# stderr sent to /dev/null, which made "command not found" indistinguishable
# from "AP is down" — so it restarted a perfectly healthy AP every 2 minutes and
# knocked every timer off the air. It passed every manual test because
# interactive shells do have /usr/sbin on PATH.
HOSTAPD_CLI=/usr/sbin/hostapd_cli
CONF=/etc/hostapd/hostapd.conf

if [ ! -x "$HOSTAPD_CLI" ]; then
    logger "hostapd-watchdog: $HOSTAPD_CLI missing -- cannot probe, not restarting"
    exit 1
fi

# Read the AP interface from the config so this works on single- and dual-AP Pis.
IFACE=$(grep -m1 "^interface=" "$CONF" 2>/dev/null | cut -d= -f2)
if [ -z "$IFACE" ]; then
    logger "hostapd-watchdog: no interface= in $CONF -- cannot probe, not restarting"
    exit 1
fi

# Probe three times before acting: hostapd_cli also fails for a second or two
# right after a legitimate restart, and one failed probe used to be enough to
# trigger another one.
for i in 1 2 3; do
    err=$("$HOSTAPD_CLI" -i "$IFACE" status 2>&1 >/tmp/.hostapd-wd-status)
    if grep -q "^state=ENABLED" /tmp/.hostapd-wd-status; then
        rm -f /tmp/.hostapd-wd-status
        exit 0
    fi
    sleep 5
done
rm -f /tmp/.hostapd-wd-status

# Log the underlying error so a broken probe is distinguishable from a downed AP
# in the journal, rather than looking like flapping hardware.
logger "hostapd-watchdog: $IFACE AP not enabled after 3 probes (last error: ${err:-none}), restarting hostapd"
systemctl restart hostapd
'

write_file /etc/cron.d/hostapd-watchdog 644 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
*/2 * * * * root /usr/local/bin/hostapd-watchdog.sh
'

# ── Restart/verify helpers (used by step 7) ───────────────────────────────

# Poll for health rather than checking once after a fixed sleep. hostapd
# restarting *from a failed state* takes noticeably longer than the ~2 s of a
# warm restart, and a loaded or slower Pi longer still. Any single sleep is a
# guess, and guessing short means rolling back a perfectly good change on a box
# nobody can log into — a far worse outcome than waiting a few extra seconds.
wait_for() {   # wait_for <timeout_s> <predicate> [args...]
    local timeout=$1; shift
    local i
    for ((i = 1; i <= timeout; i++)); do
        "$@" && return 0
        sleep 1
    done
    return 1
}

hostapd_healthy() {
    local iface=$1
    systemctl is-active --quiet hostapd || return 1
    /usr/sbin/hostapd_cli -i "$iface" status 2>/dev/null | grep -q '^state=ENABLED'
}

dnsmasq_healthy() { systemctl is-active --quiet dnsmasq; }

# Rollback exists because the operator may have no way back into this box.
rollback() {
    local svc=$1; shift
    echo "  !! $svc did not come back healthy — restoring previous config"
    for f in "$@"; do
        local src="$BACKUP_DIR/$(echo "${f#/}" | tr '/' '_')"
        [ -f "$src" ] && cp -a "$src" "$f" && echo "     restored $f"
    done
    systemctl restart "$svc"
    if wait_for 20 systemctl is-active --quiet "$svc"; then
        echo "     $svc recovered on the previous config"
    else
        echo "     !! $svc STILL DOWN after rollback — manual intervention needed"
    fi
    return 1
}

# ── 6. NetworkManager single-AP leftover ──────────────────────────────────
# 99-unmanaged-wlan0.conf is the correct file on a single-AP Pi, and dead weight
# once 99-unmanaged-wlan.conf supersedes it with both interfaces. Only remove it
# when the replacement is actually present, so this can never un-manage wlan0 on
# a Pi that still serves F3K_BASE from the onboard radio.
echo "6. NetworkManager leftovers"
NM_OLD=/etc/NetworkManager/conf.d/99-unmanaged-wlan0.conf
NM_NEW=/etc/NetworkManager/conf.d/99-unmanaged-wlan.conf
if [ -f "$NM_OLD" ] && [ -f "$NM_NEW" ]; then
    if [ "$DRY_RUN" -eq 0 ]; then
        backup "$NM_OLD"
        rm -f "$NM_OLD"
    fi
    note "$NM_OLD (superseded by 99-unmanaged-wlan.conf — removed)"
elif [ -f "$NM_OLD" ]; then
    skip "$NM_OLD (single-AP Pi — correct, keeping)"
else
    skip "no NetworkManager leftovers"
fi

echo
echo "7. Service restarts"
apply_failed=0

if [ "$DRY_RUN" -eq 1 ]; then
    echo "  (dry run — nothing restarted)"
fi

if [ "$DRY_RUN" -eq 0 ] && [ "$hostapd_dirty" -eq 1 ]; then
    echo "  restarting hostapd..."
    systemctl restart hostapd
    iface=$(grep -m1 '^interface=' /etc/hostapd/hostapd.conf 2>/dev/null | cut -d= -f2)
    if wait_for 25 hostapd_healthy "$iface"; then
        echo "  [OK] hostapd healthy ($iface state=ENABLED)"
    else
        rollback hostapd "${HOSTAPD_CONFS[@]}" || apply_failed=1
    fi
elif [ "$DRY_RUN" -eq 0 ]; then
    echo "  hostapd unchanged — not restarting"
fi

if [ "$DRY_RUN" -eq 0 ] && [ "$dnsmasq_dirty" -eq 1 ]; then
    echo "  restarting dnsmasq..."
    systemctl restart dnsmasq
    if wait_for 15 dnsmasq_healthy; then
        echo "  [OK] dnsmasq healthy"
    else
        rollback dnsmasq "${DNSMASQ_CONFS[@]}" || apply_failed=1
    fi
elif [ "$DRY_RUN" -eq 0 ]; then
    echo "  dnsmasq unchanged — not restarting"
fi

# ── 8. Unmanaged config scan (check mode only) ────────────────────────────
# Finds F3K-shaped config on the box that no script in the repo owns. This is
# the check that would have caught disable_usb_sg and the hostapd watchdog: both
# were applied by hand over SSH, worked, and were never written down — so the
# next person to run a setup script silently lost them.
UNMANAGED=()
if [ "$DRY_RUN" -eq 1 ]; then
    echo
    echo "8. Unmanaged config scan"
    is_managed() {
        local p=$1 m
        for m in "${MANAGED_PATHS[@]}"; do [ "$p" = "$m" ] && return 0; done
        return 1
    }
    for dir in "${WATCH_DIRS[@]}"; do
        [ -d "$dir" ] || continue
        # systemd drop-ins live one level down, in <unit>.d/, so a maxdepth of 1
        # could never see them — and a drop-in is invisible in the unit file
        # itself: only `systemctl cat` reveals one. That combination is exactly
        # how [I-31] hid for three weeks. A hand-added
        # f3k-server.service.d/override.conf pinned audio output to one speaker's
        # MAC address, so the app's own output setting did nothing at all, and
        # this scan — which exists precisely to catch a fix applied by hand over
        # SSH — was structurally unable to report it.
        # /etc/systemd/system/hostapd.service.d/override.conf is in MANAGED_PATHS
        # above and was equally unreachable, which should have been the clue.
        depth=1
        [ "$dir" = /etc/systemd/system ] && depth=2
        while IFS= read -r f; do
            is_managed "$f" && continue
            # Backup leftovers. Severity depends on whether the daemon actually
            # reads the whole directory:
            #   dnsmasq  — CONFIG_DIR=/etc/dnsmasq.d excludes only .dpkg-*, so a
            #              .bak there IS live config and will fight the real file
            #              (this took DHCP down on both networks once).
            #   hostapd  — given explicit file paths in ExecStart, never scans the
            #              directory, so a .bak is inert clutter.
            case "$f" in
                *.bak|*.bak[0-9]|*.save|*.orig|*~)
                    if [ "$dir" = /etc/dnsmasq.d ]; then
                        UNMANAGED+=("$f  (STRAY BACKUP — dnsmasq reads this as LIVE CONFIG)")
                    else
                        UNMANAGED+=("$f  (leftover backup — harmless, but delete it)")
                    fi
                    continue ;;
            esac
            # An unmanaged systemd drop-in is flagged on its PATH alone, with no
            # content test. A drop-in need not mention F3K anywhere to change how
            # f3k-server runs — [I-31]'s set an Environment= line, and a content
            # grep is a coin toss on whether it matches. Anything under /etc that
            # overrides a unit is admin-made local config by definition (vendor
            # drop-ins ship in /usr/lib/systemd/system), so if we do not own it,
            # it is drift and belongs in this script.
            case "$f" in
                /etc/systemd/system/*.d/*)
                    UNMANAGED+=("$f  (systemd DROP-IN — silently overrides a unit; invisible in the unit file, see 'systemctl cat')")
                    continue ;;
            esac
            # Otherwise only flag things that look like ours, or the conf dirs we
            # own outright, so OS defaults do not drown the signal.
            if grep -qiE 'f3k|wlan[01]|hostapd|dnsmasq|mt76|192\.168\.(10|20)\.' "$f" 2>/dev/null \
               || [ "$dir" = /etc/dnsmasq.d ] || [ "$dir" = /etc/hostapd ] || [ "$dir" = /etc/nftables.d ]; then
                case "$f" in */README|*/README.*) continue ;; esac
                UNMANAGED+=("$f")
            fi
        # -type f skips the *.wants/ symlink farms, which are systemd's own
        # bookkeeping and not config anybody edits.
        done < <(find "$dir" -maxdepth "$depth" -type f 2>/dev/null)
    done
    if [ ${#UNMANAGED[@]} -eq 0 ]; then
        echo "  [ok]      nothing unmanaged found"
    else
        for u in "${UNMANAGED[@]}"; do echo "  [UNMANAGED] $u"; done
    fi
fi

# ── 9. Report ─────────────────────────────────────────────────────────────
echo
echo "=========================================="
if [ "$apply_failed" -eq 1 ]; then
    echo " FAILED — config rolled back. Version not recorded."
    echo "=========================================="
    exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    if [ ${#CHANGES[@]} -eq 0 ] && [ ${#UNMANAGED[@]} -eq 0 ]; then
        echo " IN SYNC — Pi matches the repo at version $CONFIG_VERSION."
        echo "=========================================="
        exit 0
    fi
    [ ${#CHANGES[@]} -gt 0 ] && {
        echo " ${#CHANGES[@]} managed item(s) would change:"
        for c in "${CHANGES[@]}"; do echo "   - $c"; done
        echo "   -> the Pi is behind the repo; run without --check to apply."
    }
    [ ${#UNMANAGED[@]} -gt 0 ] && {
        echo " ${#UNMANAGED[@]} unmanaged item(s) on the Pi:"
        for u in "${UNMANAGED[@]}"; do echo "   - $u"; done
        echo "   -> if any of these are ours, fold them into apply-system-config.sh"
        echo "      and bump CONFIG_VERSION, or they will be lost on the next Pi."
    }
    echo "=========================================="
    exit 1
fi

if [ ${#CHANGES[@]} -eq 0 ]; then
    echo " Already at version $CONFIG_VERSION — no changes."
else
    echo " Applied ${#CHANGES[@]} change(s):"
    for c in "${CHANGES[@]}"; do echo "   - $c"; done
fi
[ "$reboot_needed" -eq 1 ] && echo " NOTE: reboot required for the mt76 USB fix to take effect."
echo "=========================================="

echo "$CONFIG_VERSION" > "$STATE_FILE"
exit 0
