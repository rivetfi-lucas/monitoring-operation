from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest

import main
from reddit_scraper.browser import RedditBrowser, ScraperError
from reddit_scraper.config import BrowserConfig
from reddit_scraper.storage import (
    IncrementalMasterWriter,
    RunCheckpointWriter,
    StateStore,
)


def _post(post_id: str) -> dict[str, object]:
    return {
        "post_id": post_id,
        "title": f"Post {post_id}",
        "subreddit": "test",
        "intake_mode": "full_scrape",
        "matched_keyword": "",
        "author": "author",
        "body": "body",
        "score": 1,
        "num_comments": 1,
        "fetched_comment_count": 1,
        "created_utc": 1,
        "created_iso": "1970-01-01T00:00:01Z",
        "permalink": f"https://old.reddit.com/r/test/comments/{post_id}/x/",
        "fetched_at": "now",
    }


def _comment(post_id: str) -> dict[str, object]:
    return {
        "comment_id": f"c_{post_id}",
        "thread_id": post_id,
        "thread_title": f"Post {post_id}",
        "thread_body": "body",
        "subreddit": "test",
        "intake_mode": "full_scrape",
        "matched_keyword": "",
        "parent_id": post_id,
        "root_comment_id": f"c_{post_id}",
        "ancestor_ids": "",
        "parent_body": "",
        "context_text": "",
        "author": "user",
        "body": "comment",
        "score": "",
        "depth": 0,
        "created_utc": 1,
        "created_iso": "1970-01-01T00:00:01Z",
        "permalink": "",
        "thread_permalink": f"https://old.reddit.com/r/test/comments/{post_id}/x/",
        "fetched_at": "now",
    }


def test_incremental_writer_appends_once_without_rewriting_duplicates(tmp_path: Path) -> None:
    comments_path = tmp_path / "comments.csv"
    posts_path = tmp_path / "posts.csv"
    writer = IncrementalMasterWriter(
        comments_path=comments_path,
        posts_path=posts_path,
        update_existing=False,
    )

    writer.write(post=_post("p1"), comments=[_comment("p1")])
    writer.write(post=_post("p1"), comments=[_comment("p1")])
    writer.write(post=_post("p2"), comments=[_comment("p2")])

    with comments_path.open("r", encoding="utf-8-sig", newline="") as handle:
        comments = list(csv.DictReader(handle))
    with posts_path.open("r", encoding="utf-8-sig", newline="") as handle:
        posts = list(csv.DictReader(handle))

    assert [row["comment_id"] for row in comments] == ["c_p1", "c_p2"]
    assert [row["post_id"] for row in posts] == ["p1", "p2"]


def test_run_checkpoint_writer_creates_repair_ready_failure_file(tmp_path: Path) -> None:
    writer = RunCheckpointWriter(tmp_path / "runs", "abcdef123456")
    writer.write_post(post=_post("p1"), comments=[_comment("p1")])
    writer.write_error(candidate=_post("p2"), attempt=2, error="timed out")

    assert writer.posts_path.exists()
    assert writer.comments_path.exists()
    assert writer.errors_path.exists()
    assert writer.failed_targets_path.read_text(encoding="utf-8").strip().endswith(
        "/comments/p2/x/"
    )


def test_state_run_progress_is_checkpointed_and_old_running_run_is_interrupted(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.sqlite3"
    with StateStore(db_path) as store:
        first = store.start_run(1)
        store.update_run_progress(first, post_count=7, new_comment_count=22)
        second = store.start_run(1)
        first_row = store.conn.execute(
            "SELECT status, post_count, new_comment_count FROM runs WHERE run_id=?",
            (first,),
        ).fetchone()
        second_row = store.conn.execute(
            "SELECT status FROM runs WHERE run_id=?", (second,)
        ).fetchone()

    assert first_row == ("interrupted", 7, 22)
    assert second_row == ("running",)


def test_past_deadline_stops_navigation_before_browser_call(tmp_path: Path) -> None:
    browser = RedditBrowser(BrowserConfig(), tmp_path / "profile")
    browser.page = object()  # type: ignore[assignment]

    with pytest.raises(ScraperError, match="Per-post timeout"):
        browser.goto("https://old.reddit.com/r/test/", deadline=time.monotonic() - 1)


def test_streaming_run_checkpoints_every_post_and_recycles_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    sources_path = tmp_path / "sources.yaml"
    keywords_path = tmp_path / "keywords.yaml"
    data_dir = tmp_path / "data"
    config_path.write_text(
        f"""
paths:
  data_dir: {data_dir.as_posix()}
  comments_csv: {(data_dir / 'exports/comments.csv').as_posix()}
  posts_csv: {(data_dir / 'exports/posts.csv').as_posix()}
  state_db: {(data_dir / 'state/state.sqlite3').as_posix()}
  profile_dir: {(data_dir / 'profile').as_posix()}
  snapshots_dir: {(data_dir / 'runs').as_posix()}
scraper:
  max_posts_per_source: 0
  post_timeout_seconds: 60
  post_retries: 0
browser:
  request_delay_min_seconds: 0
  request_delay_max_seconds: 0
  expansion_delay_min_seconds: 0
  expansion_delay_max_seconds: 0
  scroll_delay_min_seconds: 0
  scroll_delay_max_seconds: 0
  restart_every_posts: 2
output:
  append_master_csv: true
  create_run_snapshots: true
""".strip(),
        encoding="utf-8",
    )
    sources_path.write_text(
        "sources:\n  - name: test\n    subreddit: test\n    type: full_scrape\n",
        encoding="utf-8",
    )
    keywords_path.write_text("terms: []\n", encoding="utf-8")

    candidates = [_post(f"p{i}") for i in range(1, 6)]
    enters: list[int] = []
    fetches: list[str] = []

    class FakeBrowser:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            enters.append(1)
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(main, "RedditBrowser", FakeBrowser)
    monkeypatch.setattr(
        main,
        "discover_source_posts",
        lambda *args, **kwargs: [dict(row) for row in candidates],
    )

    def fake_fetch(browser, candidate, config):
        post_id = str(candidate["post_id"])
        fetches.append(post_id)
        return _post(post_id), [_comment(post_id)]

    monkeypatch.setattr(main, "fetch_complete_thread", fake_fetch)

    args = main.build_parser().parse_args(
        [
            "--source",
            "test",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
            "--keywords",
            str(keywords_path),
        ]
    )
    assert main.run(args) == 0

    # One discovery browser plus three processing sessions for 5 posts at 2/session.
    assert len(enters) == 4
    assert fetches == ["p1", "p2", "p3", "p4", "p5"]

    with StateStore(data_dir / "state/state.sqlite3") as store:
        assert store.known_post_ids() == {"p1", "p2", "p3", "p4", "p5"}

    # Re-running discovers the same posts but opens none because every post was saved.
    fetches.clear()
    assert main.run(args) == 0
    assert fetches == []
