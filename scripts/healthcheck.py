#!/usr/bin/env python3
"""Healthcheck for the monitor daemon.

Invoked by monitor-health.timer every hour. Connects to the DB, checks the
last 25h of run_log entries for at least one successful digest run, and
sends a Telegram alert if the check fails. Always exits 0 so systemd
doesn't retry or flap.

Alerts are throttled with a tiny state file (default lives next to the DB)
so the same failure is only reported once per cooldown window (24h by
default; override with MONITOR_HEALTHCHECK_ALERT_COOLDOWN_HOURS or
MONITOR_HEALTHCHECK_STATE_PATH).

Uses only the Python standard library so this still runs when the project
venv is broken — that's exactly the state where we most need the alert.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request


ALERT_WINDOW_HOURS = 25
ALERT_COOLDOWN_HOURS = 24
TELEGRAM_API_BASE = "https://api.telegram.org"
HTTP_TIMEOUT_S = 10.0
STATE_FILENAME = "healthcheck_state.json"


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


def _parse_cooldown_hours() -> int:
    raw = os.environ.get("MONITOR_HEALTHCHECK_ALERT_COOLDOWN_HOURS")
    if raw is None:
        return ALERT_COOLDOWN_HOURS
    try:
        return max(0, int(raw))
    except ValueError:
        return ALERT_COOLDOWN_HOURS


def _state_path(db_path: str) -> str:
    override = os.environ.get("MONITOR_HEALTHCHECK_STATE_PATH")
    if override:
        return override
    base_dir = os.path.dirname(db_path) or "."
    return os.path.join(base_dir, STATE_FILENAME)


def _read_last_alert(state_path: str) -> tuple[str | None, dt.datetime | None]:
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return (None, None)
    reason = data.get("reason")
    ts = data.get("ts")
    if not reason or not ts:
        return (None, None)
    try:
        return (reason, dt.datetime.fromisoformat(ts))
    except ValueError:
        return (None, None)


def _write_last_alert(state_path: str, reason: str, now: dt.datetime) -> None:
    try:
        dirpath = os.path.dirname(state_path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        payload = {"reason": reason, "ts": now.isoformat()}
        tmp_path = f"{state_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp_path, state_path)
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck.persist_state_failed: {exc}", file=sys.stderr)


def clear_alert_state(state_path: str) -> None:
    try:
        os.remove(state_path)
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck.clear_state_failed: {exc}", file=sys.stderr)


def should_send_alert(
    reason: str, now: dt.datetime, state_path: str, cooldown_hours: int
) -> bool:
    if cooldown_hours <= 0:
        return True
    last_reason, last_ts = _read_last_alert(state_path)
    if (
        last_reason == reason
        and last_ts is not None
        and now - last_ts < dt.timedelta(hours=cooldown_hours)
    ):
        return False
    _write_last_alert(state_path, reason, now)
    return True


def main() -> int:
    db_path = os.environ.get("MONITOR_DB_PATH", "/var/lib/monitor/monitor.db")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    state_path = _state_path(db_path)
    cooldown_hours = _parse_cooldown_hours()

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
    if ok:
        clear_alert_state(state_path)
        return 0

    if token and chat_id and should_send_alert(reason, now, state_path, cooldown_hours):
        send_telegram_alert(
            token, chat_id,
            f"⚠️ Monitor healthcheck failed at {now.isoformat()}\nreason: {reason}",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
