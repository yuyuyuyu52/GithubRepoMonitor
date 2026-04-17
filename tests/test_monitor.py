import datetime as dt
import tempfile
import unittest
from unittest.mock import patch

from src.github_repo_monitor import MonitorConfig, MonitorPipeline, RepoCandidate, RuleEngine, SQLiteStore


class RuleEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MonitorConfig(min_stars=100, max_repo_age_days=180, languages=["Python", "Go"]) 
        self.engine = RuleEngine(self.config)

    def test_apply_respects_language_star_and_age(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        good_repo = RepoCandidate(
            full_name="a/b",
            html_url="https://github.com/a/b",
            description="desc",
            language="Python",
            stars=120,
            forks=20,
            created_at=now - dt.timedelta(days=20),
            pushed_at=now,
        )
        old_repo = RepoCandidate(
            full_name="a/c",
            html_url="https://github.com/a/c",
            description="desc",
            language="Python",
            stars=220,
            forks=20,
            created_at=now - dt.timedelta(days=300),
            pushed_at=now,
        )
        self.assertTrue(self.engine.apply(good_repo))
        self.assertFalse(self.engine.apply(old_repo))


class PipelineTests(unittest.TestCase):
    def _repo(self, name: str, stars: int, language: str = "Python") -> RepoCandidate:
        now = dt.datetime.now(dt.timezone.utc)
        return RepoCandidate(
            full_name=name,
            html_url=f"https://github.com/{name}",
            description=f"{name} llm monitor",
            language=language,
            stars=stars,
            forks=stars // 2,
            created_at=now - dt.timedelta(days=5),
            pushed_at=now,
            star_velocity_day=float(stars // 100),
            star_velocity_week=float(stars // 50),
            contributor_growth_week=2,
            avg_issue_response_hours=3,
        )

    def test_pipeline_filters_seen_and_ranks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MonitorConfig(
                keywords=["llm"],
                languages=["Python"],
                min_stars=100,
                top_n=2,
                db_path=f"{tmpdir}/test.db",
            )
            pipeline = MonitorPipeline(config)

            fresh_high = self._repo("x/high", 300)
            fresh_mid = self._repo("x/mid", 200)
            seen_repo = self._repo("x/seen", 260)

            pipeline.store.mark_seen(seen_repo)

            with patch.object(pipeline, "_collect_candidates", return_value=[fresh_mid, seen_repo, fresh_high]), patch.object(
                pipeline,
                "_enrich",
                side_effect=lambda repo: setattr(repo, "readme_text", "# title\n## install\n## usage\n## architecture\n## license"),
            ), patch.object(
                pipeline.readme_analyzer,
                "analyze",
                side_effect=lambda repo, tags: (9.0 if repo.full_name.endswith("high") else 7.0, 1.0, "summary", "reason"),
            ), patch.object(pipeline.notifier, "notify") as notify_mock:
                ranked = pipeline.run()

            self.assertEqual([r.full_name for r in ranked], ["x/high", "x/mid"])
            self.assertFalse(any(r.full_name == "x/seen" for r in ranked))
            notify_mock.assert_called_once()
            pipeline.close()


class StoreTests(unittest.TestCase):
    def test_seen_repository_roundtrip(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(f"{tmpdir}/store.db")
            repo = RepoCandidate(
                full_name="owner/repo",
                html_url="https://github.com/owner/repo",
                description="desc",
                language="Python",
                stars=111,
                forks=11,
                created_at=now,
                pushed_at=now,
                final_score=8.2,
            )
            self.assertFalse(store.is_seen("owner/repo"))
            store.mark_seen(repo)
            self.assertTrue(store.is_seen("owner/repo"))
            store.close()


if __name__ == "__main__":
    unittest.main()
