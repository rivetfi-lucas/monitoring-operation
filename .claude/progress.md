# Progress Log

Running log of work across sessions, newest entry on top. Purpose: pick up
where a previous session left off without re-deriving context from scratch.

---

## 2026-07-26/27 — Reddit hybrid scraper: Chocodata/SocialFetch cost split

**Task:** `Scrapers/Reddit/reddit_scraper_hybrid.py` was calling SocialFetch
(paid, paginated) for every post's comments regardless of size. Implemented
cost optimization: posts below a comment-count threshold now fetch via
Chocodata's `/post` endpoint (cheap, one call) instead; only posts at/above
the threshold use SocialFetch's real pagination.

**Files changed:**
- `Scrapers/Reddit/reddit_scraper_hybrid.py` — core logic
- `Scrapers/Reddit/config.yaml` — new `hybrid.chocodata_comment_threshold` (default 10)
- `Scrapers/Reddit/README.md` — docs updated to match

**Key additions in the script:**
- `ChocodataClient.post(post_url, sort="top")` — hits `/post`. **Must pass a
  full post URL**, not a bare post_id (confirmed live: post_id alone 404s
  with "A subreddit is required for this request").
- `fetch_chocodata_comments(client, post_url, sort_passes, max_comments, expected_total)`
  — walks `comment_sort_passes` (existing config knob, previously unused by
  the hybrid script), merges by comment id, stops early once
  `_meta.truncated: false` comes back (authoritative — more reliable than
  comparing against the post's own `num_comments`, which can be stale) or a
  pass adds nothing new.
- New field-path constants: `CHOCODATA_COMMENTS_LIST_PATHS = ["comments"]`,
  `CHOCODATA_REPLIES_LIST_PATHS = ["replies"]`. `COMMENT_CREATED_PATHS` got
  `"created"` added (Chocodata's actual field name — differs from
  SocialFetch's `createdUtc`).
- Main loop in `run()`: branches per post on
  `num_comments < cfg["chocodata_comment_threshold"]`. Falls back to
  SocialFetch if Chocodata returns 0 comments for a post that reports
  having some (safety net against a bad/empty response).

**Confirmed live (real API keys, real Chocodata `/post` response shape):**
```json
{
  "post": {"id": "t3_...", "title": "...", "author": {"username": "...", "id": "..."}, ...},
  "comments": [
    {"id": "t1_...", "parent_id": null, "depth": 0, "score": 1,
     "author": {"username": "..."}, "body": "...", "created": "2026-07-26T21:07:47.239000+0000",
     "permalink": "...", "replies": [ ... nested, same shape ... ]}
  ],
  "comments_returned": 3,
  "_meta": {"source": "svc-comments+post-page", "sort": "top", "pages_fetched": 2, "truncated": false}
}
```
Verified via live test runs: sub-threshold post → 1 Chocodata call, all
CSV fields populated correctly (author, body, score, depth, parent_id,
created_utc/created_iso). Multi-post batch → correctly split between
Chocodata (<10 comments) and SocialFetch (>=10, with proper pagination
capped by `--max-api-calls`).

**Known edge case, not yet resolved:** one live post reported >10 comments
in Chocodata's post metadata but only 6 came back even after retrying all
`comment_sort_passes` — logged as
`[fewer than expected — Chocodata truncation likely]` rather than silently
dropped, but currently does **not** escalate to SocialFetch in that case.
Open question for next session / user: should
`fetch_chocodata_comments` returning fewer than `expected_total` after
exhausting all sort passes trigger an automatic SocialFetch fallback (costs
a credit, guarantees completeness), or is the current log-and-keep-partial
behavior the right tradeoff? Leaning toward leaving as-is unless the user
asks, since escalating automatically would reintroduce the SocialFetch cost
this change was meant to avoid — but flagging since it means sub-threshold
posts aren't 100% guaranteed complete the way over-threshold posts are.

**Side effects from testing (disclosed to user at the time):** live test
runs against the user's real API keys spent ~10 real SocialFetch credits
and left two real (non-fixture) CSVs in `Scrapers/Reddit/output/`, plus
updated `Scrapers/Reddit/state/seen_comment_ids.txt` with real comment ids
from those runs.

**Status:** Done and verified. No open TODO unless the fallback-on-partial-
truncation question above gets revisited.
