from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence


GITHUB_API_BASE = "https://api.github.com"
GITHUB_TRENDING_URL = "https://github.com/trending"


@dataclass
class MonitorConfig:
    keywords: List[str] = field(default_factory=lambda: ["agent", "llm", "monitor", "tooling"])
    languages: List[str] = field(default_factory=lambda: ["Python", "Rust", "Go"])
    min_stars: int = 100
    max_repo_age_days: int = 180
    top_n: int = 10
    db_path: str = "monitor.db"
    github_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"

    @classmethod
    def from_env(cls) -> "MonitorConfig":
        def parse_csv(name: str, default: Sequence[str]) -> List[str]:
            value = os.getenv(name)
            if not value:
                return list(default)
            return [item.strip() for item in value.split(",") if item.strip()]

        return cls(
            keywords=parse_csv("MONITOR_KEYWORDS", ["agent", "llm", "monitor", "tooling"]),
            languages=parse_csv("MONITOR_LANGUAGES", ["Python", "Rust", "Go"]),
            min_stars=int(os.getenv("MONITOR_MIN_STARS", "100")),
            max_repo_age_days=int(os.getenv("MONITOR_MAX_REPO_AGE_DAYS", "180")),
            top_n=int(os.getenv("MONITOR_TOP_N", "10")),
            db_path=os.getenv("MONITOR_DB_PATH", "monitor.db"),
            github_token=os.getenv("GITHUB_TOKEN"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        )


@dataclass
class RepoCandidate:
    full_name: str
    html_url: str
    description: str
    language: str
    stars: int
    forks: int
    created_at: dt.datetime
    pushed_at: dt.datetime
    readme_text: str = ""
    star_velocity_day: float = 0.0
    star_velocity_week: float = 0.0
    fork_star_ratio: float = 0.0
    avg_issue_response_hours: float = 0.0
    contributor_count: int = 0
    contributor_growth_week: int = 0
    readme_completeness: float = 0.0
    rule_score: float = 0.0
    llm_score: float = 0.0
    final_score: float = 0.0
    summary: str = ""
    recommendation_reason: str = ""


class GitHubClient:
    def __init__(self, token: str | None = None):
        self.token = token

    def _request_json(self, url: str) -> dict | list:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "GithubRepoMonitor")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=20) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))

    def _request_text(self, url: str) -> str:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "GithubRepoMonitor")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=20) as response:  # nosec B310
            return response.read().decode("utf-8", errors="ignore")

    def search_repositories(self, keyword: str, language: str, min_stars: int) -> List[RepoCandidate]:
        query = urllib.parse.quote_plus(f"{keyword} language:{language} stars:>={min_stars} archived:false")
        url = f"{GITHUB_API_BASE}/search/repositories?q={query}&sort=stars&order=desc&per_page=30"
        payload = self._request_json(url)
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return [self._repo_from_api(item) for item in items]

    def fetch_repo_events(self, full_name: str) -> tuple[float, float]:
        url = f"{GITHUB_API_BASE}/repos/{full_name}/events?per_page=100"
        try:
            events = self._request_json(url)
        except urllib.error.HTTPError:
            return (0.0, 0.0)
        if not isinstance(events, list):
            return (0.0, 0.0)

        now = dt.datetime.now(dt.timezone.utc)
        day_ago = now - dt.timedelta(days=1)
        week_ago = now - dt.timedelta(days=7)
        day_count = 0
        week_count = 0
        for event in events:
            if event.get("type") != "WatchEvent":
                continue
            created_at_raw = event.get("created_at")
            if not created_at_raw:
                continue
            created_at = parse_dt(created_at_raw)
            if created_at >= week_ago:
                week_count += 1
                if created_at >= day_ago:
                    day_count += 1
        return (float(day_count), float(week_count / 7.0))

    def fetch_contributors_growth(self, full_name: str) -> tuple[int, int]:
        url = f"{GITHUB_API_BASE}/repos/{full_name}/contributors?per_page=100"
        try:
            contributors = self._request_json(url)
        except urllib.error.HTTPError:
            return (0, 0)
        if not isinstance(contributors, list):
            return (0, 0)

        total = len(contributors)
        growth = 0
        for contributor in contributors:
            contributions = int(contributor.get("contributions", 0))
            if contributions <= 1:
                growth += 1
        return (total, growth)

    def fetch_readme(self, full_name: str) -> str:
        url = f"{GITHUB_API_BASE}/repos/{full_name}/readme"
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github.raw+json")
        req.add_header("User-Agent", "GithubRepoMonitor")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as response:  # nosec B310
                return response.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError:
            return ""

    def fetch_trending_repositories(self) -> List[RepoCandidate]:
        try:
            html = self._request_text(GITHUB_TRENDING_URL)
        except urllib.error.URLError:
            return []

        pairs = re.findall(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', html)
        seen = set()
        repos: List[RepoCandidate] = []
        for pair in pairs:
            if pair in seen:
                continue
            seen.add(pair)
            detail = self.fetch_repository_detail(pair)
            if detail:
                repos.append(detail)
            if len(repos) >= 20:
                break
        return repos

    def fetch_repository_detail(self, full_name: str) -> RepoCandidate | None:
        try:
            payload = self._request_json(f"{GITHUB_API_BASE}/repos/{full_name}")
        except urllib.error.HTTPError:
            return None
        if not isinstance(payload, dict):
            return None
        return self._repo_from_api(payload)

    def fetch_issue_response_hours(self, full_name: str) -> float:
        url = f"{GITHUB_API_BASE}/repos/{full_name}/issues?state=closed&sort=updated&direction=desc&per_page=30"
        try:
            issues = self._request_json(url)
        except urllib.error.HTTPError:
            return 0.0
        if not isinstance(issues, list):
            return 0.0

        intervals: List[float] = []
        for issue in issues:
            if issue.get("pull_request"):
                continue
            created_at = issue.get("created_at")
            closed_at = issue.get("closed_at")
            if not created_at or not closed_at:
                continue
            delta = parse_dt(closed_at) - parse_dt(created_at)
            intervals.append(delta.total_seconds() / 3600.0)
            if len(intervals) >= 10:
                break
        if not intervals:
            return 0.0
        return sum(intervals) / len(intervals)

    @staticmethod
    def _repo_from_api(item: dict) -> RepoCandidate:
        return RepoCandidate(
            full_name=item.get("full_name", ""),
            html_url=item.get("html_url", ""),
            description=item.get("description") or "",
            language=item.get("language") or "Unknown",
            stars=int(item.get("stargazers_count", 0)),
            forks=int(item.get("forks_count", 0)),
            created_at=parse_dt(item.get("created_at", "1970-01-01T00:00:00Z")),
            pushed_at=parse_dt(item.get("pushed_at", "1970-01-01T00:00:00Z")),
        )


class SQLiteStore:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_repositories (
                full_name TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_score REAL NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS repository_metrics (
                full_name TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                star_velocity_day REAL,
                star_velocity_week REAL,
                fork_star_ratio REAL,
                avg_issue_response_hours REAL,
                contributor_count INTEGER,
                contributor_growth_week INTEGER,
                readme_completeness REAL,
                PRIMARY KEY (full_name, collected_at)
            )
            """
        )
        self.conn.commit()

    def is_seen(self, full_name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_repositories WHERE full_name = ? LIMIT 1", (full_name,)
        ).fetchone()
        return row is not None

    def mark_seen(self, repo: RepoCandidate) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO seen_repositories (full_name, first_seen_at, last_score)
            VALUES (?, ?, ?)
            ON CONFLICT(full_name) DO UPDATE SET last_score = excluded.last_score
            """,
            (repo.full_name, now, repo.final_score),
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO repository_metrics (
                full_name,
                collected_at,
                star_velocity_day,
                star_velocity_week,
                fork_star_ratio,
                avg_issue_response_hours,
                contributor_count,
                contributor_growth_week,
                readme_completeness
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo.full_name,
                now,
                repo.star_velocity_day,
                repo.star_velocity_week,
                repo.fork_star_ratio,
                repo.avg_issue_response_hours,
                repo.contributor_count,
                repo.contributor_growth_week,
                repo.readme_completeness,
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class RuleEngine:
    def __init__(self, config: MonitorConfig):
        self.config = config

    def apply(self, repo: RepoCandidate) -> bool:
        max_age = dt.timedelta(days=self.config.max_repo_age_days)
        now = dt.datetime.now(dt.timezone.utc)
        if repo.stars < self.config.min_stars:
            return False
        if repo.language not in self.config.languages:
            return False
        if (now - repo.created_at) > max_age:
            return False
        return True

    def score(self, repo: RepoCandidate) -> float:
        ratio = repo.fork_star_ratio if repo.fork_star_ratio else 0.0
        freshness_days = max((dt.datetime.now(dt.timezone.utc) - repo.pushed_at).days, 0)
        freshness_score = max(0.0, 10.0 - freshness_days / 10.0)
        response_score = 10.0 if repo.avg_issue_response_hours == 0 else max(0.0, 10.0 - repo.avg_issue_response_hours / 24.0)
        score = (
            min(repo.star_velocity_day, 10.0) * 0.25
            + min(repo.star_velocity_week * 2, 10.0) * 0.2
            + min(ratio * 20, 10.0) * 0.1
            + freshness_score * 0.2
            + min(repo.contributor_growth_week, 10) * 0.1
            + response_score * 0.15
        )
        return round(score, 2)


class ReadmeAnalyzer:
    def __init__(self, api_key: str | None, model: str):
        self.api_key = api_key
        self.model = model

    def analyze(self, repo: RepoCandidate, interest_tags: Sequence[str]) -> tuple[float, float, str, str]:
        if self.api_key:
            result = self._analyze_with_openai(repo, interest_tags)
            if result:
                return result
        return self._heuristic_analysis(repo, interest_tags)

    def _heuristic_analysis(self, repo: RepoCandidate, interest_tags: Sequence[str]) -> tuple[float, float, str, str]:
        readme = repo.readme_text or ""
        lower = readme.lower()
        has_install = "install" in lower or "安装" in lower
        has_usage = "usage" in lower or "quick start" in lower or "使用" in lower
        has_arch = "architecture" in lower or "架构" in lower
        has_license = "license" in lower or "许可证" in lower
        completeness = sum([has_install, has_usage, has_arch, has_license]) / 4.0

        tag_hits = sum(1 for tag in interest_tags if tag.lower() in (repo.description + " " + readme).lower())
        llm_score = min(10.0, 4.0 + completeness * 4.0 + tag_hits)

        summary = repo.description.strip() or "README 中未提供明确描述"
        reason = (
            f"匹配兴趣标签 {tag_hits} 项，README 完整度 {completeness:.0%}，"
            f"近 24 小时 star 增速 {repo.star_velocity_day:.1f}。"
        )
        return round(llm_score, 2), completeness, summary, reason

    def _analyze_with_openai(self, repo: RepoCandidate, interest_tags: Sequence[str]) -> tuple[float, float, str, str] | None:
        prompt = {
            "repo": repo.full_name,
            "description": repo.description,
            "interest_tags": list(interest_tags),
            "readme": repo.readme_text[:12000],
            "output_format": {
                "score": "1-10 number",
                "readme_completeness": "0-1 number",
                "summary": "one sentence",
                "reason": "one sentence",
            },
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是开源项目评估助手，请输出 JSON。",
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt, ensure_ascii=False),
                },
            ],
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
        )
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:  # nosec B310
                data = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        try:
            result = json.loads(content)
        except Exception:
            return None
        try:
            return (
                float(result["score"]),
                float(result["readme_completeness"]),
                str(result["summary"]),
                str(result["reason"]),
            )
        except Exception:
            return None


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def notify(self, lines: Iterable[str]) -> None:
        text = "\n".join(lines)
        if not self.bot_token or not self.chat_id:
            print(text)
            return

        payload = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            method="POST",
            data=payload,
        )
        try:
            urllib.request.urlopen(req, timeout=20)  # nosec B310
        except Exception:
            print(text)


class MonitorPipeline:
    def __init__(self, config: MonitorConfig):
        self.config = config
        self.client = GitHubClient(config.github_token)
        self.store = SQLiteStore(config.db_path)
        self.rule_engine = RuleEngine(config)
        self.readme_analyzer = ReadmeAnalyzer(config.openai_api_key, config.openai_model)
        self.notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)

    def run(self) -> List[RepoCandidate]:
        candidates = self._collect_candidates()
        filtered = [r for r in candidates if not self.store.is_seen(r.full_name)]
        filtered = [r for r in filtered if self.rule_engine.apply(r)]

        for repo in filtered:
            self._enrich(repo)
            repo.rule_score = self.rule_engine.score(repo)
            llm_score, completeness, summary, reason = self.readme_analyzer.analyze(repo, self.config.keywords)
            repo.llm_score = llm_score
            repo.readme_completeness = completeness
            repo.summary = summary
            repo.recommendation_reason = reason
            repo.final_score = round(repo.rule_score * 0.55 + repo.llm_score * 0.45, 2)

        ranked = sorted(filtered, key=lambda x: x.final_score, reverse=True)[: self.config.top_n]
        self._push_report(ranked)
        for repo in ranked:
            self.store.mark_seen(repo)
        return ranked

    def close(self) -> None:
        self.store.close()

    def _collect_candidates(self) -> List[RepoCandidate]:
        all_candidates: dict[str, RepoCandidate] = {}

        for keyword in self.config.keywords:
            for language in self.config.languages:
                try:
                    repos = self.client.search_repositories(keyword, language, self.config.min_stars)
                except urllib.error.URLError:
                    repos = []
                for repo in repos:
                    all_candidates[repo.full_name] = repo

        for repo in self.client.fetch_trending_repositories():
            all_candidates[repo.full_name] = repo

        return list(all_candidates.values())

    def _enrich(self, repo: RepoCandidate) -> None:
        repo.fork_star_ratio = (repo.forks / repo.stars) if repo.stars else 0.0
        day_vel, week_vel = self.client.fetch_repo_events(repo.full_name)
        repo.star_velocity_day = day_vel
        repo.star_velocity_week = week_vel
        repo.avg_issue_response_hours = self.client.fetch_issue_response_hours(repo.full_name)
        contributor_count, contributor_growth = self.client.fetch_contributors_growth(repo.full_name)
        repo.contributor_count = contributor_count
        repo.contributor_growth_week = contributor_growth
        repo.readme_text = self.client.fetch_readme(repo.full_name)

    def _push_report(self, ranked: Sequence[RepoCandidate]) -> None:
        if not ranked:
            self.notifier.notify(["今日未发现符合条件的新项目。"])
            return

        lines = ["GitHub 项目推荐（Top 列表）"]
        for idx, repo in enumerate(ranked, 1):
            lines.append(
                (
                    f"{idx}. {repo.full_name} | 分数 {repo.final_score:.2f}\n"
                    f"一句话: {repo.summary}\n"
                    f"推荐原因: {repo.recommendation_reason}\n"
                    f"链接: {repo.html_url}"
                )
            )
        self.notifier.notify(lines)


def parse_dt(value: str) -> dt.datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def load_config(config_file: str | None) -> MonitorConfig:
    config = MonitorConfig.from_env()
    if not config_file:
        return config

    with open(config_file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    for key, value in payload.items():
        if hasattr(config, key):
            setattr(config, key, value)
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub 开源项目监控工具")
    parser.add_argument("--config", help="JSON 配置文件路径", default=None)
    args = parser.parse_args()

    pipeline = MonitorPipeline(load_config(args.config))
    try:
        pipeline.run()
    finally:
        pipeline.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
