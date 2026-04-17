# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Run the monitor pipeline end-to-end:

```bash
python src/github_repo_monitor.py
python src/github_repo_monitor.py --config /absolute/path/config.json
```

Tests (stdlib `unittest` only — no pytest, no external deps):

```bash
# Full suite (must run from repo root so `src.github_repo_monitor` imports resolve)
python -m unittest discover -s tests -v

# Single test
python -m unittest tests.test_monitor.RuleEngineTests.test_apply_respects_language_star_and_age
```

There is no `requirements.txt`, `pyproject.toml`, or lint config — the code is pure Python 3 stdlib (urllib, sqlite3, json, dataclasses). Do not introduce third-party dependencies without discussing first.

## Architecture

Single-file pipeline in `src/github_repo_monitor.py`. `MonitorPipeline.run()` orchestrates a fixed sequence; each stage is a separate class so tests can patch at boundaries:

1. **`_collect_candidates`** — `GitHubClient.search_repositories` (keyword × language cross-product) + `fetch_trending_repositories` (scrapes `github.com/trending` HTML with a regex). Deduped into a `dict` keyed by `full_name`.
2. **Dedupe** — `SQLiteStore.is_seen` against the `seen_repositories` table.
3. **Coarse filter** — `RuleEngine.apply` drops repos failing `min_stars`, `languages`, or `max_repo_age_days`.
4. **Enrichment** — `_enrich` calls Events, Issues, Contributors, and README endpoints, filling fields on the `RepoCandidate` dataclass in place.
5. **Scoring** — `RuleEngine.score` (weighted combo of star velocity, fork ratio, freshness, contributor growth, issue response) + `ReadmeAnalyzer.analyze` (OpenAI if `OPENAI_API_KEY` is set, else heuristic on README sections + interest-tag hits). `final_score = rule_score*0.55 + llm_score*0.45`.
6. **Push** — `TelegramNotifier.notify` (falls back to `print` if Telegram creds missing), then `SQLiteStore.mark_seen` persists metrics to `repository_metrics` and records the repo as seen.

### Config resolution order

`MonitorConfig.from_env()` reads env vars first; `load_config(--config)` then overlays JSON values onto matching dataclass fields via `setattr`. Env vars are the baseline; JSON wins when both are present.

### Conventions worth preserving

- **No external HTTP client** — all network calls use `urllib.request` with `User-Agent: GithubRepoMonitor` and optional `Authorization: Bearer <token>`. The `# nosec B310` comments silence bandit on `urlopen` and should stay.
- **Timezone-aware datetimes everywhere.** `parse_dt` normalizes `Z`-suffixed and offset ISO strings to UTC; new date handling must go through it or produce `tzinfo=UTC` values, otherwise `RuleEngine.apply` comparisons against `datetime.now(tz.utc)` will raise.
- **User-facing strings are Chinese** (Telegram messages, recommendation reasons, fallback text). Match that style when adding new output.
- **SQLite schema changes** need `_init_schema` updates and a migration story — the DB file (`monitor.db`) is created lazily and not versioned.

### Testing pattern

`tests/test_monitor.py` uses `unittest.mock.patch.object` on pipeline instances to stub `_collect_candidates`, `_enrich`, `readme_analyzer.analyze`, and `notifier.notify` — so network is never hit. When adding pipeline stages, keep them patchable from the outside (methods on the pipeline or its collaborators, not free functions inside `run`).
