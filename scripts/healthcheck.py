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
