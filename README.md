# UltraMsg Reminder Bot

A Flask application that receives UltraMsg WhatsApp webhooks, stores reminders in a
database-backed SQLite queue, and sends them through UltraMsg on schedule.

**Deployment target: Render.** No Nginx, systemd, VPS, Docker, or manually managed
Gunicorn process is required. Render runs the Flask WSGI entry point with its native
Python runtime.

---

## Contents

1. [Architecture and Render constraints](#architecture-and-render-constraints)
2. [Features](#features)
3. [Project layout](#project-layout)
4. [Local development](#local-development)
5. [Environment variables](#environment-variables)
6. [Render deployment](#render-deployment)
7. [Database initialization and persistence](#database-initialization-and-persistence)
8. [UltraMsg webhook configuration and testing](#ultramsg-webhook-configuration-and-testing)
9. [Reminder scheduling](#reminder-scheduling)
10. [Custom domain and HTTPS](#custom-domain-and-https)
11. [Timezone handling](#timezone-handling)
12. [Verification checklist](#verification-checklist)
13. [Operations and backups](#operations-and-backups)
14. [Troubleshooting](#troubleshooting)
15. [API reference](#api-reference)
16. [Security notes](#security-notes)

---

## Architecture and Render constraints

Render has two important restrictions that shape this deployment:

1. **Persistent disks are available only on paid services** and can be attached only
   to a web service, private service, or background worker. A Free web service has an
   ephemeral filesystem, so a SQLite file would be lost on every restart, deploy, or
   spin-down.
2. **Cron Jobs cannot attach or read a persistent disk.** A Render Cron Job runs on
   separate, ephemeral compute.

Therefore the deployment uses one stateful web service plus one stateless cron job:

```text
UltraMsg
   |
   | HTTPS POST /webhook/ultramsg?token=...
   v
Render Web Service  --->  /var/data/reminders.sqlite3  (persistent disk)
   ^
   | HTTPS POST /tasks/run-reminders
   | Authorization: Bearer <REMINDER_TASK_TOKEN>
   |
Render Cron Job  --->  python run_reminders.py --remote
   (every 5 minutes, UTC)
```

- The **web service** owns the SQLite file at `/var/data/reminders.sqlite3`.
- The **cron job** never opens SQLite. It calls a token-protected endpoint on the web
  service, and the web service performs the atomic claim, UltraMsg send, and status
  update.
- `run_reminders.py` also supports **local mode**, where it opens SQLite directly.
  That mode is used locally or on any host where the script shares the same disk as
  the Flask app. Do **not** use local mode for the Render cron job.
- Only one web instance may run because a Render disk is accessible by exactly one
  service instance. Do not enable scaling for this service.

Render Cron Jobs are billed per run with a minimum monthly charge, and their
schedules are evaluated in **UTC**. See the Render docs:
<https://render.com/docs/cronjobs>, <https://render.com/docs/disks>,
<https://render.com/docs/free>.

---

## Features

- Flask app factory with a WSGI entry point (`wsgi:application`).
- SQLite reminder queue with statuses: `pending`, `processing`, `sent`, `failed`,
  `cancelled`.
- Atomic claiming with `BEGIN IMMEDIATE` so overlapping dispatcher runs cannot send
  the same reminder twice.
- Stale `processing` lock recovery, retry limits, exponential backoff, and manual
  retry from the dashboard.
- Persistent reminder state across cron executions and deploys.
- UltraMsg incoming-message webhook with command handling.
- Duplicate webhook detection using a unique event ID table.
- Optional HTTP Basic authentication for the dashboard and API.
- Timezone-aware scheduling and display using `Asia/Karachi`.
- Render Blueprint (`render.yaml`) for a web service, persistent disk, and cron job.

---

## Project layout

```text
.
├── app.py                  Flask app factory, routes, webhook, task endpoint
├── wsgi.py                 WSGI entry point: application = create_app()
├── config.py               Environment-driven configuration and absolute paths
├── db.py                   SQLite connection, schema, and request-scoped helpers
├── reminders.py            Queue operations, claiming, retries, dispatcher logic
├── commands.py             WhatsApp command parser and replies
├── ultramsg.py             UltraMsg HTTP client
├── utils.py                Phone normalization, time parsing, timezone formatting
├── init_db.py              Creates/verifies the database and prints paths
├── run_reminders.py        Local SQLite dispatcher or remote cron trigger
├── render.yaml             Render Blueprint
├── requirements.txt        Runtime dependencies
├── requirements-dev.txt    Runtime + test dependencies
├── .env.example            Local environment template
├── .python-version         Python 3.12.16 for Render/local
├── templates/              Dashboard HTML
├── static/                 Dashboard CSS
└── tests/                  Pytest suite
```

---

## Local development

### 1. Create a virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

### 2. Create the local environment file

```bash
cp .env.example .env
```

Fill in your UltraMsg instance ID and token. Generate long random values for
`SECRET_KEY`, `ULTRAMSG_WEBHOOK_TOKEN`, and `REMINDER_TASK_TOKEN`.

For local development the example uses:

```dotenv
DATA_DIR=./data
DATABASE_PATH=reminders.sqlite3
APP_TIMEZONE=Asia/Karachi
FORCE_HTTPS=false
TRUST_PROXY=false
```

A relative `DATABASE_PATH` is resolved **inside `DATA_DIR`**. A relative `DATA_DIR`
is resolved against the project directory. Both are converted to absolute paths at
runtime. On Render, use the absolute path `/var/data/reminders.sqlite3`.

> `.env` is git-ignored. Never commit real tokens. The `.env` in this workspace is a
> local development file; rotate any credential that has ever been shared or exposed.

### 3. Initialize the database

```bash
python init_db.py
```

Expected output includes an absolute path such as:

```text
Database ready: /Users/you/Desktop/personal/data/reminders.sqlite3
Data directory: /Users/you/Desktop/personal/data
Timezone: Asia/Karachi
Writable: yes
```

### 4. Run the app

```bash
gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 4 --timeout 180 wsgi:application
```

Open <http://127.0.0.1:8000>. If `DASHBOARD_PASSWORD` is set, use HTTP Basic auth.

For development with auto-reload:

```bash
flask --app wsgi run --debug
```

### 5. Run the dispatcher locally

```bash
# Show due reminders without claiming or sending
python run_reminders.py --dry-run

# Claim and send up to 10 due reminders from the local SQLite file
python run_reminders.py

# Process a different batch size
python run_reminders.py --limit 25
```

Local mode acquires an OS file lock (`DATA_DIR/run_reminders.lock`) and then uses the
same atomic SQLite claim as the web endpoint.

---

## Environment variables

Set these in the Render Dashboard (or let `render.yaml` generate/prompt for them).
Do not put production values in `render.yaml` for secrets marked `sync: false`.

| Variable | Required | Example / default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | Yes | long random string | Flask session signing key. |
| `DATA_DIR` | Yes | `/var/data` | Persistent directory for SQLite files. |
| `DATABASE_PATH` | No | `/var/data/reminders.sqlite3` | Absolute SQLite path. A relative value is resolved under `DATA_DIR`. |
| `APP_TIMEZONE` | No | `Asia/Karachi` | Timezone for parsing and displaying reminders. |
| `APP_BASE_URL` | No | `https://reminders.example.com` | Optional public base URL used in the dashboard. |
| `ULTRAMSG_INSTANCE_ID` | Yes | `instance123456` | UltraMsg instance ID. |
| `ULTRAMSG_TOKEN` | Yes | UltraMsg token | UltraMsg API token. |
| `ULTRAMSG_API_URL` | No | `https://api.ultramsg.com` | UltraMsg API base URL. |
| `ULTRAMSG_TIMEOUT` | No | `10` | Outbound UltraMsg request timeout in seconds. |
| `ULTRAMSG_WEBHOOK_TOKEN` | Yes | long random string | Token required on `/webhook/ultramsg`. |
| `DEFAULT_COUNTRY_CODE` | No | `92` | Adds a country code to local numbers starting with `0`. |
| `REMINDER_TASK_TOKEN` | Yes | long random string | Bearer token for `/tasks/run-reminders`. |
| `REMINDER_POLL_LIMIT` | No | `10` | Max reminders per dispatcher run. |
| `REMINDER_MAX_ATTEMPTS` | No | `6` | Attempts before a reminder becomes `failed`. |
| `REMINDER_RETRY_BASE_SECONDS` | No | `300` | Base for exponential retry backoff. |
| `REMINDER_STALE_SECONDS` | No | `900` | Age after which a `processing` lock is recovered. |
| `DASHBOARD_USERNAME` | No | `admin` | HTTP Basic auth username. |
| `DASHBOARD_PASSWORD` | No | long random string | HTTP Basic auth password. Empty disables auth (not recommended in production). |
| `FORCE_HTTPS` | No | `true` on Render | Redirects non-HTTPS requests with HTTP 308. `/healthz` is exempt. |
| `TRUST_PROXY` | No | `true` on Render | Enables Werkzeug `ProxyFix` for proxy headers. |
| `MAX_REMINDER_MESSAGE_LENGTH` | No | `1000` | Maximum reminder text length. |
| `REMINDER_TASK_URL` | Cron only | `https://<service>.onrender.com/tasks/run-reminders` | Endpoint called by `run_reminders.py --remote`. |
| `REMINDER_TASK_TOKEN` | Cron only | same value as web service | Bearer token for the task endpoint. |
| `PYTHON_VERSION` | No | `3.12.16` | Python version for Render native runtime. |

`render.yaml` uses `generateValue: true` for `SECRET_KEY`,
`ULTRAMSG_WEBHOOK_TOKEN`, `REMINDER_TASK_TOKEN`, and `DASHBOARD_PASSWORD`, and shares
`REMINDER_TASK_TOKEN` with the cron job through `fromService`. You can copy the
generated values from the web service's **Environment** page.

---

## Render deployment

### Prerequisites

- A Render account with a payment method. The SQLite persistent disk requires a paid
  instance plan; the Free plan cannot attach disks and spins down on idle.
- A Git repository containing this project (GitHub, GitLab, or Bitbucket).
- An UltraMsg account with an instance ID and token.
- Optional: a domain you control for a custom webhook URL.

### 1. Push the project to Git

```bash
git init
git add .
git commit -m "Add UltraMsg reminder bot"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Before pushing, verify that `.env`, `data/`, and `*.sqlite3` are not staged:

```bash
git status --short
```

### 2. Option A: Deploy with the Blueprint (recommended)

1. Open the Render Dashboard and choose **Blueprints → New Blueprint Instance**.
2. Connect the repository and select the branch containing `render.yaml`.
3. Review the two services:
   - `ultramsg-reminders` — Python web service, disk mounted at `/var/data`.
   - `ultramsg-reminder-dispatcher` — Python cron job, every 5 minutes.
4. When Render prompts for values marked `sync: false`, enter:
   - `ULTRAMSG_INSTANCE_ID`
   - `ULTRAMSG_TOKEN`
5. Render generates the remaining secrets. After the first deploy, open the web
   service's **Environment** page and copy:
   - `ULTRAMSG_WEBHOOK_TOKEN` — needed in the UltraMsg webhook URL.
   - `DASHBOARD_PASSWORD` — needed to log in to the dashboard.
   - `REMINDER_TASK_TOKEN` — normally not needed manually because the cron job
     receives it via `fromService`.
6. If you rename the web service, update the cron job's `REMINDER_TASK_URL` to
   `https://<new-name>.onrender.com/tasks/run-reminders` and redeploy the cron job.
7. Wait for the web service deploy to become live. On first start the application
   creates the SQLite schema automatically on `/var/data`.

### 3. Option B: Deploy manually

1. **New → Web Service**, connect the repository, and choose the Python runtime.
2. Set:
   - Name: `ultramsg-reminders`
   - Region: a region close to your users (the Blueprint uses `oregon`)
   - Branch: `main`
   - Build command: `pip install -r requirements.txt`
   - Start command:
     ```bash
     gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180 wsgi:application
     ```
   - Instance plan: a paid plan such as `0.5c-512mb` (required for a disk).
3. Under **Advanced → Disks**, add:
   - Name: `reminder-data`
   - Mount path: `/var/data`
   - Size: `1 GB` (increase only when needed; disks cannot be shrunk).
4. Add environment variables from the table above. At minimum:
   ```text
   PYTHON_VERSION=3.12.16
   DATA_DIR=/var/data
   DATABASE_PATH=/var/data/reminders.sqlite3
   APP_TIMEZONE=Asia/Karachi
   TRUST_PROXY=true
   FORCE_HTTPS=true
   SECRET_KEY=<generated>
   ULTRAMSG_INSTANCE_ID=<your instance>
   ULTRAMSG_TOKEN=<your token>
   ULTRAMSG_WEBHOOK_TOKEN=<long random value>
   REMINDER_TASK_TOKEN=<long random value>
   DEFAULT_COUNTRY_CODE=92
   DASHBOARD_USERNAME=admin
   DASHBOARD_PASSWORD=<long random value>
   ```
5. Set the health check path to `/healthz`.
6. Create the cron job: **New → Cron Job**, same repository and Python runtime.
   - Name: `ultramsg-reminder-dispatcher`
   - Schedule: `*/5 * * * *`
   - Build command: `pip install -r requirements.txt`
   - Command: `python run_reminders.py --remote`
   - Environment:
     ```text
     PYTHON_VERSION=3.12.16
     REMINDER_TASK_URL=https://<web-service>.onrender.com/tasks/run-reminders
     REMINDER_TASK_TOKEN=<same value as the web service>
     ```

### 4. Reload / redeploy the Flask application

Render deploys automatically on every push to the connected branch. To deploy
manually:

1. Open the web service in the Render Dashboard.
2. Click **Manual Deploy → Deploy latest commit**.
3. Environment variable changes also trigger a restart/redeploy.

Because the web service has a persistent disk, deploys briefly stop the old instance
before starting the new one. Zero-downtime deploys are not available on disk-backed
services.

The WSGI entry point is `wsgi.py`:

```python
from app import create_app

application = create_app()
app = application
```

Render's start command references `wsgi:application`; no separate WSGI configuration
file needs to be uploaded to a shared hosting path.

---

## Database initialization and persistence

The application calls `initialize_database()` during startup, so a new disk is
initialized automatically on the first deploy. It is safe to run repeatedly because
the schema uses `CREATE TABLE IF NOT EXISTS`.

To verify or initialize explicitly from the Render Shell (paid services only):

```bash
cd /opt/render/project/src
python init_db.py
```

Expected output:

```text
Database ready: /var/data/reminders.sqlite3
Data directory: /var/data
Timezone: Asia/Karachi
Writable: yes
```

Check the disk mount and permissions:

```bash
ls -la /var/data
df -h /var/data
python -c "import os; print(os.access('/var/data', os.W_OK))"
```

SQLite in WAL mode also creates these files in the same directory:

```text
/var/data/reminders.sqlite3
/var/data/reminders.sqlite3-wal
/var/data/reminders.sqlite3-shm
```

Important:

- Do **not** initialize the database in a Render build command, pre-deploy command,
  or cron job. Those run on separate compute and cannot see the disk.
- Do not point `DATABASE_PATH` at `/tmp`, the project directory, or any ephemeral
  path. Use `/var/data`.
- Do not use `chmod 777`. Render mounts the disk with permissions for the service
  user; the app creates its own files.
- The disk survives restarts and deploys. Deleting the service or the disk destroys
  the data.

---

## UltraMsg webhook configuration and testing

### 1. Build the webhook URL

Use the generated webhook token from the web service environment:

```text
https://<your-service>.onrender.com/webhook/ultramsg?token=<ULTRAMSG_WEBHOOK_TOKEN>
```

If you use a custom domain:

```text
https://reminders.example.com/webhook/ultramsg?token=<ULTRAMSG_WEBHOOK_TOKEN>
```

The app also accepts the token in the `X-UltraMsg-Token` header. A query parameter is
easiest to configure in UltraMsg.

### 2. Configure UltraMsg

1. Open the UltraMsg instance settings.
2. Find the incoming-message webhook setting.
3. Set the webhook URL to the HTTPS URL above.
4. Enable the `message_received` event.
5. Save the settings.

The app returns HTTP 200 for ignored/unknown events so that UltraMsg does not retry
them indefinitely. Invalid tokens return HTTP 401.

### 3. Test the endpoint

Check that the app is reachable over HTTPS:

```bash
curl -i "https://<your-service>.onrender.com/webhook/ultramsg?token=<ULTRAMSG_WEBHOOK_TOKEN>"
```

Expected:

```json
{"endpoint":"/webhook/ultramsg","status":"ready"}
```

Send a simulated incoming message:

```bash
curl -i -X POST \
  "https://<your-service>.onrender.com/webhook/ultramsg?token=<ULTRAMSG_WEBHOOK_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "message_received",
    "data": {
      "id": "test-message-1",
      "from": "923001234567@c.us",
      "type": "chat",
      "body": "help"
    }
  }'
```

Expected response:

```json
{"reply_sent":true,"status":"ok"}
```

This sends a real WhatsApp reply to the `from` number. Use your own number for the
test. Then send `help` from WhatsApp to the UltraMsg-connected number and confirm the
bot replies with the command menu.

Useful commands:

```text
help
list
cancel 12
remind me in 10 minutes to call Ali
remind me in 2 hours to send the report
remind me tomorrow at 09:00 to submit the form
remind me at 2026-11-01 14:30 to attend the meeting
add goto get milk at 7:30
remind goto get milk at 7:30
```

`add` and `remind` accept one-line messages in the form `<message> at <time>`.
A clock time without a date uses today, or tomorrow if that time has already passed.
Reusing the same `data.id` returns `{"status":"duplicate"}` and does not create a
second reminder.

### 4. Confirm HTTPS

UltraMsg must call an HTTPS URL. With `FORCE_HTTPS=true`, plain HTTP requests are
redirected with HTTP 308; `/healthz` is excluded so Render health checks keep working.
Render provides managed TLS for both `*.onrender.com` and verified custom domains.

---

## Reminder scheduling

### Queue lifecycle

1. A reminder is inserted with `status=pending`, `due_at` in UTC, and
   `next_attempt_at=due_at`.
2. Every 5 minutes the Render Cron Job calls `POST /tasks/run-reminders` with
   `Authorization: Bearer <REMINDER_TASK_TOKEN>`.
3. The web service recovers stale `processing` rows, then atomically claims up to
   `REMINDER_POLL_LIMIT` due rows with `BEGIN IMMEDIATE` and sets them to
   `processing` with a worker ID.
4. Each claimed reminder is sent through UltraMsg.
5. Success → `status=sent`, `sent_at` recorded. Failure → attempts increment,
   `last_error` recorded, and the row returns to `pending` with exponential backoff,
   or becomes `failed` after `REMINDER_MAX_ATTEMPTS`.
6. A stale `processing` row whose lock is older than `REMINDER_STALE_SECONDS` returns
   to `pending`.

### Why not APScheduler in the Flask process?

Render services restart on deploys, can be spun down (free plan), and run behind a web
request lifecycle. An in-process scheduler is not durable across those events and can
run multiple times if more than one worker exists. This deployment keeps scheduling
outside the web process and uses the database as the durable queue.

### Render Cron Job settings

- Schedule: `*/5 * * * *` (every 5 minutes, UTC).
- Command: `python run_reminders.py --remote`.
- Build: `pip install -r requirements.txt`.
- Environment: `REMINDER_TASK_URL` and `REMINDER_TASK_TOKEN`.

Render guarantees at most one active run of a cron service at a time and cancels an
active run if you manually trigger another. The database claim adds a second layer of
protection.

Scheduling-frequency limitations:

- All Render cron schedules are evaluated in UTC. `Asia/Karachi` is UTC+5 with no
  daylight saving time; convert daily schedules accordingly. A 5-minute polling loop
  is unaffected.
- Render Cron Jobs cannot access persistent disks. Always use `--remote` on Render;
  local mode from a cron job would open a fresh, empty SQLite file and never see the
  web service's reminders.
- Cron Jobs are paid services with a minimum monthly charge. Every 5 minutes is a
  reasonable balance between delivery latency and cost. A 1-minute schedule
  (`* * * * *`) is possible but costs more and increases load.
- Reminders can be delivered up to roughly 5 minutes after their due time. UltraMsg
  delivery time is additional.
- Cron runs are capped at 12 hours per invocation. This job is designed to finish in
  seconds and process a small batch (`REMINDER_POLL_LIMIT`, default 10).
- Render does not automatically retry a failed cron run. The next scheduled run picks
  up reminders that remain `pending`.
- If the web service is mid-deploy, the cron request may fail; the reminders stay
  pending and are retried on the next run.

### Duplicate-execution safety

- **Local mode:** `fcntl.flock` prevents two local dispatcher processes from running
  together.
- **Database:** the `BEGIN IMMEDIATE` claim selects only `pending` rows and flips them
  to `processing` atomically.
- **Render:** the cron service has a single-run guarantee.
- **Trade-off:** delivery is at-least-once. If the process is killed after UltraMsg
  accepts a message but before the database update commits, that reminder can be sent
  again. The status and `attempts` fields make this visible.

### Manual dispatcher runs

```bash
# Local direct-SQLite mode
python run_reminders.py --dry-run
python run_reminders.py --limit 5

# Remote mode (same call the Render cron job makes)
REMINDER_TASK_URL="https://<service>.onrender.com/tasks/run-reminders" \
REMINDER_TASK_TOKEN="<task token>" \
python run_reminders.py --remote
```

To retry a failed reminder, use the dashboard's **Retry** button or call the API with
a new reminder. The task endpoint response includes `claimed`, `sent`, `failed`,
`recovered`, and per-reminder `details`.

---

## Custom domain and HTTPS

Custom domains on Render require a paid workspace/service plan.

1. In the web service, open **Settings → Custom Domains**.
2. Add the fully qualified hostname, for example `reminders.example.com`.
3. At your DNS provider, create a `CNAME` record:
   ```text
   reminders.example.com  CNAME  <your-service>.onrender.com
   ```
4. Return to Render and wait for domain verification.
5. Render provisions and auto-renews a managed TLS certificate (Let's Encrypt).
6. Update the UltraMsg webhook URL to the custom domain.
7. Optional: set `APP_BASE_URL=https://reminders.example.com` so the dashboard
   displays the canonical webhook host.
8. Optional: if you use the custom domain for the cron job, update
   `REMINDER_TASK_URL` and redeploy the cron job. The `onrender.com` hostname
   continues to work.

For a naked/apex domain (`example.com`), use your registrar's redirect/forwarding
feature to `https://www.example.com`, or add a separate Render redirect service. See
<https://render.com/docs/custom-domains> and
<https://render.com/docs/naked-domains>.

`FORCE_HTTPS=true` and `TRUST_PROXY=true` are set in `render.yaml`. They ensure the
app sees the original HTTPS scheme and redirects plain HTTP clients. `/healthz` is
exempt from the redirect.

---

## Timezone handling

- `APP_TIMEZONE` defaults to `Asia/Karachi`.
- All timestamps are stored in SQLite as UTC ISO-8601 strings (`...Z`).
- Queue comparisons (`next_attempt_at <= now`) are performed in UTC.
- Dashboard input and WhatsApp command parsing interpret local times in
  `Asia/Karachi` using `zoneinfo`.
- Dashboard display converts UTC to `Asia/Karachi`.
- Render Cron schedules are UTC; the 5-minute dispatcher interval is timezone-neutral.
- The `tzdata` package is included so timezone data is available on the native
  runtime even if the base image changes.

Example: `remind me tomorrow at 09:00` on 30 October 2026 in Karachi is stored as
`2026-10-31T04:00:00Z`.

---

## Verification checklist

Run these after the first deploy and after configuration changes.

### SQLite permissions and persistent paths

```bash
# Render Shell
ls -la /var/data
df -h /var/data
cd /opt/render/project/src
python init_db.py
```

- [ ] `DATABASE_PATH` is `/var/data/reminders.sqlite3`.
- [ ] `/var/data` is writable by the service user.
- [ ] `init_db.py` prints `Writable: yes`.
- [ ] The file survives a manual redeploy (`python init_db.py` shows the same row
      count).

### WSGI import path

```bash
cd /opt/render/project/src
python -c "from wsgi import application; print(application)"
```

- [ ] No `ModuleNotFoundError`.
- [ ] Gunicorn start command references `wsgi:application`.

### Virtual environment / dependencies

Render manages the runtime environment; there is no server-side virtualenv to
activate. Locally:

```bash
source .venv/bin/activate
pip check
python -m pytest -q
```

- [ ] `pip check` reports no broken requirements.
- [ ] All tests pass.

### Health and queue

```bash
curl -sS "https://<service>.onrender.com/healthz" | python -m json.tool
```

- [ ] `status` is `ok`.
- [ ] `database_writable` is `true`.
- [ ] `timezone` is `Asia/Karachi`.
- [ ] `database` points at `/var/data/reminders.sqlite3`.
- [ ] `queue` counts look correct.

### Webhook accessibility

- [ ] `GET /webhook/ultramsg?...` returns `{"status":"ready"}` over HTTPS.
- [ ] Invalid token returns HTTP 401.
- [ ] Valid simulated POST returns HTTP 200 and `reply_sent: true`.
- [ ] Sending `help` from WhatsApp receives the command menu.
- [ ] Render logs show the request and no UltraMsg authentication errors.

### Scheduled-task execution

- [ ] The cron job exists with schedule `*/5 * * * *`.
- [ ] The cron job has `REMINDER_TASK_URL` and the same `REMINDER_TASK_TOKEN` as the
      web service.
- [ ] A manual **Trigger Run** succeeds and returns a JSON summary.
- [ ] A reminder scheduled 2 minutes in the future is delivered within ~5 minutes.
- [ ] The reminder row becomes `sent` with a `sent_at` value.
- [ ] A failed UltraMsg call increments `attempts` and schedules a retry.

### Timezone handling

- [ ] Create a reminder through the dashboard for a known Karachi time.
- [ ] The table displays the same local time.
- [ ] The stored `due_at` (via `/api/reminders`) is 5 hours behind Karachi.
- [ ] An incoming `remind me tomorrow at 09:00 ...` command stores 04:00 UTC.

---

## Operations and backups

### Logs

- Web requests and UltraMsg errors: Render **Logs** for the web service.
- Dispatcher output and failures: Render **Runs** and logs for the cron job.
- Local errors include worker IDs and reminder IDs; tokens are not logged.

### Backups

SQLite backups can be created without stopping the service using the online backup
command:

The Render native Python image may not include the `sqlite3` CLI, so use Python's
online backup API:

```bash
python - <<'PY'
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

backup_dir = Path("/var/data/backups")
backup_dir.mkdir(parents=True, exist_ok=True)
target = backup_dir / f"reminders-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.sqlite3"

source = sqlite3.connect("/var/data/reminders.sqlite3")
destination = sqlite3.connect(target)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
print(target)
PY
```

Download a copy off the Render disk periodically. A backup stored on the same disk
does not protect against disk loss. Use Render's SSH/SCP access for paid services or
your preferred secure transfer method.

Restoring:

1. Put the service into maintenance or stop the cron job.
2. Replace `/var/data/reminders.sqlite3` with the backup.
3. Remove stale `-wal` and `-shm` files if present.
4. Restart the web service and run `python init_db.py`.
5. Re-enable the cron job.

### Rotating secrets

- **UltraMsg token:** regenerate it in UltraMsg, update `ULTRAMSG_TOKEN` in the Render
  environment, update `.env` locally, and redeploy.
- **Webhook token:** generate a new value, update `ULTRAMSG_WEBHOOK_TOKEN`, redeploy,
  then update the webhook URL in UltraMsg immediately.
- **Task token:** update `REMINDER_TASK_TOKEN` on the web service; the cron job uses
  `fromService` in the Blueprint. For a manually created cron job, update both
  services.
- **Dashboard password:** update `DASHBOARD_PASSWORD` and redeploy.

### Disk and scaling

- The disk can be increased in size but not decreased.
- With a disk attached, the service is limited to one instance.
- Do not add multiple web workers that assume independent in-memory state; SQLite is
  shared on disk, but the design intentionally uses one Gunicorn worker with threads.
- To scale horizontally, migrate the queue and reminders to Render Postgres and use a
  shared worker/cron architecture.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Deploy fails at build | Missing `requirements.txt`/wrong root directory | Set root directory to the repo root and build command to `pip install -r requirements.txt`. |
| Web service 502/503 at startup | Import error, missing env var, or disk not mounted | Check Render logs; run `python -c "from wsgi import application"` in the Shell. |
| `database is readonly` | `DATABASE_PATH` points at an ephemeral or read-only path | Set `DATA_DIR=/var/data`, `DATABASE_PATH=/var/data/reminders.sqlite3`, and verify the disk is attached. |
| Data disappears after restart | Free plan or no disk attached | Use a paid instance and attach a persistent disk at `/var/data`. |
| Webhook returns 401 | Token mismatch or URL-encoded spaces | Copy `ULTRAMSG_WEBHOOK_TOKEN` exactly; keep the token in the query string. |
| Webhook returns 200 but no WhatsApp reply | UltraMsg credentials invalid, recipient not on WhatsApp, or UltraMsg API error | Check Render logs for `UltraMsg` errors; verify the instance status in UltraMsg. |
| Cron run succeeds but sends nothing | No due reminders, wrong task URL, or token mismatch | Check the JSON summary; verify `REMINDER_TASK_URL` and the shared token. |
| Cron returns 401 | `REMINDER_TASK_TOKEN` differs between web and cron | Use `fromService` or copy the value into both services. |
| Cron returns 503 | Task endpoint disabled or UltraMsg not configured | Set `REMINDER_TASK_TOKEN` and `ULTRAMSG_*` on the web service. |
| Reminders stay pending | `next_attempt_at` is in the future due to retry backoff | Inspect `attempts` and `last_error` in the dashboard; wait for backoff or retry manually. |
| Duplicate WhatsApp messages | At-least-once delivery after a crash between send and commit | Expected edge case; check `sent_at`/`attempts` and reduce external retries. |
| Timestamps are 5 hours off | `APP_TIMEZONE` missing or set incorrectly | Set `APP_TIMEZONE=Asia/Karachi` and redeploy. |
| Cron runs at the wrong time | Render cron expressions are UTC | Convert local times to UTC (Karachi is UTC+5, no DST). |
| Deploy causes brief downtime | Disk-backed services cannot do zero-downtime deploys | Expected. Schedule deploys for low-traffic periods. |
| Health check fails but app works manually | `/healthz` DB check failing or startup incomplete | Inspect `/healthz` output and logs; verify disk permissions. |
| `database is locked` | Long-running write transaction or too many concurrent writers | Keep one Gunicorn worker; the app sets a 30s busy timeout. |
| `ModuleNotFoundError` on Render | Dependencies not installed or wrong Python version | Check the build logs and `PYTHON_VERSION`; run `pip install -r requirements.txt`. |

---

## API reference

If `DASHBOARD_PASSWORD` is set, dashboard and API routes require HTTP Basic auth.
Webhook and task routes use their own tokens.

### `GET /healthz`

Public. Returns service, database, queue, and timezone status.

### `GET /api/reminders`

Query parameters: `status` (one of the five statuses), `limit` (default 50, max 500).

```bash
curl -u admin:"$DASHBOARD_PASSWORD" \
  "https://<service>.onrender.com/api/reminders?status=pending&limit=20"
```

### `POST /api/reminders`

```bash
curl -u admin:"$DASHBOARD_PASSWORD" \
  -X POST "https://<service>.onrender.com/api/reminders" \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+923001234567",
    "message": "Call Ali about the project",
    "due_at": "2026-11-01T14:30:00+05:00"
  }'
```

`due_at` may be ISO 8601 with an offset, or a local date/time such as
`2026-11-01 14:30`, which is interpreted in `APP_TIMEZONE`.

### `POST /webhook/ultramsg?token=<ULTRAMSG_WEBHOOK_TOKEN>`

Accepts UltraMsg JSON or form-encoded payloads. Handles `message_received` events,
deduplicates by message ID, and replies to commands. Returns 200 for ignored events.

### `POST /tasks/run-reminders`

Protected by `Authorization: Bearer <REMINDER_TASK_TOKEN>`. Claims and sends due
reminders. This is the endpoint called by the Render cron job. An optional
`?limit=N` query parameter overrides `REMINDER_POLL_LIMIT` for that run.

```bash
curl -X POST "https://<service>.onrender.com/tasks/run-reminders" \
  -H "Authorization: Bearer $REMINDER_TASK_TOKEN"
```

### Dashboard

- `GET /` — queue overview, statistics, create form, webhook hint.
- `POST /reminders` — form-encoded reminder creation.
- `POST /reminders/<id>/cancel` — cancel a pending/failed reminder.
- `POST /reminders/<id>/retry` — requeue a failed reminder.

---

## Security notes

- Keep `SECRET_KEY`, UltraMsg credentials, webhook token, task token, and dashboard
  password in Render environment variables. Never commit `.env`.
- Rotate the UltraMsg token if it has been shared, pasted into a chat, or committed to
  Git. Update Render and the UltraMsg dashboard afterward.
- The webhook URL includes a secret token. Treat the full URL as a credential.
- Use a long, random `DASHBOARD_PASSWORD`; the dashboard exposes recipient phone
  numbers and message contents.
- Use HTTPS end to end (`FORCE_HTTPS=true`, managed TLS, HTTPS webhook URL).
- Do not add public unauthenticated endpoints that expose the reminder database.
- The task endpoint uses a bearer token; do not expose it in client-side code.
- Review Render's access controls and disk backup strategy for production data.
- UltraMsg messages and phone numbers are personal data; retain only what you need and
  protect database backups.
# personalreminder
