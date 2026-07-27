#!/bin/bash
# F3K Base Station — install on a fresh Raspberry Pi OS image.
#
# Builds a working base station from nothing: OS packages, the clone, a venv,
# the systemd unit. Idempotent — safe to re-run over an existing install.
#
#   bash <(curl -s https://raw.githubusercontent.com/gadjt12a/F3K_Timer_Base_Station/main/setup/install.sh)
#
# This does the APPLICATION half only. The two access points are a separate,
# riskier step (they reconfigure networking) — run setup/upgrade-to-dual-ap.sh
# afterwards, and read its warnings first if the Pi is remote.
#
# Deliberately NOT done here:
#   - eth0 / NetworkManager are never touched. That is the admin lifeline, and
#     on a Pi with no out-of-band access, breaking it strands the unit.
#   - Bluetooth audio pairing. The 3.5 mm jack works with no configuration;
#     BT is paired from Settings in the web UI when there is a speaker present.

set -euo pipefail

REPO_URL="https://github.com/gadjt12a/F3K_Timer_Base_Station.git"
REPO_DIR="$HOME/f3k_repo"
APP_DIR="$REPO_DIR/base_station"
SERVICE="f3k-server"

echo "=== F3K Base Station install ==="
echo

# ── 1. OS packages ───────────────────────────────────────────────────
# Split by purpose so a failure tells you what you lose, not just that apt died.
echo "[1/6] Installing OS packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    git python3-venv python3-pip \
    alsa-utils \
    mdbtools \
    avahi-daemon
#   git/python3-venv  the app itself
#   alsa-utils        aplay/amixer — the audio engine shells out to these
#   mdbtools          mdb-export — reading GliderScore .mdb on import
#   avahi-daemon      makes the Pi reachable as f3kbase.local

# Bluetooth is optional: a Pi driving a wired speaker does not need it, and on
# some images bluez-alsa-utils is unavailable. Never fail the install over it.
echo "      optional: bluetooth audio..."
sudo apt-get install -y -qq bluez bluez-alsa-utils 2>/dev/null \
    || echo "      (bluez-alsa-utils unavailable — 3.5mm audio still works)"

# ── 2. Clone or update ───────────────────────────────────────────────
echo "[2/6] Fetching the application..."
if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$REPO_DIR"
fi
git -C "$REPO_DIR" config core.hooksPath .githooks

# ── 3. Virtualenv ────────────────────────────────────────────────────
# A venv is required, not preference: Raspberry Pi OS marks the system Python
# as externally-managed (PEP 668) and refuses a bare `pip install`.
echo "[3/6] Building the virtualenv..."
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ── 4. Carry over an older install ───────────────────────────────────
# Pre-git deployments kept everything in ~/f3k_base. server.py migrates the DB
# itself on first start; the rest is moved here so nothing is silently lost.
OLD_DIR="$HOME/f3k_base"
if [ -d "$OLD_DIR" ]; then
    echo "[4/6] Carrying over runtime data from $OLD_DIR..."
    [ -f "$OLD_DIR/audio_config.json" ] && cp -n "$OLD_DIR/audio_config.json" "$APP_DIR/" || true
    [ -d "$OLD_DIR/downloads" ]         && cp -rn "$OLD_DIR/downloads"        "$APP_DIR/" || true
    # Pilot-name clips are gitignored, so a clone never brings them — but an old
    # install may have them. They are the only audio not shipped in the repo.
    if compgen -G "$OLD_DIR/frontend/data/audio/ZZ*.wav" > /dev/null; then
        cp -n "$OLD_DIR"/frontend/data/audio/ZZ*.wav "$APP_DIR/frontend/data/audio/" || true
        echo "      carried over $(ls "$APP_DIR"/frontend/data/audio/ZZ*.wav 2>/dev/null | wc -l) pilot-name clips"
    fi
else
    echo "[4/6] No previous install to carry over — skipping."
fi

# ── 5. systemd unit ──────────────────────────────────────────────────
# Installed from the repo copy and rewritten for this user's home, so the unit
# is correct on a box where the account is not `pi`.
echo "[5/6] Installing the $SERVICE service..."
sudo install -m 644 "$REPO_DIR/setup/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
sudo sed -i \
    -e "s|^User=.*|User=$USER|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" \
    -e "s|^ExecStart=.*|ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/server.py|" \
    "/etc/systemd/system/$SERVICE.service"
sudo systemctl daemon-reload
sudo systemctl enable -q "$SERVICE"

# ── 6. Start and verify ──────────────────────────────────────────────
echo "[6/6] Starting $SERVICE..."
sudo systemctl restart "$SERVICE"
# Poll rather than sleep: first start compiles bytecode and runs DB migrations,
# and a fixed sleep either wastes time or reports a false failure.
for i in $(seq 1 20); do
    [ "$(systemctl is-active "$SERVICE")" = "active" ] && break
    sleep 1
done

echo
if [ "$(systemctl is-active "$SERVICE")" = "active" ] \
   && curl -sf -o /dev/null http://127.0.0.1:8080/health; then
    echo "=== Install complete ==="
    echo "  Web UI:  http://$(hostname -I | awk '{print $1}'):8080  (or http://$(hostname).local:8080)"
    echo "  Timers:  TCP port 8765"
    echo
    echo "Next, if this Pi runs the field networks:"
    echo "  bash $REPO_DIR/setup/upgrade-to-dual-ap.sh"
    echo "  (read its header first — it reconfigures wlan0/wlan1 and hostapd)"
else
    echo "=== Install FAILED — service did not come up ==="
    sudo systemctl status "$SERVICE" --no-pager -n 20
    exit 1
fi
