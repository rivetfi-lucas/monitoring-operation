"""
Reddit monitor — Chocodata for post discovery (and low-comment-count posts),
SocialFetch for full comment trees on everything else. Single
self-contained script (no cross-imports of reddit_scraper.py /
reddit_scraper_sf.py) combining the things already established in this
project:

  - Chocodata's /subreddit and /search listings paginate fine once you build
    the `after` cursor yourself — their own after_cursor field comes back
    null (confirmed in Chocodata's own reddit-subreddit-scraper repo), but
    Reddit's own listing cursor scheme (base64 of the last post's fullname)
    still works even though Chocodata never hands it back. See
    build_after_cursor(). Cheap, and good for discovering which posts exist.
  - Chocodata's /post endpoint has NO pagination mechanism for comments at
    all — confirmed live by passing after/comment_after/cursor/children/
    offset params and getting byte-identical responses every time — and it
    truncates to roughly the top ~13 comments per call regardless of sort
    (see config.yaml's comment_sort_passes). That ceiling is a non-issue for
    a post with fewer comments than the ceiling, though, so posts below
    hybrid.chocodata_comment_threshold (default 10) are fetched via
    Chocodata's /post instead of SocialFetch — one cheap call, no
    SocialFetch credit spent. See fetch_chocodata_comments().
  - SocialFetch's /posts/comments genuinely paginates every level of a
    comment tree: its cursor is an opaque token that fully encodes which
    branch it continues (confirmed live by decoding one), so recursively
    following every hasMore really does reach every comment. Used for every
    post at/above hybrid.chocodata_comment_threshold, where Chocodata's
    truncation would otherwise silently miss comments. See
    fetch_all_comments().

Three input files drive this, same as the other scraper scripts:
    config.yaml   — scraper settings (days back, thresholds, caps)
    keywords.yaml — search terms for "keyword_search" type sources
    sources.yaml  — the list of subreddits to scrape, each with a type

Cost model: Chocodata calls (discovery, plus the full comment fetch for
posts below hybrid.chocodata_comment_threshold) are the cheap part. Every
page of a post's comment tree via SocialFetch is a priced credit —
hybrid.max_api_calls_per_post (default 20) caps how many pages one post's
tree walk can spend, and hybrid.max_sf_calls_total_per_run (or
--max-sf-calls-total) additionally caps total SocialFetch spend across the
whole run — posts beyond that run budget are skipped with a clear log line,
not silently truncated.

Usage:
    python reddit_scraper_hybrid.py                          # interactive menu
    python reddit_scraper_hybrid.py --all
    python reddit_scraper_hybrid.py --source merval --days 1 --max-posts 2 --max-sf-calls-total 30

Output:
    output/reddit_comments_<UTC timestamp>.csv   (only created if there's new data)
    state/seen_comment_ids.txt                    (updated after every run)

Requires both CHOCODATA_API_KEY and SOCIALFETCH_API_KEY (env vars or .env).
"""
import argparse
import base64
import csv
import os
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CHOCODATA_BASE = "https://api.chocodata.com/api/v1/reddit"
SOCIALFETCH_BASE = "https://api.socialfetch.dev/v1/reddit"
REDDIT_BASE = "https://www.reddit.com"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
SEEN_IDS_PATH = os.path.join(STATE_DIR, "seen_comment_ids.txt")

COMMENT_FIELDS = [
    "comment_id", "thread_id", "thread_title", "subreddit", "intake_mode",
    "matched_keyword", "parent_id", "author", "body", "score", "depth",
    "created_utc", "created_iso", "permalink", "thread_permalink", "fetched_at",
]

# ---------- field-path constants ----------
# Posts come from Chocodata's listings; comments come from SocialFetch's
# /posts/comments. Each is a list of candidate dotted paths to try, in
# order — first found wins. See first_match()'s warn_label for a pointer to
# fix these if a real response doesn't match.
POST_ID_PATHS = ["post.id", "id"]
POST_TITLE_PATHS = ["post.title", "title"]
POST_SCORE_PATHS = ["post.score", "score"]
POST_NUM_COMMENTS_PATHS = ["post.num_comments", "num_comments"]
POST_PERMALINK_PATHS = ["post.permalink", "permalink"]
POST_CREATED_PATHS = ["post.created_utc", "post.created_at", "post.created", "created_utc", "created_at", "created"]

DISCOVERY_FIELDS = [
    "post_id", "subreddit", "source_type", "matched_keyword", "title",
    "score", "num_comments", "created_utc", "created_iso", "permalink",
]

COMMENT_ID_PATHS = ["id"]
COMMENT_AUTHOR_PATHS = ["author.username", "author"]
COMMENT_SCORE_PATHS = ["score", "upvotes"]
COMMENT_BODY_PATHS = ["text", "body"]
COMMENT_PERMALINK_PATHS = ["url", "permalink"]
COMMENT_CREATED_PATHS = ["createdUtc", "createdAt", "created_utc", "created_at", "created"]
COMMENT_PARENT_ID_PATHS = ["parentId", "parent_id"]
COMMENT_REPLIES_ITEMS_PATHS = ["replies.items"]
COMMENT_REPLIES_PAGE_PATHS = ["replies.page"]

# Chocodata's /post response, unlike SocialFetch's, isn't paginated at all —
# it's a nested comment tree returned in a single shot (just truncated, see
# fetch_chocodata_comments()). Unconfirmed against a live response since
# this path wasn't exercised before; check these against a real payload the
# first time chocodata_comment_threshold routes a post through it, same as
# the PATHS constants above.
CHOCODATA_COMMENTS_LIST_PATHS = ["comments"]
CHOCODATA_REPLIES_LIST_PATHS = ["replies"]


def dig(obj, dotted_path):
    current = obj
    for part in dotted_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def first_match(obj, candidate_paths, warn_label=None):
    for path in candidate_paths:
        value = dig(obj, path)
        if value is not None:
            return value
    if warn_label:
        print(f"  [warn] none of {candidate_paths} found for {warn_label} — check FIELD PATHS constants")
    return None


# ---------- small helpers ----------

def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen_ids(path=SEEN_IDS_PATH):
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_seen_ids(new_ids, path=SEEN_IDS_PATH):
    if not new_ids:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for cid in new_ids:
            f.write(cid + "\n")


def write_csv(rows, fieldnames):
    if not rows:
        return None
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"reddit_comments_{timestamp}.csv")
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(OUTPUT_DIR, f"reddit_comments_{timestamp}_{suffix}.csv")
        suffix += 1
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def parse_created(value):
    """Returns a unix epoch float from a 'created' field, which may arrive
    as a unix timestamp (int/float/numeric string) or an ISO 8601 string."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue

    try:
        normalized = s.replace("Z", "+00:00")
        if len(normalized) >= 5 and normalized[-5] in "+-" and normalized[-3] != ":":
            normalized = normalized[:-2] + ":" + normalized[-2:]
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        print(f"  [warn] couldn't parse created timestamp: {value!r}")
        return None


def to_iso(created_epoch):
    if not created_epoch:
        return ""
    try:
        return datetime.fromtimestamp(float(created_epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def clean(text):
    return (text or "").replace("\n", " ").strip()


def subreddit_name_from_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "r":
        return parts[1]
    return path


def to_fullname(post_id, kind="t3"):
    """Normalizes a post id to Reddit's fullname form (e.g. 'abc123' ->
    't3_abc123'). Chocodata's listing `id` field has been observed already
    in fullname form — this handles the bare-id case too."""
    s = str(post_id)
    prefix = f"{kind}_"
    return s if s.startswith(prefix) else prefix + s


def build_after_cursor(post_id, kind="t3"):
    """Chocodata's own after_cursor field comes back null, but /subreddit
    and /search are plain Reddit listings underneath, so Reddit's own
    cursor scheme — base64 of the last post's fullname — still works as
    the `after` param. See module docstring."""
    return base64.b64encode(to_fullname(post_id, kind=kind).encode("utf-8")).decode("ascii")


def to_full_reddit_url(candidate):
    """SocialFetch's /posts/comments needs a full URL. Chocodata's
    permalink field has been observed as either a full URL or a relative
    path — normalize either."""
    if not candidate:
        return None
    if candidate.startswith("http"):
        return candidate
    return REDDIT_BASE.rstrip("/") + "/" + candidate.lstrip("/")


def strip_fullname_prefix(reddit_id):
    if reddit_id and str(reddit_id)[:3] in ("t1_", "t3_"):
        return reddit_id[3:]
    return reddit_id


# ---------- Chocodata client (post discovery only) ----------

class ChocodataClient:
    def __init__(self, api_key, delay_seconds=1.0):
        self.api_key = api_key
        self.delay = delay_seconds
        self.session = requests.Session()
        self.total_requests = 0

    def _get(self, endpoint, params, retries=3):
        params = {**params, "api_key": self.api_key}
        url = f"{CHOCODATA_BASE}/{endpoint}"
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.exceptions.RequestException as e:
                print(f"  [warn] network error calling Chocodata {endpoint}: {e}")
                time.sleep(self.delay * (attempt + 1))
                continue

            if resp.status_code == 429:
                wait = self.delay * (2 ** (attempt + 1))
                print(f"  [429] Chocodata rate limit, backing off {wait:.0f}s...")
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                print(f"  [warn] Chocodata HTTP {resp.status_code} for {endpoint}: {resp.text[:200]}")
                time.sleep(self.delay)
                return None

            time.sleep(self.delay)
            try:
                data = resp.json()
            except ValueError:
                print(f"  [warn] Chocodata returned non-JSON for {endpoint}")
                return None
            self.total_requests += 1
            return data

    def subreddit_feed(self, subreddit, sort="new", limit=100, after=None):
        params = {"subreddit": subreddit, "sort": sort, "limit": limit}
        if after:
            params["after"] = after
        return self._get("subreddit", params)

    def search(self, query, subreddit=None, sort="new", limit=100, after=None):
        params = {"q": query, "sort": sort, "limit": limit}
        if subreddit:
            params["subreddit"] = subreddit
        if after:
            params["after"] = after
        return self._get("search", params)

    def post(self, post_url, sort="top"):
        """/post endpoint: no pagination (confirmed — see module
        docstring), truncates to roughly the top ~13 comments per call
        regardless of `sort`. Only called for posts under
        hybrid.chocodata_comment_threshold, where that ceiling isn't
        actually a limitation. Needs a full post URL — confirmed live
        that post_id alone 404s with "A subreddit is required for this
        request. Pass a full post URL, or add subreddit alongside
        post_id" — so this takes the same full permalink SocialFetch's
        post_comments() uses, not a bare id."""
        return self._get("post", {"url": post_url, "sort": sort})


# ---------- SocialFetch client (comment trees only) ----------

class SocialFetchClient:
    def __init__(self, api_key, delay_seconds=1.0):
        self.api_key = api_key
        self.delay = delay_seconds
        self.session = requests.Session()
        self.total_credits_charged = 0
        self.total_requests = 0

    def _get(self, endpoint, params, retries=3):
        headers = {"x-api-key": self.api_key}
        url = f"{SOCIALFETCH_BASE}/{endpoint}"
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
            except requests.exceptions.RequestException as e:
                print(f"  [warn] network error calling SocialFetch {endpoint}: {e}")
                time.sleep(self.delay * (attempt + 1))
                continue

            if resp.status_code == 429:
                wait = self.delay * (2 ** (attempt + 1))
                print(f"  [429] SocialFetch rate limit, backing off {wait:.0f}s...")
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                print(f"  [warn] SocialFetch HTTP {resp.status_code} for {endpoint}: {resp.text[:200]}")
                time.sleep(self.delay)
                return None

            time.sleep(self.delay)
            try:
                envelope = resp.json()
            except ValueError:
                print(f"  [warn] SocialFetch returned non-JSON for {endpoint}")
                return None

            self.total_requests += 1
            credits = dig(envelope, "meta.creditsCharged")
            if isinstance(credits, (int, float)):
                self.total_credits_charged += credits
            return envelope.get("data")

        return None

    def post_comments(self, post_url, cursor=None):
        params = {"url": post_url}
        if cursor:
            params["cursor"] = cursor
        return self._get("posts/comments", params)


# ---------- full comment-tree extraction (recursive cursor pagination) ----------

def fetch_all_comments(client, post_url, max_comments=None, max_api_calls=None):
    """Walks SocialFetch's comment pagination to depth-and-breadth-complete
    coverage. depth is tracked locally (not trusted from the API's own
    `depth` field, which was observed resetting to 0 on reply-branch pages).
    Stops once max_comments or max_api_calls is hit — pass 0/None for
    unbounded extraction. Returns (flat_list, post_detail, calls_made,
    truncated_by_cap)."""
    collected = []
    seen_ids = set()
    calls_made = 0
    truncated_by_cap = False
    post_detail = None

    queue = deque([(None, 0)])  # (cursor_or_None, depth)

    while queue:
        if max_comments and len(collected) >= max_comments:
            truncated_by_cap = True
            break
        if max_api_calls and calls_made >= max_api_calls:
            truncated_by_cap = True
            break

        cursor, depth = queue.popleft()
        data = client.post_comments(post_url, cursor=cursor)
        calls_made += 1
        if not data:
            continue
        if post_detail is None:
            post_detail = data.get("post")

        for item in data.get("comments", []) or []:
            cid = first_match(item, COMMENT_ID_PATHS)
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                collected.append({"comment": item, "depth": depth})

            replies_page = first_match(item, COMMENT_REPLIES_PAGE_PATHS) or {}
            if replies_page.get("hasMore") and replies_page.get("nextCursor"):
                queue.append((replies_page["nextCursor"], depth + 1))

            inline_replies = first_match(item, COMMENT_REPLIES_ITEMS_PATHS)
            if inline_replies:
                for reply in inline_replies:
                    rid = first_match(reply, COMMENT_ID_PATHS)
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        collected.append({"comment": reply, "depth": depth + 1})

        page = data.get("page") or {}
        if page.get("hasMore") and page.get("nextCursor"):
            queue.append((page["nextCursor"], depth))

    if max_comments:
        collected = collected[:max_comments]
    return collected, post_detail, calls_made, truncated_by_cap


def _flatten_chocodata_comments(items, depth, seen_ids, collected):
    for item in items or []:
        cid = first_match(item, COMMENT_ID_PATHS)
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            collected.append({"comment": item, "depth": depth})
        replies = first_match(item, CHOCODATA_REPLIES_LIST_PATHS)
        if replies:
            _flatten_chocodata_comments(replies, depth + 1, seen_ids, collected)


def fetch_chocodata_comments(client, post_url, sort_passes, max_comments=None, expected_total=None):
    """Cheap path for low-comment posts (below hybrid.chocodata_comment_
    threshold): Chocodata's /post has no real client-facing pagination,
    but its ~13-comment truncation (see config.yaml's comment_sort_passes)
    isn't a real limitation there, so one call is normally enough. Still
    walks comment_sort_passes and merges by id as a safety net for posts
    that turn out to run a little over — stops as soon as a pass reports
    _meta.truncated: false (confirmed live — that field is authoritative,
    more so than comparing against the post's own num_comments, which can
    be stale), adds nothing new, or expected_total is reached. Returns
    (flat_list, calls_made, truncated_below_expected)."""
    collected = []
    seen_ids = set()
    calls_made = 0
    meta_truncated = None

    for sort in sort_passes or ["top"]:
        data = client.post(post_url, sort=sort)
        calls_made += 1
        if not data:
            continue

        before = len(collected)
        comments = first_match(data, CHOCODATA_COMMENTS_LIST_PATHS, warn_label="chocodata comments list") or []
        _flatten_chocodata_comments(comments, 0, seen_ids, collected)
        added = len(collected) - before
        meta_truncated = dig(data, "_meta.truncated")

        if max_comments and len(collected) >= max_comments:
            break
        if meta_truncated is False:
            break
        if expected_total and len(collected) >= expected_total:
            break
        if added == 0 and calls_made > 1:
            break

    if meta_truncated is False:
        truncated = False
    else:
        truncated = bool(expected_total and len(collected) < expected_total)
    if max_comments:
        collected = collected[:max_comments]
    return collected, calls_made, truncated


# ---------- row builder ----------

def comment_row(flat_item, thread_id, thread_title, thread_permalink, subreddit, intake_mode, matched_keyword):
    c = flat_item["comment"]
    created = first_match(c, COMMENT_CREATED_PATHS)
    created_epoch = parse_created(created)
    return {
        "comment_id": first_match(c, COMMENT_ID_PATHS, warn_label="comment id") or "",
        "thread_id": thread_id,
        "thread_title": clean(thread_title),
        "subreddit": subreddit,
        "intake_mode": intake_mode,
        "matched_keyword": matched_keyword,
        "parent_id": first_match(c, COMMENT_PARENT_ID_PATHS) or "",
        "author": first_match(c, COMMENT_AUTHOR_PATHS) or "",
        "body": clean(first_match(c, COMMENT_BODY_PATHS)),
        "score": first_match(c, COMMENT_SCORE_PATHS) or "",
        "depth": flat_item["depth"],
        "created_utc": created_epoch or "",
        "created_iso": to_iso(created_epoch),
        "permalink": first_match(c, COMMENT_PERMALINK_PATHS) or "",
        "thread_permalink": thread_permalink,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------- candidate post collection (Chocodata, paginated) ----------

def collect_full_scrape_candidates(client, source, cfg, cutoff_ts):
    subreddit = subreddit_name_from_url(source["url"])
    posts = {}
    after = None
    page_size = cfg["page_size"]
    max_posts = cfg["max_posts_per_source"]
    max_pages = cfg.get("max_pages_per_source")
    page_num = 0

    while True:
        page_num += 1
        data = client.subreddit_feed(subreddit, sort=cfg["full_scrape_sort"], limit=page_size, after=after)
        if not data:
            break
        page_posts = data.get("posts", [])
        if not page_posts:
            break

        new_count = 0
        stop_early = False
        for p in page_posts:
            post_id = first_match(p, POST_ID_PATHS, warn_label="post id")
            if not post_id:
                continue
            created = first_match(p, POST_CREATED_PATHS)
            created_epoch = parse_created(created)
            if cutoff_ts and created_epoch and created_epoch < cutoff_ts:
                stop_early = True
                continue
            if post_id not in posts:
                new_count += 1
            posts.setdefault(post_id, (p, ""))
            if max_posts and len(posts) >= max_posts:
                break

        print(f"    page {page_num}: {len(page_posts)} posts, {new_count} new (total {len(posts)})")

        if stop_early or (max_posts and len(posts) >= max_posts):
            break
        if new_count == 0:
            print("    no new posts this page — stopping (end of feed or rejected cursor)")
            break
        if max_pages and page_num >= max_pages:
            print(f"    hit max_pages_per_source ({max_pages}) — stopping")
            break

        last_id = first_match(page_posts[-1], POST_ID_PATHS)
        if not last_id:
            break
        after = build_after_cursor(last_id)

    return posts


def normalize_for_match(text):
    """Lowercases and strips diacritics for a forgiving-but-strict compare —
    'Dólar cripto' and 'dolar cripto' should both match, but the words still
    have to appear together and in that order (a real substring), not just
    scattered anywhere in the text."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def title_matches_keyword(title, keyword):
    """Chocodata's /search is relevance-ranked, not literal — confirmed
    live: e.g. searching the single distinctive word 'tarjetear' returned
    12 results but only 2 actually contained that word anywhere, the rest
    were just topically related. Quoting a phrase (the usual 'exact phrase'
    trick) made zero difference in testing either — Chocodata doesn't seem
    to support it. So Chocodata's own ranking can't be trusted as a filter
    at all; this is the actual exact-match/whole-phrase check, applied
    client-side against the post title (search results don't include body
    text, so a match that exists only in a post's body, not its title,
    won't be caught here)."""
    return normalize_for_match(keyword) in normalize_for_match(title)


def collect_keyword_search_candidates(client, source, keywords, cfg, cutoff_ts):
    """One call per term, no pagination attempt — confirmed live that
    /search has none. Its envelope carries `_source: "rss"` and an
    `_rss_limitations` field: it's backed by Reddit's RSS/Atom search feed
    as a fallback surface, which is a flat, unpaginated, hard-capped list —
    confirmed both by total_results always reading 25 and by the server
    itself rejecting limit>25 ("Number must be less than or equal to 25").
    Building an `after` cursor the way /subreddit's pagination does (which
    genuinely works there) is a no-op here: page 2 comes back byte-identical
    to page 1 every time, since there's no cursor for the RSS surface to
    honor. So a second call would just be a guaranteed-wasted charge — this
    is a real, unavoidable ceiling of ~25 raw candidates per term per
    subreddit, not a bug in our own pagination guard."""
    subreddit = subreddit_name_from_url(source["url"])
    posts = {}
    limit = min(cfg["page_size"], 25)
    max_posts = cfg["max_posts_per_source"]

    for term in keywords:
        print(f"  searching '{term}'...")
        data = client.search(term, subreddit=subreddit, sort=cfg["keyword_search_sort"], limit=limit)
        if not data:
            continue
        results = data.get("results", [])

        matched_count = 0
        for p in results:
            post_id = first_match(p, POST_ID_PATHS, warn_label="post id")
            if not post_id:
                continue

            title = first_match(p, POST_TITLE_PATHS) or ""
            if not title_matches_keyword(title, term):
                continue

            created = first_match(p, POST_CREATED_PATHS)
            created_epoch = parse_created(created)
            if cutoff_ts and created_epoch and created_epoch < cutoff_ts:
                continue
            if post_id not in posts:
                matched_count += 1
            posts.setdefault(post_id, (p, term))

        print(f"    {len(results)} raw result(s) (of up to {data.get('total_results', '?')} total), "
              f"{matched_count} actually contain '{term}' (total matched {len(posts)})")

        if max_posts and len(posts) >= max_posts:
            break

    if max_posts and len(posts) > max_posts:
        posts = dict(list(posts.items())[:max_posts])

    return posts


# ---------- discovery mode (Chocodata only — no comment fetch) ----------

def write_discovery_csv(rows):
    if not rows:
        return None
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"discovery_posts_{timestamp}.csv")
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(OUTPUT_DIR, f"discovery_posts_{timestamp}_{suffix}.csv")
        suffix += 1
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DISCOVERY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def parse_index_selection(text, count):
    """Parses input like 'all', '3', '1,4,7', or '1-5,8,10-12' into a sorted
    list of valid 1-based indices within [1, count]."""
    text = text.strip().lower()
    if not text or text == "all":
        return list(range(1, count + 1))
    result = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                a, b = int(a), int(b)
                result.update(range(min(a, b), max(a, b) + 1))
            except ValueError:
                print(f"  [warn] couldn't parse range '{part}', skipping")
        else:
            try:
                result.add(int(part))
            except ValueError:
                print(f"  [warn] couldn't parse index '{part}', skipping")
    return sorted(i for i in result if 1 <= i <= count)


def run_discovery(config_path="config.yaml", keywords_path="keywords.yaml", sources_path="sources.yaml"):
    """Chocodata-only discovery: find candidate posts, let the user review
    and pick which ones matter, export just that post list to CSV. No
    comment fetching (Chocodata or SocialFetch) happens here at all — this
    is meant as a cheap first look before spending SocialFetch credits."""
    settings = load_yaml(config_path)["scraper"]
    keywords_data = load_yaml(keywords_path)
    sources_data = load_yaml(sources_path)["sources"]
    source_names = [s["name"] for s in sources_data]

    api_key = os.environ.get("CHOCODATA_API_KEY")
    if not api_key:
        print("ERROR: set the CHOCODATA_API_KEY environment variable (or put it in a .env file).")
        return

    all_keywords = []
    for category, terms in keywords_data.items():
        all_keywords.extend(terms or [])
    print(f"Loaded {len(all_keywords)} keyword(s) from {keywords_path}.")
    max_keywords_str = input(f"Max keywords to search, in order (blank = all {len(all_keywords)}): ").strip()
    if max_keywords_str:
        max_keywords = int(max_keywords_str)
        print(f"Using first {min(max_keywords, len(all_keywords))} of {len(all_keywords)} keyword(s).")
        all_keywords = all_keywords[:max_keywords]

    src = _prompt_source(source_names, allow_all=True)
    default_keyword_days = settings.get("scrape_days_keyword_search") or settings["scrape_days"]
    days_str = input(f"Days back for full_scrape sources (blank = config default {settings['scrape_days']}): ").strip()
    days_full = int(days_str) if days_str else settings["scrape_days"]
    keyword_days_str = input(f"Days back for keyword_search sources (blank = config default {default_keyword_days}): ").strip()
    days_keyword = int(keyword_days_str) if keyword_days_str else default_keyword_days
    posts_str = input(f"Max posts per source (blank = config default {settings['max_posts_per_source'] or 'unlimited'}): ").strip()
    max_posts = int(posts_str) if posts_str else settings["max_posts_per_source"]
    threshold_str = input(f"Min comments threshold (blank = config default {settings['min_comments_threshold']}): ").strip()
    min_comments_threshold = int(threshold_str) if threshold_str else settings["min_comments_threshold"]

    cfg = {
        "max_posts_per_source": max_posts,
        "page_size": settings["page_size"],
        "full_scrape_sort": settings["full_scrape_sort"],
        "keyword_search_sort": settings["keyword_search_sort"],
        "max_pages_per_source": settings.get("max_pages_per_source", 20),
    }
    cutoff_ts_full = None
    if days_full:
        cutoff_ts_full = (datetime.now(timezone.utc) - timedelta(days=days_full)).timestamp()
        print(f"\nScraping last {days_full} day(s) for full_scrape sources (since {to_iso(cutoff_ts_full)}).")
    cutoff_ts_keyword = None
    if days_keyword:
        cutoff_ts_keyword = (datetime.now(timezone.utc) - timedelta(days=days_keyword)).timestamp()
        print(f"Scraping last {days_keyword} day(s) for keyword_search sources (since {to_iso(cutoff_ts_keyword)}).")

    client = ChocodataClient(api_key, delay_seconds=settings["request_delay_seconds"])

    all_candidates = []
    for source in sources_data:
        if src and source["name"].lower() != src.lower():
            continue
        print(f"\n=== {source['name']} ({source['type']}) ===")
        if source["type"] == "full_scrape":
            candidates = collect_full_scrape_candidates(client, source, cfg, cutoff_ts_full)
        elif source["type"] == "keyword_search":
            candidates = collect_keyword_search_candidates(client, source, all_keywords, cfg, cutoff_ts_keyword)
        else:
            print(f"  [warn] unknown source type '{source['type']}', skipping")
            continue

        if min_comments_threshold:
            before = len(candidates)
            candidates = {
                pid: v for pid, v in candidates.items()
                if (first_match(v[0], POST_NUM_COMMENTS_PATHS) or 0) >= min_comments_threshold
            }
            print(f"  {before} candidates -> {len(candidates)} with >= {min_comments_threshold} comments")

        for post_id, (post, matched_keyword) in candidates.items():
            all_candidates.append({
                "post_id": post_id, "post": post,
                "source_name": source["name"], "source_type": source["type"],
                "matched_keyword": matched_keyword,
            })

    print(f"\nDiscovery done: {client.total_requests} Chocodata request(s), {len(all_candidates)} candidate post(s) found.")
    if not all_candidates:
        print("No candidates found — nothing to export.")
        return

    all_candidates.sort(key=lambda c: parse_created(first_match(c["post"], POST_CREATED_PATHS)) or 0, reverse=True)

    print("\nCandidate posts (newest first):")
    for i, c in enumerate(all_candidates, 1):
        title = first_match(c["post"], POST_TITLE_PATHS) or ""
        num_comments = first_match(c["post"], POST_NUM_COMMENTS_PATHS)
        created_iso = to_iso(parse_created(first_match(c["post"], POST_CREATED_PATHS)))
        print(f"  {i:>3}) [{c['source_name']}] {num_comments if num_comments is not None else '?':>4} comments  {created_iso[:10]}  {title[:70]!r}")

    selection = input("\nExport which posts? [all / comma-separated indices / ranges e.g. 1-5,8,10-12]: ").strip()
    selected_idx = parse_index_selection(selection, len(all_candidates))
    if not selected_idx:
        print("No valid selection — nothing exported.")
        return

    rows = []
    for i in selected_idx:
        c = all_candidates[i - 1]
        post = c["post"]
        created_epoch = parse_created(first_match(post, POST_CREATED_PATHS))
        rows.append({
            "post_id": c["post_id"],
            "subreddit": c["source_name"],
            "source_type": c["source_type"],
            "matched_keyword": c["matched_keyword"],
            "title": clean(first_match(post, POST_TITLE_PATHS)),
            "score": first_match(post, POST_SCORE_PATHS) or "",
            "num_comments": first_match(post, POST_NUM_COMMENTS_PATHS) or "",
            "created_utc": created_epoch or "",
            "created_iso": to_iso(created_epoch),
            "permalink": to_full_reddit_url(first_match(post, POST_PERMALINK_PATHS)) or "",
        })

    path = write_discovery_csv(rows)
    if path:
        print(f"\nExported {len(rows)} post(s) to:\n  {path}")
    else:
        print("\nNothing exported.")


# ---------- main run ----------

def run(config_path="config.yaml", keywords_path="keywords.yaml", sources_path="sources.yaml",
        only_source=None, days_override=None, max_posts_override=None, max_comments_override=None,
        max_api_calls_override=None, max_sf_calls_total_override=None, max_keywords_override=None,
        skip_dedup=False, dry_run=False):
    settings = load_yaml(config_path)["scraper"]
    hybrid_settings = load_yaml(config_path).get("hybrid", {})
    keywords_data = load_yaml(keywords_path)
    sources_data = load_yaml(sources_path)["sources"]

    choco_api_key = os.environ.get("CHOCODATA_API_KEY")
    sf_api_key = os.environ.get("SOCIALFETCH_API_KEY")
    if not choco_api_key:
        print("ERROR: set the CHOCODATA_API_KEY environment variable (or put it in a .env file).")
        return
    if not sf_api_key:
        print("ERROR: set the SOCIALFETCH_API_KEY environment variable (or put it in a .env file).")
        return

    cfg = {
        "scrape_days": days_override if days_override is not None else settings["scrape_days"],
        "scrape_days_keyword_search": days_override if days_override is not None else (
            settings.get("scrape_days_keyword_search") or settings["scrape_days"]
        ),
        "min_comments_threshold": settings["min_comments_threshold"],
        "max_posts_per_source": max_posts_override if max_posts_override is not None else settings["max_posts_per_source"],
        "max_comments_per_post": max_comments_override if max_comments_override is not None else settings["max_comments_per_post"],
        "page_size": settings["page_size"],
        "full_scrape_sort": settings["full_scrape_sort"],
        "keyword_search_sort": settings["keyword_search_sort"],
        "max_pages_per_source": settings.get("max_pages_per_source", 20),
        "max_api_calls_per_post": max_api_calls_override if max_api_calls_override is not None else hybrid_settings.get("max_api_calls_per_post", 20),
        "max_sf_calls_total_per_run": max_sf_calls_total_override if max_sf_calls_total_override is not None else hybrid_settings.get("max_sf_calls_total_per_run"),
        "chocodata_comment_threshold": hybrid_settings.get("chocodata_comment_threshold", 10),
        "comment_sort_passes": settings.get("comment_sort_passes") or ["top"],
    }

    all_keywords = []
    for category, terms in keywords_data.items():
        all_keywords.extend(terms or [])
    if max_keywords_override:
        print(f"Using first {min(max_keywords_override, len(all_keywords))} of {len(all_keywords)} keyword(s) "
              f"from {keywords_path} (--max-keywords {max_keywords_override}).")
        all_keywords = all_keywords[:max_keywords_override]

    choco_client = ChocodataClient(choco_api_key, delay_seconds=settings["request_delay_seconds"])
    sf_client = SocialFetchClient(sf_api_key, delay_seconds=settings["request_delay_seconds"])

    seen_ids = set() if skip_dedup else load_seen_ids()
    print(f"Loaded {len(seen_ids)} previously-exported comment IDs from state."
          + ("  [--no-dedup: ignoring for this run]" if skip_dedup else ""))

    cutoff_ts_full = None
    if cfg["scrape_days"]:
        cutoff_ts_full = (datetime.now(timezone.utc) - timedelta(days=cfg["scrape_days"])).timestamp()
        print(f"Scraping last {cfg['scrape_days']} day(s) for full_scrape sources (since {to_iso(cutoff_ts_full)}).")

    cutoff_ts_keyword = None
    if cfg["scrape_days_keyword_search"]:
        cutoff_ts_keyword = (datetime.now(timezone.utc) - timedelta(days=cfg["scrape_days_keyword_search"])).timestamp()
        print(f"Scraping last {cfg['scrape_days_keyword_search']} day(s) for keyword_search sources (since {to_iso(cutoff_ts_keyword)}).")

    if cfg["max_sf_calls_total_per_run"]:
        print(f"SocialFetch run budget: {cfg['max_sf_calls_total_per_run']} call(s) total.")

    all_new_rows = []
    new_ids_this_run = set()
    posts_skipped_budget = 0
    total_posts_via_chocodata = 0
    total_posts_via_socialfetch = 0

    for source in sources_data:
        name = source["name"]
        if only_source and name.lower() != only_source.lower():
            continue
        source_type = source["type"]
        print(f"\n=== {name} ({source_type}) ===")

        if source_type == "full_scrape":
            candidates = collect_full_scrape_candidates(choco_client, source, cfg, cutoff_ts_full)
        elif source_type == "keyword_search":
            candidates = collect_keyword_search_candidates(choco_client, source, all_keywords, cfg, cutoff_ts_keyword)
        else:
            print(f"  [warn] unknown source type '{source_type}', skipping")
            continue

        threshold = cfg["min_comments_threshold"]
        if threshold:
            before = len(candidates)
            candidates = {
                pid: v for pid, v in candidates.items()
                if (first_match(v[0], POST_NUM_COMMENTS_PATHS) or 0) >= threshold
            }
            print(f"  {before} candidates -> {len(candidates)} with >= {threshold} comments")

        sub_new_count = 0
        posts_via_chocodata = 0
        posts_via_socialfetch = 0
        for post_id, (post, matched_keyword) in candidates.items():
            title = first_match(post, POST_TITLE_PATHS) or ""
            permalink = to_full_reddit_url(first_match(post, POST_PERMALINK_PATHS))
            num_comments = first_match(post, POST_NUM_COMMENTS_PATHS)
            print(f"  fetching comments for {post_id} ({title[:60]!r})")
            if dry_run:
                continue
            if not permalink:
                print("    [warn] no permalink found for this post, skipping comments")
                continue

            threshold = cfg["chocodata_comment_threshold"]
            use_chocodata = bool(threshold) and num_comments is not None and num_comments < threshold

            flat = None
            if use_chocodata:
                flat, calls_made, truncated_by_cap = fetch_chocodata_comments(
                    choco_client, permalink, cfg["comment_sort_passes"],
                    max_comments=cfg["max_comments_per_post"], expected_total=num_comments,
                )
                if not flat and num_comments:
                    print(f"    [warn] Chocodata returned 0 comments for a post reporting {num_comments} — falling back to SocialFetch")
                    flat = None
                else:
                    posts_via_chocodata += 1
                    cap_note = " [fewer than expected — Chocodata truncation likely]" if truncated_by_cap else ""
                    print(f"    {len(flat)} comment(s) via Chocodata, {calls_made} call(s){cap_note}")

            if flat is None:
                budget_total = cfg["max_sf_calls_total_per_run"]
                per_post_cap = cfg["max_api_calls_per_post"]
                if budget_total:
                    remaining = budget_total - sf_client.total_requests
                    if remaining <= 0:
                        print(f"    [budget] SocialFetch run budget ({budget_total}) exhausted — skipping")
                        posts_skipped_budget += 1
                        continue
                    effective_cap = remaining if not per_post_cap else min(per_post_cap, remaining)
                else:
                    effective_cap = per_post_cap

                flat, _detail, calls_made, truncated_by_cap = fetch_all_comments(
                    sf_client, permalink,
                    max_comments=cfg["max_comments_per_post"], max_api_calls=effective_cap,
                )
                posts_via_socialfetch += 1
                cap_note = " [capped — more comments/replies remained]" if truncated_by_cap else ""
                print(f"    {len(flat)} comment(s) via SocialFetch, {calls_made} call(s){cap_note}")

            for item in flat:
                cid = first_match(item["comment"], COMMENT_ID_PATHS)
                if cid and cid not in seen_ids and cid not in new_ids_this_run:
                    all_new_rows.append(
                        comment_row(item, post_id, title, permalink, name, source_type, matched_keyword)
                    )
                    new_ids_this_run.add(cid)
                    sub_new_count += 1

        print(f"  -> {len(candidates)} candidate posts, {sub_new_count} new comments found"
              f" ({posts_via_chocodata} via Chocodata, {posts_via_socialfetch} via SocialFetch)")
        total_posts_via_chocodata += posts_via_chocodata
        total_posts_via_socialfetch += posts_via_socialfetch

    print(f"\nChocodata: {choco_client.total_requests} request(s) (discovery + comments).")
    print(f"SocialFetch: {sf_client.total_requests} request(s), {sf_client.total_credits_charged} credit(s) charged."
          + (f" {posts_skipped_budget} post(s) skipped (run budget exhausted)." if posts_skipped_budget else ""))
    print(f"Cost split: {total_posts_via_chocodata} post(s) via Chocodata (<{cfg['chocodata_comment_threshold']} comments), "
          f"{total_posts_via_socialfetch} post(s) via SocialFetch (>={cfg['chocodata_comment_threshold']} comments, or count unknown).")

    if dry_run:
        print("\n[--dry-run] Stopped before fetching comments — no CSV written, no state updated.")
        return

    csv_path = write_csv(all_new_rows, COMMENT_FIELDS)
    if not skip_dedup:
        append_seen_ids(new_ids_this_run)

    if csv_path:
        print(f"\nDone. {len(all_new_rows)} new comments written to:\n  {csv_path}")
    else:
        print("\nDone. No new comments since the last run — no file created.")


# ---------- interactive menu ----------

def interactive_menu(config_path="config.yaml", keywords_path="keywords.yaml", sources_path="sources.yaml"):
    sources_data = load_yaml(sources_path)["sources"]
    source_names = [s["name"] for s in sources_data]

    print("Reddit monitor (Chocodata discovery + SocialFetch comments) — interactive mode\n")
    print("  1) Full run — all sources, config.yaml settings")
    print("  2) Test one source — small batch (last 1 day, max 5 posts)")
    print("  3) Test one source — custom day window / post cap / comment cap")
    print("  4) Dry run — list candidate posts only, don't fetch comments or write CSV")
    print("  5) Discovery — Chocodata only: pick a subreddit/keywords, review candidate posts, export selection to CSV")
    print("  6) Quit")
    choice = input("\nChoice [1-6]: ").strip()

    if choice == "1":
        run(config_path, keywords_path, sources_path)
    elif choice == "2":
        src = _prompt_source(source_names)
        run(config_path, keywords_path, sources_path, only_source=src, days_override=1, max_posts_override=5)
    elif choice == "3":
        src = _prompt_source(source_names)
        days_str = input("Days back (blank = config default): ").strip()
        posts_str = input("Max posts (blank = config default): ").strip()
        comments_str = input("Max comments per post (blank = config default): ").strip()
        keywords_str = input("Max keywords to search, in order (blank = all): ").strip()
        run(
            config_path, keywords_path, sources_path,
            only_source=src,
            days_override=int(days_str) if days_str else None,
            max_posts_override=int(posts_str) if posts_str else None,
            max_comments_override=int(comments_str) if comments_str else None,
            max_keywords_override=int(keywords_str) if keywords_str else None,
        )
    elif choice == "4":
        src = _prompt_source(source_names, allow_all=True)
        run(config_path, keywords_path, sources_path, only_source=src, days_override=1, max_posts_override=5, dry_run=True)
    elif choice == "5":
        run_discovery(config_path, keywords_path, sources_path)
    else:
        print("Bye.")


def _prompt_source(names, allow_all=False):
    options = (["(all)"] if allow_all else []) + names
    for i, name in enumerate(options, 1):
        print(f"  {i}) {name}")
    choice = input(f"Source [1-{len(options)}]: ").strip()
    try:
        idx = int(choice) - 1
        picked = options[idx]
        return None if picked == "(all)" else picked
    except (ValueError, IndexError):
        print("Invalid choice, defaulting to first source.")
        return names[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reddit monitor (Chocodata discovery + SocialFetch full comment trees)")
    parser.add_argument("--all", action="store_true", help="Full run, all sources, no menu")
    parser.add_argument("--source", default=None, help="Run only this source")
    parser.add_argument("--days", type=int, default=None, help="Override scrape_days for this run")
    parser.add_argument("--max-posts", type=int, default=None, help="Override max_posts_per_source for this run")
    parser.add_argument("--max-comments", type=int, default=None, help="Override max_comments_per_post for this run")
    parser.add_argument("--max-api-calls", type=int, default=None, help="Override max_api_calls_per_post (SocialFetch pages per post, default 20)")
    parser.add_argument("--max-sf-calls-total", type=int, default=None, help="Cap total SocialFetch calls across the whole run")
    parser.add_argument("--max-keywords", type=int, default=None, help="Only search the first N keywords (in keywords.yaml's order) for keyword_search sources")
    parser.add_argument("--no-dedup", action="store_true", help="Ignore state/seen_comment_ids.txt for this run")
    parser.add_argument("--dry-run", action="store_true", help="List candidate posts only — no comment fetch, no CSV, no state update")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--keywords", default="keywords.yaml")
    parser.add_argument("--sources", default="sources.yaml")
    args = parser.parse_args()

    if not (args.all or args.source or args.days or args.max_posts or args.max_comments or args.max_api_calls or args.max_sf_calls_total or args.max_keywords or args.dry_run):
        interactive_menu(args.config, args.keywords, args.sources)
    else:
        run(
            args.config, args.keywords, args.sources,
            only_source=args.source,
            days_override=args.days,
            max_posts_override=args.max_posts,
            max_comments_override=args.max_comments,
            max_api_calls_override=args.max_api_calls,
            max_sf_calls_total_override=args.max_sf_calls_total,
            max_keywords_override=args.max_keywords,
            skip_dedup=args.no_dedup,
            dry_run=args.dry_run,
        )
