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


def test_should_send_alert_throttles_same_reason(healthcheck_module, tmp_path):
    state_path = tmp_path / "state.json"
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)

    assert healthcheck_module.should_send_alert(
        "no_successful_digest_in_25h", now, state_path, cooldown_hours=24
    ) is True
    assert state_path.exists() is True
    assert healthcheck_module.should_send_alert(
        "no_successful_digest_in_25h",
        now + dt.timedelta(hours=1),
        state_path,
        cooldown_hours=24,
    ) is False
    assert healthcheck_module.should_send_alert(
        "db_connect_failed", now + dt.timedelta(hours=1), state_path, cooldown_hours=24
    ) is True


def test_clear_alert_state_resets_throttle(healthcheck_module, tmp_path):
    state_path = tmp_path / "state.json"
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)

    assert healthcheck_module.should_send_alert(
        "no_successful_digest_in_25h", now, state_path, cooldown_hours=24
    ) is True
    healthcheck_module.clear_alert_state(state_path)
    assert healthcheck_module.should_send_alert(
        "no_successful_digest_in_25h",
        now + dt.timedelta(hours=1),
        state_path,
        cooldown_hours=24,
    ) is True
