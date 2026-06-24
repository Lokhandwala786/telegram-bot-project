# VPS pe bot chalana (24/7)

Laptop band rakho — bot Linux VPS par `systemd` se chalega.

## Sirf `app.main` (recommended)

Ek hi service chahiye — **job alerts + subscriber bot** (`/start`, `/pay`, `/admin`, trials):

```bash
python -m app.main run
```

| Chalao | Mat chalao |
|--------|------------|
| `systemctl enable amazon-bot` | `amazon-shift-alert` — **skip** |
| `jobs.db` + `.env` copy | `amazon_shift_session.json` — zaroori nahi |

**RAM:** Oracle free **1 GB AMD** micro often enough. Agar `config.yaml` mein `use_playwright_for_jobsatamazon: true` ho to **2 GB** ya Oracle **ARM** safer.

**Free VPS:** Oracle Always Free AMD (1 GB) ya ARM (24 GB) — sirf `amazon-bot` service.

---

## (Optional) Shift watcher alag

Sirf agar laptop par `python -m app.shift_alert run` chalate ho — tab `amazon-shift-alert.service`. **`app.main` only deploy mein ignore karo.**

---

## 1) VPS choose karo

- **Ubuntu 22.04 ya 24.04** (sabse aasaan)
- **Sirf app.main:** 1 GB RAM OK (Oracle free AMD); Playwright heavy ho to 2 GB
- Provider: Oracle (free), Hetzner, Contabo, etc.

SSH login:

```bash
ssh root@YOUR_VPS_IP
```

---

## 2) Project + database laptop se copy karo

**Zaroori:** `data/jobs.db` git mein nahi hai — subscribers VPS par tabhi rahenge jab aap DB copy karoge.

### Windows (PowerShell) se — example

```powershell
# Project folder (binaries ke bina snapshots optional)
scp -r "C:\Users\Development\Downloads\amazon_telegram_bot-main\amazon_telegram_bot-main" root@YOUR_VPS_IP:/opt/amazon-bot

# Sirf database + env (agar project pehle se VPS par hai)
scp "C:\...\amazon_telegram_bot-main\data\jobs.db" root@YOUR_VPS_IP:/opt/amazon-bot/data/jobs.db
scp "C:\...\amazon_telegram_bot-main\.env" root@YOUR_VPS_IP:/opt/amazon-bot/.env
```

*(Shift session file sirf `app.shift_alert` ke liye — `app.main` only mein skip.)*

---

## 3) VPS par install script

```bash
cd /opt/amazon-bot
chmod +x deploy/install-vps.sh
sudo INSTALL_DIR=/opt/amazon-bot BOT_USER=amazonbot ./deploy/install-vps.sh
```

Ya manual:

```bash
apt update && apt install -y python3 python3-venv python3-pip git
cd /opt/amazon-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-playwright.txt
.venv/bin/python -m playwright install chromium
# Linux libs (agar browser crash ho):
.venv/bin/python -m playwright install-deps chromium
```

---

## 4) `.env` (VPS paths)

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
POLL_INTERVAL_SECONDS=20
CONFIG_PATH=/opt/amazon-bot/config.yaml
SQLITE_PATH=/opt/amazon-bot/data/jobs.db
DRY_RUN=false
```

`SQLITE_PATH` **absolute** rakho taake `git pull` se DB dubara empty na ho.

---

## 5) `config.yaml` — VPS

**Sirf `app.main`:** `shift_alert:` section VPS par use nahi hoti — Windows Comet / `playwright_headless: false` rehne do ya ignore.

Agar job sources mein **jobsatamazon.co.uk** hai aur ye on hai:

```yaml
behavior:
  use_playwright_for_jobsatamazon: true   # app.main Chromium use karega
```

To VPS par Playwright install karo (install script karta hai). **1 GB RAM tight ho** to temporarily:

```yaml
behavior:
  use_playwright_for_jobsatamazon: false   # sirf amazon.jobs — Playwright skip, 1 GB OK
```

Phir `requirements-playwright.txt` install optional.

---

## 6) systemd start

```bash
sudo cp /opt/amazon-bot/deploy/amazon-bot.service /etc/systemd/system/
# Path alag ho to file mein WorkingDirectory / ExecStart edit karo

sudo systemctl daemon-reload
sudo systemctl enable --now amazon-bot
sudo systemctl status amazon-bot
```

Logs:

```bash
journalctl -u amazon-bot -f
tail -f /opt/amazon-bot/logs/amazon_shift_alert.log
```

Test (ek baar):

```bash
cd /opt/amazon-bot
sudo -u amazonbot .venv/bin/python -m app.main test-alert
```

---

*(Section removed — sirf `app.main` deploy: `amazon-shift-alert` enable mat karo.)*

---

## 7) Laptop par bot band karo

**Do jagah same bot token se poll mat chalao** — duplicate alerts / Telegram conflict ho sakta hai.

Laptop par Ctrl+C, ya Task Scheduler task disable.

Sirf VPS par:

```bash
systemctl start amazon-bot
```

---

## 8) Update (code change)

```bash
cd /opt/amazon-bot
sudo -u amazonbot git pull   # agar git se deploy kiya
sudo systemctl restart amazon-bot
# data/jobs.db aur .env touch mat karo
```

---

## 9) Firewall / security

- `.env` kabhi git commit mat karo
- `chmod 600 /opt/amazon-bot/.env`
- SSH key use karo, password login band
- Dashboard (`uvicorn`) public internet par expose mat karo bina reverse proxy + auth ke

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Subscribers 0 after deploy | `jobs.db` copy nahi hua ya galat `SQLITE_PATH` |
| Playwright crash | `playwright_headless: true`, `playwright install-deps chromium` |
| Out of memory | VPS 2GB+, ya shift service alag band karke sirf `amazon-bot` |
| Duplicate messages | Laptop + VPS dono band — ek jagah chalao |

---

## Quick command cheat sheet

```bash
systemctl status amazon-bot
systemctl restart amazon-bot
systemctl stop amazon-bot
journalctl -u amazon-bot -n 100 --no-pager
```
