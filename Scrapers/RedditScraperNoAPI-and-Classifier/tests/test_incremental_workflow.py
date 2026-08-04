from pathlib import Path

from main import filter_new_candidates, load_repair_candidates
from reddit_scraper.storage import StateStore


def test_default_filter_skips_known_posts_before_applying_cap() -> None:
    candidates = [
        {"post_id": "old1"},
        {"post_id": "old2"},
        {"post_id": "new1"},
        {"post_id": "new2"},
        {"post_id": "new3"},
    ]

    selected, skipped = filter_new_candidates(
        candidates,
        {"old1", "old2"},
        max_posts=2,
    )

    assert skipped == 2
    assert [post["post_id"] for post in selected] == ["new1", "new2"]


def test_repair_file_accepts_ids_urls_comments_and_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    repair_path = tmp_path / "repair.txt"
    repair_path.write_text(
        """
# selected repairs
known1
https://www.reddit.com/r/test/comments/url2/example/
t3_known1
""".strip(),
        encoding="utf-8",
    )

    known_post = {
        "post_id": "known1",
        "subreddit": "test",
        "intake_mode": "full_scrape",
        "matched_keyword": "",
        "title": "Known",
        "body": "",
        "permalink": "https://old.reddit.com/r/test/comments/known1/known/",
        "num_comments": 1,
        "fetched_comment_count": 1,
    }

    with StateStore(db_path) as store:
        store.record([known_post], [])
        candidates = load_repair_candidates(repair_path, store)

    assert {row["post_id"] for row in candidates} == {"known1", "url2"}
    assert all(row["intake_mode"] == "repair" for row in candidates)
    known = next(row for row in candidates if row["post_id"] == "known1")
    assert known["permalink"].endswith("/r/test/comments/known1/known/")


def test_bare_unknown_repair_id_uses_generic_comments_url(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    repair_path = tmp_path / "repair.txt"
    repair_path.write_text("abc123\n", encoding="utf-8")

    with StateStore(db_path) as store:
        candidates = load_repair_candidates(repair_path, store)

    assert candidates[0]["post_id"] == "abc123"
    assert "/comments/abc123/" in candidates[0]["permalink"]
