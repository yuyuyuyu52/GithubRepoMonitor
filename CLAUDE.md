# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Install deps (src layout, editable):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the daemon (M1 scaffolding: config load + DB migrate + wait for SIGTERM):

```bash
python -m monitor
```

Legacy demo (still runnable until M4 replaces it):

```bash
python -m monitor.legacy
```

Tests (pytest, async enabled by pyproject config):

```bash
pytest                                    # all
pytest tests/unit -v                      # unit only
pytest tests/unit/test_db.py::test_fresh_db_runs_all_migrations -v  # single test
```

## Architecture

**Migration status (as of 2026-04-17):** The productized daemon lives in `src/monitor/`. The pre-productization single-file pipeline is preserved at `src/monitor/legacy.py` and will be replaced module-by-module over M2–M6. Both coexist during the transition; tests for `monitor.legacy` continue to pass.

The productized pipeline orchestrated by `src/monitor/main.py` runs as a single async daemon that (in final form) holds three concurrent tasks: TG bot long-polling, APScheduler with four jobs (morning/evening digest, 30-min surge poll, weekly digest), and the pipeline executor guarded by an `asyncio.Lock` for non-reentrance. M1 only wires up config loading, DB migrations, structured logging, and SIGTERM handling — pipeline/bot/scheduler modules are stubs.

Design spec: `docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md`.
M1 plan: `docs/superpowers/plans/2026-04-17-m1-scaffolding.md`.

### Config resolution order

`Settings` (pydantic-settings) reads secrets and paths from env vars (`GITHUB_TOKEN`, `MINIMAX_API_KEY`, `TELEGRAM_*`, `MONITOR_DB_PATH`, `MONITOR_CONFIG`, `MONITOR_LOG_PATH`). `ConfigFile` (regular pydantic BaseModel, `extra="forbid"` so operator typos fail loud) is loaded from the JSON file pointed to by `MONITOR_CONFIG` and holds tuning knobs (keywords, thresholds, weights, model name). Code defaults apply where neither env nor file provides a value.

### Schema migrations

`src/monitor/db.py` tracks `SCHEMA_VERSION` (code constant) and the `schema_version` table. On startup `run_migrations` applies anything missing — idempotent, safe to re-run. Migration 001 creates the new table set and, if an old `seen_repositories` table exists (from the demo), copies rows into `pushed_items` (skipping any already present so partial-crash recovery doesn't duplicate) while adding `stars`/`forks` columns to `repository_metrics`.

### Logging

`src/monitor/logging_config.py` wires structlog → JSON → stdout + optional file. `SECRET_FIELDS` masks known-sensitive keys to `"***"` before render. `filter_by_level` short-circuits below-threshold messages; `format_exc_info` routes tracebacks through the JSON renderer.

### Legacy conventions still worth preserving

- Timezone-aware datetimes everywhere; `parse_dt` normalizes Z/offset ISO strings to UTC.
- Chinese user-facing strings (carried into M4's Telegram renders).
- No-external-HTTP-client was the demo rule; M2 replaces urllib with httpx across the new codebase.
