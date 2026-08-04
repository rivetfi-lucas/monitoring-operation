from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from typing import Any
from urllib.parse import quote_plus

from .browser import RedditBrowser, ScraperError
from .config import AppConfig
from .parsers import (
    enrich_comment_context,
    parse_comments_html,
    parse_listing_html,
    parse_modern_listing_links,
    parse_post_html,
    source_subreddit,
)
from .utils import OLD_REDDIT_BASE, old_reddit_url, subreddit_from_url, with_query


def flatten_keywords(keywords_doc: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for value in keywords_doc.values():
        if isinstance(value, list):
            terms.extend(str(item).strip() for item in value if str(item).strip())
    return list(dict.fromkeys(terms))


def listing_url(subreddit: str, sort: str, timeframe: str) -> str:
    safe_sort = sort if sort in {"hot", "new", "rising", "controversial", "top"} else "new"
    url = f"{OLD_REDDIT_BASE}/r/{quote_plus(subreddit)}/{safe_sort}/"
    params: dict[str, Any] = {"limit": 100}
    if safe_sort in {"top", "controversial"}:
        params["t"] = timeframe
    return with_query(url, **params)


def search_url(subreddit: str, keyword: str, sort: str, timeframe: str) -> str:
    safe_sort = sort if sort in {"relevance", "hot", "top", "new", "comments"} else "new"
    return with_query(
        f"{OLD_REDDIT_BASE}/r/{quote_plus(subreddit)}/search",
        q=keyword,
        restrict_sr="on",
        sort=safe_sort,
        t=timeframe,
        limit=100,
    )


def collect_listing_pages(
    browser: RedditBrowser,
    *,
    start_url: str,
    source: dict[str, Any],
    source_type: str,
    cutoff_ts: float,
    max_pages: int,
    max_posts: int,
    matched_keyword: str = "",
    stop_when_old: bool,
    skip_stickied: bool,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    seen_posts: set[str] = set()
    seen_pages: set[str] = set()
    url: str | None = start_url
    page_number = 0

    while url and url not in seen_pages and (max_pages <= 0 or page_number < max_pages):
        seen_pages.add(url)
        page_number += 1
        page = browser.goto(url)
        browser.hydrate_listing()
        posts, next_url = parse_listing_html(
            page.content(), source, source_type, matched_keyword
        )

        if browser.is_modern_page():
            live_links = browser.extract_modern_listing_links()
            live_posts = parse_modern_listing_links(
                live_links, source, source_type, matched_keyword
            )
            merged_posts = {post["post_id"]: post for post in posts}
            for live_post in live_posts:
                existing = merged_posts.get(live_post["post_id"])
                if existing is None:
                    merged_posts[live_post["post_id"]] = live_post
                    continue
                if len(str(live_post.get("title", ""))) > len(
                    str(existing.get("title", ""))
                ):
                    existing["title"] = live_post["title"]
                for key in ("author", "created_utc", "created_iso"):
                    if not existing.get(key) and live_post.get(key):
                        existing[key] = live_post[key]
                existing["num_comments"] = max(
                    int(existing.get("num_comments", 0) or 0),
                    int(live_post.get("num_comments", 0) or 0),
                )
            posts = list(merged_posts.values())
            print(
                f"    modern DOM: {len(live_links)} post link(s), "
                f"{len(posts)} unique post(s)"
            )

        if not posts:
            print(
                "    [warn] listing loaded but no Reddit post permalinks were "
                "found; try --headed once to warm the persistent profile"
            )
            break

        page_has_recent = False
        added = 0
        for post in posts:
            created_utc = float(post.get("created_utc") or 0)
            if created_utc and created_utc < cutoff_ts:
                continue
            page_has_recent = True
            if skip_stickied and post.get("is_stickied"):
                continue
            if post["post_id"] in seen_posts:
                continue
            seen_posts.add(post["post_id"])
            collected.append(post)
            added += 1
            if max_posts > 0 and len(collected) >= max_posts:
                return collected

        print(f"    listing page {page_number}: {added} candidate(s)")
        if stop_when_old and not page_has_recent:
            break
        url = next_url

    return collected


def discover_source_posts(
    browser: RedditBrowser,
    source: dict[str, Any],
    keywords: list[str],
    config: AppConfig,
    *,
    days_override: int | None,
    max_posts_override: int | None,
) -> list[dict[str, Any]]:
    scraper = config.scraper
    source_type = str(source.get("type", "full_scrape"))
    subreddit = source_subreddit(source)
    max_posts = (
        scraper.max_posts_per_source
        if max_posts_override is None
        else max(0, max_posts_override)
    )

    if source_type == "keyword_search":
        days = (
            scraper.scrape_days_keyword_search
            if days_override is None
            else max(0, days_override)
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        merged: dict[str, dict[str, Any]] = {}

        for keyword in keywords:
            if max_posts > 0 and len(merged) >= max_posts:
                break
            print(f"  Searching r/{subreddit} for {keyword!r}")
            remaining = 0 if max_posts <= 0 else max_posts - len(merged)
            posts = collect_listing_pages(
                browser,
                start_url=search_url(
                    subreddit,
                    keyword,
                    scraper.keyword_search_sort,
                    scraper.timeframe,
                ),
                source=source,
                source_type=source_type,
                cutoff_ts=cutoff,
                max_pages=scraper.max_pages_per_source,
                max_posts=remaining,
                matched_keyword=keyword,
                stop_when_old=scraper.keyword_search_sort == "new",
                skip_stickied=scraper.skip_stickied,
            )
            for post in posts:
                existing = merged.get(post["post_id"])
                if existing:
                    old_terms = [
                        item.strip()
                        for item in str(existing.get("matched_keyword", "")).split(";")
                        if item.strip()
                    ]
                    if keyword not in old_terms:
                        existing["matched_keyword"] = "; ".join(old_terms + [keyword])
                else:
                    merged[post["post_id"]] = post
        values = list(merged.values())
        return values if max_posts <= 0 else values[:max_posts]

    days = scraper.scrape_days if days_override is None else max(0, days_override)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    print(f"  Reading r/{subreddit}/{scraper.full_scrape_sort}")
    return collect_listing_pages(
        browser,
        start_url=listing_url(
            subreddit, scraper.full_scrape_sort, scraper.timeframe
        ),
        source=source,
        source_type=source_type,
        cutoff_ts=cutoff,
        max_pages=scraper.max_pages_per_source,
        max_posts=max_posts,
        stop_when_old=scraper.full_scrape_sort == "new",
        skip_stickied=scraper.skip_stickied,
    )


def manual_candidate(post_url: str) -> dict[str, Any]:
    url = old_reddit_url(post_url)
    parts = [part for part in url.split("/") if part]
    post_id = ""
    try:
        comments_index = parts.index("comments")
        post_id = parts[comments_index + 1]
    except (ValueError, IndexError):
        pass
    if not post_id:
        raise ValueError("Could not find a Reddit post ID in --post-url")
    return {
        "post_id": post_id,
        "title": "",
        "subreddit": subreddit_from_url(url),
        "intake_mode": "manual",
        "matched_keyword": "",
        "author": "",
        "body": "",
        "score": 0,
        "num_comments": 0,
        "created_utc": "",
        "created_iso": "",
        "permalink": url,
    }


def fetch_complete_thread(
    browser: RedditBrowser,
    candidate: dict[str, Any],
    config: AppConfig,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scraper = config.scraper
    max_continue = config.browser.max_continue_pages_per_post
    deadline = (
        time.monotonic() + scraper.post_timeout_seconds
        if scraper.post_timeout_seconds > 0
        else None
    )
    root_url = with_query(
        old_reddit_url(candidate["permalink"]),
        limit=500,
        sort=scraper.comment_sort,
        show="all",
    )
    queue = [root_url]
    visited: set[str] = set()
    comments_by_id: dict[str, dict[str, Any]] = {}
    post_row: dict[str, Any] | None = None

    while queue and (max_continue <= 0 or len(visited) < max_continue + 1):
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        page = browser.goto(url, deadline=deadline)
        browser.hydrate_comments(deadline=deadline)
        if post_row is None:
            post_row = parse_post_html(page.content(), candidate)
            candidate.update(post_row)

        expanded = browser.expand_more_comments(deadline=deadline)
        browser.hydrate_comments(deadline=deadline)
        if expanded:
            print(f"      expanded {expanded} inline branch loader(s)")

        # Parse after all currently available inline branches have loaded.
        page_post = post_row or candidate
        rows, continuation_urls = parse_comments_html(
            page.content(),
            page_post,
            include_deleted=scraper.include_deleted_comments,
        )
        for row in rows:
            comments_by_id[row["comment_id"]] = row
            if (
                scraper.max_comments_per_post > 0
                and len(comments_by_id) >= scraper.max_comments_per_post
            ):
                break

        if (
            scraper.max_comments_per_post > 0
            and len(comments_by_id) >= scraper.max_comments_per_post
        ):
            print("      max_comments_per_post reached")
            break

        if len(visited) > 1 and len(visited) % 10 == 0:
            print(
                f"      continuation progress: {len(visited)} page(s), "
                f"{len(comments_by_id)} unique comment(s)"
            )

        for continuation in continuation_urls:
            normalized = with_query(
                continuation,
                limit=500,
                sort=scraper.comment_sort,
                show="all",
            )
            if normalized not in visited and normalized not in queue:
                queue.append(normalized)

    if queue:
        print(
            "      [warn] continuation-page cap reached; increase "
            "max_continue_pages_per_post for this thread"
        )

    post_row = post_row or parse_post_html("", candidate)
    comments = enrich_comment_context(
        list(comments_by_id.values()),
        post_id=post_row["post_id"],
        ancestor_limit=scraper.context_ancestor_limit,
    )
    post_row["fetched_comment_count"] = len(comments)
    return post_row, comments


def scrape_candidates(
    browser: RedditBrowser,
    candidates: list[dict[str, Any]],
    config: AppConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    posts: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []

    for index, candidate in enumerate(candidates, start=1):
        title = str(candidate.get("title") or candidate.get("permalink", ""))
        print(f"  ({index}/{len(candidates)}) {title[:100]}")
        try:
            post, thread_comments = fetch_complete_thread(browser, candidate, config)
        except ScraperError as error:
            print(f"      [error] {error}")
            continue
        posts.append(post)
        comments.extend(thread_comments)
        print(
            f"      {len(thread_comments)} visible comment(s) collected "
            f"across the expanded tree"
        )

    return posts, comments
