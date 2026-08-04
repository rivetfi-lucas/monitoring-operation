from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from .utils import (
    clean_text,
    modern_reddit_url,
    old_reddit_url,
    parse_int,
    parse_iso_datetime,
    strip_fullname,
    subreddit_from_url,
    to_iso,
    utc_now_iso,
)


def _body_text(node: Tag | None) -> str:
    if node is None:
        return ""
    return clean_text(node.get_text("\n", strip=True))


def _direct_child_with_class(node: Tag, class_name: str) -> Tag | None:
    for child in node.find_all("div", recursive=False):
        if class_name in (child.get("class") or []):
            return child
    return None


def _first_attr(node: Tag, *names: str) -> str:
    for name in names:
        value = node.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _first_node(node: Tag, *selectors: str) -> Tag | None:
    for selector in selectors:
        result = node.select_one(selector)
        if result is not None:
            return result
    return None


def _truthy_attr(node: Tag, *names: str) -> bool:
    for name in names:
        if not node.has_attr(name):
            continue
        value = str(node.get(name, "true")).strip().casefold()
        if value not in {"", "0", "false", "no", "none"}:
            return True
    return False


def source_subreddit(source: dict[str, Any]) -> str:
    return subreddit_from_url(str(source.get("url", ""))) or str(
        source.get("name", "")
    ).strip()


def _modern_post_id(node: Tag) -> str:
    raw = _first_attr(
        node,
        "thingid",
        "thing-id",
        "post-id",
        "id",
        "data-fullname",
    )
    return strip_fullname(raw)


def _modern_post_permalink(node: Tag) -> str:
    raw = _first_attr(node, "permalink", "content-href", "href")
    if not raw:
        anchor = node.select_one("a[href*='/comments/']")
        raw = str(anchor.get("href", "")) if anchor else ""
    return modern_reddit_url(raw)


def _modern_created(node: Tag) -> float | None:
    raw = _first_attr(
        node,
        "created-timestamp",
        "created_timestamp",
        "timestamp",
        "datetime",
    )
    if not raw:
        time_node = _first_node(
            node,
            "time[datetime]",
            "faceplate-timeago[ts]",
            "faceplate-timeago[datetime]",
        )
        if time_node:
            raw = _first_attr(time_node, "datetime", "ts")
    return parse_iso_datetime(raw)


def _parse_old_listing(
    soup: BeautifulSoup,
    source: dict[str, Any],
    source_type: str,
    matched_keyword: str,
) -> tuple[list[dict[str, Any]], str | None]:
    subreddit = source_subreddit(source)
    posts: list[dict[str, Any]] = []

    for thing in soup.select("div.thing.link[data-fullname]"):
        classes = set(thing.get("class") or [])
        if {"promoted", "promotedlink"} & classes:
            continue

        fullname = str(thing.get("data-fullname", ""))
        if not fullname.startswith("t3_"):
            continue

        title_node = thing.select_one("a.title")
        comments_node = thing.select_one("a.comments")
        score_node = thing.select_one(".score[title]")
        time_node = thing.select_one("time[datetime]")
        created_utc = parse_iso_datetime(
            str(time_node.get("datetime")) if time_node else None
        )
        permalink = thing.get("data-permalink") or (
            comments_node.get("href") if comments_node else ""
        )

        posts.append(
            {
                "post_id": strip_fullname(fullname),
                "title": clean_text(
                    title_node.get_text(" ", strip=True) if title_node else ""
                ),
                "subreddit": subreddit,
                "intake_mode": source_type,
                "matched_keyword": matched_keyword,
                "author": clean_text(str(thing.get("data-author", ""))),
                "body": "",
                "score": parse_int(
                    thing.get("data-score"),
                    parse_int(score_node.get("title") if score_node else 0),
                ),
                "num_comments": parse_int(
                    thing.get("data-comments-count")
                    or (
                        comments_node.get_text(" ", strip=True)
                        if comments_node
                        else "0"
                    )
                ),
                "created_utc": created_utc or "",
                "created_iso": to_iso(created_utc),
                "permalink": old_reddit_url(str(permalink or "")),
                "is_stickied": "stickied" in classes,
            }
        )

    next_node = soup.select_one("span.next-button a[href]")
    next_url = old_reddit_url(str(next_node.get("href"))) if next_node else None
    return posts, next_url


POST_PATH_RE = re.compile(
    r"/r/(?P<subreddit>[^/]+)/comments/(?P<post_id>[A-Za-z0-9]+)/?",
    flags=re.I,
)


def _candidate_title(*values: Any) -> str:
    rejected = re.compile(
        r"^(?:\d+[.,]?\d*\s*)?(?:comments?|replies|votes?|shares?)$",
        flags=re.I,
    )
    for value in values:
        text = clean_text(str(value or ""))
        if not text or rejected.fullmatch(text):
            continue
        if text.casefold() in {
            "open post",
            "view post",
            "share",
            "save",
            "more options",
        }:
            continue
        return text
    return ""


def parse_modern_listing_links(
    links: Iterable[dict[str, Any]],
    source: dict[str, Any],
    source_type: str,
    matched_keyword: str = "",
) -> list[dict[str, Any]]:
    """Build listing candidates from links extracted from Playwright's live DOM."""

    expected_subreddit = source_subreddit(source)
    expected_folded = expected_subreddit.casefold()
    posts_by_id: dict[str, dict[str, Any]] = {}

    for item in links:
        permalink = modern_reddit_url(str(item.get("href", "")))
        match = POST_PATH_RE.search(permalink)
        if match is None:
            continue

        subreddit = match.group("subreddit")
        post_id = strip_fullname(str(item.get("post_id", ""))) or match.group(
            "post_id"
        )
        if not post_id:
            continue

        # Listing pages can contain recommendation/sidebar links from unrelated
        # communities. Keep only the configured source subreddit.
        if expected_folded and subreddit.casefold() != expected_folded:
            continue

        title = _candidate_title(
            item.get("card_title"),
            item.get("aria_label"),
            item.get("title_attr"),
            item.get("text"),
        )
        created_utc = parse_iso_datetime(str(item.get("created", "")))
        stickied_value = str(item.get("stickied", "")).strip().casefold()
        card_text = clean_text(str(item.get("card_text", ""))).casefold()
        is_stickied = stickied_value not in {"", "0", "false", "no", "none"}
        if not is_stickied:
            is_stickied = "pinned by moderators" in card_text or "stickied post" in card_text

        row = {
            "post_id": post_id,
            "title": title,
            "subreddit": subreddit or expected_subreddit,
            "intake_mode": source_type,
            "matched_keyword": matched_keyword,
            "author": clean_text(str(item.get("author", ""))).removeprefix("u/"),
            "body": "",
            "score": 0,
            "num_comments": parse_int(item.get("comment_count", 0)),
            "created_utc": created_utc or "",
            "created_iso": to_iso(created_utc),
            "permalink": permalink,
            "is_stickied": is_stickied,
        }

        existing = posts_by_id.get(post_id)
        if existing is None:
            posts_by_id[post_id] = row
            continue

        # A listing usually links to the same post several times (title, image,
        # comment count). Keep the richest values from all matching anchors.
        if len(row["title"]) > len(str(existing.get("title", ""))):
            existing["title"] = row["title"]
        if not existing.get("author") and row["author"]:
            existing["author"] = row["author"]
        if not existing.get("created_utc") and row["created_utc"]:
            existing["created_utc"] = row["created_utc"]
            existing["created_iso"] = row["created_iso"]
        existing["num_comments"] = max(
            parse_int(existing.get("num_comments", 0)),
            parse_int(row.get("num_comments", 0)),
        )
        existing["is_stickied"] = bool(
            existing.get("is_stickied") or row.get("is_stickied")
        )

    return list(posts_by_id.values())


def _parse_modern_listing(
    soup: BeautifulSoup,
    source: dict[str, Any],
    source_type: str,
    matched_keyword: str,
) -> tuple[list[dict[str, Any]], None]:
    subreddit = source_subreddit(source)
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()

    for thing in soup.select("shreddit-post"):
        post_id = _modern_post_id(thing)
        permalink = _modern_post_permalink(thing)
        if not post_id and permalink:
            parts = [part for part in permalink.split("/") if part]
            try:
                post_id = parts[parts.index("comments") + 1]
            except (ValueError, IndexError):
                pass
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)

        if _truthy_attr(thing, "promoted", "is-promoted", "is_ad"):
            continue

        title = _first_attr(thing, "post-title", "title")
        if not title:
            title_node = _first_node(
                thing,
                "[slot='title']",
                "h1",
                "h2",
                "h3",
                "a[href*='/comments/']",
            )
            title = _body_text(title_node)

        created_utc = _modern_created(thing)
        author = _first_attr(thing, "author", "data-author")
        node_subreddit = _first_attr(
            thing, "subreddit-prefixed-name", "subreddit-name", "subreddit"
        )
        node_subreddit = node_subreddit.removeprefix("r/")

        posts.append(
            {
                "post_id": post_id,
                "title": clean_text(title),
                "subreddit": node_subreddit or subreddit,
                "intake_mode": source_type,
                "matched_keyword": matched_keyword,
                "author": clean_text(author),
                "body": "",
                "score": parse_int(
                    _first_attr(thing, "score", "data-score", "vote-count")
                ),
                "num_comments": parse_int(
                    _first_attr(
                        thing,
                        "comment-count",
                        "comment-count-number",
                        "comments-count",
                        "data-comments-count",
                    )
                ),
                "created_utc": created_utc or "",
                "created_iso": to_iso(created_utc),
                "permalink": permalink,
                "is_stickied": _truthy_attr(
                    thing, "is-stickied", "stickied", "is_stickied"
                ),
            }
        )

    # Modern Reddit listings use infinite scrolling rather than a next button.
    return posts, None


def parse_listing_html(
    html: str,
    source: dict[str, Any],
    source_type: str,
    matched_keyword: str = "",
) -> tuple[list[dict[str, Any]], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("shreddit-post") is not None:
        return _parse_modern_listing(
            soup, source, source_type, matched_keyword
        )

    old_posts, next_url = _parse_old_listing(
        soup, source, source_type, matched_keyword
    )
    if old_posts:
        return old_posts, next_url

    # Current Reddit layouts do not always expose <shreddit-post> in the
    # serialized page HTML. Fall back to stable /comments/ permalinks.
    html_links = [
        {
            "href": anchor.get("href", ""),
            "text": anchor.get_text(" ", strip=True),
            "aria_label": anchor.get("aria-label", ""),
            "title_attr": anchor.get("title", ""),
        }
        for anchor in soup.select("a[href*='/comments/']")
    ]
    return (
        parse_modern_listing_links(
            html_links, source, source_type, matched_keyword
        ),
        None,
    )


def _parse_old_post(
    soup: BeautifulSoup,
    candidate: dict[str, Any],
    fetched_at: str,
) -> dict[str, Any] | None:
    thing = soup.select_one("div.thing.link[data-fullname^='t3_']")
    if thing is None:
        return None

    title_node = thing.select_one("a.title")
    body_node = thing.select_one(".usertext-body .md")
    comments_node = thing.select_one("a.comments")
    score_node = thing.select_one(".score[title]")
    time_node = thing.select_one("time[datetime]")
    created_utc = parse_iso_datetime(
        str(time_node.get("datetime")) if time_node else None
    )

    return {
        "post_id": strip_fullname(
            str(thing.get("data-fullname") or candidate.get("post_id", ""))
        ),
        "title": clean_text(
            title_node.get_text(" ", strip=True)
            if title_node
            else str(candidate.get("title", ""))
        ),
        "subreddit": candidate.get("subreddit", "")
        or source_subreddit({"url": str(candidate.get("permalink", ""))}),
        "intake_mode": candidate.get("intake_mode", "manual"),
        "matched_keyword": candidate.get("matched_keyword", ""),
        "author": clean_text(
            str(thing.get("data-author", candidate.get("author", "")))
        ),
        "body": _body_text(body_node),
        "score": parse_int(
            thing.get("data-score"),
            parse_int(
                score_node.get("title") if score_node else candidate.get("score", 0)
            ),
        ),
        "num_comments": parse_int(
            thing.get("data-comments-count")
            or (
                comments_node.get_text(" ", strip=True)
                if comments_node
                else candidate.get("num_comments", 0)
            )
        ),
        "created_utc": (
            created_utc
            if created_utc is not None
            else candidate.get("created_utc", "")
        ),
        "created_iso": (
            to_iso(created_utc)
            if created_utc is not None
            else candidate.get("created_iso", "")
        ),
        "permalink": old_reddit_url(
            str(thing.get("data-permalink") or candidate.get("permalink", ""))
        ),
        "fetched_at": fetched_at,
    }


def _parse_modern_post(
    soup: BeautifulSoup,
    candidate: dict[str, Any],
    fetched_at: str,
) -> dict[str, Any] | None:
    things = soup.select("shreddit-post")
    if not things:
        return None

    candidate_id = str(candidate.get("post_id", ""))
    thing = next(
        (node for node in things if _modern_post_id(node) == candidate_id),
        things[0],
    )

    post_id = _modern_post_id(thing) or candidate_id
    title = _first_attr(thing, "post-title", "title")
    if not title:
        title = _body_text(
            _first_node(thing, "[slot='title']", "h1", "h2", "h3")
        )

    body_node = _first_node(
        thing,
        "[slot='text-body']",
        "[slot='post-body']",
        "div[data-post-click-location='text-body']",
        ".md",
    )
    created_utc = _modern_created(thing)
    permalink = _modern_post_permalink(thing) or modern_reddit_url(
        str(candidate.get("permalink", ""))
    )
    subreddit = _first_attr(
        thing, "subreddit-prefixed-name", "subreddit-name", "subreddit"
    ).removeprefix("r/")

    return {
        "post_id": post_id,
        "title": clean_text(title or str(candidate.get("title", ""))),
        "subreddit": subreddit
        or candidate.get("subreddit", "")
        or subreddit_from_url(permalink),
        "intake_mode": candidate.get("intake_mode", "manual"),
        "matched_keyword": candidate.get("matched_keyword", ""),
        "author": clean_text(
            _first_attr(thing, "author", "data-author")
            or str(candidate.get("author", ""))
        ),
        "body": _body_text(body_node) or str(candidate.get("body", "")),
        "score": parse_int(
            _first_attr(thing, "score", "data-score", "vote-count"),
            parse_int(candidate.get("score", 0)),
        ),
        "num_comments": parse_int(
            _first_attr(
                thing,
                "comment-count",
                "comment-count-number",
                "comments-count",
                "data-comments-count",
            ),
            parse_int(candidate.get("num_comments", 0)),
        ),
        "created_utc": (
            created_utc
            if created_utc is not None
            else candidate.get("created_utc", "")
        ),
        "created_iso": (
            to_iso(created_utc)
            if created_utc is not None
            else candidate.get("created_iso", "")
        ),
        "permalink": permalink,
        "fetched_at": fetched_at,
    }


def parse_post_html(html: str, candidate: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    fetched_at = utc_now_iso()

    parsed = _parse_modern_post(soup, candidate, fetched_at)
    if parsed is None:
        parsed = _parse_old_post(soup, candidate, fetched_at)
    if parsed is not None:
        return parsed

    return {
        **candidate,
        "body": candidate.get("body", ""),
        "fetched_at": fetched_at,
    }


def _comment_row(
    *,
    comment_id: str,
    post: dict[str, Any],
    parent_id: str,
    author: str,
    body: str,
    score: int,
    depth: int,
    created_utc: float | None,
    permalink: str,
    fetched_at: str,
) -> dict[str, Any]:
    return {
        "comment_id": comment_id,
        "thread_id": post["post_id"],
        "thread_title": post.get("title", ""),
        "thread_body": post.get("body", ""),
        "subreddit": post.get("subreddit", ""),
        "intake_mode": post.get("intake_mode", ""),
        "matched_keyword": post.get("matched_keyword", ""),
        "parent_id": parent_id,
        "root_comment_id": "",
        "ancestor_ids": "",
        "parent_body": "",
        "context_text": "",
        "author": clean_text(author) or "[deleted]",
        "body": body,
        "score": score,
        "depth": depth,
        "created_utc": created_utc or "",
        "created_iso": to_iso(created_utc),
        "permalink": permalink,
        "thread_permalink": post.get("permalink", ""),
        "fetched_at": fetched_at,
    }


def _nearest_old_comment_parent_id(thing: Tag) -> str:
    """Return the closest enclosing old-Reddit comment ID, when available."""

    for parent in thing.parents:
        if not isinstance(parent, Tag):
            continue
        classes = set(parent.get("class") or [])
        if "thing" not in classes or "comment" not in classes:
            continue
        parent_id = strip_fullname(str(parent.get("data-fullname", "")))
        if parent_id:
            return parent_id
    return ""


def _nearest_modern_comment_parent_id(thing: Tag) -> str:
    """Return the closest enclosing modern-Reddit comment ID, when available."""

    for parent in thing.parents:
        if not isinstance(parent, Tag) or parent.name != "shreddit-comment":
            continue
        parent_id = strip_fullname(
            _first_attr(parent, "thingid", "thing-id", "comment-id", "id")
        )
        if parent_id:
            return parent_id
    return ""


def _parent_from_depth_stack(
    depth_stack: dict[int, str],
    *,
    depth: int,
    post_id: str,
) -> str:
    """Infer a parent from document order when Reddit omits parent metadata."""

    if depth <= 0:
        return post_id
    return depth_stack.get(depth - 1, "")


def _update_depth_stack(
    depth_stack: dict[int, str],
    *,
    depth: int,
    comment_id: str,
) -> None:
    """Keep only the active ancestor chain for the next comment in DOM order."""

    for known_depth in list(depth_stack):
        if known_depth >= depth:
            depth_stack.pop(known_depth, None)
    depth_stack[depth] = comment_id


def _parse_old_comments(
    soup: BeautifulSoup,
    post: dict[str, Any],
    *,
    include_deleted: bool,
    fetched_at: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    depth_stack: dict[int, str] = {}
    post_id = str(post.get("post_id", ""))

    for thing in soup.select("div.thing.comment[data-fullname]"):
        fullname = str(thing.get("data-fullname", ""))
        if not fullname.startswith("t1_"):
            continue
        comment_id = strip_fullname(fullname)

        entry = _direct_child_with_class(thing, "entry")
        if entry is None:
            continue

        body = _body_text(entry.select_one(".usertext-body .md"))
        if not include_deleted and body.casefold() in {"", "[deleted]", "[removed]"}:
            continue

        time_node = entry.select_one("time[datetime]")
        created_utc = parse_iso_datetime(
            str(time_node.get("datetime")) if time_node else None
        )
        score_node = entry.select_one(".score[title]")
        author_node = entry.select_one("a.author")
        permalink_node = entry.select_one("a.bylink[href]")
        permalink = thing.get("data-permalink") or (
            permalink_node.get("href") if permalink_node else ""
        )
        depth_value = thing.get("data-depth")
        if depth_value is None:
            depth_value = sum(
                1
                for parent in thing.parents
                if isinstance(parent, Tag)
                and "comment" in (parent.get("class") or [])
            )
        depth = max(0, parse_int(depth_value))

        parent_id = strip_fullname(
            str(thing.get("data-parent-fullname", ""))
        )
        if not parent_id:
            parent_id = _nearest_old_comment_parent_id(thing)
        if not parent_id:
            parent_id = _parent_from_depth_stack(
                depth_stack,
                depth=depth,
                post_id=post_id,
            )
        if not parent_id and depth <= 0:
            parent_id = post_id

        rows.append(
            _comment_row(
                comment_id=comment_id,
                post=post,
                parent_id=parent_id,
                author=(
                    author_node.get_text(" ", strip=True)
                    if author_node
                    else "[deleted]"
                ),
                body=body,
                score=parse_int(score_node.get("title") if score_node else 0),
                depth=depth,
                created_utc=created_utc,
                permalink=old_reddit_url(str(permalink or "")),
                fetched_at=fetched_at,
            )
        )
        _update_depth_stack(
            depth_stack,
            depth=depth,
            comment_id=comment_id,
        )

    return rows


def _parse_modern_comments(
    soup: BeautifulSoup,
    post: dict[str, Any],
    *,
    include_deleted: bool,
    fetched_at: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    depth_stack: dict[int, str] = {}
    post_id = str(post.get("post_id", ""))

    for thing in soup.select("shreddit-comment"):
        comment_id = strip_fullname(
            _first_attr(thing, "thingid", "thing-id", "comment-id", "id")
        )
        if not comment_id or comment_id in seen:
            continue
        seen.add(comment_id)

        body_node = _first_node(
            thing,
            "[slot='comment']",
            "div[slot='comment']",
            "[data-testid='comment']",
            ".md",
        )
        body = _body_text(body_node)
        if not include_deleted and body.casefold() in {"", "[deleted]", "[removed]"}:
            continue

        depth = max(
            0,
            parse_int(
                _first_attr(thing, "depth", "data-depth", "nest-level")
            ),
        )

        parent_id = strip_fullname(
            _first_attr(
                thing,
                "parentid",
                "parent-id",
                "parent_id",
                "data-parent-fullname",
            )
        )
        if not parent_id:
            parent_id = _nearest_modern_comment_parent_id(thing)
        if not parent_id:
            parent_id = _parent_from_depth_stack(
                depth_stack,
                depth=depth,
                post_id=post_id,
            )
        if not parent_id and depth <= 0:
            parent_id = post_id

        permalink = _first_attr(thing, "permalink", "href")
        if not permalink:
            link = thing.select_one("a[href*='/comments/'][href*='/comment/']")
            permalink = str(link.get("href", "")) if link else ""

        rows.append(
            _comment_row(
                comment_id=comment_id,
                post=post,
                parent_id=parent_id,
                author=_first_attr(thing, "author", "data-author") or "[deleted]",
                body=body,
                score=parse_int(
                    _first_attr(thing, "score", "data-score", "vote-count")
                ),
                depth=depth,
                created_utc=_modern_created(thing),
                permalink=modern_reddit_url(permalink),
                fetched_at=fetched_at,
            )
        )
        _update_depth_stack(
            depth_stack,
            depth=depth,
            comment_id=comment_id,
        )

    return rows


def parse_comments_html(
    html: str,
    post: dict[str, Any],
    *,
    include_deleted: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    fetched_at = utc_now_iso()

    if soup.select_one("shreddit-comment") is not None:
        rows = _parse_modern_comments(
            soup,
            post,
            include_deleted=include_deleted,
            fetched_at=fetched_at,
        )
    else:
        rows = _parse_old_comments(
            soup,
            post,
            include_deleted=include_deleted,
            fetched_at=fetched_at,
        )

    continue_urls: list[str] = []
    for anchor in soup.select("a[href]"):
        text = clean_text(anchor.get_text(" ", strip=True)).casefold()
        if "continue this thread" in text:
            href = str(anchor.get("href", ""))
            continue_urls.append(
                modern_reddit_url(href)
                if soup.select_one("shreddit-comment") is not None
                else old_reddit_url(href)
            )

    return rows, list(dict.fromkeys(continue_urls))


def merge_comment_rows(groups: Iterable[Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for rows in groups:
        for row in rows:
            comment_id = str(row.get("comment_id", ""))
            if comment_id:
                merged[comment_id] = row
    return list(merged.values())


def enrich_comment_context(
    rows: list[dict[str, Any]],
    *,
    post_id: str,
    ancestor_limit: int,
) -> list[dict[str, Any]]:
    by_id = {str(row["comment_id"]): row for row in rows}

    def lineage(comment_id: str) -> list[str]:
        result: list[str] = []
        visited: set[str] = set()
        current = by_id.get(comment_id)
        while current:
            parent_id = str(current.get("parent_id", ""))
            if not parent_id or parent_id == post_id or parent_id in visited:
                break
            parent = by_id.get(parent_id)
            if parent is None:
                break
            visited.add(parent_id)
            result.append(parent_id)
            current = parent
        result.reverse()
        return result

    for row in rows:
        comment_id = str(row["comment_id"])
        ancestors = lineage(comment_id)
        parent_id = str(row.get("parent_id", ""))
        parent = by_id.get(parent_id)

        if ancestors:
            row["depth"] = len(ancestors)
            row["root_comment_id"] = ancestors[0]
        else:
            row["depth"] = (
                0 if parent_id == post_id else int(row.get("depth", 0) or 0)
            )
            row["root_comment_id"] = comment_id

        row["ancestor_ids"] = ">".join(ancestors)
        row["parent_body"] = str(parent.get("body", "")) if parent else ""

        context_ids = ancestors[-ancestor_limit:] if ancestor_limit > 0 else []
        context_parts = [
            f"{by_id[item].get('author', '[deleted]')}: {by_id[item].get('body', '')}"
            for item in context_ids
            if item in by_id
        ]
        row["context_text"] = "\n".join(context_parts)

    rows.sort(
        key=lambda row: (
            float(row.get("created_utc", 0) or 0),
            int(row.get("depth", 0) or 0),
            str(row.get("comment_id", "")),
        )
    )
    return rows
