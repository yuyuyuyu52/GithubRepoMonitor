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
