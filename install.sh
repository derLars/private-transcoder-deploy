#!/bin/bash
set -e

# Configuration
REPO_URL="https://github.com/derLars/private-transcoder-deploy.git"
INSTALL_DIR="/opt/transcode"
SERVICE_NAME="transcode"

# Ensure script is run as root
if [ "$EUID" -ne 0 ]; then 
  echo "Please run as root"
  exit 1
fi

echo "Updating package lists..."
apt-get update

echo "Installing dependencies..."
apt-get install -y git python3 python3-pip ffmpeg

echo "Installing Python libraries..."
pip3 install fastapi uvicorn --break-system-packages || pip3 install fastapi uvicorn

# Clone or Update Repository
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Fresh installation..."
    git clone "$REPO_URL" "$INSTALL_DIR"
else
    echo "Updating repository..."
    cd "$INSTALL_DIR"
    git fetch origin
    git reset --hard origin/main
    git pull origin main
fi

# Create Systemd Service File
echo "Creating systemd service file..."
cat <<EOF > /etc/systemd/system/${SERVICE_NAME}.service
[Unit]
Description=Video Transcoding Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 -m uvicorn transcode:app --host 0.0.0.0 --port 9009
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Reload and Start Service
echo "Reloading systemd and restarting service..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "Installation complete! Service is running."
