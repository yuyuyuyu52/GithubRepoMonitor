# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Install deps (src layout, editable):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the daemon:

```bash
python -m monitor
```

Tests (pytest, async enabled by pyproject config):

```bash
pytest                                    # all
pytest tests/unit -v                      # unit only
pytest tests/unit/test_db.py::test_fresh_db_runs_all_migrations -v  # single test
```

## Architecture

The productized pipeline orchestrated by `src/monitor/main.py` runs as a single async daemon holding three concurrent concerns: TG bot long-polling, APScheduler with four jobs (morning/evening digest, 30-min surge poll, weekly digest), and the pipeline executor guarded by `DaemonState.digest_lock` for non-reentrance.

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

### M2 additions

`src/monitor/clients/github.py` is the async httpx client. All requests go through `_retrying_request`, which handles primary rate limits (via `RateLimiter.acquire()` before each call and header-driven state updates after), 429/secondary-limit retries that honor `Retry-After` (plus 403 bodies containing "rate limit" / "abuse detection" / "secondary rate limit"), 5xx retries with exponential backoff (1/2/4/8 s capped at 30), and network-error retries under the same budget (max 4 attempts). `/search/repositories` additionally goes through `SearchRateLimiter` (2 s minimum spacing). 4xx other than rate-limit raise `GitHubError(status_code, message)` immediately, including from the per-endpoint fetch methods — `fetch_repo_events` / `fetch_contributors_growth` / `fetch_issue_response_hours` let `GitHubError` bubble so `enrich_repo`'s per-field try/except can record an `EnrichError`. Only `fetch_readme` catches 404 specifically (returns `""` — empty README is a normal state, not a failure) and `fetch_repository_detail` catches 404 (returns `None`).

`src/monitor/clients/rate_limit.py` exposes `RateLimiter` (primary) + `SearchRateLimiter` (secondary). Both hold their `asyncio.Lock` across the sleep so one coroutine blocks the others for the rate-limit window instead of all thundering at reset. Primary sleep is capped at `_MAX_SLEEP_S` (~1 reset cycle) to defend against malformed reset headers.

`src/monitor/pipeline/collect.py` exposes `collect_candidates(client, keywords, languages, min_stars)` — keyword × language search cross-product plus trending scrape, deduped by `full_name`. Individual search-pair failures are logged and swallowed.

`src/monitor/pipeline/enrich.py` exposes `enrich_repo(client, repo) -> list[EnrichError]`. Each of the four enrichment endpoints (events, contributors, issues, readme) is tried in isolation; a failure there records an `EnrichError(step, message, repo)` but leaves other fields and the repo usable.

The shared domain model is `monitor.models.RepoCandidate` (`@dataclass(slots=True)`) plus `EnrichError`. Fields are populated at distinct stages: collect fills metadata; enrich fills metrics + readme; M3 will fill scoring fields; M4 will fill push metadata.

Tests use fixtures from `tests/fixtures/github_payloads.py` (canonical dict literals) with `respx` mocking httpx. `tests/integration/test_pipeline_m2.py` exercises collect + enrich end-to-end against a respx-mocked GitHub. No live GitHub calls in the suite yet — a live smoke test is deferred to M5 when the scheduler wires everything up.

### M3 additions

`src/monitor/scoring/` holds four focused modules: `rules.py` (`RuleEngine` — coarse filter + deterministic rule score, ported from legacy with an injectable clock), `heuristic.py` (`heuristic_score_readme` — returns a `ScoreResult` so orchestrator treats fallback output identically to LLM output; summary truncated to 140 chars to respect `ScoreResult.summary` max_length), `preference.py` (`PreferenceBuilder.regenerate()` — reads last 20 👍 and 20 👎 from `user_feedback`, prompts the LLM for a 250-300 字 preference description, persists to the single-row `preference_profile` table), and `score.py` (`score_repo()` orchestrator — rule score + cached-or-new LLM score + heuristic fallback + weighted `final_score`).

`src/monitor/clients/llm.py` is the MiniMax-via-Anthropic-SDK client. `LLMClient.score_repo()` sends a forced `submit_repo_score` tool use with strict schema (score 1-10, readme_completeness 0-1, summary ≤140 chars enforced both in JSON schema and in `ScoreResult.summary`, reason ≤240 chars, matched_interests, red_flags). The system prompt has two ephemeral-cached blocks: the scoring rubric (constant) and the current preference profile (refreshes every N feedback). Any SDK error, missing tool_use block, or pydantic validation failure is re-raised as `LLMScoreError` — the orchestrator in `scoring/score.py` catches it and falls back to the heuristic scorer.

`llm_score_cache` (DB table from M1) is keyed by `(full_name, readme_sha256)`: orchestrator skips LLM entirely on cache hit, and writes the cache on both LLM and heuristic success. An unchanged README costs zero LLM calls. If MiniMax is temporarily down, a digest run falls back to heuristic for each repo and caches that — subsequent runs don't retry LLM until the README changes and the sha256 differs (flush the table manually if a permanent LLM outage recovers).

Data model contract: `RepoCandidate` receives `rule_score`, `llm_score`, `final_score`, `summary`, `recommendation_reason`, `readme_completeness` from this layer. These were declared at default zero/empty in M2's model; M3 is the first stage that populates them.

Tests use DI-mocked Anthropic SDK (no respx for LLM, we stub the `anthropic_client` constructor parameter with a `SimpleNamespace` whose `.messages.create` is an `AsyncMock`). The MiniMax endpoint's actual forced-tool-use / ephemeral-cache behavior is NOT exercised in CI — verifying it requires a one-off smoke script with real credentials, tracked as an operational TODO before M3 is wired into the scheduler in M5.

### M4 additions

`src/monitor/bot/` has four focused modules. `render.py` turns a scored `RepoCandidate` into a message text + 4-button `InlineKeyboardMarkup` with callback data shaped `fb:{action}:{push_id}`. `feedback.py` parses those callbacks, writes `user_feedback` + `blacklist` rows, edits the source message to show acknowledgement, and triggers `PreferenceBuilder.regenerate()` once `count_feedback_since_last_profile` crosses `config.preference_refresh_every`. `commands.py` exposes `/top` `/status` `/pause` `/resume` `/reload` as pure async handlers. `app.py` wires them into a PTB `Application` with a chat-id filter — updates from any chat other than the configured `TELEGRAM_CHAT_ID` are silently ignored.

`src/monitor/state.py` introduces `DaemonState`: a singleton that holds the live `ConfigFile` reference and a `paused` bool persisted to the new `daemon_state` table (migration 002). M4 flips the flag via `/pause` / `/resume`; M5's scheduler reads it before every tick. `/reload` re-reads the JSON config file and swaps `state.config` atomically.

`LLMClient.generate_text(prompt)` was added alongside `score_repo` so `PreferenceBuilder` can call it without touching the SDK internals. It uses a plain messages call (no tool use) and raises `LLMScoreError` on SDK failure or missing text block.

`src/monitor/main.py` now boots the bot alongside the existing lifecycle. Without both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, the bot is skipped (logs `telegram.disabled`) — M1's existing SIGTERM integration test still passes because that scenario is the no-bot path. Without `MINIMAX_API_KEY` the bot still runs but preference regeneration is a no-op.

DB additions: migration 002 creates `daemon_state`. Seven new DAOs in `db.py` — `get_daemon_state`, `set_daemon_paused`, `insert_pushed_item`, `update_pushed_tg_message_id`, `record_user_feedback`, `count_feedback_since_last_profile`, `get_recent_pushes`, `get_latest_run_logs`. Tests continue the DI-mocked pattern — no real Telegram API calls in the suite.

M4 does NOT yet fire push messages on a schedule. That is M5's scheduler task: it will call `score_repo`, then `insert_pushed_item`, then `render_repo_message`, then `bot.send_message(chat_id=..., text=text, reply_markup=markup)`, then `update_pushed_tg_message_id(id, msg_id)`. M4 provides all the building blocks.

### M5 additions

`src/monitor/scheduler.py` hosts the `AsyncIOScheduler` with four jobs (`digest_morning` @ 08:00, `digest_evening` @ 20:00, `surge_poll` every 30 min, `weekly_digest` Sunday 21:00). Each job is guarded by `DaemonState.digest_lock` so two jobs (or `/digest_now`) cannot overlap; an overlapping trigger logs "skipped" and exits.

`src/monitor/pipeline/` grows three orchestrators: `digest.py` (`run_digest` collect→filter→enrich→score→push with `run_log` + top_n + pause guard), `surge.py` (`run_surge` re-surfaces cooldown-expired repos whose events velocity crossed the `surge.velocity_multiple × previous` AND `surge.velocity_absolute_day` thresholds), and `weekly.py` (pure SQL aggregate of pushed_items + user_feedback + run_log + preference_profile into a text block for the Sunday push).

`src/monitor/pipeline/filter.py` is the coarse filter stage used by `run_digest`: rule engine + blacklist (repo/author/topic) + pushed_cooldown_state, all in one pass before enrichment.

`src/monitor/bot/push.py` centralizes the push send flow (`insert_pushed_item` → `render_repo_message` → `bot.send_message` → `update_pushed_tg_message_id`). Called by both `run_digest` and `run_surge`; surge adds a 🔥 prefix.

`src/monitor/bot/commands.py` grows `/digest_now` — it attempts to acquire `state.digest_lock` and replies busy if held (no queueing).

DB: migration 002 was from M4. M5 adds no schema changes, only DAOs: `start_run_log` / `finish_run_log` for run accounting; `upsert_repositories` + `upsert_repository_metrics` for post-enrich persistence; `get_latest_metric` + `get_surge_candidates` for surge; `get_pushed_since` + `get_feedback_counts_since` for the weekly aggregate.

`src/monitor/main.py` now opens a `GitHubClient` for the daemon lifetime, builds `LLMClient` (if keyed), constructs all four pre-bound scheduler callables, and installs the scheduler alongside the bot. Shutdown order is scheduler → bot → conn, each step individually guarded.

`monitor.legacy` is gone. The productized pipeline is the single entry. Legacy tests at `tests/test_monitor.py` were deleted in the same commit.

Operational note: the daemon uses `timezone="Asia/Shanghai"` for the scheduler. Morning digest at 08:00 Shanghai → 00:00 UTC; evening 20:00 Shanghai → 12:00 UTC.

### M6 additions

Deployment lives under `deploy/` + `scripts/`. Five systemd units: `monitor.service` (main daemon, hardened with `ProtectSystem=strict` + `ReadWritePaths` scoped to data/log dirs + `MemoryDenyWriteExecute`), `monitor-health.{service,timer}` (hourly probe), `monitor-backup.{service,timer}` (daily 03:15 UTC).

`scripts/healthcheck.py` is stdlib-only by design — it must run even when the project venv is broken. It queries `run_log` for an `ok|partial` digest within 25h; on failure it POSTs a Telegram alert via the Bot API using `urllib.request`. Exits 0 unconditionally so the timer does not flap. The testable core is `check_last_digest(db_path, now) -> tuple[bool, str]`; 6 unit tests in `tests/unit/test_healthcheck.py` load the script via `importlib.util` against an in-memory run_log.

`scripts/backup.sh` uses `sqlite3 .backup` (online backup API, safe with concurrent writers) + gzip + prune-by-mtime. Keeps 14 days by default via `MONITOR_BACKUP_KEEP_DAYS`.

`deploy/install.sh` is the idempotent bootstrap. Creates the `monitor` system user (`/usr/sbin/nologin`), installs code to `/opt/monitor` (rsync with `--delete`, excluding `.venv`/`tests`/`__pycache__`), builds a venv, `pip install -e`, seeds `/etc/monitor/{monitor.env,config.json}` from `deploy/templates/` only when absent, installs unit + logrotate files, `daemon-reload`. Safe to re-run on every upgrade.

`deploy/logrotate/monitor`: weekly, keep 8, `copytruncate` (structlog appends, no signal handshake needed).

`deploy/README.md` is the operator runbook — install / upgrade / inspect / restore-from-backup / rollback / uninstall.

Paths: code `/opt/monitor`, data+backups `/var/lib/monitor`, logs `/var/log/monitor`, config `/etc/monitor`. Non-root `monitor` user.

Not covered by M6: LLM-consecutive-failure alerting (the design mentioned it but implementing it cleanly needs a dedicated counter table — digest failures that fall back to heuristic currently land as `status=ok`, so run_log alone doesn't see the signal). Left as future work; the 25h-stale-digest alert catches daemon-level outages, which is the bigger risk.
