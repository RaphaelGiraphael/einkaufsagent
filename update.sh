#!/bin/bash
# =============================================================================
# Böck Einkaufsagent – Code-Update auf dem VPS
# Ausführen wenn neue Version deployed werden soll
#
# Verwendung (lokal):
#   chmod +x update.sh
#   ./update.sh root@SERVER_IP
# =============================================================================
set -euo pipefail

SERVER="${1:-}"
APP_DIR="/opt/boeck-agent"
APP_USER="boeck"

if [ -z "$SERVER" ]; then
    echo "Verwendung: ./update.sh user@server_ip"
    exit 1
fi

echo "Deploye auf $SERVER..."

# Code übertragen (ohne Secrets und DB)
rsync -av --progress \
    --exclude='.env' \
    --exclude='boeck_agent.db' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='venv' \
    ./ "$SERVER:$APP_DIR/"

# Remote: Dependencies aktualisieren + Bot neustarten
ssh "$SERVER" "
    cd $APP_DIR
    chown -R $APP_USER:$APP_USER .
    sudo -u $APP_USER bash -c '
        source $APP_DIR/venv/bin/activate
        pip install -r requirements.txt -q
    '
    systemctl restart boeck-agent
    echo 'Bot neugestartet.'
    sleep 2
    systemctl status boeck-agent --no-pager
"

echo ""
echo "Update abgeschlossen! Logs: ssh $SERVER journalctl -u boeck-agent -f"
