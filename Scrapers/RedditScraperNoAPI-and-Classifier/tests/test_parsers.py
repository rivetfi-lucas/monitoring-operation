from pathlib import Path

from reddit_scraper.parsers import (
    enrich_comment_context,
    parse_comments_html,
    parse_listing_html,
    parse_post_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_listing_parser_reads_post_and_next_page() -> None:
    html = (FIXTURES / "listing.html").read_text(encoding="utf-8")
    posts, next_url = parse_listing_html(
        html,
        {"name": "test", "url": "https://www.reddit.com/r/test"},
        "full_scrape",
    )
    assert len(posts) == 1
    assert posts[0]["post_id"] == "post1"
    assert posts[0]["num_comments"] == 12
    assert posts[0]["permalink"].startswith("https://old.reddit.com/")
    assert next_url and "after=t3_post1" in next_url


def test_nested_tree_and_context_are_preserved() -> None:
    html = (FIXTURES / "thread.html").read_text(encoding="utf-8")
    candidate = {
        "post_id": "post1",
        "title": "Example post",
        "subreddit": "test",
        "intake_mode": "manual",
        "matched_keyword": "",
        "permalink": "https://old.reddit.com/r/test/comments/post1/example/",
    }
    post = parse_post_html(html, candidate)
    comments, continuation_urls = parse_comments_html(html, post)
    comments = enrich_comment_context(comments, post_id="post1", ancestor_limit=8)
    by_id = {row["comment_id"]: row for row in comments}

    assert post["body"] == "Post body here"
    assert len(comments) == 3
    assert by_id["c1"]["depth"] == 0
    assert by_id["c2"]["parent_id"] == "c1"
    assert by_id["c3"]["depth"] == 2
    assert by_id["c3"]["root_comment_id"] == "c1"
    assert by_id["c3"]["ancestor_ids"] == "c1>c2"
    assert by_id["c3"]["parent_body"] == "First reply"
    assert "bob: Root comment" in by_id["c3"]["context_text"]
    assert "carol: First reply" in by_id["c3"]["context_text"]
    assert len(continuation_urls) == 1


def test_old_reddit_infers_missing_parent_ids_from_nested_dom() -> None:
    html = (FIXTURES / "thread_missing_parent.html").read_text(encoding="utf-8")
    candidate = {
        "post_id": "post1",
        "title": "Example post",
        "subreddit": "test",
        "intake_mode": "manual",
        "matched_keyword": "",
        "permalink": "https://old.reddit.com/r/test/comments/post1/example/",
    }
    post = parse_post_html(html, candidate)
    comments, _ = parse_comments_html(html, post)
    comments = enrich_comment_context(comments, post_id="post1", ancestor_limit=8)
    by_id = {row["comment_id"]: row for row in comments}

    assert by_id["c1"]["parent_id"] == "post1"
    assert by_id["c2"]["parent_id"] == "c1"
    assert by_id["c3"]["parent_id"] == "c2"
    assert by_id["c3"]["root_comment_id"] == "c1"
    assert by_id["c3"]["ancestor_ids"] == "c1>c2"
    assert by_id["c3"]["parent_body"] == "First reply"
    assert "bob: Root comment" in by_id["c3"]["context_text"]
    assert "carol: First reply" in by_id["c3"]["context_text"]


def test_old_reddit_infers_missing_parent_ids_from_depth_order() -> None:
    html = (FIXTURES / "thread_flat_missing_parent.html").read_text(
        encoding="utf-8"
    )
    candidate = {
        "post_id": "post1",
        "title": "Example post",
        "subreddit": "test",
        "intake_mode": "manual",
        "matched_keyword": "",
        "permalink": "https://old.reddit.com/r/test/comments/post1/example/",
    }
    post = parse_post_html(html, candidate)
    comments, _ = parse_comments_html(html, post)
    comments = enrich_comment_context(comments, post_id="post1", ancestor_limit=8)
    by_id = {row["comment_id"]: row for row in comments}

    assert by_id["c2"]["parent_id"] == "c1"
    assert by_id["c3"]["parent_id"] == "c2"
    assert by_id["c4"]["parent_id"] == "post1"
    assert by_id["c4"]["root_comment_id"] == "c4"


def test_modern_listing_parser_reads_shreddit_posts() -> None:
    html = (FIXTURES / "modern_listing.html").read_text(encoding="utf-8")
    posts, next_url = parse_listing_html(
        html,
        {"name": "test", "url": "https://www.reddit.com/r/test"},
        "full_scrape",
    )
    assert next_url is None
    assert len(posts) == 1
    assert posts[0]["post_id"] == "modern1"
    assert posts[0]["title"] == "Modern example"
    assert posts[0]["num_comments"] == 17
    assert posts[0]["permalink"].startswith("https://www.reddit.com/")


def test_modern_thread_parser_preserves_parent_tree() -> None:
    html = (FIXTURES / "modern_thread.html").read_text(encoding="utf-8")
    candidate = {
        "post_id": "modern1",
        "title": "",
        "subreddit": "test",
        "intake_mode": "manual",
        "matched_keyword": "",
        "permalink": "https://www.reddit.com/r/test/comments/modern1/modern_example/",
    }
    post = parse_post_html(html, candidate)
    comments, continuation_urls = parse_comments_html(html, post)
    comments = enrich_comment_context(comments, post_id="modern1", ancestor_limit=8)
    by_id = {row["comment_id"]: row for row in comments}

    assert post["body"] == "Modern post body"
    assert len(comments) == 2
    assert by_id["mc1"]["depth"] == 0
    assert by_id["mc2"]["parent_id"] == "mc1"
    assert by_id["mc2"]["root_comment_id"] == "mc1"
    assert by_id["mc2"]["parent_body"] == "Modern root comment"
    assert continuation_urls == []


def test_modern_listing_parser_falls_back_to_comment_links() -> None:
    html = (FIXTURES / "modern_anchor_listing.html").read_text(encoding="utf-8")
    posts, next_url = parse_listing_html(
        html,
        {"name": "test", "url": "https://www.reddit.com/r/test"},
        "full_scrape",
    )
    assert next_url is None
    assert len(posts) == 1
    assert posts[0]["post_id"] == "abc123"
    assert posts[0]["title"] == "A current Reddit post"
    assert posts[0]["permalink"].startswith("https://www.reddit.com/r/test/comments/")
