import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "reddit_scraper_hybrid.py"
spec = importlib.util.spec_from_file_location("reddit_scraper_hybrid", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def comment(cid, parent=None, replies=None, cursor=None):
    payload = {
        "id": cid,
        "parentId": parent,
        "text": f"body-{cid}",
        "replies": {"items": replies or [], "page": {"hasMore": False, "nextCursor": None}},
    }
    if cursor:
        payload["replies"]["page"] = {"hasMore": True, "nextCursor": cursor}
    return payload


class FakeSocialFetchClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def post_comments(self, post_url, cursor=None):
        self.calls.append(cursor)
        return self.pages[cursor]


class CommentTreeTests(unittest.TestCase):
    def test_recurses_inline_replies_and_all_cursor_branches(self):
        pages = {
            None: {
                "post": {"id": "post1"},
                "comments": [
                    comment(
                        "a",
                        "t3_post1",
                        replies=[
                            comment(
                                "b",
                                "t1_a",
                                replies=[comment("c", "t1_b")],
                                cursor="cursor-b",
                            )
                        ],
                        cursor="cursor-a",
                    )
                ],
                "page": {"hasMore": True, "nextCursor": "root-2"},
            },
            "cursor-b": {
                "comments": [comment("d", "t1_b", replies=[comment("e", "t1_d")])],
                "page": {"hasMore": False, "nextCursor": None},
            },
            "cursor-a": {
                "comments": [comment("f", "t1_a")],
                "page": {"hasMore": False, "nextCursor": None},
            },
            "root-2": {
                "comments": [comment("g", "t3_post1")],
                "page": {"hasMore": False, "nextCursor": None},
            },
        }
        client = FakeSocialFetchClient(pages)
        rows, post, calls, truncated = mod.fetch_all_comments(client, "https://reddit.test/post")
        depths = {row["comment"]["id"]: row["depth"] for row in rows}
        self.assertEqual(depths, {"a": 0, "b": 1, "c": 2, "d": 2, "e": 3, "f": 1, "g": 0})
        self.assertEqual(post["id"], "post1")
        self.assertEqual(calls, 4)
        self.assertFalse(truncated)

    def test_deduplicates_comments_repeated_across_pages(self):
        duplicate = comment("same", "t3_post1")
        pages = {
            None: {
                "comments": [duplicate],
                "page": {"hasMore": True, "nextCursor": "next"},
            },
            "next": {
                "comments": [duplicate, comment("new", "t3_post1")],
                "page": {"hasMore": False, "nextCursor": None},
            },
        }
        rows, _, _, truncated = mod.fetch_all_comments(FakeSocialFetchClient(pages), "url")
        self.assertEqual([r["comment"]["id"] for r in rows], ["same", "new"])
        self.assertFalse(truncated)

    def test_marks_truncated_when_api_call_cap_stops_pending_pages(self):
        pages = {
            None: {
                "comments": [comment("a")],
                "page": {"hasMore": True, "nextCursor": "next"},
            },
            "next": {
                "comments": [comment("b")],
                "page": {"hasMore": False, "nextCursor": None},
            },
        }
        rows, _, calls, truncated = mod.fetch_all_comments(
            FakeSocialFetchClient(pages), "url", max_api_calls=1
        )
        self.assertEqual([r["comment"]["id"] for r in rows], ["a"])
        self.assertEqual(calls, 1)
        self.assertTrue(truncated)

    def test_sqlite_state_tracks_comments_and_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.db")
            conn = mod.open_state_db(db_path)
            rows = [{
                "comment_id": "c1", "thread_id": "p1", "subreddit": "demo",
                "parent_id": "t3_p1", "depth": 0, "fetched_at": "2026-07-31T00:00:00+00:00",
            }]
            posts = [{
                "post_id": "p1", "subreddit": "demo", "source_type": "full_scrape",
                "matched_keyword": "", "title": "Demo", "permalink": "https://reddit.test/p1",
                "reported_comment_count": 1, "fetched_comment_count": 1,
                "was_truncated": False, "last_scraped_at": "2026-07-31T00:00:00+00:00",
            }]
            mod.save_scrape_state(conn, rows, posts)
            self.assertEqual(mod.load_seen_ids(conn), {"c1"})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scraped_posts").fetchone()[0], 1)
            conn.close()


class IncrementalRepairTests(unittest.TestCase):
    def _post_payload(self, post_id="t3_old1", permalink=None):
        return {
            "post_id": post_id,
            "subreddit": "demo",
            "source_type": "full_scrape",
            "matched_keyword": "",
            "title": "Demo",
            "permalink": permalink or f"https://www.reddit.com/r/demo/comments/{mod.normalize_post_id(post_id)}/demo/",
            "reported_comment_count": 1,
            "fetched_comment_count": 1,
            "was_truncated": False,
            "last_scraped_at": "2026-08-01T00:00:00+00:00",
        }

    def test_default_filter_skips_known_posts_before_applying_cap(self):
        candidates = {
            "t3_old1": ({"id": "t3_old1"}, ""),
            "old2": ({"id": "old2"}, ""),
            "new1": ({"id": "new1"}, ""),
            "new2": ({"id": "new2"}, ""),
            "new3": ({"id": "new3"}, ""),
        }
        selected, skipped = mod.filter_new_candidates(
            candidates,
            {"old1", "old2"},
            max_posts=2,
        )
        self.assertEqual(skipped, 2)
        self.assertEqual(list(selected), ["new1", "new2"])

    def test_repair_file_accepts_ids_urls_comments_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = mod.open_state_db(str(Path(tmp) / "state.db"))
            mod.save_scrape_state(conn, [], [self._post_payload("t3_known1")])
            repair_path = Path(tmp) / "repair_posts.txt"
            repair_path.write_text(
                """
# selected repairs
known1
https://www.reddit.com/r/demo/comments/url2/example/
t3_known1
""".strip(),
                encoding="utf-8",
            )
            candidates = mod.load_repair_candidates(str(repair_path), conn)
            conn.close()

        self.assertEqual(
            {row["normalized_post_id"] for row in candidates},
            {"known1", "url2"},
        )
        known = next(row for row in candidates if row["normalized_post_id"] == "known1")
        self.assertEqual(known["post_id"], "t3_known1")
        self.assertTrue(known["post"]["permalink"].endswith("/comments/known1/demo/"))
        self.assertTrue(all(row["intake_mode"] == "repair" for row in candidates))

    def test_unknown_bare_repair_id_uses_generic_comments_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = mod.open_state_db(str(Path(tmp) / "state.db"))
            repair_path = Path(tmp) / "repair_posts.txt"
            repair_path.write_text("abc123\n", encoding="utf-8")
            candidates = mod.load_repair_candidates(str(repair_path), conn)
            conn.close()

        self.assertEqual(candidates[0]["normalized_post_id"], "abc123")
        self.assertIn("/comments/abc123/", candidates[0]["post"]["permalink"])

    def test_state_upsert_refreshes_comment_metadata_without_resetting_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = mod.open_state_db(str(Path(tmp) / "state.db"))
            first = {
                "comment_id": "c1",
                "thread_id": "p1",
                "subreddit": "demo",
                "parent_id": "",
                "depth": 0,
                "fetched_at": "2026-07-31T00:00:00+00:00",
            }
            repaired = {
                **first,
                "parent_id": "t1_parent",
                "depth": 3,
                "fetched_at": "2026-08-01T00:00:00+00:00",
            }
            mod.save_scrape_state(conn, [first], [self._post_payload("t3_p1")])
            mod.save_scrape_state(conn, [repaired], [self._post_payload("t3_p1")])
            row = conn.execute(
                "SELECT parent_id, depth, first_seen_at FROM scraped_comments WHERE comment_id='c1'"
            ).fetchone()
            known = mod.load_known_post_ids(conn)
            conn.close()

        self.assertEqual(row, ("t1_parent", 3, "2026-07-31T00:00:00+00:00"))
        self.assertIn("p1", known)


if __name__ == "__main__":
    unittest.main()
