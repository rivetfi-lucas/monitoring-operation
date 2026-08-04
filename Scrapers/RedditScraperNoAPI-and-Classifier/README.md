# Reddit Scraper — Firefox Edition

A local Python scraper for monitoring configured subreddits, selecting posts, and exporting public post/comment data to CSV. It uses **Playwright Firefox**, requires **no Reddit login** and **no official Reddit API credentials**, and stores scraped IDs in SQLite.

## Default weekly workflow

A normal run is incremental:

1. Read the configured subreddit listings/searches.
2. Apply the date, keyword, comment-threshold, sort, and source filters.
3. Check each discovered `post_id` against SQLite.
4. **Skip posts that have already been scraped.**
5. Open only new posts, collect their visible comment trees, and append/upsert them into the master CSV files.

Previously scraped posts are not opened again unless repair mode or `--post-url` is used explicitly.

## Features

- `full_scrape` and `keyword_search` source types.
- Firefox headless by default; `--headed` for debugging.
- Old Reddit first, with automatic modern Reddit fallback.
- Full visible nested comments, continuation pages, parent IDs, ancestors, and context.
- Per-post CSV/SQLite checkpoints and resumable multi-thousand-post runs.
- Automatic Firefox recycling, post timeouts, retries, and stuck-loader detection.
- SQLite post/comment deduplication and run history.
- Optional targeted repair mode for selected post IDs or URLs.
- No Reddit account or official Reddit API credentials required.

## Project layout

```text
main.py
config.yaml
sources.yaml
keywords.yaml
repair_posts.example.txt
requirements.txt
reddit_scraper/
  browser.py
  config.py
  parsers.py
  pipeline.py
  storage.py
  utils.py
data/
  exports/
  runs/
  state/
  browser_profile/firefox/
```

Generated files:

- `data/exports/reddit_comments.csv` — master comment export keyed by `comment_id`.
- `data/exports/reddit_posts.csv` — master post export keyed by `post_id`.
- `data/runs/` — live run checkpoints, plus `failed_posts.csv`/`.txt` when needed.
- `data/state/scraper.sqlite3` — known IDs and run history.
- `data/browser_profile/firefox/` — isolated Firefox browser state.

## Windows setup

Run once from the project folder:

```bat
setup_windows.bat
```

Then run the normal incremental workflow:

```bat
run_windows.bat
```

The setup script recreates `.venv`, so it also fixes environments broken by moving or renaming the project folder.

## Linux setup

```bash
chmod +x setup_linux.sh run_linux.sh
./setup_linux.sh
./run_linux.sh
```

## Common commands

Run all configured sources and scrape only new posts:

```bat
run_windows.bat
```

Run one source and scrape only new posts:

```bat
run_windows.bat --source merval
```

Limit the run to two **new** posts after known IDs are skipped:

```bat
run_windows.bat --source merval --days 7 --max-posts 2
```

Preview only the new posts that would be opened:

```bat
run_windows.bat --source merval --days 7 --max-posts 5 --dry-run
```

Force-scrape one thread directly, even if it already exists:

```bat
run_windows.bat --post-url "https://www.reddit.com/r/OffGrid/comments/POST_ID/example/"
```

Show Firefox for debugging:

```bat
run_windows.bat --source merval --headed --days 1 --max-posts 2
```

Use a temporary browser context:

```bat
run_windows.bat --no-profile
```


## Long-running and resumable runs

The scraper is designed to handle thousands of posts without keeping the full dataset in memory:

1. One post is scraped at a time.
2. Its master CSV rows, run snapshot, and SQLite state are saved immediately.
3. The large in-memory comment/context list is released before the next post.
4. Firefox is recycled every configured number of posts to release browser memory.
5. A stuck thread is retried and then logged instead of freezing the entire run.

During a run, each successful post prints a checkpoint similar to:

```text
[checkpoint] saved post abc123; run total 816/2422, 152340 new comment(s)
```

If the terminal, VM, or process is interrupted, run the same command again. Completed posts are already in SQLite and will be skipped, so the run resumes with the remaining posts.

Failed threads are written to the current run folder:

```text
data/runs/<timestamp>_<run-id>/failed_posts.csv
data/runs/<timestamp>_<run-id>/failed_posts.txt
```

The `.txt` file can be passed directly to repair mode:

```bat
run_windows.bat --repair-posts "data\runs\<run-folder>\failed_posts.txt"
```

### Long-run safety configuration

```yaml
scraper:
  post_timeout_seconds: 900
  post_retries: 1

browser:
  restart_every_posts: 50
  max_no_progress_rounds: 5
  progress_log_every_clicks: 10
```

- `post_timeout_seconds: 0` disables the per-post deadline.
- `restart_every_posts: 0` keeps one Firefox session for the full run.
- Increasing the timeout may help unusually large threads.

## Targeted repair/update mode

Repair mode is opt-in. It re-scrapes only the posts listed in a text file and updates matching CSV/SQLite rows without creating duplicate IDs.

Copy the example file:

```bat
copy repair_posts.example.txt repair_posts.txt
```

Add one post ID or full Reddit URL per line:

```text
# Comments and blank lines are ignored.
1vb991e
https://www.reddit.com/r/OffGrid/comments/1vb991e/be_realstic_no_hate/
```

Run the repair:

```bat
run_windows.bat --repair-posts repair_posts.txt
```

Preview the repair list without scraping:

```bat
run_windows.bat --repair-posts repair_posts.txt --dry-run
```

For a bare post ID already known to SQLite, the stored permalink is reused. For an unknown bare ID, the scraper opens Reddit's generic `/comments/POST_ID/` route.

## Source configuration

### Full scrape

```yaml
sources:
  - name: merval
    subreddit: merval
    type: full_scrape
```

The scraper reads the configured listing, applies the filters, removes known post IDs, and opens only new posts.

### Keyword search

```yaml
sources:
  - name: example_search
    subreddit: example
    type: keyword_search
```

Search terms come from `keywords.yaml`. Matching posts are merged by `post_id`, then known posts are removed before comment scraping.

## Browser configuration

Firefox headless is the tested default:

```yaml
browser:
  headless: true
  persistent_profile: true
  prefer_old_reddit: true
  fallback_to_modern_reddit: true
```

Randomized timing is configurable:

```yaml
browser:
  request_delay_min_seconds: 2.0
  request_delay_max_seconds: 4.5
  expansion_delay_min_seconds: 0.9
  expansion_delay_max_seconds: 2.0
  scroll_delay_min_seconds: 0.8
  scroll_delay_max_seconds: 1.7
```

## CSV context fields

The comments export includes fields useful for an LLM/classification pipeline:

- `post_id`/`thread_id`, `comment_id`, and `parent_id`
- `root_comment_id`, `depth`, and ancestor IDs/text
- post title/body and comment body
- parent body and `context_text`
- author, timestamps, score when publicly visible, and permalinks

When Reddit displays `[score hidden]`, a usable comment score is not publicly available at scrape time.

## Deduplication behavior

### Normal source runs

- Known `post_id` values are skipped before their thread pages are opened.
- Only new posts are scraped.
- New posts/comments are added to SQLite and the master CSV files.
- `--max-posts` is applied **after** known posts are removed.

### Repair and direct-post runs

- `--repair-posts FILE` intentionally revisits only the listed posts.
- `--post-url URL` intentionally revisits one explicit post.
- Existing master rows are updated by `post_id`/`comment_id`; duplicate IDs are not appended.

Close CSV files in Excel before repair mode so Windows can replace the master files.

## Troubleshooting

### Firefox executable is missing

```bat
.venv\Scripts\python.exe -m playwright install firefox
```

Or rerun `setup_windows.bat`.

### Project folder was moved or renamed

Rerun `setup_windows.bat` to rebuild `.venv` with the current path.

### Browser profile is already in use

Close the other scraper process or use:

```bat
run_windows.bat --no-profile
```

### A run stopped or the VM restarted

Run the same command again. Every completed post was already checkpointed, and known post IDs will be skipped.

### One post times out or repeatedly fails

The run continues and writes that URL to `failed_posts.txt` in the run checkpoint folder. Retry it later with `--repair-posts`. You can increase `post_timeout_seconds` for exceptionally large threads.

### A known post was not updated

That is expected during a normal run. Add its ID or URL to `repair_posts.txt` and run:

```bat
run_windows.bat --repair-posts repair_posts.txt
```

## Limitations

The scraper can collect only content exposed to its public browser session. It cannot recover deleted, removed, private, quarantined, or otherwise inaccessible content. Reddit may change its markup or access behavior, so occasional maintenance may be required.
