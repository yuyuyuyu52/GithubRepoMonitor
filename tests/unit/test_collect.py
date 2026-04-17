import datetime as dt

import pytest

from monitor.models import RepoCandidate
from monitor.pipeline.collect import collect_candidates


def _repo(name: str) -> RepoCandidate:
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language="Python",
        stars=100,
        forks=10,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        pushed_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
        owner_login=name.split("/")[0],
    )


class FakeClient:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, str, int]] = []
        self.trending_calls = 0
        self._search_results: dict[tuple[str, str], list[RepoCandidate]] = {}
        self._trending: list[RepoCandidate] = []

    def set_search(self, keyword: str, language: str, repos: list[RepoCandidate]) -> None:
        self._search_results[(keyword, language)] = repos

    def set_trending(self, repos: list[RepoCandidate]) -> None:
        self._trending = repos

    async def search_repositories(self, *, keyword: str, language: str, min_stars: int):
        self.search_calls.append((keyword, language, min_stars))
        return list(self._search_results.get((keyword, language), []))

    async def fetch_trending_repositories(self):
        self.trending_calls += 1
        return list(self._trending)


async def test_collect_searches_cross_product_of_keywords_and_languages() -> None:
    client = FakeClient()
    client.set_search("llm", "Python", [_repo("a/one")])
    client.set_search("llm", "Rust", [_repo("a/two")])
    client.set_search("agent", "Python", [_repo("a/three")])
    client.set_search("agent", "Rust", [])

    repos = await collect_candidates(
        client,
        keywords=["llm", "agent"],
        languages=["Python", "Rust"],
        min_stars=100,
    )

    assert {r.full_name for r in repos} == {"a/one", "a/two", "a/three"}
    assert sorted(client.search_calls) == [
        ("agent", "Python", 100),
        ("agent", "Rust", 100),
        ("llm", "Python", 100),
        ("llm", "Rust", 100),
    ]
    assert client.trending_calls == 1


async def test_collect_dedupes_across_searches_and_trending() -> None:
    client = FakeClient()
    client.set_search("llm", "Python", [_repo("a/dup"), _repo("a/uniq")])
    client.set_trending([_repo("a/dup"), _repo("a/trend")])

    repos = await collect_candidates(
        client,
        keywords=["llm"],
        languages=["Python"],
        min_stars=100,
    )

    names = [r.full_name for r in repos]
    assert sorted(names) == ["a/dup", "a/trend", "a/uniq"]
    assert len(names) == len(set(names))


async def test_collect_tolerates_search_failure_for_single_pair(monkeypatch) -> None:
    client = FakeClient()
    client.set_search("llm", "Python", [_repo("a/ok")])
    client.set_trending([])

    orig = client.search_repositories

    async def flaky_search(**kwargs):
        if kwargs["language"] == "Rust":
            raise RuntimeError("boom")
        return await orig(**kwargs)

    monkeypatch.setattr(client, "search_repositories", flaky_search)

    repos = await collect_candidates(
        client,
        keywords=["llm"],
        languages=["Python", "Rust"],
        min_stars=100,
    )
    # Rust failed but Python's result survives
    assert [r.full_name for r in repos] == ["a/ok"]
