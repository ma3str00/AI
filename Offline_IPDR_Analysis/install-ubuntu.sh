#!/bin/bash
# Installs the web app only. llama-server.service is left as it is.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -r requirements.txt

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "Edit config.yaml and set the MySQL password before asking questions."
fi

sudo tee /etc/systemd/system/ipdr-assistant.service >/dev/null <<EOF
[Unit]
Description=IPDR offline assistant
After=network.target llama-server.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ipdr-assistant.service
echo "Open http://127.0.0.1:8090 on this VM. llama-server stays on port 11434."
