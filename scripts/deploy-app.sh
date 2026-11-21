#!/usr/bin/env bash
# Deploys (or redeploys) the webapp on this server:
#   browser -> gateway HTTP port -> nginx on port 80 -> Streamlit on 127.0.0.1:8501
# Streamlit runs as a systemd service under your regular user, so it keeps running after you
# log out and starts again after a reboot. Safe to run again.
#
# Usage (on the server, from the repo folder, as your regular user):
#   git pull && uv sync
#   sudo bash scripts/deploy-app.sh

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo: sudo bash $0" >&2
    exit 1
fi

APP_USER="${SUDO_USER:-}"
if [ -z "$APP_USER" ] || [ "$APP_USER" = "root" ]; then
    echo "Run this with sudo from your regular user, not as root: the app runs as that user." >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="stadvdb-app"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
NGINX_SITE="/etc/nginx/sites-available/stadvdb"

if [ ! -x "$REPO_DIR/.venv/bin/streamlit" ]; then
    echo "Streamlit isn't installed in $REPO_DIR/.venv." >&2
    echo "Run 'uv sync' first, as your regular user (without sudo)." >&2
    exit 1
fi

echo "==> Installing the app service ($SERVICE_FILE)"
# Fill in the placeholders. The last expression strips Windows line endings, just in case.
sed -e "s|__USER__|$APP_USER|g" \
    -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e 's/\r$//' \
    "$REPO_DIR/deploy/stadvdb-app.service" > "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" > /dev/null 2>&1
systemctl restart "$SERVICE_NAME"

echo "==> Installing the nginx site ($NGINX_SITE)"
sed -e 's/\r$//' "$REPO_DIR/deploy/nginx-stadvdb.conf" > "$NGINX_SITE"
ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/stadvdb
# The default site is the "Welcome to nginx!" page, which also claims port 80.
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> Waiting for Streamlit to start"
# curl's own messages are hidden: the first tries normally fail while Streamlit is still starting.
if curl -fsS --retry 15 --retry-delay 1 --retry-connrefused http://127.0.0.1:8501/_stcore/health > /dev/null 2>&1; then
    echo "    Streamlit is answering on 127.0.0.1:8501"
else
    echo "Streamlit didn't start. Its log:  journalctl -u $SERVICE_NAME -n 50" >&2
    exit 1
fi

echo "==> Checking nginx passes requests through on port 80"
if curl -fsS http://127.0.0.1/_stcore/health > /dev/null; then
    echo "    nginx -> Streamlit works"
else
    echo "nginx isn't passing requests to Streamlit. Check:  sudo nginx -t  and  /var/log/nginx/error.log" >&2
    exit 1
fi

echo
echo "==> Deployed. Open this server's HTTP URL in your browser, e.g. http://103.231.240.136:10160"
echo "    App log (live):  sudo journalctl -u $SERVICE_NAME -f"
echo "    Restart app:     sudo systemctl restart $SERVICE_NAME"
