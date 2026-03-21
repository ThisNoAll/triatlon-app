#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/triatlon"

sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx

sudo mkdir -p "$APP_DIR"
sudo chown -R "$USER":"$USER" "$APP_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

sudo cp "$APP_DIR/deploy/oracle/triatlon.service" /etc/systemd/system/triatlon.service
sudo cp "$APP_DIR/deploy/oracle/nginx-triatlon.conf" /etc/nginx/sites-available/triatlon
sudo ln -sf /etc/nginx/sites-available/triatlon /etc/nginx/sites-enabled/triatlon
sudo rm -f /etc/nginx/sites-enabled/default

sudo systemctl daemon-reload
sudo systemctl enable triatlon
sudo systemctl restart triatlon
sudo nginx -t
sudo systemctl restart nginx

echo "Telepites kesz. Ellenorzes:"
echo "  sudo systemctl status triatlon"
echo "  sudo systemctl status nginx"
