import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

MODULE_PATH = Path(__file__).resolve().parents[1] / "reddit_scraper_hybrid.py"
spec = importlib.util.spec_from_file_location("reddit_scraper_hybrid_run", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.total_requests = 0
        self.total_credits_charged = 0


def make_files(root: Path) -> tuple[Path, Path, Path]:
    config = root / "config.yaml"
    keywords = root / "keywords.yaml"
    sources = root / "sources.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "scraper": {
                    "scrape_days": 7,
                    "scrape_days_keyword_search": 30,
                    "min_comments_threshold": 0,
                    "max_posts_per_source": 1,
                    "max_comments_per_post": 0,
                    "page_size": 25,
                    "request_delay_seconds": 0,
                    "full_scrape_sort": "new",
                    "keyword_search_sort": "relevance",
                    "max_pages_per_source": 2,
                    "comment_sort_passes": ["top"],
                },
                "hybrid": {
                    "chocodata_comment_threshold": 0,
                    "max_api_calls_per_post": 20,
                    "max_sf_calls_total_per_run": None,
                },
            }
        ),
        encoding="utf-8",
    )
    keywords.write_text("terms: []\n", encoding="utf-8")
    sources.write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "name": "demo",
                        "type": "full_scrape",
                        "url": "https://www.reddit.com/r/demo/",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return config, keywords, sources


class RunWorkflowTests(unittest.TestCase):
    def test_normal_run_fetches_only_new_posts_and_cap_is_after_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, keywords, sources = make_files(root)
            db_path = root / "state.db"
            original_open = mod.open_state_db
            conn = original_open(str(db_path))
            mod.save_scrape_state(
                conn,
                [],
                [
                    {
                        "post_id": "t3_old",
                        "subreddit": "demo",
                        "source_type": "full_scrape",
                        "matched_keyword": "",
                        "title": "Old",
                        "permalink": "https://www.reddit.com/r/demo/comments/old/old/",
                        "reported_comment_count": 5,
                        "fetched_comment_count": 5,
                        "was_truncated": False,
                        "last_scraped_at": "2026-08-01T00:00:00+00:00",
                    }
                ],
            )
            conn.close()

            candidates = {
                "t3_old": (
                    {
                        "id": "t3_old",
                        "title": "Old",
                        "permalink": "/r/demo/comments/old/old/",
                        "num_comments": 5,
                    },
                    "",
                ),
                "t3_new1": (
                    {
                        "id": "t3_new1",
                        "title": "New 1",
                        "permalink": "/r/demo/comments/new1/new/",
                        "num_comments": 5,
                    },
                    "",
                ),
                "t3_new2": (
                    {
                        "id": "t3_new2",
                        "title": "New 2",
                        "permalink": "/r/demo/comments/new2/new/",
                        "num_comments": 5,
                    },
                    "",
                ),
            }
            fetched = []

            def fake_fetch(client, permalink, max_comments=None, max_api_calls=None):
                fetched.append(permalink)
                post_id = mod.post_id_from_url(permalink)
                return (
                    [
                        {
                            "comment": {
                                "id": f"c_{post_id}",
                                "parentId": f"t3_{post_id}",
                                "text": "body",
                            },
                            "depth": 0,
                        }
                    ],
                    {"id": post_id},
                    1,
                    False,
                )

            with mock.patch.dict(
                os.environ,
                {"CHOCODATA_API_KEY": "test", "SOCIALFETCH_API_KEY": "test"},
                clear=False,
            ), mock.patch.object(mod, "open_state_db", side_effect=lambda: original_open(str(db_path))), mock.patch.object(
                mod, "ChocodataClient", FakeClient
            ), mock.patch.object(mod, "SocialFetchClient", FakeClient), mock.patch.object(
                mod, "collect_full_scrape_candidates", return_value=candidates
            ), mock.patch.object(mod, "fetch_all_comments", side_effect=fake_fetch), mock.patch.object(
                mod, "write_csv", return_value=None
            ):
                mod.run(str(config), str(keywords), str(sources), only_source="demo")

            self.assertEqual(len(fetched), 1)
            self.assertIn("/comments/new1/", fetched[0])

    def test_repair_mode_bypasses_discovery_and_fetches_only_file_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, keywords, sources = make_files(root)
            db_path = root / "state.db"
            repair = root / "repair.txt"
            repair.write_text("known\nhttps://www.reddit.com/r/demo/comments/url2/title/\n", encoding="utf-8")
            original_open = mod.open_state_db
            conn = original_open(str(db_path))
            mod.save_scrape_state(
                conn,
                [],
                [
                    {
                        "post_id": "known",
                        "subreddit": "demo",
                        "source_type": "full_scrape",
                        "matched_keyword": "",
                        "title": "Known",
                        "permalink": "https://www.reddit.com/r/demo/comments/known/title/",
                        "reported_comment_count": 1,
                        "fetched_comment_count": 1,
                        "was_truncated": False,
                        "last_scraped_at": "2026-08-01T00:00:00+00:00",
                    }
                ],
            )
            conn.close()

            fetched = []
            captured = {}

            def fake_fetch(client, permalink, max_comments=None, max_api_calls=None):
                fetched.append(permalink)
                post_id = mod.post_id_from_url(permalink)
                return (
                    [{"comment": {"id": f"c_{post_id}", "text": "body"}, "depth": 0}],
                    {"id": post_id, "title": f"Title {post_id}"},
                    1,
                    False,
                )

            def fake_write(rows, fields, filename_prefix="reddit_comments"):
                captured["rows"] = list(rows)
                captured["prefix"] = filename_prefix
                return str(root / "repair.csv")

            with mock.patch.dict(
                os.environ,
                {"SOCIALFETCH_API_KEY": "test"},
                clear=True,
            ), mock.patch.object(mod, "open_state_db", side_effect=lambda: original_open(str(db_path))), mock.patch.object(
                mod, "SocialFetchClient", FakeClient
            ), mock.patch.object(
                mod, "collect_full_scrape_candidates", side_effect=AssertionError("discovery should not run")
            ), mock.patch.object(mod, "fetch_all_comments", side_effect=fake_fetch), mock.patch.object(
                mod, "write_csv", side_effect=fake_write
            ):
                mod.run(
                    str(config),
                    str(keywords),
                    str(sources),
                    repair_posts_path=str(repair),
                )

            self.assertEqual(len(fetched), 2)
            self.assertEqual(captured["prefix"], "reddit_comments_repair")
            self.assertEqual({row["comment_id"] for row in captured["rows"]}, {"c_known", "c_url2"})


if __name__ == "__main__":
    unittest.main()
