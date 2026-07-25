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
cat > /etc/dnsmasq.d/f3k-timer.conf << 'EOF'
interface=wlan1
bind-interfaces
dhcp-range=192.168.10.10,192.168.10.50,255.255.255.0,24h
dhcp-option=3,192.168.10.1
EOF
echo "[OK] /etc/dnsmasq.d/f3k-timer.conf (wlan1)"

# ── 7. dnsmasq — OPS network (wlan0) ──────────────────────────────────────
cat > /etc/dnsmasq.d/f3k-ops.conf << 'EOF'
interface=wlan0
bind-interfaces
dhcp-range=192.168.20.10,192.168.20.50,255.255.255.0,24h
dhcp-option=3,192.168.20.1
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
# Requires= ensures systemd waits for the MT7612U to enumerate before running
# the ip commands. Without this, the service fires before the USB adapter is
# ready on boot and fails with "Cannot find device wlan1".
cat > /etc/systemd/system/wlan1-setup.service << 'EOF'
[Unit]
Description=F3K timer network interface setup (wlan1 - MT7612U)
Requires=sys-subsystem-net-devices-wlan1.device
Before=hostapd.service dnsmasq.service
After=sys-subsystem-net-devices-wlan1.device network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip link set wlan1 up
ExecStart=/bin/sh -c '/sbin/ip addr show dev wlan1 | grep -q 192.168.10.1 || /sbin/ip addr add 192.168.10.1/24 dev wlan1'

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

# ── 12. Reload systemd and NetworkManager ─────────────────────────────────
echo ""
echo "Reloading systemd and restarting services..."
systemctl daemon-reload
systemctl reload NetworkManager

# ── 12a. Reset eth0 to DHCP ───────────────────────────────────────────────
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

# ── 13. Status report ─────────────────────────────────────────────────────
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
echo "Health check:"
curl -s http://localhost:8080/health && echo ""

echo ""
echo "=========================================="
echo " Dual-AP upgrade complete."
echo "   F3K_BASE (timers)  — wlan1  192.168.10.1  pw: f3ktimer"
echo "   F3K_OPS  (CD/web)  — wlan0  192.168.20.1  pw: f3kmanage"
echo "=========================================="
