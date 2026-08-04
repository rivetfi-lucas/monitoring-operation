import csv

from reddit_scraper.storage import StateStore, write_master_exports


def test_sqlite_deduplicates_comments(tmp_path) -> None:
    db_path = tmp_path / "state.sqlite3"
    post = {
        "post_id": "p1",
        "subreddit": "test",
        "intake_mode": "manual",
        "matched_keyword": "",
        "title": "Title",
        "body": "Body",
        "permalink": "https://old.reddit.com/r/test/comments/p1/x/",
        "num_comments": 1,
        "fetched_comment_count": 1,
    }
    comment = {
        "comment_id": "c1",
        "thread_id": "p1",
        "subreddit": "test",
        "parent_id": "p1",
        "root_comment_id": "c1",
        "depth": 0,
    }

    with StateStore(db_path) as store:
        store.record([post], [comment])
        store.record([post], [comment])
        assert store.known_comment_ids() == {"c1"}
        assert store.known_post_ids() == {"p1"}


def test_sqlite_refreshes_existing_comment_tree_metadata(tmp_path) -> None:
    db_path = tmp_path / "state.sqlite3"
    post = {
        "post_id": "p1",
        "subreddit": "test",
        "intake_mode": "manual",
        "matched_keyword": "",
        "title": "Title",
        "body": "Body",
        "permalink": "https://old.reddit.com/r/test/comments/p1/x/",
        "num_comments": 1,
        "fetched_comment_count": 1,
    }
    original = {
        "comment_id": "c1",
        "thread_id": "p1",
        "subreddit": "test",
        "parent_id": "",
        "root_comment_id": "c1",
        "depth": 1,
    }
    repaired = {
        **original,
        "parent_id": "parent1",
        "root_comment_id": "parent1",
        "depth": 2,
    }

    with StateStore(db_path) as store:
        store.record([post], [original])
        store.record([post], [repaired])
        row = store.conn.execute(
            "SELECT parent_id, root_comment_id, depth FROM comments WHERE comment_id='c1'"
        ).fetchone()

    assert row == ("parent1", "parent1", 2)


def test_master_csv_upserts_existing_comment_rows(tmp_path) -> None:
    comments_path = tmp_path / "comments.csv"
    posts_path = tmp_path / "posts.csv"
    post = {
        "post_id": "p1",
        "title": "Title",
        "subreddit": "test",
    }
    original = {
        "comment_id": "c1",
        "thread_id": "p1",
        "body": "Reply",
        "parent_id": "",
        "root_comment_id": "c1",
        "depth": 1,
    }
    repaired = {
        **original,
        "parent_id": "parent1",
        "root_comment_id": "parent1",
        "ancestor_ids": "parent1",
        "parent_body": "Parent",
        "context_text": "alice: Parent",
    }

    write_master_exports(
        comments_path=comments_path,
        posts_path=posts_path,
        comments=[original],
        posts=[post],
    )
    write_master_exports(
        comments_path=comments_path,
        posts_path=posts_path,
        comments=[repaired],
        posts=[post],
    )

    with comments_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]["parent_id"] == "parent1"
    assert rows[0]["root_comment_id"] == "parent1"
    assert rows[0]["parent_body"] == "Parent"


def test_sqlite_returns_known_post_permalink(tmp_path) -> None:
    db_path = tmp_path / "state.sqlite3"
    post = {
        "post_id": "p1",
        "subreddit": "test",
        "intake_mode": "manual",
        "matched_keyword": "",
        "title": "Title",
        "body": "Body",
        "permalink": "https://old.reddit.com/r/test/comments/p1/x/",
        "num_comments": 1,
        "fetched_comment_count": 1,
    }

    with StateStore(db_path) as store:
        store.record([post], [])
        assert store.post_permalink("p1") == post["permalink"]
        assert store.post_permalink("missing") is None
