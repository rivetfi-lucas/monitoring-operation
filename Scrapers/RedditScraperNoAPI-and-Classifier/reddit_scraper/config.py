from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class PathsConfig:
    data_dir: Path
    comments_csv: Path
    posts_csv: Path
    state_db: Path
    profile_dir: Path
    snapshots_dir: Path


@dataclass(slots=True)
class BrowserConfig:
    headless: bool = True
    persistent_profile: bool = True
    timeout_seconds: float = 45.0
    navigation_retries: int = 3

    # Randomized human-like pacing. The older single-value settings are still
    # accepted by load_app_config for backwards compatibility.
    request_delay_min_seconds: float = 1.8
    request_delay_max_seconds: float = 3.8
    expansion_delay_min_seconds: float = 0.8
    expansion_delay_max_seconds: float = 1.8
    scroll_delay_min_seconds: float = 0.7
    scroll_delay_max_seconds: float = 1.5

    max_listing_scrolls: int = 18
    max_comment_scrolls: int = 24
    scroll_step_pixels: int = 900
    scroll_stable_rounds: int = 3

    max_more_clicks_per_post: int = 750
    max_continue_pages_per_post: int = 150
    max_no_progress_rounds: int = 5
    progress_log_every_clicks: int = 10
    restart_every_posts: int = 50
    slow_mo_ms: int = 0
    block_images: bool = True
    user_agent: str | None = None

    prefer_old_reddit: bool = True
    fallback_to_modern_reddit: bool = True


@dataclass(slots=True)
class ScraperConfig:
    scrape_days: int = 7
    scrape_days_keyword_search: int = 30
    min_comments_threshold: int = 0
    max_posts_per_source: int = 0
    max_comments_per_post: int = 0
    max_pages_per_source: int = 10
    full_scrape_sort: str = "new"
    keyword_search_sort: str = "new"
    timeframe: str = "all"
    comment_sort: str = "old"
    skip_stickied: bool = True
    include_deleted_comments: bool = False
    context_ancestor_limit: int = 8
    post_timeout_seconds: float = 900.0
    post_retries: int = 1


@dataclass(slots=True)
class OutputConfig:
    append_master_csv: bool = True
    create_run_snapshots: bool = True


@dataclass(slots=True)
class AppConfig:
    paths: PathsConfig
    browser: BrowserConfig
    scraper: ScraperConfig
    output: OutputConfig


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    return value if isinstance(value, dict) else {}


def _resolve(base_dir: Path, raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _delay_range(
    doc: dict[str, Any],
    *,
    min_key: str,
    max_key: str,
    legacy_key: str,
    default_min: float,
    default_max: float,
) -> tuple[float, float]:
    legacy = doc.get(legacy_key)
    minimum = doc.get(min_key, legacy if legacy is not None else default_min)
    maximum = doc.get(max_key, legacy if legacy is not None else default_max)
    low = max(0.0, float(minimum))
    high = max(low, float(maximum))
    return low, high


def load_app_config(config_path: Path, project_dir: Path) -> AppConfig:
    doc = load_yaml(config_path)
    paths_doc = doc.get("paths") or {}
    browser_doc = doc.get("browser") or {}
    scraper_doc = doc.get("scraper") or {}
    output_doc = doc.get("output") or {}

    data_dir = _resolve(project_dir, paths_doc.get("data_dir", "data"))
    paths = PathsConfig(
        data_dir=data_dir,
        comments_csv=_resolve(
            project_dir,
            paths_doc.get("comments_csv", data_dir / "exports" / "reddit_comments.csv"),
        ),
        posts_csv=_resolve(
            project_dir,
            paths_doc.get("posts_csv", data_dir / "exports" / "reddit_posts.csv"),
        ),
        state_db=_resolve(
            project_dir,
            paths_doc.get("state_db", data_dir / "state" / "scraper.sqlite3"),
        ),
        profile_dir=_resolve(
            project_dir,
            paths_doc.get("profile_dir", data_dir / "browser_profile"),
        ),
        snapshots_dir=_resolve(
            project_dir,
            paths_doc.get("snapshots_dir", data_dir / "runs"),
        ),
    )

    request_min, request_max = _delay_range(
        browser_doc,
        min_key="request_delay_min_seconds",
        max_key="request_delay_max_seconds",
        legacy_key="request_delay_seconds",
        default_min=1.8,
        default_max=3.8,
    )
    expansion_min, expansion_max = _delay_range(
        browser_doc,
        min_key="expansion_delay_min_seconds",
        max_key="expansion_delay_max_seconds",
        legacy_key="expansion_delay_seconds",
        default_min=0.8,
        default_max=1.8,
    )
    scroll_min, scroll_max = _delay_range(
        browser_doc,
        min_key="scroll_delay_min_seconds",
        max_key="scroll_delay_max_seconds",
        legacy_key="scroll_delay_seconds",
        default_min=0.7,
        default_max=1.5,
    )

    browser = BrowserConfig(
        headless=_as_bool(browser_doc.get("headless"), True),
        persistent_profile=_as_bool(browser_doc.get("persistent_profile"), True),
        timeout_seconds=float(browser_doc.get("timeout_seconds", 45.0)),
        navigation_retries=max(1, int(browser_doc.get("navigation_retries", 3))),
        request_delay_min_seconds=request_min,
        request_delay_max_seconds=request_max,
        expansion_delay_min_seconds=expansion_min,
        expansion_delay_max_seconds=expansion_max,
        scroll_delay_min_seconds=scroll_min,
        scroll_delay_max_seconds=scroll_max,
        max_listing_scrolls=max(0, int(browser_doc.get("max_listing_scrolls", 18))),
        max_comment_scrolls=max(0, int(browser_doc.get("max_comment_scrolls", 24))),
        scroll_step_pixels=max(100, int(browser_doc.get("scroll_step_pixels", 900))),
        scroll_stable_rounds=max(1, int(browser_doc.get("scroll_stable_rounds", 3))),
        max_more_clicks_per_post=max(
            0, int(browser_doc.get("max_more_clicks_per_post", 750))
        ),
        max_continue_pages_per_post=max(
            0, int(browser_doc.get("max_continue_pages_per_post", 150))
        ),
        max_no_progress_rounds=max(
            1, int(browser_doc.get("max_no_progress_rounds", 5))
        ),
        progress_log_every_clicks=max(
            1, int(browser_doc.get("progress_log_every_clicks", 10))
        ),
        restart_every_posts=max(
            0, int(browser_doc.get("restart_every_posts", 50))
        ),
        slow_mo_ms=max(0, int(browser_doc.get("slow_mo_ms", 0))),
        block_images=_as_bool(browser_doc.get("block_images"), True),
        user_agent=(
            str(browser_doc["user_agent"]).strip()
            if browser_doc.get("user_agent")
            else None
        ),
        prefer_old_reddit=_as_bool(browser_doc.get("prefer_old_reddit"), True),
        fallback_to_modern_reddit=_as_bool(
            browser_doc.get("fallback_to_modern_reddit"), True
        ),
    )

    scraper = ScraperConfig(
        scrape_days=max(0, int(scraper_doc.get("scrape_days", 7))),
        scrape_days_keyword_search=max(
            0, int(scraper_doc.get("scrape_days_keyword_search", 30))
        ),
        min_comments_threshold=max(
            0, int(scraper_doc.get("min_comments_threshold", 0))
        ),
        max_posts_per_source=max(
            0, int(scraper_doc.get("max_posts_per_source", 0))
        ),
        max_comments_per_post=max(
            0, int(scraper_doc.get("max_comments_per_post", 0))
        ),
        max_pages_per_source=max(
            0, int(scraper_doc.get("max_pages_per_source", 10))
        ),
        full_scrape_sort=str(scraper_doc.get("full_scrape_sort", "new")),
        keyword_search_sort=str(scraper_doc.get("keyword_search_sort", "new")),
        timeframe=str(scraper_doc.get("timeframe", "all")),
        comment_sort=str(scraper_doc.get("comment_sort", "old")),
        skip_stickied=_as_bool(scraper_doc.get("skip_stickied"), True),
        include_deleted_comments=_as_bool(
            scraper_doc.get("include_deleted_comments"), False
        ),
        context_ancestor_limit=max(
            0, int(scraper_doc.get("context_ancestor_limit", 8))
        ),
        post_timeout_seconds=max(
            0.0, float(scraper_doc.get("post_timeout_seconds", 900.0))
        ),
        post_retries=max(
            0, int(scraper_doc.get("post_retries", 1))
        ),
    )

    output = OutputConfig(
        append_master_csv=_as_bool(output_doc.get("append_master_csv"), True),
        create_run_snapshots=_as_bool(
            output_doc.get("create_run_snapshots"), True
        ),
    )

    return AppConfig(paths=paths, browser=browser, scraper=scraper, output=output)
