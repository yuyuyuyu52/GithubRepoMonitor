"""Canonical GitHub API response payloads for mocked tests.

Minimal but representative — each dict has the fields the production code
reads from. Do not add fields we don't parse; doing so creates maintenance
drag with no test value.
"""
from __future__ import annotations


SEARCH_REPOSITORIES_OK = {
    "total_count": 2,
    "incomplete_results": False,
    "items": [
        {
            "full_name": "acme/widget",
            "html_url": "https://github.com/acme/widget",
            "description": "Widgets for agents",
            "language": "Python",
            "stargazers_count": 420,
            "forks_count": 21,
            "created_at": "2026-01-05T12:00:00Z",
            "pushed_at": "2026-04-16T10:00:00Z",
            "owner": {"login": "acme"},
            "topics": ["agent", "llm"],
        },
        {
            "full_name": "acme/gear",
            "html_url": "https://github.com/acme/gear",
            "description": "Reliable gear",
            "language": "Python",
            "stargazers_count": 180,
            "forks_count": 9,
            "created_at": "2026-02-10T00:00:00Z",
            "pushed_at": "2026-04-17T00:00:00Z",
            "owner": {"login": "acme"},
            "topics": ["tooling"],
        },
    ],
}

REPO_DETAIL_WIDGET = {
    "full_name": "acme/widget",
    "html_url": "https://github.com/acme/widget",
    "description": "Widgets for agents",
    "language": "Python",
    "stargazers_count": 420,
    "forks_count": 21,
    "created_at": "2026-01-05T12:00:00Z",
    "pushed_at": "2026-04-16T10:00:00Z",
    "owner": {"login": "acme"},
    "topics": ["agent", "llm"],
}


def events_payload(day_watches: int = 5, week_watches: int = 12) -> list[dict]:
    """Build a /events response: `day_watches` within the last 24h, and
    `week_watches - day_watches` in the last 7d but older than 24h."""
    import datetime as dt
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    events: list[dict] = []
    for i in range(day_watches):
        events.append({
            "type": "WatchEvent",
            "created_at": (now - dt.timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    older = max(week_watches - day_watches, 0)
    for i in range(older):
        events.append({
            "type": "WatchEvent",
            "created_at": (now - dt.timedelta(days=1, hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    # Add some non-watch noise we should ignore.
    events.append({"type": "PushEvent", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return events


CONTRIBUTORS_PAYLOAD = [
    {"login": "alice", "contributions": 120},
    {"login": "bob", "contributions": 8},
    {"login": "carol", "contributions": 1},
    {"login": "dave", "contributions": 1},
]  # total 4, growth (contributions <= 1) == 2


ISSUES_CLOSED_PAYLOAD = [
    {
        "number": 10,
        "created_at": "2026-04-10T00:00:00Z",
        "closed_at": "2026-04-11T12:00:00Z",  # 36h
        "pull_request": None,
    },
    {
        "number": 11,
        "created_at": "2026-04-12T00:00:00Z",
        "closed_at": "2026-04-12T06:00:00Z",  # 6h
        "pull_request": None,
    },
    {
        "number": 12,
        "created_at": "2026-04-13T00:00:00Z",
        "closed_at": "2026-04-13T00:30:00Z",
        "pull_request": {"url": "..."},  # PR, must be skipped
    },
]  # expected mean = (36 + 6) / 2 = 21.0 hours


README_RAW = (
    "# acme/widget\n\n"
    "## Install\n```bash\npip install widget\n```\n\n"
    "## Usage\nSee docs.\n\n"
    "## License\nMIT\n"
)


TRENDING_HTML = """<!doctype html><html><body>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/widget">acme / widget</a></h2>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/gear">acme / gear</a></h2>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/widget">acme / widget</a></h2>
</article>
</body></html>"""


def rate_limit_headers(remaining: int = 4999, reset_epoch: int = 9999999999) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_epoch),
    }
