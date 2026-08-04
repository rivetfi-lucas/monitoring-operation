from __future__ import annotations

import argparse
import gc
import re
import sys
from pathlib import Path
from typing import Any

from reddit_scraper.browser import RedditBrowser, ScraperError
from reddit_scraper.config import load_app_config, load_yaml
from reddit_scraper.pipeline import (
    discover_source_posts,
    flatten_keywords,
    fetch_complete_thread,
    manual_candidate,
)
from reddit_scraper.storage import (
    IncrementalMasterWriter,
    RunCheckpointWriter,
    StateStore,
)

PROJECT_DIR = Path(__file__).resolve().parent
POST_ID_RE = re.compile(r"^[a-z0-9]+$", re.I)


def select_sources(
    sources: list[dict[str, Any]], source_name: str | None
) -> list[dict[str, Any]]:
    if not source_name:
        return sources
    selected = [
        source
        for source in sources
        if str(source.get("name", "")).casefold() == source_name.casefold()
    ]
    if not selected:
        available = ", ".join(str(item.get("name", "")) for item in sources)
        raise SystemExit(f"Unknown source {source_name!r}. Available: {available}")
    return selected


def resolve_project_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_DIR / path


def _configured_post_cap(args: argparse.Namespace, config: Any) -> int:
    if args.max_posts is not None:
        return max(0, int(args.max_posts))
    return max(0, int(config.scraper.max_posts_per_source))


def filter_new_candidates(
    candidates: list[dict[str, Any]],
    known_post_ids: set[str],
    *,
    max_posts: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Remove posts already stored in SQLite, then apply the requested cap.

    Applying the cap after deduplication is important: a limit of five means
    five new posts, not five listing entries that may all have been scraped.
    """

    new_candidates = [
        post
        for post in candidates
        if str(post.get("post_id", "")).strip() not in known_post_ids
    ]
    skipped = len(candidates) - len(new_candidates)
    if max_posts > 0:
        new_candidates = new_candidates[:max_posts]
    return new_candidates, skipped


def _repair_target_to_candidate(raw: str, state: StateStore) -> dict[str, Any]:
    target = raw.strip()
    if not target:
        raise ValueError("Repair target cannot be empty")

    if target.startswith(("http://", "https://")):
        candidate = manual_candidate(target)
    else:
        post_id = target.removeprefix("t3_").strip()
        if not POST_ID_RE.fullmatch(post_id):
            raise ValueError(
                f"Invalid repair target {raw!r}; expected a Reddit post ID or URL"
            )
        permalink = state.post_permalink(post_id)
        candidate = manual_candidate(
            permalink or f"https://www.reddit.com/comments/{post_id}/"
        )

    candidate["intake_mode"] = "repair"
    return candidate


def load_repair_candidates(path: Path, state: StateStore) -> list[dict[str, Any]]:
    """Load post IDs or URLs from a text file, ignoring blank/comment lines."""

    if not path.exists():
        raise FileNotFoundError(f"Repair input file not found: {path}")

    candidates_by_id: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            target = line.strip()
            if not target or target.startswith("#"):
                continue
            try:
                candidate = _repair_target_to_candidate(target, state)
            except ValueError as error:
                errors.append(f"line {line_number}: {error}")
                continue
            candidates_by_id[candidate["post_id"]] = candidate

    if errors:
        raise ValueError("Invalid repair input:\n  - " + "\n  - ".join(errors))
    if not candidates_by_id:
        raise ValueError(f"No post IDs or URLs were found in {path}")
    return list(candidates_by_id.values())


def _merge_candidate(
    candidates_by_id: dict[str, dict[str, Any]], post: dict[str, Any]
) -> None:
    existing = candidates_by_id.get(post["post_id"])
    if not existing:
        candidates_by_id[post["post_id"]] = post
        return

    existing_terms = {
        term.strip()
        for term in str(existing.get("matched_keyword", "")).split(";")
        if term.strip()
    }
    existing_terms.update(
        term.strip()
        for term in str(post.get("matched_keyword", "")).split(";")
        if term.strip()
    )
    existing["matched_keyword"] = "; ".join(sorted(existing_terms))


def _print_dry_run(candidates: list[dict[str, Any]], *, label: str) -> None:
    print(f"\n{len(candidates)} {label}:")
    for post in candidates:
        print(
            f"  - [{post.get('num_comments', 0)} comments] "
            f"{post.get('title', '')} — {post.get('permalink', '')}"
        )


def run(args: argparse.Namespace) -> int:
    config_path = resolve_project_path(args.config)
    config = load_app_config(config_path, PROJECT_DIR)

    headless_override: bool | None = None
    if args.headed:
        headless_override = False
    elif args.headless:
        headless_override = True
    persistent_override = False if args.no_profile else None

    selected_sources: list[dict[str, Any]] = []
    keywords: list[str] = []
    mode = "sources"

    if args.post_url:
        mode = "manual"
    elif args.repair_posts:
        mode = "repair"
    else:
        sources_doc = load_yaml(resolve_project_path(args.sources))
        keywords_doc = load_yaml(resolve_project_path(args.keywords))
        sources = list(sources_doc.get("sources") or [])
        if not sources:
            raise SystemExit("No sources were found in sources.yaml")
        selected_sources = select_sources(sources, args.source)
        keywords = flatten_keywords(keywords_doc)

    processed_count = 0
    failed_count = 0
    new_comment_count = 0
    refreshed_comment_count = 0
    skipped_known_posts = 0
    master_comments = master_posts = None
    snapshot_run_dir: Path | None = None
    run_id: str | None = None

    def make_browser() -> RedditBrowser:
        browser = RedditBrowser(
            config.browser,
            config.paths.profile_dir,
            headless_override=headless_override,
            persistent_override=persistent_override,
        )
        return browser.__enter__()

    browser: RedditBrowser | None = None

    try:
        with StateStore(config.paths.state_db) as state:
            known_post_ids = state.known_post_ids()

            if mode == "manual":
                candidates = [manual_candidate(args.post_url)]
                if args.dry_run:
                    print(f"Manual post: {candidates[0]['permalink']}")
                    return 0
            elif mode == "repair":
                repair_path = resolve_project_path(args.repair_posts)
                candidates = load_repair_candidates(repair_path, state)
                if args.dry_run:
                    _print_dry_run(candidates, label="repair target(s)")
                    return 0
            else:
                # Listing discovery gets its own short-lived Firefox session.
                # The comment-processing sessions are recycled separately.
                candidates_by_id: dict[str, dict[str, Any]] = {}
                configured_cap = _configured_post_cap(args, config)
                with RedditBrowser(
                    config.browser,
                    config.paths.profile_dir,
                    headless_override=headless_override,
                    persistent_override=persistent_override,
                ) as discovery_browser:
                    for source in selected_sources:
                        print(
                            f"\n[{source.get('name')}] "
                            f"{source.get('type', 'full_scrape')}"
                        )
                        try:
                            source_candidates = discover_source_posts(
                                discovery_browser,
                                source,
                                keywords,
                                config,
                                days_override=args.days,
                                max_posts_override=0,
                            )
                        except ScraperError as error:
                            print(f"  [error] skipping source: {error}")
                            continue

                        source_candidates = [
                            post
                            for post in source_candidates
                            if int(post.get("num_comments", 0) or 0)
                            >= config.scraper.min_comments_threshold
                        ]
                        new_source_candidates, skipped = filter_new_candidates(
                            source_candidates,
                            known_post_ids,
                            max_posts=configured_cap,
                        )
                        skipped_known_posts += skipped
                        print(
                            f"  {len(source_candidates)} post candidate(s) after filters; "
                            f"{skipped} already scraped; "
                            f"{len(new_source_candidates)} new post(s) selected"
                        )
                        for post in new_source_candidates:
                            _merge_candidate(candidates_by_id, post)

                candidates = list(candidates_by_id.values())
                if args.dry_run:
                    _print_dry_run(candidates, label="new post candidate(s)")
                    return 0

            source_count = len(selected_sources) if mode == "sources" else 1
            run_id = state.start_run(source_count)

            master_writer = (
                IncrementalMasterWriter(
                    comments_path=config.paths.comments_csv,
                    posts_path=config.paths.posts_csv,
                    update_existing=mode in {"repair", "manual"},
                )
                if config.output.append_master_csv
                else None
            )
            checkpoint_writer = (
                RunCheckpointWriter(config.paths.snapshots_dir, run_id)
                if config.output.create_run_snapshots
                else None
            )
            if checkpoint_writer:
                snapshot_run_dir = checkpoint_writer.run_dir

            restart_every = config.browser.restart_every_posts
            posts_in_browser = 0
            total = len(candidates)

            try:
                for index, candidate in enumerate(candidates, start=1):
                    title = str(candidate.get("title") or candidate.get("permalink", ""))
                    print(f"  ({index}/{total}) {title[:100]}")

                    if browser is None or (
                        restart_every > 0 and posts_in_browser >= restart_every
                    ):
                        if browser is not None:
                            print(
                                f"      [browser] recycling Firefox after "
                                f"{posts_in_browser} post(s)"
                            )
                            browser.__exit__(None, None, None)
                            browser = None
                            gc.collect()
                        browser = make_browser()
                        posts_in_browser = 0

                    post: dict[str, Any] | None = None
                    thread_comments: list[dict[str, Any]] = []
                    last_error: Exception | None = None
                    max_attempts = config.scraper.post_retries + 1

                    for attempt in range(1, max_attempts + 1):
                        try:
                            assert browser is not None
                            post, thread_comments = fetch_complete_thread(
                                browser, candidate, config
                            )
                            break
                        except KeyboardInterrupt:
                            raise
                        except Exception as error:
                            last_error = error
                            print(
                                f"      [error] attempt {attempt}/{max_attempts}: {error}"
                            )
                            if browser is not None:
                                browser.__exit__(None, None, None)
                                browser = None
                            gc.collect()
                            if attempt < max_attempts:
                                print("      [browser] restarting Firefox and retrying post")
                                browser = make_browser()
                                posts_in_browser = 0

                    posts_in_browser += 1

                    if post is None:
                        failed_count += 1
                        if checkpoint_writer:
                            checkpoint_writer.write_error(
                                candidate=candidate,
                                attempt=max_attempts,
                                error=last_error or "Unknown post failure",
                            )
                        print("      [skip] post failed; progress remains checkpointed")
                        continue

                    comment_ids = [
                        str(row.get("comment_id", ""))
                        for row in thread_comments
                        if row.get("comment_id")
                    ]
                    existing_ids = state.existing_comment_ids(comment_ids)
                    post_new_comments = len(set(comment_ids) - existing_ids)
                    post_refreshed = max(0, len(set(comment_ids)) - post_new_comments)

                    # Write files first. If a CSV is locked or disk write fails, the
                    # post is not marked complete in SQLite and can be retried safely.
                    if master_writer:
                        (
                            master_comment_path,
                            master_post_path,
                            _,
                            _,
                        ) = master_writer.write(post=post, comments=thread_comments)
                        master_comments = master_comment_path or master_comments
                        master_posts = master_post_path or master_posts

                    if checkpoint_writer:
                        checkpoint_writer.write_post(
                            post=post, comments=thread_comments
                        )

                    state.record([post], thread_comments)
                    processed_count += 1
                    new_comment_count += post_new_comments
                    refreshed_comment_count += post_refreshed
                    state.update_run_progress(
                        run_id,
                        post_count=processed_count,
                        new_comment_count=new_comment_count,
                    )

                    print(
                        f"      {len(thread_comments)} visible comment(s) collected "
                        "across the expanded tree"
                    )
                    print(
                        f"      [checkpoint] saved post {post.get('post_id', '')}; "
                        f"run total {processed_count}/{total}, "
                        f"{new_comment_count} new comment(s)"
                    )

                    # Drop the potentially large context strings before the next post.
                    thread_comments.clear()
                    del thread_comments
                    del post
                    if processed_count % 25 == 0:
                        gc.collect()

                status = "completed_with_errors" if failed_count else "completed"
                state.finish_run(
                    run_id,
                    status=status,
                    post_count=processed_count,
                    new_comment_count=new_comment_count,
                    error=(
                        f"{failed_count} post(s) failed; see run failed_posts.csv"
                        if failed_count
                        else None
                    ),
                )

            except KeyboardInterrupt:
                state.finish_run(
                    run_id,
                    status="interrupted",
                    post_count=processed_count,
                    new_comment_count=new_comment_count,
                    error="Interrupted by user",
                )
                print(
                    "\nRun interrupted. Every completed post was already saved; "
                    "run the same command to resume with remaining posts."
                )
                return 130
            except Exception as error:
                state.finish_run(
                    run_id,
                    status="failed",
                    post_count=processed_count,
                    new_comment_count=new_comment_count,
                    error=str(error),
                )
                raise
            finally:
                if browser is not None:
                    browser.__exit__(None, None, None)
                    browser = None
                gc.collect()

    except Exception as error:
        message = str(error)
        if "Executable doesn't exist" in message or "playwright install" in message:
            print(
                "Playwright Firefox is not installed. Run: "
                "python -m playwright install firefox",
                file=sys.stderr,
            )
            return 2
        if (
            "ProcessSingleton" in message
            or "user data directory is already in use" in message.casefold()
        ):
            print(
                "The persistent browser profile is already in use. Close the other "
                "scraper/browser process or run with --no-profile.",
                file=sys.stderr,
            )
            return 3
        raise

    print("\nRun complete")
    print(f"  Mode:            {mode}")
    if mode == "sources":
        print(f"  Known posts skipped: {skipped_known_posts}")
    print(f"  Posts processed: {processed_count}")
    print(f"  Posts failed:    {failed_count}")
    print(f"  New comments:    {new_comment_count}")
    if refreshed_comment_count and mode in {"repair", "manual"}:
        print(f"  Refreshed rows:  {refreshed_comment_count}")
    print(f"  SQLite state:    {config.paths.state_db}")
    if master_comments:
        print(f"  Comments CSV:    {master_comments}")
    elif config.output.append_master_csv:
        print("  Comments CSV:    no rows written")
    if master_posts:
        print(f"  Posts CSV:       {master_posts}")
    elif config.output.append_master_csv:
        print("  Posts CSV:       no rows written")
    if snapshot_run_dir:
        print(f"  Run checkpoints: {snapshot_run_dir}")
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Keyless Firefox/Playwright scraper for public Reddit posts and "
            "full visible comment trees"
        )
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--all", action="store_true", help="scrape every configured source (default)"
    )
    source_group.add_argument(
        "--source", help="scrape one sources.yaml entry by name"
    )
    source_group.add_argument(
        "--post-url",
        help="force-scrape one Reddit thread directly, even if already known",
    )
    source_group.add_argument(
        "--repair-posts",
        metavar="FILE",
        help=(
            "force-rescrape only the post IDs or URLs listed in a text file and "
            "update their existing CSV/SQLite rows"
        ),
    )

    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument(
        "--headed", action="store_true", help="show the Firefox window"
    )
    browser_group.add_argument(
        "--headless", action="store_true", help="force hidden browser mode"
    )

    parser.add_argument("--days", type=int, help="override source lookback days")
    parser.add_argument(
        "--max-posts",
        type=int,
        help="maximum NEW posts per source after known posts are skipped",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="discover/select posts without opening their comment trees",
    )
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="use a temporary browser context for this run",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--sources", default="sources.yaml")
    parser.add_argument("--keywords", default="keywords.yaml")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
