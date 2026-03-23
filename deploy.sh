#!/bin/bash
# =============================================================================
# Böck Einkaufsagent – Erstinstallation auf Hetzner VPS CX22 (Ubuntu 24.04)
# CX22: 2 vCPU, 4 GB RAM, 40 GB SSD
# Einmalig ausführen als root oder sudo-fähiger User
#
# Verwendung:
#   chmod +x deploy.sh
#   sudo ./deploy.sh
# =============================================================================
set -euo pipefail

APP_USER="boeck"
APP_DIR="/opt/boeck-agent"
PYTHON_MIN="3.11"

echo "========================================"
echo " Böck Einkaufsagent – Setup (CX22)"
echo "========================================"

# ----- 1. System-Pakete -----
echo "[1/7] System-Pakete installieren..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    git curl wget \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libcairo2

# ----- 2. App-User anlegen -----
echo "[2/7] App-User '$APP_USER' anlegen..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --create-home --shell /bin/bash "$APP_USER"
fi

# ----- 3. App-Verzeichnis -----
echo "[3/7] App-Verzeichnis einrichten: $APP_DIR"
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"

# ----- 4. Code deployen -----
echo "[4/7] Code nach $APP_DIR kopieren..."
# Wenn per git:
#   git clone https://github.com/DEIN_REPO/boeck-agent.git "$APP_DIR"
# Oder manuell via rsync vom lokalen Rechner:
#   rsync -av --exclude='.env' --exclude='boeck_agent.db' \
#       ./ root@SERVER_IP:$APP_DIR/
echo "  → Code muss manuell nach $APP_DIR kopiert werden"
echo "    (rsync oder git clone, .env separat übertragen)"

# ----- 5. Python venv + Abhängigkeiten -----
echo "[5/7] Python-Umgebung einrichten..."
sudo -u "$APP_USER" bash -c "
    cd $APP_DIR
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    playwright install chromium
    playwright install-deps chromium
"

# ----- 6. .env prüfen -----
echo "[6/7] .env-Datei prüfen..."
if [ ! -f "$APP_DIR/.env" ]; then
    if [ -f "$APP_DIR/.env.example" ]; then
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
        chmod 600 "$APP_DIR/.env"
        echo "  → $APP_DIR/.env aus .env.example erstellt"
        echo "  ⚠️  BITTE AUSFÜLLEN: $APP_DIR/.env"
    fi
else
    echo "  → .env gefunden"
fi

# ----- 7. systemd Service installieren -----
echo "[7/7] systemd Service installieren..."
cat > /etc/systemd/system/boeck-agent.service << 'UNIT'
[Unit]
Description=Böck Einkaufsagent (Telegram Bot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=boeck
WorkingDirectory=/opt/boeck-agent
EnvironmentFile=/opt/boeck-agent/.env
ExecStart=/opt/boeck-agent/venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=boeck-agent

# Sicherheit
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable boeck-agent

echo ""
echo "========================================"
echo " Installation abgeschlossen!"
echo "========================================"
echo ""
echo "Nächste Schritte:"
echo "  1. .env ausfüllen:  nano $APP_DIR/.env"
echo "  2. Bot starten:     systemctl start boeck-agent"
echo "  3. Logs prüfen:     journalctl -u boeck-agent -f"
echo ""
echo "Weitere Befehle:"
echo "  systemctl status boeck-agent   – Status"
echo "  systemctl restart boeck-agent  – Neustart"
echo "  systemctl stop boeck-agent     – Stoppen"
