#!/bin/bash
# One-command installation: no onboarding flow, no separate portal service,
# no arguments. Installs a bare, unlinked agent — you link the token and
# plugin(s) afterwards via the local status page (http://voltahub.local:8080),
# not via this script.
#
# On the Pi, as root/via sudo:
#   curl -fsSL https://raw.githubusercontent.com/voltahub-eu/bridge/main/install.sh \
#     | sudo bash
#
# Or locally after a git clone: bash install.sh
set -euo pipefail

REPO_URL="https://github.com/voltahub-eu/bridge.git"
BRANCH="main"
INSTALL_DIR="/opt/voltahub-bridge"

# Fixed hostname, reachable via mDNS (avahi) as voltahub.local. Each customer
# has one bridge on their home network, so a single fixed name is simpler
# than a per-device unique one.
HOSTNAME_VALUE="voltahub"

echo "=== Voltahub Bridge — automatic installation ==="

echo "[1/6] System packages..."
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip avahi-daemon

echo "[2/6] Setting hostname ($HOSTNAME_VALUE.local)..."
if [ "$(hostname)" != "$HOSTNAME_VALUE" ]; then
  hostnamectl set-hostname "$HOSTNAME_VALUE"
  sed -i "s/127\.0\.1\.1.*/127.0.1.1\t$HOSTNAME_VALUE/" /etc/hosts
  if ! grep -q "127\.0\.1\.1" /etc/hosts; then
    echo -e "127.0.1.1\t$HOSTNAME_VALUE" >> /etc/hosts
  fi
  systemctl restart avahi-daemon 2>/dev/null || true
fi

echo "[3/6] Fetching repo (branch: $BRANCH)..."
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

mkdir -p /data /etc/voltahub-bridge

echo "[4/6] Preparing env file (token filled in later via the local status page)..."
if [ ! -f /etc/voltahub-bridge/env ]; then
  cat > /etc/voltahub-bridge/env <<EOF
AGENT_KEY=
DB_PATH=/data/agent.db
PLUGIN_DIR=/data/plugins
EOF
  chmod 600 /etc/voltahub-bridge/env
fi

echo "[5/6] Creating Python environment (venv, deps)..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

echo "[6/6] Installing systemd unit and starting agent..."
cp "$INSTALL_DIR/systemd/voltahub-bridge.service" /etc/systemd/system/
systemctl daemon-reload

"$INSTALL_DIR/venv/bin/python" <<'PYEOF'
import sys
sys.path.insert(0, "/opt/voltahub-bridge")
import config as _cfg
_cfg.DB_PATH = "/data/agent.db"
from core import database

database.init_db("/data/agent.db")
database.set_device_config("onboarded", "true")
database.set_device_config("network_mode", "lan")

print("device_config initialized")
PYEOF

systemctl enable voltahub-bridge
systemctl restart voltahub-bridge

echo ""
echo "Done. Status:  sudo systemctl status voltahub-bridge"
echo "      Logs:    sudo journalctl -u voltahub-bridge -f"
echo "      Local status page: http://$HOSTNAME_VALUE.local:8080 (or http://$(hostname -I | awk '{print $1}'):8080)"
echo ""
echo "No agent token yet — fill it in on the local status page above."
