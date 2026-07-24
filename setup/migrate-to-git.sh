#!/usr/bin/env bash
# migrate-to-git.sh
# One-time migration: converts the SCP-deployed ~/f3k_base copy into a
# git-tracked clone so future updates can be pulled via the Settings page.
#
# Run this on each Pi over SSH:
#   bash <(curl -s https://raw.githubusercontent.com/gadjt12a/F3K_Timer_Base_Station/main/setup/migrate-to-git.sh)
# Or copy and paste the contents into an SSH session.

set -e

REPO_URL="https://github.com/gadjt12a/F3K_Timer_Base_Station.git"
NEW_DIR="$HOME/f3k_repo"
OLD_DIR="$HOME/f3k_base"
SERVICE="f3k-server"

echo "=== F3K Base Station: migrating to git-based updates ==="
echo ""

# 1. git
if ! command -v git &>/dev/null; then
    echo "[1/6] Installing git..."
    sudo apt-get install -y git
else
    echo "[1/6] git already installed ($(git --version))"
fi

# 2. Clone
if [ -d "$NEW_DIR/.git" ]; then
    echo "[2/6] $NEW_DIR already a git repo — skipping clone"
else
    [ -d "$NEW_DIR" ] && { echo "ERROR: $NEW_DIR exists but is not a git repo. Remove it first."; exit 1; }
    echo "[2/6] Cloning $REPO_URL → $NEW_DIR..."
    git clone "$REPO_URL" "$NEW_DIR"
fi

# 3. Python venv
echo "[3/6] Creating venv at $NEW_DIR/base_station/venv ..."
python3 -m venv "$NEW_DIR/base_station/venv"
"$NEW_DIR/base_station/venv/bin/pip" install -q -r "$NEW_DIR/base_station/requirements.txt"
echo "      packages installed"

# 4. Copy runtime data from old location → canonical location inside the repo
#    The service now resolves all runtime paths relative to server.py, so
#    ~/f3k_repo/base_station/ is the single source of truth going forward.
echo "[4/6] Copying runtime data from $OLD_DIR → $NEW_DIR/base_station/ ..."
if [ -f "$OLD_DIR/f3k.db" ]; then
    cp -v "$OLD_DIR/f3k.db" "$NEW_DIR/base_station/f3k.db"
else
    echo "      (no f3k.db found in $OLD_DIR — skipping)"
fi
[ -f "$OLD_DIR/audio_config.json" ] && cp -v "$OLD_DIR/audio_config.json" "$NEW_DIR/base_station/audio_config.json"
[ -d "$OLD_DIR/downloads" ]         && cp -rv "$OLD_DIR/downloads"         "$NEW_DIR/base_station/downloads"

# 5. Update systemd service
echo "[5/6] Updating f3k-server.service..."
SVCFILE="/etc/systemd/system/$SERVICE.service"
sudo sed -i \
    "s|WorkingDirectory=.*|WorkingDirectory=$NEW_DIR/base_station|" \
    "$SVCFILE"
sudo sed -i \
    "s|ExecStart=.*|ExecStart=$NEW_DIR/base_station/venv/bin/python3 $NEW_DIR/base_station/server.py|" \
    "$SVCFILE"
echo "      service file updated:"
grep -E "WorkingDirectory|ExecStart" "$SVCFILE"
sudo systemctl daemon-reload

# 6. Restart
echo "[6/6] Restarting $SERVICE..."
sudo systemctl restart "$SERVICE"
sleep 3
STATUS=$(sudo systemctl is-active "$SERVICE")
if [ "$STATUS" = "active" ]; then
    echo ""
    echo "=== Migration complete! ==="
    echo "    Running from: $NEW_DIR/base_station"
    echo "    Future updates: open Settings in the web UI and click 'Update from GitHub'"
    echo ""
    echo "    The old $OLD_DIR directory is still there — safe to remove once you"
    echo "    confirm everything is working:"
    echo "      rm -rf $OLD_DIR"
else
    echo ""
    echo "ERROR: Service did not come back up (status: $STATUS)"
    echo "Check logs with: sudo journalctl -u $SERVICE -n 50"
    exit 1
fi
