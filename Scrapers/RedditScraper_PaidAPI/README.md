# Reddit Monitor — Chocodata + SocialFetch

This scraper uses:

- **Chocodata** to discover posts from configured subreddits and keyword searches.
- **Chocodata** for very small posts when the configured threshold allows it.
- **SocialFetch** to retrieve complete paginated comment trees, including nested reply branches.
- **SQLite** to track scraped post/comment IDs across runs.
- **CSV** files for downstream analysis and classification.

## Important workflow

Normal runs are incremental by default:

1. Discover posts from `sources.yaml`.
2. Apply date, keyword, and comment-count filters.
3. Remove post IDs already stored in SQLite.
4. Apply `max_posts_per_source` **after** known posts are removed.
5. Fetch and export only new posts/comments.

Previously scraped posts are not fetched again unless `--repair-posts` or `--no-dedup` is used explicitly. This prevents weekly runs from spending API credits on the full historical dataset.

## Files

- `reddit_scraper_hybrid.py` — main scraper.
- `config.yaml` — lookback windows, thresholds, API-call limits, and caps.
- `keywords.yaml` — terms used by `keyword_search` sources.
- `sources.yaml` — subreddit sources tagged `full_scrape` or `keyword_search`.
- `.env.example` — API-key template. Copy it to `.env` and insert your keys.
- `repair_posts.example.txt` — example targeted-repair input.
- `state/scraper_state.db` — created automatically; stores scraped IDs/history.
- `output/` — generated CSV files.

## Setup

### Windows

```bat
setup_windows.bat
```

The setup script creates `.venv`, installs dependencies, and copies `.env.example` to `.env` when needed.

Edit `.env`:

```env
CHOCODATA_API_KEY=your_chocodata_api_key
SOCIALFETCH_API_KEY=your_socialfetch_api_key
```

### Linux/macOS

```bash
chmod +x setup_linux.sh run_linux.sh
./setup_linux.sh
```

Then add both keys to `.env`.

## Normal incremental runs

Run every configured source:

```bat
run_windows.bat --all
```

Linux/macOS equivalent: `./run_linux.sh --all`.

Test one source with a small batch:

```bat
run_windows.bat --source merval --days 1 --max-posts 2 --max-sf-calls-total 30
```

`--max-posts 2` means **two new posts after known post IDs are skipped**. Already-scraped posts do not consume that limit.

Preview new posts without fetching comments:

```bat
run_windows.bat --source merval --days 7 --max-posts 10 --dry-run
```

## Targeted repair/update mode

Repair mode is opt-in. It re-fetches only the posts listed in a text file and refreshes their post/comment metadata in SQLite.

Copy the example:

```bat
copy repair_posts.example.txt repair_posts.txt
```

Add one bare post ID, Reddit fullname, or full post URL per line:

```text
# Blank lines and comments are ignored.
1vb991e
t3_abc123
https://www.reddit.com/r/OffGrid/comments/1vb991e/be_realstic_no_hate/
```

Run the targeted repair:

```bat
run_windows.bat --repair-posts repair_posts.txt
```

Validate the file without making API calls:

```bat
run_windows.bat --repair-posts repair_posts.txt --dry-run
```

Repair mode:

- skips subreddit/keyword discovery;
- uses SocialFetch for a complete targeted refresh;
- exports every fetched comment for those posts to `output/reddit_comments_repair_<timestamp>.csv`;
- upserts existing comment parent/depth/thread metadata in SQLite;
- updates the post's last-scraped metadata without duplicating IDs.

For a known bare ID, the stored permalink is reused. For an unknown bare ID, the generic Reddit route `https://www.reddit.com/comments/POST_ID/` is used.

## Output

Normal incremental run:

```text
output/reddit_comments_<UTC timestamp>.csv
```

Only newly discovered comments are included. If there are no new comments, no CSV is created.

Targeted repair run:

```text
output/reddit_comments_repair_<UTC timestamp>.csv
```

The repair CSV contains the complete fetched rows for only the requested posts, including previously known comment IDs, so the downstream dataset can upsert those records.

State database:

```text
state/scraper_state.db
```

Do not delete this database during normal use. It is what lets the scraper skip old posts before spending comment-fetch credits.

## Useful options

```text
--all                     Run every configured source.
--source NAME             Run one source from sources.yaml.
--days N                  Override the lookback window.
--max-posts N             Maximum NEW posts per source after known posts are removed.
--max-comments N          Cap flattened comments per post; 0 means unlimited.
--max-api-calls N         Cap SocialFetch pages per post.
--max-sf-calls-total N    Cap SocialFetch calls across the run.
--max-keywords N          Use only the first N configured keywords.
--dry-run                 Discover/validate without comment fetches or state changes.
--repair-posts FILE       Re-fetch only listed IDs/URLs and refresh their metadata.
--no-dedup                Ignore post/comment state and force normal reprocessing.
```

`--no-dedup` is retained for diagnostics and full reprocessing. For ordinary corrections, prefer `--repair-posts` because it limits cost and runtime to selected posts.

## Configuration notes

- `scrape_days` — lookback for `full_scrape` sources.
- `scrape_days_keyword_search` — separate lookback for keyword sources.
- `min_comments_threshold` — discard posts below this count.
- `max_posts_per_source` — cap on new posts after known IDs are removed.
- `max_comments_per_post` — cap after flattening; `0` is unlimited.
- `hybrid.chocodata_comment_threshold` — posts below this count may use Chocodata comments.
- `hybrid.max_api_calls_per_post` — SocialFetch pagination cap per post.
- `hybrid.max_sf_calls_total_per_run` — total SocialFetch call budget.

## Full-tree handling

The SocialFetch walker recursively processes every inline `replies.items` list and follows:

- top-level `page.nextCursor` pagination;
- nested `replies.page.nextCursor` pagination;
- grandchildren and deeper reply branches;
- duplicate comment/cursor protection.

## Tests

```bash
python -m unittest discover -s tests -v
```
