#!/usr/bin/env bash
# One-time VPS setup (Ubuntu 22.04 / 24.04). Run as root or with sudo.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/amazon-bot}"
BOT_USER="${BOT_USER:-amazonbot}"
REPO_URL="${REPO_URL:-}"  # optional: git clone URL; if empty, copy project manually

echo "==> Installing system packages..."
apt-get update
apt-get install -y \
  python3 python3-venv python3-pip git \
  ca-certificates curl \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2

if ! id "$BOT_USER" &>/dev/null; then
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$BOT_USER"
fi

mkdir -p "$INSTALL_DIR"
chown "$BOT_USER:$BOT_USER" "$INSTALL_DIR"

if [[ -n "$REPO_URL" ]] && [[ ! -f "$INSTALL_DIR/app/main.py" ]]; then
  echo "==> Cloning repository..."
  sudo -u "$BOT_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi

if [[ ! -f "$INSTALL_DIR/app/main.py" ]]; then
  echo "ERROR: Project not found in $INSTALL_DIR"
  echo "Copy your project there (rsync/scp) or set REPO_URL=..."
  exit 1
fi

echo "==> Python venv + dependencies..."
sudo -u "$BOT_USER" bash -c "
  cd '$INSTALL_DIR'
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
  .venv/bin/pip install -r requirements-playwright.txt
  .venv/bin/python -m playwright install chromium
"

mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
chown -R "$BOT_USER:$BOT_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  sudo -u "$BOT_USER" cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  echo ""
  echo ">>> Edit $INSTALL_DIR/.env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SQLITE_PATH)"
fi

echo "==> Installing systemd units..."
sed "s|/opt/amazon-bot|$INSTALL_DIR|g" "$INSTALL_DIR/deploy/amazon-bot.service" \
  > /etc/systemd/system/amazon-bot.service
sed "s|/opt/amazon-bot|$INSTALL_DIR|g" "$INSTALL_DIR/deploy/amazon-shift-alert.service" \
  > /etc/systemd/system/amazon-shift-alert.service

systemctl daemon-reload

echo ""
echo "Done. Next steps:"
echo "  1. Copy jobs.db + .env from laptop (see deploy/DEPLOY_VPS.md)"
echo "  2. Set SQLITE_PATH=/opt/amazon-bot/data/jobs.db in .env"
echo "  3. In config.yaml set shift_alert.playwright_headless: true (VPS has no screen)"
echo "  4. systemctl enable --now amazon-bot"
echo "  5. Skip amazon-shift-alert unless you run app.shift_alert"
echo "     (Sirf app.main: systemctl enable --now amazon-bot only)"
