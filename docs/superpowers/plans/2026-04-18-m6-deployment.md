# M6 — Deployment (systemd + healthcheck + backup + logrotate) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Debian-ready deployment bundle: systemd units for the daemon, an independent healthcheck with TG alerting, a daily SQLite `.backup` rotation, logrotate config, a one-shot install script, and deploy docs.

**Architecture:** All ops artifacts live under `deploy/` (static configs + shell scripts), plus one Python healthcheck at `scripts/healthcheck.py` that is testable. Install script drops files into canonical Linux locations (`/opt/monitor` code, `/var/lib/monitor` DB+backups, `/var/log/monitor` logs, `/etc/monitor/*` config+env, `/etc/systemd/system/*` units, `/etc/logrotate.d/monitor`). Daemon runs as a dedicated `monitor` system user under a hardened service unit.

**Tech Stack:** systemd, logrotate, sqlite3 CLI (Debian package `sqlite3`), POSIX shell, Python stdlib only for healthcheck (intentional — the healthcheck must run even if the project venv is broken, and its only external I/O is a plain HTTP POST to api.telegram.org).

---

## File Structure

```
scripts/
  healthcheck.py                 # oneshot health probe + TG alert (stdlib only)
  backup.sh                      # sqlite3 .backup + gzip + prune
deploy/
  systemd/
    monitor.service              # main daemon
    monitor-health.service       # oneshot healthcheck
    monitor-health.timer         # hourly
    monitor-backup.service       # oneshot backup
    monitor-backup.timer         # daily at 03:15 UTC
  logrotate/
    monitor                      # weekly, keep 8
  templates/
    monitor.env.example          # env template operator edits in /etc/monitor
    config.json.example          # config file template
  install.sh                     # one-shot root bootstrap
  README.md                      # deploy procedure / upgrade / rollback notes
tests/unit/
  test_healthcheck.py            # pure logic tests of check_last_digest
```

**Boundaries:**
- `scripts/healthcheck.py` — zero dependency on the `monitor` package so it survives a broken venv. All inputs via env vars. One side effect: TG POST on failure.
- `scripts/backup.sh` — reads `MONITOR_DB_PATH` + `MONITOR_BACKUP_DIR` env; shells to `sqlite3 .backup`; no DB reads via SQL.
- `deploy/systemd/*` — static unit files, no substitutions. `EnvironmentFile=/etc/monitor/monitor.env` brings runtime secrets.
- `deploy/install.sh` — idempotent: safe to re-run after a code update; only edits files we own.

---

## Task 1: `scripts/healthcheck.py` + tests

**Files:**
- Create: `scripts/healthcheck.py`
- Create: `tests/unit/test_healthcheck.py`

Context: Runs hourly via `monitor-health.timer`. Checks the last 25h of `run_log` for at least one `ok|partial` `digest` row. On failure, POSTs a Telegram alert via the TG Bot API and exits 0 (we never want systemd to flap on alert failures). The `check_last_digest` function is the testable core; `main()` is the env-reading + side-effect shell.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_healthcheck.py`:

```python
import datetime as dt
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


@pytest.fixture
def healthcheck_module():
    """Import scripts/healthcheck.py as a module without it being on sys.path."""
    path = Path(__file__).resolve().parent.parent.parent / "scripts" / "healthcheck.py"
    spec = importlib.util.spec_from_file_location("healthcheck_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "monitor.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT,
            stats TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_run(db: Path, kind: str, started_at: dt.datetime, status: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO run_log (kind, started_at, status) VALUES (?, ?, ?)",
        (kind, started_at.isoformat(), status),
    )
    conn.commit()
    conn.close()


def test_check_last_digest_ok_when_recent_digest_succeeded(healthcheck_module, tmp_db):
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    _insert_run(tmp_db, "digest", now - dt.timedelta(hours=4), "ok")
    ok, reason = healthcheck_module.check_last_digest(str(tmp_db), now)
    assert ok is True
    assert "1" in reason


def test_check_last_digest_ok_when_partial_counts(healthcheck_module, tmp_db):
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    _insert_run(tmp_db, "digest", now - dt.timedelta(hours=8), "partial")
    ok, _reason = healthcheck_module.check_last_digest(str(tmp_db), now)
    assert ok is True


def test_check_last_digest_fails_when_only_failed_runs(healthcheck_module, tmp_db):
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    _insert_run(tmp_db, "digest", now - dt.timedelta(hours=4), "failed")
    ok, reason = healthcheck_module.check_last_digest(str(tmp_db), now)
    assert ok is False
    assert "no_successful_digest" in reason


def test_check_last_digest_fails_when_digest_too_old(healthcheck_module, tmp_db):
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    _insert_run(tmp_db, "digest", now - dt.timedelta(hours=30), "ok")
    ok, _reason = healthcheck_module.check_last_digest(str(tmp_db), now)
    assert ok is False


def test_check_last_digest_ignores_surge_runs(healthcheck_module, tmp_db):
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    # Surge runs are not counted toward digest health.
    _insert_run(tmp_db, "surge", now - dt.timedelta(hours=1), "ok")
    ok, _reason = healthcheck_module.check_last_digest(str(tmp_db), now)
    assert ok is False


def test_check_last_digest_handles_missing_db(healthcheck_module, tmp_path):
    missing = tmp_path / "does-not-exist.db"
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    ok, reason = healthcheck_module.check_last_digest(str(missing), now)
    assert ok is False
    assert "db_connect_failed" in reason or "db_query_failed" in reason
```

- [ ] **Step 2: Run, expect fail**

```bash
.venv/bin/pytest tests/unit/test_healthcheck.py -v
```

Expected: collection error (module path not found).

- [ ] **Step 3: Implement `scripts/healthcheck.py`**

Create `scripts/healthcheck.py`:

```python
#!/usr/bin/env python3
"""Healthcheck for the monitor daemon.

Invoked by monitor-health.timer every hour. Connects to the DB, checks the
last 25h of run_log entries for at least one successful digest run, and
sends a Telegram alert if the check fails. Always exits 0 so systemd
doesn't retry or flap.

Uses only the Python standard library so this still runs when the project
venv is broken — that's exactly the state where we most need the alert.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import urllib.parse
import urllib.request


ALERT_WINDOW_HOURS = 25
TELEGRAM_API_BASE = "https://api.telegram.org"
HTTP_TIMEOUT_S = 10.0


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def check_last_digest(db_path: str, now: dt.datetime) -> tuple[bool, str]:
    """Return (ok, reason). ok=True means a successful digest ran within
    ALERT_WINDOW_HOURS. Surge runs are ignored — a scheduler outage still
    lets surge polls fire, but digest is the core product signal."""
    cutoff = (now - dt.timedelta(hours=ALERT_WINDOW_HOURS)).isoformat()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return (False, f"db_connect_failed: {exc}")
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM run_log "
            "WHERE kind = 'digest' AND status IN ('ok', 'partial') "
            "AND started_at >= ?",
            (cutoff,),
        ).fetchone()
    except sqlite3.Error as exc:
        conn.close()
        return (False, f"db_query_failed: {exc}")
    conn.close()
    count = row[0] if row else 0
    if count == 0:
        return (False, f"no_successful_digest_in_{ALERT_WINDOW_HOURS}h")
    return (True, f"ok_{count}_digest_runs")


def send_telegram_alert(token: str, chat_id: str, text: str) -> None:
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=HTTP_TIMEOUT_S) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck.alert_send_failed: {exc}", file=sys.stderr)


def main() -> int:
    db_path = os.environ.get("MONITOR_DB_PATH", "/var/lib/monitor/monitor.db")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    now = _now_utc()

    if not os.path.exists(db_path):
        msg = f"healthcheck.db_missing path={db_path}"
        print(msg, file=sys.stderr)
        if token and chat_id:
            send_telegram_alert(
                token, chat_id,
                f"⚠️ Monitor healthcheck: DB missing at {db_path}",
            )
        return 0

    ok, reason = check_last_digest(db_path, now)
    print(f"healthcheck.result ok={ok} reason={reason}")
    if not ok and token and chat_id:
        send_telegram_alert(
            token, chat_id,
            f"⚠️ Monitor healthcheck failed at {now.isoformat()}\nreason: {reason}",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests — expect pass**

```bash
.venv/bin/pytest tests/unit/test_healthcheck.py -v
```

Expected: **6 passed**.

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/pytest tests/ 2>&1 | tail -3
```

Expected: **198 passed** (192 + 6 new).

- [ ] **Step 6: Commit**

```bash
git add scripts/healthcheck.py tests/unit/test_healthcheck.py
git commit -m "feat(healthcheck): stdlib-only oneshot with TG alert on stale digest"
```

---

## Task 2: `scripts/backup.sh`

**Files:**
- Create: `scripts/backup.sh`

Context: Invoked daily via `monitor-backup.timer`. Runs `sqlite3 "$DB" ".backup '$target'"` (which uses SQLite's online backup API — safe with concurrent writers), gzips, prunes files older than `MONITOR_BACKUP_KEEP_DAYS` (default 14).

- [ ] **Step 1: Create script**

Create `scripts/backup.sh`:

```bash
#!/bin/bash
# SQLite online backup with gzip + pruning.
#
# Reads:
#   MONITOR_DB_PATH           default /var/lib/monitor/monitor.db
#   MONITOR_BACKUP_DIR        default /var/lib/monitor/backups
#   MONITOR_BACKUP_KEEP_DAYS  default 14
#
# Exits non-zero on failure so systemd surfaces it.
set -euo pipefail

DB_PATH="${MONITOR_DB_PATH:-/var/lib/monitor/monitor.db}"
BACKUP_DIR="${MONITOR_BACKUP_DIR:-/var/lib/monitor/backups}"
KEEP_DAYS="${MONITOR_BACKUP_KEEP_DAYS:-14}"

if [ ! -f "$DB_PATH" ]; then
  echo "backup.db_missing path=$DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_DIR/monitor-$ts.db"

# sqlite3 .backup uses the online backup API — safe while the daemon
# holds connections open. Requires the sqlite3 CLI (apt: sqlite3).
sqlite3 "$DB_PATH" ".backup '$target'"
gzip -f "$target"

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'monitor-*.db.gz' \
  -mtime +"$KEEP_DAYS" -print -delete

echo "backup.ok file=$target.gz kept_days=$KEEP_DAYS"
```

- [ ] **Step 2: Make executable + smoke test locally**

```bash
chmod +x scripts/backup.sh

# Smoke: run against a throwaway SQLite DB
tmpdb=$(mktemp /tmp/monitor-XXXXXX.db)
tmpdir=$(mktemp -d)
sqlite3 "$tmpdb" "CREATE TABLE t(x); INSERT INTO t VALUES(1);"
MONITOR_DB_PATH="$tmpdb" MONITOR_BACKUP_DIR="$tmpdir" MONITOR_BACKUP_KEEP_DAYS=14 \
  ./scripts/backup.sh
ls -la "$tmpdir"
rm -rf "$tmpdb" "$tmpdir"
```

Expected: prints `backup.ok file=...db.gz`, directory contains one `monitor-<ts>.db.gz`.

- [ ] **Step 3: Commit**

```bash
git add scripts/backup.sh
git commit -m "feat(backup): sqlite3 online backup + gzip + 14-day prune"
```

---

## Task 3: systemd unit files

**Files:**
- Create: `deploy/systemd/monitor.service`
- Create: `deploy/systemd/monitor-health.service`
- Create: `deploy/systemd/monitor-health.timer`
- Create: `deploy/systemd/monitor-backup.service`
- Create: `deploy/systemd/monitor-backup.timer`

Context: Five static unit files. Main service has hardening (`ProtectSystem=strict`, `NoNewPrivileges`, `ReadWritePaths` scoped to data/log dirs). Health + backup are `Type=oneshot` triggered by matching `.timer` units.

- [ ] **Step 1: Create `deploy/systemd/monitor.service`**

```ini
[Unit]
Description=GitHub Repo Monitor daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=monitor
Group=monitor
WorkingDirectory=/opt/monitor
EnvironmentFile=/etc/monitor/monitor.env
Environment=MONITOR_CONFIG=/etc/monitor/config.json
Environment=MONITOR_DB_PATH=/var/lib/monitor/monitor.db
Environment=MONITOR_LOG_PATH=/var/log/monitor/app.log
ExecStart=/opt/monitor/venv/bin/python -m monitor
Restart=on-failure
RestartSec=30s
StartLimitBurst=5
StartLimitIntervalSec=600
TimeoutStopSec=30s

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/monitor /var/log/monitor
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create `deploy/systemd/monitor-health.service`**

```ini
[Unit]
Description=GitHub Repo Monitor healthcheck
After=network-online.target

[Service]
Type=oneshot
User=monitor
Group=monitor
WorkingDirectory=/opt/monitor
EnvironmentFile=/etc/monitor/monitor.env
Environment=MONITOR_DB_PATH=/var/lib/monitor/monitor.db
ExecStart=/opt/monitor/venv/bin/python /opt/monitor/scripts/healthcheck.py
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadOnlyPaths=/var/lib/monitor
```

- [ ] **Step 3: Create `deploy/systemd/monitor-health.timer`**

```ini
[Unit]
Description=Hourly trigger for monitor healthcheck
Requires=monitor-health.service

[Timer]
# Fire 15m after boot, then every hour.
OnBootSec=15min
OnUnitActiveSec=1h
Unit=monitor-health.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Create `deploy/systemd/monitor-backup.service`**

```ini
[Unit]
Description=GitHub Repo Monitor SQLite backup

[Service]
Type=oneshot
User=monitor
Group=monitor
WorkingDirectory=/opt/monitor
Environment=MONITOR_DB_PATH=/var/lib/monitor/monitor.db
Environment=MONITOR_BACKUP_DIR=/var/lib/monitor/backups
Environment=MONITOR_BACKUP_KEEP_DAYS=14
ExecStart=/opt/monitor/scripts/backup.sh
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/monitor
```

- [ ] **Step 5: Create `deploy/systemd/monitor-backup.timer`**

```ini
[Unit]
Description=Daily SQLite backup for monitor
Requires=monitor-backup.service

[Timer]
# Daily at 03:15 UTC (= 11:15 Asia/Shanghai), well outside digest windows.
OnCalendar=*-*-* 03:15:00
Persistent=true
Unit=monitor-backup.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 6: Verify syntax (optional, only if on Linux with systemd-analyze)**

```bash
# macOS dev host has no systemd-analyze; skip or run on target VPS.
which systemd-analyze && for f in deploy/systemd/*.{service,timer}; do
  echo "=== $f ==="; systemd-analyze verify "$f" || true
done
```

On macOS: this step is a no-op. Verification happens on the VPS during install.

- [ ] **Step 7: Commit**

```bash
git add deploy/systemd/
git commit -m "feat(deploy): systemd units for daemon + hourly healthcheck + daily backup"
```

---

## Task 4: `deploy/logrotate/monitor`

**Files:**
- Create: `deploy/logrotate/monitor`

Context: Weekly rotation, keep 8 compressed archives. `copytruncate` avoids having to signal the daemon since structlog is appending and we never hold a write lock across rotations. `su monitor monitor` makes logrotate run the rotation as the monitor user so ownership stays consistent.

- [ ] **Step 1: Create config**

```
/var/log/monitor/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su monitor monitor
}
```

- [ ] **Step 2: Commit**

```bash
git add deploy/logrotate/monitor
git commit -m "feat(deploy): logrotate weekly keep-8 with copytruncate"
```

---

## Task 5: config + env templates

**Files:**
- Create: `deploy/templates/monitor.env.example`
- Create: `deploy/templates/config.json.example`

Context: Operator copies these to `/etc/monitor/` during install and fills secrets. Env template carries the secret quartet; config template carries tuning knobs that map 1:1 onto `ConfigFile`.

- [ ] **Step 1: Create `deploy/templates/monitor.env.example`**

```
# /etc/monitor/monitor.env — systemd EnvironmentFile
# Edit with real secrets, then: chmod 640, chown root:monitor

GITHUB_TOKEN=ghp_replace_me
MINIMAX_API_KEY=replace_me
TELEGRAM_BOT_TOKEN=replace_me
TELEGRAM_CHAT_ID=replace_me

# Paths are also set in the unit file Environment= lines; override here
# only if you relocate the data/log/config directories.
# MONITOR_DB_PATH=/var/lib/monitor/monitor.db
# MONITOR_CONFIG=/etc/monitor/config.json
# MONITOR_LOG_PATH=/var/log/monitor/app.log
```

- [ ] **Step 2: Create `deploy/templates/config.json.example`**

```json
{
  "keywords": ["ai agent", "llm agent", "rag"],
  "languages": ["Python", "TypeScript", "Rust", "Go"],
  "min_stars": 50,
  "max_repo_age_days": 3650,
  "top_n_digest": 10,
  "preference_refresh_every": 10,
  "llm_model": "minimax-01",
  "llm_base_url": "https://api.minimax.io/anthropic",
  "weights": {
    "rule": 0.4,
    "llm": 0.6
  },
  "surge": {
    "velocity_multiple": 3.0,
    "velocity_absolute_day": 20.0,
    "cooldown_days": 3
  }
}
```

- [ ] **Step 3: Commit**

```bash
git add deploy/templates/
git commit -m "feat(deploy): env + config.json templates for /etc/monitor"
```

---

## Task 6: `deploy/install.sh`

**Files:**
- Create: `deploy/install.sh`

Context: Idempotent root-run bootstrap. Creates the `monitor` system user, the four directory roots, rsyncs the repo tree into `/opt/monitor`, builds a venv, installs the package editably, drops templates to `/etc/monitor` only if absent, copies systemd + logrotate configs, runs `daemon-reload`. Designed to be re-runnable for upgrades.

- [ ] **Step 1: Create install script**

```bash
#!/bin/bash
# One-shot bootstrap / upgrade for the monitor daemon on Debian.
# Safe to re-run: config files under /etc are preserved, code under /opt
# is rsynced, venv is only created if missing, systemd units are always
# re-written, then daemon-reload.
#
# Run as root from the repo root:
#   sudo ./deploy/install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh must run as root (try: sudo $0)" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/monitor"
DB_DIR="/var/lib/monitor"
LOG_DIR="/var/log/monitor"
CONF_DIR="/etc/monitor"

echo "==> Ensuring monitor user"
if ! id monitor >/dev/null 2>&1; then
  useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" monitor
fi

echo "==> Ensuring directories"
install -d -m 755 -o monitor -g monitor "$INSTALL_DIR"
install -d -m 750 -o monitor -g monitor "$DB_DIR" "$DB_DIR/backups"
install -d -m 755 -o monitor -g monitor "$LOG_DIR"
install -d -m 750 -o root    -g monitor "$CONF_DIR"

echo "==> Syncing code to $INSTALL_DIR"
# --delete keeps the tree in sync on upgrades. Excludes avoid shipping
# dev artifacts and the developer's local venv.
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'tests' \
  "$REPO_ROOT/" "$INSTALL_DIR/"
chown -R monitor:monitor "$INSTALL_DIR"

echo "==> Preparing venv"
if [ ! -d "$INSTALL_DIR/venv" ]; then
  sudo -u monitor python3 -m venv "$INSTALL_DIR/venv"
fi
sudo -u monitor "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
sudo -u monitor "$INSTALL_DIR/venv/bin/pip" install -e "$INSTALL_DIR"

echo "==> Installing config templates (preserving existing)"
if [ ! -f "$CONF_DIR/monitor.env" ]; then
  install -m 640 -o root -g monitor \
    "$REPO_ROOT/deploy/templates/monitor.env.example" "$CONF_DIR/monitor.env"
  echo "    -> created $CONF_DIR/monitor.env (EDIT THIS with real secrets)"
fi
if [ ! -f "$CONF_DIR/config.json" ]; then
  install -m 644 -o root -g monitor \
    "$REPO_ROOT/deploy/templates/config.json.example" "$CONF_DIR/config.json"
  echo "    -> created $CONF_DIR/config.json"
fi

echo "==> Installing systemd units"
install -m 644 "$REPO_ROOT/deploy/systemd/monitor.service"         /etc/systemd/system/
install -m 644 "$REPO_ROOT/deploy/systemd/monitor-health.service"  /etc/systemd/system/
install -m 644 "$REPO_ROOT/deploy/systemd/monitor-health.timer"    /etc/systemd/system/
install -m 644 "$REPO_ROOT/deploy/systemd/monitor-backup.service"  /etc/systemd/system/
install -m 644 "$REPO_ROOT/deploy/systemd/monitor-backup.timer"    /etc/systemd/system/
systemctl daemon-reload

echo "==> Installing logrotate config"
install -m 644 "$REPO_ROOT/deploy/logrotate/monitor" /etc/logrotate.d/monitor

cat <<EOF

install.sh done.

Next steps:
  1. Edit $CONF_DIR/monitor.env   — fill GITHUB_TOKEN / MINIMAX_API_KEY /
                                     TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
  2. Review $CONF_DIR/config.json — tune keywords / thresholds if needed.
  3. systemctl enable --now monitor.service
  4. systemctl enable --now monitor-health.timer monitor-backup.timer
  5. journalctl -u monitor.service -f   # watch startup
EOF
```

- [ ] **Step 2: Make executable + lint**

```bash
chmod +x deploy/install.sh
bash -n deploy/install.sh   # syntax check
# shellcheck is nice-to-have; skip if not installed.
which shellcheck && shellcheck deploy/install.sh scripts/backup.sh || true
```

Expected: `bash -n` is silent. shellcheck may print warnings — address clear errors, ignore style-only ones.

- [ ] **Step 3: Commit**

```bash
git add deploy/install.sh
git commit -m "feat(deploy): idempotent install/upgrade bootstrap for Debian"
```

---

## Task 7: `deploy/README.md`

**Files:**
- Create: `deploy/README.md`

Context: Operator-facing doc. Tells you how to first-install, upgrade, inspect logs, restore from backup, roll back, and wire the bot.

- [ ] **Step 1: Write doc**

```markdown
# Deploying monitor on Debian

Target: Debian 12 (bookworm) or newer, systemd-based.

## Prereqs

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip sqlite3 logrotate rsync
```

## First install

```bash
git clone <this-repo> ~/monitor-src
cd ~/monitor-src
sudo ./deploy/install.sh
```

Then edit the config:

```bash
sudo -e /etc/monitor/monitor.env     # paste real GITHUB_TOKEN / MINIMAX_API_KEY /
                                     # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
sudo -e /etc/monitor/config.json     # tune keywords / thresholds
```

Start it:

```bash
sudo systemctl enable --now monitor.service
sudo systemctl enable --now monitor-health.timer monitor-backup.timer
```

Watch it:

```bash
sudo journalctl -u monitor.service -f
sudo systemctl list-timers monitor-*
```

## Upgrade

From the repo checkout:

```bash
git pull
sudo ./deploy/install.sh     # rsync + pip install -e + daemon-reload
sudo systemctl restart monitor.service
```

`install.sh` preserves `/etc/monitor/*` and the existing `/var/lib/monitor/monitor.db`. Only the code tree at `/opt/monitor` and the systemd units are refreshed.

## Inspect

| Question                           | Command                                                     |
|------------------------------------|-------------------------------------------------------------|
| Daemon running?                    | `systemctl status monitor.service`                          |
| Recent logs?                       | `journalctl -u monitor.service --since '1 hour ago'`        |
| App log file?                      | `sudo tail -f /var/log/monitor/app.log`                     |
| Healthcheck firing?                | `systemctl list-timers monitor-health.timer`                |
| Last healthcheck result?           | `journalctl -u monitor-health.service -n 50`                |
| Last backup?                       | `ls -la /var/lib/monitor/backups/`                          |
| DB size?                           | `sudo du -sh /var/lib/monitor/monitor.db`                   |

## Restore a backup

```bash
sudo systemctl stop monitor.service
sudo -u monitor gunzip -c /var/lib/monitor/backups/monitor-<ts>.db.gz \
  > /var/lib/monitor/monitor.db
sudo systemctl start monitor.service
```

## Rollback to a previous commit

```bash
cd ~/monitor-src
git checkout <known-good-sha>
sudo ./deploy/install.sh
sudo systemctl restart monitor.service
```

## Uninstall

```bash
sudo systemctl disable --now monitor.service monitor-health.timer monitor-backup.timer
sudo rm /etc/systemd/system/monitor.service
sudo rm /etc/systemd/system/monitor-health.{service,timer}
sudo rm /etc/systemd/system/monitor-backup.{service,timer}
sudo systemctl daemon-reload
sudo rm /etc/logrotate.d/monitor
# Data deletion is deliberate and manual — do these only if you really mean it:
# sudo rm -rf /opt/monitor /var/lib/monitor /var/log/monitor /etc/monitor
# sudo userdel monitor
```

## Security posture

- `monitor` is a non-login system user (`/usr/sbin/nologin`).
- Service unit uses `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges`,
  `MemoryDenyWriteExecute`, and writable-path allowlist. Only
  `/var/lib/monitor` and `/var/log/monitor` are writable at runtime.
- Secrets live in `/etc/monitor/monitor.env` (mode 640, root:monitor). The
  daemon reads them via `EnvironmentFile` — no secrets on the process
  command line or in the repo.
- Health and backup units reuse the same user; health has the DB mounted
  read-only via `ReadOnlyPaths`.
```

- [ ] **Step 2: Commit**

```bash
git add deploy/README.md
git commit -m "docs(deploy): operator runbook — install / upgrade / restore"
```

---

## Task 8: CLAUDE.md M6 additions

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append subsection**

Locate the last line of `### M5 additions` (ends with "Morning digest at 08:00 Shanghai → 00:00 UTC; evening 20:00 Shanghai → 12:00 UTC."). Append one blank line, then:

```markdown

### M6 additions

Deployment lives under `deploy/` + `scripts/`. Five systemd units: `monitor.service` (main daemon, hardened with ProtectSystem=strict + ReadWritePaths scoped to data/log dirs), `monitor-health.{service,timer}` (hourly probe), `monitor-backup.{service,timer}` (daily 03:15 UTC).

`scripts/healthcheck.py` is stdlib-only by design — it must run even when the project venv is broken. It queries `run_log` for a successful digest within 25h; on failure it POSTs a Telegram alert via the Bot API. Exits 0 unconditionally so the timer does not flap.

`scripts/backup.sh` uses `sqlite3 .backup` (online backup API, safe with concurrent writers) + gzip + prune-by-mtime. Keeps 14 days by default via `MONITOR_BACKUP_KEEP_DAYS`.

`deploy/install.sh` is the idempotent bootstrap. Creates the `monitor` system user, installs code to `/opt/monitor` (rsync), builds a venv, seeds `/etc/monitor/{monitor.env,config.json}` from templates (preserving existing), installs unit + logrotate files, `daemon-reload`. Safe to re-run for upgrades.

`deploy/logrotate/monitor`: weekly, keep 8, `copytruncate` (structlog appends, no signal handshake needed).

`deploy/README.md` is the operator runbook — install / upgrade / inspect / restore-from-backup / rollback / uninstall.

Paths: code `/opt/monitor`, data+backups `/var/lib/monitor`, logs `/var/log/monitor`, config `/etc/monitor`. Non-root `monitor` user.

Not covered by M6: LLM-consecutive-failure alerting (the design mentioned it but implementing it cleanly needs a dedicated counter table — digest failures with LLM fallback currently land as `status=ok`, so run_log alone doesn't see the signal). Left as future work; the 25h-stale-digest alert catches daemon-level outages which is the bigger risk.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: extend CLAUDE.md for M6 deployment layout"
```

---

## M6 Verification Criteria

- [ ] `.venv/bin/pytest tests/` — **198 passed** (192 + 6 new healthcheck tests)
- [ ] `scripts/healthcheck.py` exists, uses stdlib only, check_last_digest is pure
- [ ] `scripts/backup.sh` exists, uses `sqlite3 .backup`, prunes by mtime
- [ ] All 5 systemd unit files present under `deploy/systemd/`
- [ ] `deploy/logrotate/monitor` exists
- [ ] `deploy/install.sh` present, executable, `bash -n` clean
- [ ] `deploy/README.md` covers install / upgrade / restore / rollback
- [ ] `deploy/templates/monitor.env.example` + `config.json.example` present
- [ ] CLAUDE.md has `### M6 additions`

## Out of scope (future)

- LLM consecutive-failure alerting (needs a new counter / schema change)
- Off-host backup replication (rsync to S3 or another server)
- Metrics export to Prometheus / Grafana dashboard
- Blue/green deploy — current install.sh is a stop-the-world upgrade
- Live smoke test against real credentials (operator does this once after first install)
