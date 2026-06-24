# amazon-shift-telegram-alert

Fast, production-ready Telegram alert system for **public** Amazon UK job pages. For `amazon.jobs` **search** URLs, the site is a React app: the bot calls the public **`/…/search.json`** endpoint (same query params as the browser) instead of scraping empty HTML shells.

## Safety / constraints

- This project targets **public pages only**.
- It does **not** bypass authentication, solve CAPTCHAs, or interact with employee-only systems.
- If Amazon blocks automated requests, reduce polling frequency and/or use fewer sources. Respect site terms.

## Why some Telegram channels look “more up to date”

`jobsatamazon.co.uk` is a single-page app: the **structured job details (schedule, weekly hours, seasonal/part-time, etc.) are usually delivered as JSON** after the page loads (often via AppSync/GraphQL-style endpoints), not as static HTML.

This project now **captures those public JSON responses during Playwright rendering** and merges the fields into your alerts (see `behavior.capture_jobsatamazon_network_json` in `config.yaml`). When payloads look like GraphQL/AppSync shapes (for example `data.getJobDetails`), `app/parsers/jobsatamazon_graphql.py` maps fields such as **pay, schedule, weekly hours, first day, applyState, location/postcode**, and then **merges** that with a generic JSON fallback extractor if needed.

That typically gets you closer to the “rich” channel format than DOM scraping alone — without using private/employee systems.

## Features

- **Low-latency polling** using `httpx` with connection reuse + retries/backoff
- **Modular architecture** (`fetchers/`, `parsers/`, `storage/`, `notifiers/`)
- **jobsatamazon.co.uk JSON capture** (optional): records public JSON during rendering; **structured GraphQL/AppSync** responses are parsed via `jobsatamazon_graphql.py`, with a heuristic fallback for other shapes
- **SQLite deduplication** (new/updated/unchanged via content hash)
- **Filters**: location keywords, title keywords, min pay (only when pay is visible)
- **UK timezone** timestamps (Europe/London)
- **Rotating logs** + rich console logs
- **Dry-run mode**
- **Snapshots** saved when parsing returns 0 jobs / fetch errors (for debugging)
- **CLI**:
  - `python -m app.main` or `python -m app.main run` — run continuously (default)
  - `python -m app.main once`
  - `python -m app.main test-alert`
- **Multi-profile shift watcher** (optional): `shift_alert.profiles` in `config.yaml`; `python -m app.shift_alert login --profile <id>` and `run` over enabled profiles (see `config.yaml` comments).

## Project layout

```
app/
  main.py
  dashboard.py
  templates/
  config.py
  fetchers/
  parsers/
  notifiers/
  storage/
  models/
  utils/
tests/
.env.example
config.yaml
requirements.txt
README.md
```

## Setup (Windows, PowerShell)

From the project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and set:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- optionally `POLL_INTERVAL_SECONDS` (default 20)

Edit `config.yaml` and set your public `sources.urls` plus filters for locations/keywords near Leicester.

## Create a Telegram bot (BotFather)

- In Telegram, open `@BotFather`
- Run `/newbot` and follow prompts
- Copy the token into `.env` as `TELEGRAM_BOT_TOKEN`

## Get your chat ID

Two common approaches:

- **Private chat**: message your bot once, then use a helper bot like `@userinfobot` to get your numeric ID.
- **Group**: add the bot to a group, send a message, then inspect updates via Telegram getUpdates (manual step).

Set `.env` `TELEGRAM_CHAT_ID` to that ID (example: `123456789` or `-100123...` for supergroups).

## Run

Send a sample alert to confirm your bot works:

```powershell
python -m app.main test-alert
```

## Using jobsatamazon.co.uk (optional)

`jobsatamazon.co.uk` is a dynamic single-page app. For those URLs, this project can use an optional Playwright renderer.

Install the optional dependency and Chromium:

```powershell
pip install -r requirements-playwright.txt
python -m playwright install chromium
```

Then add your `jobsatamazon.co.uk` URL(s) under `sources.urls` in `config.yaml`. The app will automatically use Playwright for those URLs and keep `amazon.jobs` on fast `httpx`.

Run a single poll:

```powershell
python -m app.main once
```

Run continuously:

```powershell
python -m app.main run
```

## Deploy / run continuously

### Windows Task Scheduler

Create a task that runs (Start in = project directory):

```powershell
.\.venv\Scripts\python.exe -m app.main run
```

### VPS (24/7, laptop off)

Full step-by-step (Ubuntu, systemd, copy `jobs.db`, Playwright on Linux):

**[deploy/DEPLOY_VPS.md](deploy/DEPLOY_VPS.md)**

Quick start after copying the project to `/opt/amazon-bot`:

```bash
sudo ./deploy/install-vps.sh
# edit .env, then:
sudo systemctl enable --now amazon-bot
```

## Job History Dashboard (read-only)

Uses the same SQLite file as the poller (`SQLITE_PATH` in `.env`). The UI reads the **`jobs`** and **`subscribers`** tables defined in `app/storage/sqlite.py` (not the hypothetical `seen_jobs` schema). There is no `notified` column in the stock schema; the dashboard still loads, shows **n/a** in the alert column, and keeps alert-based counts at **0** until you add and populate `notified` on `jobs`.

```powershell
pip install -r requirements.txt
uvicorn app.dashboard:app --host 127.0.0.1 --port 8080 --reload
```

Then open `http://127.0.0.1:8080/`. Run this alongside or instead of the bot UI; it does not start the poller.

## Tests

```powershell
pytest -q
```

## Notes on pay and “expected pay”

- If pay is not visible on the public page, alerts show **Pay not listed**.
- You can optionally set:
  - `DEFAULT_EXPECTED_PAY_GBP_PER_HOUR_MIN`
  - `DEFAULT_EXPECTED_PAY_GBP_PER_HOUR_MAX`
  in `.env` to display a **user-estimated** range (clearly labeled).

