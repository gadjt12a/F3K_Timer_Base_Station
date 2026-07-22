# F3K Timer Base Station — Setup Guide

## What you have

A Raspberry Pi 4 running the F3K Timer Base Station. It manages WiFi connections for F3K/F5K timer devices and provides a web interface for competition management.

---

## Step 1: Connect the Pi to your network

1. Plug an ethernet cable from the Pi into your router or switch
2. Power on the Pi (USB-C power supply)
3. Wait about 30 seconds for it to boot
4. Find the Pi's IP address — check your router's device list, or try `f3kbase.local` in a browser
5. Open a browser and go to: `http://<Pi-IP>:8080`

You should see the F3K Timer web interface.

---

## Step 2: Check the timer WiFi is broadcasting

The Pi's onboard WiFi is already set up as an access point called **F3K_BASE**.

- **Network name:** `F3K_BASE`
- **Password:** `f3ktimer`
- **Timer connection address:** `http://192.168.10.1:8080`

You can connect a timer (or any device) to `F3K_BASE` to test the timer network. The timer should be able to reach the base station at `192.168.10.1`.

> **Note:** In this initial setup, the onboard WiFi (wlan0) is doing double duty as the timer network. Once you add the USB adapter (Step 3), the timer network moves to the USB adapter and the onboard WiFi becomes the ops/CD network.

---

## Step 3: Add the USB WiFi adapter (MT7612U) for full dual-AP setup

When you have the **MT7612U USB WiFi adapter**:

1. Plug it into any USB port on the Pi
2. Wait about 10 seconds for the system to detect it
3. SSH into the Pi:
   ```
   ssh pi@<Pi-IP>
   ```
   Password: `f3ksystem`

4. Run the upgrade script:
   ```
   sudo bash ~/upgrade-to-dual-ap.sh
   ```

5. The script will automatically:
   - Move `F3K_BASE` (timer network) to the USB adapter
   - Add `F3K_OPS` (ops/CD network) on the onboard WiFi
   - Reset the wired ethernet back to DHCP
   - Restart all services
   - Print a status report

After the upgrade completes, verify with:
```
ip addr show wlan0
ip addr show wlan1
```

---

## Network summary (after upgrade)

| Network | Interface | IP | Password | Who connects |
|---|---|---|---|---|
| F3K_BASE | wlan1 (USB) | 192.168.10.1 | f3ktimer | Timer devices |
| F3K_OPS | wlan0 (onboard) | 192.168.20.1 | f3kmanage | CD / ops phones |

Timers connect to `F3K_BASE` and reach the base station at `http://192.168.10.1:8080`.

Phones/tablets for ops use connect to `F3K_OPS` — any HTTP request redirects automatically to the web UI.

---

## Step 4: Set up Bluetooth audio (optional)

Bluetooth audio is not set up by default. You can test everything (timers, comp management, scoring) without it. Audio via the **3.5mm jack works out of the box** — just plug in a speaker.

To add Bluetooth A2DP support:

1. Make sure the Pi is connected to the internet via ethernet
2. SSH in and run:
   ```
   sudo bash ~/setup-bluetooth.sh
   ```
3. The script installs the required packages and prints step-by-step pairing instructions
4. After pairing, open the web UI → **Settings → Audio** and enter your speaker's MAC address

> **Note:** Bluetooth audio has an inherent A2DP delay. Once paired, use the **Audio Lead** slider in Settings to compensate — start at 0 and increase until audio cues fire at the right time.

---

## SSH / Admin access

| Setting | Value |
|---|---|
| Username | `pi` |
| Password | `f3ksystem` |
| Hostname | `f3kbase.local` (mDNS) |

---

## Troubleshooting

**Can't find the Pi on the network**
Check your router's connected devices list for a device named `f3kbase`. If mDNS isn't working, the IP address is your only option.

**Web UI not loading**
SSH in and check the server:
```
sudo systemctl status f3k-server
sudo journalctl -u f3k-server -n 30
```

**F3K_BASE not visible as a WiFi network**
```
sudo systemctl status hostapd
sudo journalctl -u hostapd -n 30
```

**Wired ethernet lost after upgrade**
The upgrade script resets eth0 to DHCP. If the Pi doesn't get an address, unplug and replug the cable — most routers issue a new lease within a few seconds.

---

## Service health check

Quick check from SSH:
```
curl http://localhost:8080/health
```
Should return: `{"status":"ok","timers_connected":0}`

Check all F3K services:
```
for s in wlan0-setup wlan1-setup hostapd dnsmasq f3k-server; do
  echo "$s: $(systemctl is-active $s)"
done
```
