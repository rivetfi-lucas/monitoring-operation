# Reddit Monitor

Scrapes Reddit — Chocodata for post discovery, SocialFetch for full comment
trees — and writes new comments to CSV, with cross-run dedup.

## Files

- `reddit_scraper_hybrid.py` — **the active scraper.** Uses Chocodata to find candidate posts (cheap). Posts with fewer comments than `hybrid.chocodata_comment_threshold` (default 10) get their comments pulled straight from Chocodata's `/post` endpoint too — its per-call truncation (~13 comments) isn't a real limit at that size, so it's a free win. Posts at or above that threshold go to SocialFetch instead, since only SocialFetch can genuinely paginate a full comment tree.
- `config.yaml` — scrape settings (days back, comment threshold, post/comment caps, sort orders)
- `keywords.yaml` — search terms for `keyword_search` type sources
- `sources.yaml` — subreddits to scrape, each tagged `full_scrape` or `keyword_search`
- `.env` — your API keys (`CHOCODATA_API_KEY` and `SOCIALFETCH_API_KEY`)
- `output/reddit_scraper.py`, `output/reddit_scraper_sf.py` — earlier single-backend variants (Chocodata-only, and SocialFetch-only). Kept for reference; not part of the active pipeline.

## First-time setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in CHOCODATA_API_KEY and SOCIALFETCH_API_KEY
```

`sources.yaml` and `keywords.yaml` already have real entries — edit them directly if you want to add/remove subreddits or search terms.

## First real run — start small

```bash
python reddit_scraper_hybrid.py --source merval --days 1 --max-posts 2 --max-sf-calls-total 30
```

This limits the run to one subreddit, the last 24h, 2 posts, and 30 total SocialFetch calls — cheap, fast, and enough to confirm everything's wired correctly (both API keys, correct subreddit names, output looks right) before spending more credits.

Check `output/` for the resulting CSV. If it looks right:

```bash
python reddit_scraper_hybrid.py --all
```

Runs every source in `sources.yaml` with your normal `config.yaml` settings.

## Other useful runs

```bash
python reddit_scraper_hybrid.py                              # interactive menu (no flags needed)
python reddit_scraper_hybrid.py --dry-run --source X          # list candidate posts, don't spend SocialFetch credits
python reddit_scraper_hybrid.py --source X --days 7           # override days-back for one run (both source types)
python reddit_scraper_hybrid.py --max-sf-calls-total 200      # cap total SocialFetch spend for the run
```

The interactive menu (run with no flags) also has a **Discovery** mode — Chocodata-only, no SocialFetch credits spent — that lists candidate posts for a subreddit/keyword set so you can eyeball them before committing to a full comment-tree pull.

## Config knobs (`config.yaml`)

- `scrape_days` — lookback window for `full_scrape` sources (default 7 days).
- `scrape_days_keyword_search` — separate, usually longer, lookback for `keyword_search` sources (default 30 days), since keyword matches turn up far less often than a subreddit's full feed. Falls back to `scrape_days` if unset.
- `min_comments_threshold` — drop posts with fewer comments than this.
- `max_posts_per_source` / `max_comments_per_post` — 0 = unlimited.
- `full_scrape_sort` / `keyword_search_sort` — Reddit sort requested from Chocodata per source type.
- `hybrid.chocodata_comment_threshold` — posts with fewer comments than this (default 10) are fetched via Chocodata's `/post` instead of SocialFetch, saving a SocialFetch credit per post.
- `hybrid.max_api_calls_per_post` — cap on SocialFetch pages walked per post (default 20); the rest of that post's tree is left unfetched, not silently truncated in a misleading way — it's logged.
- `hybrid.max_sf_calls_total_per_run` — cap on total SocialFetch calls for the whole run; posts beyond the budget are skipped and logged. `null` = unlimited.

See the comments in `config.yaml` for the full list, including Chocodata-specific pagination/backoff settings.

## Output

- `output/reddit_comments_<UTC timestamp>.csv` — new file each run, only new comments
- `state/seen_comment_ids.txt` — dedup ledger; don't delete unless you want a full re-scrape

Both folders are created automatically on first run.
