#!/bin/bash
# One-shot bootstrap / upgrade for the monitor daemon on Debian.
# Safe to re-run: config files under /etc are preserved, code under /opt
# is rsynced, venv is only created if missing, systemd units are always
# re-written, then daemon-reload.
#
# Run as root from the repo root:
#   sudo ./deploy/install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh must run as root (try: sudo $0)" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/monitor"
DB_DIR="/var/lib/monitor"
LOG_DIR="/var/log/monitor"
CONF_DIR="/etc/monitor"

echo "==> Ensuring monitor user"
if ! id monitor >/dev/null 2>&1; then
  useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" monitor
fi

echo "==> Ensuring directories"
install -d -m 755 -o monitor -g monitor "$INSTALL_DIR"
install -d -m 750 -o monitor -g monitor "$DB_DIR" "$DB_DIR/backups"
install -d -m 755 -o monitor -g monitor "$LOG_DIR"
install -d -m 750 -o root    -g monitor "$CONF_DIR"

echo "==> Syncing code to $INSTALL_DIR"
# --delete keeps the tree in sync on upgrades. Excludes avoid shipping
# dev artifacts and the developer's local venv.
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'tests' \
  "$REPO_ROOT/" "$INSTALL_DIR/"
chown -R monitor:monitor "$INSTALL_DIR"

echo "==> Preparing venv"
if [ ! -d "$INSTALL_DIR/venv" ]; then
  sudo -u monitor python3 -m venv "$INSTALL_DIR/venv"
fi
sudo -u monitor "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
sudo -u monitor "$INSTALL_DIR/venv/bin/pip" install -e "$INSTALL_DIR"

echo "==> Installing config templates (preserving existing)"
if [ ! -f "$CONF_DIR/monitor.env" ]; then
  install -m 640 -o root -g monitor \
    "$REPO_ROOT/deploy/templates/monitor.env.example" "$CONF_DIR/monitor.env"
  echo "    -> created $CONF_DIR/monitor.env (EDIT THIS with real secrets)"
fi
if [ ! -f "$CONF_DIR/config.json" ]; then
  install -m 644 -o root -g monitor \
    "$REPO_ROOT/deploy/templates/config.json.example" "$CONF_DIR/config.json"
  echo "    -> created $CONF_DIR/config.json"
fi

echo "==> Installing systemd units"
install -m 644 "$REPO_ROOT/deploy/systemd/monitor.service"         /etc/systemd/system/
install -m 644 "$REPO_ROOT/deploy/systemd/monitor-health.service"  /etc/systemd/system/
install -m 644 "$REPO_ROOT/deploy/systemd/monitor-health.timer"    /etc/systemd/system/
install -m 644 "$REPO_ROOT/deploy/systemd/monitor-backup.service"  /etc/systemd/system/
install -m 644 "$REPO_ROOT/deploy/systemd/monitor-backup.timer"    /etc/systemd/system/
systemctl daemon-reload

echo "==> Installing logrotate config"
install -m 644 "$REPO_ROOT/deploy/logrotate/monitor" /etc/logrotate.d/monitor

cat <<EOF

install.sh done.

Next steps:
  1. Edit $CONF_DIR/monitor.env   — fill GITHUB_TOKEN / MINIMAX_API_KEY /
                                     TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
  2. Review $CONF_DIR/config.json — tune keywords / thresholds if needed.
  3. systemctl enable --now monitor.service
  4. systemctl enable --now monitor-health.timer monitor-backup.timer
  5. journalctl -u monitor.service -f   # watch startup
EOF
