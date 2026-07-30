#!/bin/bash
# upgrade-to-dual-ap.sh
# Reconfigures this Pi from single-AP (wlan0 = F3K_BASE) to the full dual-AP
# setup used on the development Pi:
#   wlan1 (MT7612U USB adapter) = F3K_BASE  192.168.10.1/24  timers
#   wlan0 (onboard)             = F3K_OPS   192.168.20.1/24  CD/phones
#
# Prerequisites:
#   sudo apt-get install -y firmware-mediatek
#   (then reboot so the MT7612U enumerates as wlan1 before running this script)
#
# Run with: sudo bash ~/upgrade-to-dual-ap.sh

set -e

echo "=========================================="
echo " F3K Base Station — Dual-AP Upgrade"
echo "=========================================="

# ── 1. Check wlan1 is present ──────────────────────────────────────────────
if ! ip link show wlan1 &>/dev/null; then
    echo ""
    echo "ERROR: wlan1 not found."
    echo "  1. Install firmware: sudo apt-get install -y firmware-mediatek"
    echo "  2. Reboot: sudo reboot"
    echo "  3. Re-run this script after reboot."
    echo "  (Run 'ip link' to confirm wlan1 appears before retrying.)"
    exit 1
fi
echo "[OK] wlan1 detected"

# ── 2. Check nftables is available ────────────────────────────────────────
if ! command -v nft &>/dev/null; then
    echo ""
    echo "ERROR: nftables (nft) not installed."
    echo "  Install it with: sudo apt-get install nftables"
    exit 1
fi
echo "[OK] nftables available"

# ── 3. hostapd — F3K_BASE on wlan1 ────────────────────────────────────────
cat > /etc/hostapd/hostapd.conf << 'EOF'
interface=wlan1
driver=nl80211
ssid=F3K_BASE
hw_mode=g
channel=6
macaddr_acl=0
auth_algs=1
wpa=2
wpa_passphrase=f3ktimer
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
ap_max_inactivity=1800
ctrl_interface=/var/run/hostapd
ctrl_interface_group=0
EOF
echo "[OK] /etc/hostapd/hostapd.conf (F3K_BASE on wlan1)"

# ── 4. hostapd — F3K_OPS on wlan0 ─────────────────────────────────────────
cat > /etc/hostapd/hostapd-ops.conf << 'EOF'
interface=wlan0
driver=nl80211
ssid=F3K_OPS
hw_mode=g
channel=11
macaddr_acl=0
auth_algs=1
wpa=2
wpa_passphrase=f3kmanage
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
ap_max_inactivity=1800
ctrl_interface=/var/run/hostapd
ctrl_interface_group=0
EOF
echo "[OK] /etc/hostapd/hostapd-ops.conf (F3K_OPS on wlan0)"

# ── 5. hostapd systemd override — load both configs ───────────────────────
mkdir -p /etc/systemd/system/hostapd.service.d
cat > /etc/systemd/system/hostapd.service.d/override.conf << 'EOF'
[Service]
Type=simple
ExecStart=
ExecStart=/usr/sbin/hostapd /etc/hostapd/hostapd.conf /etc/hostapd/hostapd-ops.conf
EOF
echo "[OK] hostapd override — loading both configs"

# ── 6. dnsmasq — timer network (wlan1) ────────────────────────────────────
# bind-dynamic, not bind-interfaces: with bind-interfaces a missing wlan1 at
# startup (USB adapter not yet enumerated) aborts dnsmasq completely, taking
# DHCP down on the OPS network too. bind-dynamic binds interfaces as they appear.
#
# Options are TAGGED, and the ranges set the tag. An untagged dhcp-option in
# dnsmasq is GLOBAL, not per-interface, and /etc/dnsmasq.d is read alphabetically
# — so the untagged router option here (f3k-timer, second) silently overrode the
# one in f3k-ops for *every* subnet. OPS clients were handed 192.168.20.x
# addresses with a 192.168.10.1 gateway, which is not even routable from their
# own subnet. A laptop whose only network is F3K_OPS had a broken default route.
cat > /etc/dnsmasq.d/f3k-timer.conf << 'EOF'
interface=wlan1
bind-dynamic
dhcp-range=set:timer,192.168.10.10,192.168.10.50,255.255.255.0,24h
dhcp-option=tag:timer,3,192.168.10.1
EOF
echo "[OK] /etc/dnsmasq.d/f3k-timer.conf (wlan1)"

# ── 7. dnsmasq — OPS network (wlan0) ──────────────────────────────────────
# Tagged for the same reason as f3k-timer above.
cat > /etc/dnsmasq.d/f3k-ops.conf << 'EOF'
interface=wlan0
bind-dynamic
dhcp-range=set:ops,192.168.20.10,192.168.20.50,255.255.255.0,24h
dhcp-option=tag:ops,3,192.168.20.1
address=/#/192.168.20.1
EOF
echo "[OK] /etc/dnsmasq.d/f3k-ops.conf (wlan0)"

# ── 8. nftables captive portal (port 80 → 8080 on wlan0) ─────────────────
mkdir -p /etc/nftables.d
cat > /etc/nftables.d/f3k-captive.conf << 'EOF'
table ip f3k_captive {
    chain prerouting {
        type nat hook prerouting priority dstnat;
        policy accept;
        iifname wlan0 tcp dport 80 redirect to :8080
    }
}
EOF
echo "[OK] /etc/nftables.d/f3k-captive.conf"

# ── 9. wlan0-setup.service — OPS network (192.168.20.1) ──────────────────
cat > /etc/systemd/system/wlan0-setup.service << 'EOF'
[Unit]
Description=F3K OPS network interface setup (wlan0)
Before=hostapd.service dnsmasq.service
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/rfkill unblock wifi
ExecStart=/sbin/ip link set wlan0 up
ExecStart=/bin/sh -c '/sbin/ip addr show dev wlan0 | grep -q 192.168.20.1 || /sbin/ip addr add 192.168.20.1/24 dev wlan0'
ExecStart=/bin/sh -c '/usr/sbin/nft delete table ip f3k_captive 2>/dev/null; /usr/sbin/nft -f /etc/nftables.d/f3k-captive.conf'
ExecStop=/bin/sh -c '/usr/sbin/nft delete table ip f3k_captive 2>/dev/null; true'

[Install]
WantedBy=multi-user.target
EOF
echo "[OK] wlan0-setup.service (OPS — 192.168.20.1)"

# ── 10. wlan1-setup.service — timer network (192.168.10.1) ───────────────
# Poll for the interface rather than using Requires=sys-subsystem-net-devices-wlan1.device.
# The MT7612U is a USB adapter and can take tens of seconds to enumerate; the
# device unit approach raced it and the service failed outright with "Cannot
# find device wlan1", leaving hostapd and dnsmasq with no interface to bind.
# Waiting up to 60s costs boot time but is reliable.
cat > /etc/systemd/system/wlan1-setup.service << 'EOF'
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
EOF
echo "[OK] wlan1-setup.service (timer — 192.168.10.1)"

# ── 11. NetworkManager — unmanage both wlan interfaces ────────────────────
cat > /etc/NetworkManager/conf.d/99-unmanaged-wlan.conf << 'EOF'
[keyfile]
unmanaged-devices=interface-name:wlan0,interface-name:wlan1
EOF
# Remove the old single-interface file if present
rm -f /etc/NetworkManager/conf.d/99-unmanaged-wlan0.conf
echo "[OK] NetworkManager — wlan0 and wlan1 both unmanaged"

# ── 12. OS-level config (watchdog, mt76 fix, ctrl_interface, bind mode) ───
# Delegated to apply-system-config.sh so there is exactly one definition of the
# hostapd watchdog and friends. Duplicating them here is what let this script
# silently revert hand-applied fixes on every run. That script is idempotent, so
# it simply confirms what the heredocs above already wrote and fills in the rest
# (notably the mt76 USB scatter-gather fix, which this script never carried).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/apply-system-config.sh" ]; then
    bash "$SCRIPT_DIR/apply-system-config.sh" || echo "[WARN] system config apply reported a problem — see output above"
else
    echo "[SKIP] apply-system-config.sh not found next to this script"
fi

# ── 13. Reload systemd and NetworkManager ─────────────────────────────────
echo ""
echo "Reloading systemd and restarting services..."
systemctl daemon-reload
systemctl reload NetworkManager

# ── 13a. Reset eth0 to DHCP ───────────────────────────────────────────────
ETH_CON=$(nmcli -t -f NAME,DEVICE con show 2>/dev/null | grep ':eth0$' | cut -d: -f1 | head -1)
if [ -n "$ETH_CON" ]; then
    nmcli con mod "$ETH_CON" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns "" 2>/dev/null || true
    nmcli con up "$ETH_CON" 2>/dev/null || true
    echo "[OK] eth0 reset to DHCP (profile: $ETH_CON)"
else
    echo "[SKIP] eth0 — no NetworkManager profile found (likely already DHCP)"
fi

systemctl enable wlan0-setup wlan1-setup
systemctl restart wlan0-setup
systemctl restart wlan1-setup
systemctl restart hostapd
systemctl restart dnsmasq
systemctl restart f3k-server

sleep 3

# ── 14. Status report ─────────────────────────────────────────────────────
echo ""
echo "=========================================="
echo " Status"
echo "=========================================="
for svc in wlan0-setup wlan1-setup hostapd dnsmasq f3k-server; do
    status=$(systemctl is-active "$svc" 2>/dev/null)
    if [ "$status" = "active" ]; then
        echo "  [OK]   $svc"
    else
        echo "  [FAIL] $svc — $status"
    fi
done

echo ""
echo "Interface addresses:"
ip -brief addr show wlan0 2>/dev/null || echo "  wlan0: not found"
ip -brief addr show wlan1 2>/dev/null || echo "  wlan1: not found"

echo ""
echo "hostapd control interface (what the watchdog probes):"
# env -i replicates cron's bare environment deliberately — probing from this
# interactive shell would pass even with the PATH bug that caused the restart loop.
for IFACE in wlan0 wlan1; do
    if env -i PATH=/usr/bin:/bin /usr/sbin/hostapd_cli -i "$IFACE" status 2>/dev/null | grep -q "^state=ENABLED"; then
        echo "  [OK]   $IFACE — state=ENABLED"
    else
        echo "  [FAIL] $IFACE — probe failed; watchdog would restart hostapd every 2 min"
    fi
done

echo ""
echo "Health check:"
curl -s http://localhost:8080/health && echo ""

echo ""
echo "=========================================="
echo " Dual-AP upgrade complete."
echo "   F3K_BASE (timers)  — wlan1  192.168.10.1  pw: f3ktimer"
echo "   F3K_OPS  (CD/web)  — wlan0  192.168.20.1  pw: f3kmanage"
echo "=========================================="
