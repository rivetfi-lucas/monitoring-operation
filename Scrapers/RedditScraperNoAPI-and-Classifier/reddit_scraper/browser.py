from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .config import BrowserConfig
from .utils import clean_text, reddit_url_variants


class ScraperError(RuntimeError):
    """Raised when Reddit or the browser prevents a scrape from continuing."""


class RedditBrowser:
    """A Playwright Firefox session with an optional isolated persistent profile."""

    BLOCKED_MARKERS = (
        "whoa there, pardner",
        "you've been blocked by network security",
        "request has been blocked",
        "too many requests",
        "blocked by network security",
        "your request has been blocked",
    )

    MODERN_MORE_PATTERN = re.compile(
        r"(?:view|load|show)\s+(?:\d+\s+)?more\s+(?:comments?|replies)|"
        r"more\s+replies|continue\s+this\s+thread",
        flags=re.I,
    )

    def __init__(
        self,
        settings: BrowserConfig,
        profile_dir: Path,
        *,
        headless_override: bool | None = None,
        persistent_override: bool | None = None,
    ) -> None:
        self.settings = settings
        # Store Firefox state under a dedicated profile subdirectory.
        self.profile_dir = profile_dir / "firefox"
        self.headless = (
            settings.headless if headless_override is None else headless_override
        )
        self.persistent = (
            settings.persistent_profile
            if persistent_override is None
            else persistent_override
        )

        self._playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.current_reddit_ui: str = "unknown"

    def __enter__(self) -> "RedditBrowser":
        try:
            self._playwright = sync_playwright().start()
            launch_kwargs = self._build_launch_kwargs()
            context_kwargs = self._build_context_kwargs()

            if self.persistent:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self.context = self._playwright.firefox.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    **launch_kwargs,
                    **context_kwargs,
                )
            else:
                self.browser = self._playwright.firefox.launch(**launch_kwargs)
                self.context = self.browser.new_context(**context_kwargs)

            self.context.set_default_timeout(
                int(self.settings.timeout_seconds * 1000)
            )
            self.context.add_cookies(
                [
                    {
                        "name": "over18",
                        "value": "1",
                        "domain": ".reddit.com",
                        "path": "/",
                    }
                ]
            )

            if self.settings.block_images:
                self.context.route("**/*", self._route_request)

            self.page = (
                self.context.pages[0]
                if self.context.pages
                else self.context.new_page()
            )
            return self

        except PlaywrightError as error:
            self._shutdown()
            mode = "headless" if self.headless else "headed"
            raise ScraperError(
                f"Could not launch Firefox in {mode} mode: {error}\n\n"
                "Install the Playwright Firefox browser with:\n"
                ".venv\\Scripts\\python.exe -m playwright install firefox"
            ) from error

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._shutdown()

    def _build_launch_kwargs(self) -> dict[str, Any]:
        # Firefox supports Playwright headless mode directly. browser-engine-specific
        # Chromium options such as channel="chromium" or --headless=new must not be
        # passed here.
        return {
            "headless": self.headless,
            "slow_mo": self.settings.slow_mo_ms,
        }

    def _build_context_kwargs(self) -> dict[str, Any]:
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 1000},
            "locale": "en-US",
            "timezone_id": "UTC",
        }
        # Let Firefox use its native Playwright user agent. Reusing a Chromium user agent with Firefox creates an inconsistent browser fingerprint.
        configured_user_agent = (self.settings.user_agent or "").strip()
        if configured_user_agent and "firefox" in configured_user_agent.casefold():
            context_kwargs["user_agent"] = configured_user_agent

        return context_kwargs

    def _shutdown(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except PlaywrightError:
                pass
            finally:
                self.context = None

        if self.browser is not None:
            try:
                self.browser.close()
            except PlaywrightError:
                pass
            finally:
                self.browser = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except PlaywrightError:
                pass
            finally:
                self._playwright = None

        self.page = None
        self.current_reddit_ui = "unknown"

    @staticmethod
    def _route_request(route: Any) -> None:
        # Keep document/script/XHR/style requests because modern Reddit needs
        # JavaScript to hydrate posts and comments.
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
        else:
            route.continue_()

    @staticmethod
    def _random_between(minimum: float, maximum: float) -> float:
        if maximum <= minimum:
            return minimum
        return random.uniform(minimum, maximum)

    def _sleep_request(self) -> float:
        delay = self._random_between(
            self.settings.request_delay_min_seconds,
            self.settings.request_delay_max_seconds,
        )
        time.sleep(delay)
        return delay

    def _wait_expansion(self) -> float:
        delay = self._random_between(
            self.settings.expansion_delay_min_seconds,
            self.settings.expansion_delay_max_seconds,
        )
        if self.page is not None:
            self.page.wait_for_timeout(max(50, int(delay * 1000)))
        else:
            time.sleep(delay)
        return delay

    def _wait_scroll(self) -> float:
        delay = self._random_between(
            self.settings.scroll_delay_min_seconds,
            self.settings.scroll_delay_max_seconds,
        )
        if self.page is not None:
            self.page.wait_for_timeout(max(50, int(delay * 1000)))
        else:
            time.sleep(delay)
        return delay

    @staticmethod
    def _reddit_ui_from_url(url: str) -> str:
        host = urlparse(url).netloc.casefold()
        if host.startswith("old.reddit.com"):
            return "old"
        if host.endswith("reddit.com"):
            return "modern"
        return "unknown"

    def is_modern_page(self) -> bool:
        return self.current_reddit_ui == "modern"

    def _navigation_variants(self, url: str) -> list[str]:
        variants = reddit_url_variants(
            url, prefer_old=self.settings.prefer_old_reddit
        )
        if not self.settings.fallback_to_modern_reddit:
            return variants[:1]
        return variants

    def _body_text(self) -> str:
        if self.page is None:
            return ""
        try:
            return self.page.locator("body").inner_text(timeout=5000).casefold()
        except (PlaywrightTimeoutError, PlaywrightError):
            return ""

    @staticmethod
    def _remaining_seconds(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return deadline - time.monotonic()

    @classmethod
    def _ensure_deadline(cls, deadline: float | None, *, stage: str) -> None:
        remaining = cls._remaining_seconds(deadline)
        if remaining is not None and remaining <= 0:
            raise ScraperError(f"Per-post timeout reached while {stage}")

    def _bounded_timeout_ms(self, deadline: float | None, default_seconds: float) -> int:
        remaining = self._remaining_seconds(deadline)
        if remaining is None:
            return max(1, int(default_seconds * 1000))
        if remaining <= 0:
            raise ScraperError("Per-post timeout reached")
        return max(1, int(min(default_seconds, remaining) * 1000))

    def _comment_progress_signature(self) -> tuple[int, int]:
        """Return a cheap DOM signature for expansion progress."""

        if self.page is None:
            return (0, 0)
        try:
            if self.is_modern_page():
                comments = self.page.locator("shreddit-comment").count()
                loaders = self.page.locator("button, a, [role='button']").filter(
                    has_text=self.MODERN_MORE_PATTERN
                ).count()
            else:
                comments = self.page.locator(".thing.comment").count()
                loaders = self.page.locator("span.morecomments a").count()
            return (int(comments), int(loaders))
        except PlaywrightError:
            return (0, 0)

    def _validate_response(self, status: int, body_text: str) -> None:
        if status == 429 or any(
            marker in body_text for marker in self.BLOCKED_MARKERS
        ):
            raise ScraperError(
                "Reddit rate-limited or blocked this browser session"
            )
        if status >= 500:
            raise ScraperError(f"Reddit returned HTTP {status}")
        if status in {401, 403}:
            raise ScraperError(
                f"Reddit returned HTTP {status}; this page may require access"
            )

    @staticmethod
    def _should_switch_host(error: Exception) -> bool:
        message = str(error).casefold()
        hard_failures = (
            "err_http_response_code_failure",
            "blocked this browser session",
            "http 401",
            "http 403",
        )
        return any(marker in message for marker in hard_failures)

    def goto(self, url: str, *, deadline: float | None = None) -> Page:
        """Navigate with retries and automatic old -> modern Reddit fallback."""

        if self.page is None:
            raise RuntimeError("Browser session is not open")

        variants = self._navigation_variants(url)
        all_errors: list[str] = []

        for variant_index, target_url in enumerate(variants):
            last_error: Exception | None = None

            if variant_index > 0:
                target_ui = self._reddit_ui_from_url(target_url)
                print(f"    [fallback] trying {target_ui} Reddit: {target_url}")

            for attempt in range(1, self.settings.navigation_retries + 1):
                self._ensure_deadline(deadline, stage="navigating Reddit")
                try:
                    response = self.page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=self._bounded_timeout_ms(
                            deadline, self.settings.timeout_seconds
                        ),
                    )

                    self.page.wait_for_timeout(700)
                    status = response.status if response is not None else 0
                    self._validate_response(status, self._body_text())

                    self.current_reddit_ui = self._reddit_ui_from_url(
                        self.page.url or target_url
                    )
                    self._ensure_deadline(deadline, stage="waiting after navigation")
                    self._sleep_request()
                    return self.page

                except (PlaywrightTimeoutError, PlaywrightError, ScraperError) as error:
                    last_error = error
                    if "Per-post timeout" in str(error):
                        raise
                    if self._should_switch_host(error):
                        break
                    if attempt >= self.settings.navigation_retries:
                        break

                    base = self._random_between(
                        self.settings.request_delay_min_seconds,
                        self.settings.request_delay_max_seconds,
                    )
                    delay = base * (2 ** (attempt - 1))
                    remaining = self._remaining_seconds(deadline)
                    if remaining is not None:
                        delay = min(delay, max(0.0, remaining))
                    print(
                        f"    [retry {attempt}/{self.settings.navigation_retries}] "
                        f"{error}; waiting {delay:.1f}s"
                    )
                    if delay > 0:
                        time.sleep(delay)

            all_errors.append(f"{target_url}: {last_error}")

        raise ScraperError(
            "Could not load Reddit page after trying available hosts:\n  - "
            + "\n  - ".join(all_errors)
        )

    def _scroll_until_stable(
        self,
        *,
        selector: str,
        max_scrolls: int,
        label: str,
        deadline: float | None = None,
    ) -> int:
        if self.page is None:
            raise RuntimeError("Browser session is not open")
        if max_scrolls <= 0:
            return 0

        stable_rounds = 0
        last_count = -1
        last_height = -1
        performed = 0

        for _ in range(max_scrolls):
            self._ensure_deadline(deadline, stage=f"hydrating {label}")
            try:
                count = self.page.locator(selector).count()
                height = int(
                    self.page.evaluate(
                        "Math.max(document.body.scrollHeight, "
                        "document.documentElement.scrollHeight)"
                    )
                    or 0
                )
                viewport_bottom = int(
                    self.page.evaluate("window.scrollY + window.innerHeight") or 0
                )

                if count == last_count and height == last_height:
                    stable_rounds += 1
                else:
                    stable_rounds = 0

                if stable_rounds >= self.settings.scroll_stable_rounds:
                    break

                last_count = count
                last_height = height

                if viewport_bottom + self.settings.scroll_step_pixels >= height:
                    self.page.evaluate(
                        "window.scrollTo({top: document.documentElement.scrollHeight, "
                        "behavior: 'smooth'})"
                    )
                else:
                    self.page.evaluate(
                        "step => window.scrollBy({top: step, behavior: 'smooth'})",
                        self.settings.scroll_step_pixels,
                    )

                performed += 1
                self._wait_scroll()

            except (PlaywrightTimeoutError, PlaywrightError):
                break

        if performed:
            try:
                self.page.evaluate("window.scrollTo({top: 0, behavior: 'auto'})")
                self.page.wait_for_timeout(150)
            except PlaywrightError:
                pass

        if performed and self.is_modern_page():
            print(f"      hydrated {label} with {performed} scroll step(s)")
        return performed

    def hydrate_listing(self) -> int:
        """Scroll modern Reddit listings until post links become stable."""

        if not self.is_modern_page():
            return 0

        # Reddit changes its custom-element names frequently.  A post permalink
        # is the most stable signal and Playwright locators can also see links
        # inside open shadow roots, unlike page.content()/BeautifulSoup.
        try:
            self.page.locator("a[href*='/comments/']").first.wait_for(
                state="attached",
                timeout=min(12_000, int(self.settings.timeout_seconds * 1000)),
            )
        except (PlaywrightTimeoutError, PlaywrightError):
            pass

        return self._scroll_until_stable(
            selector="a[href*='/comments/'], shreddit-post, article",
            max_scrolls=self.settings.max_listing_scrolls,
            label="listing",
        )

    def extract_modern_listing_links(self) -> list[dict[str, Any]]:
        """Extract Reddit post links from the live DOM, including shadow DOM."""

        if self.page is None:
            raise RuntimeError("Browser session is not open")
        if not self.is_modern_page():
            return []

        links = self.page.locator("a[href*='/comments/']")
        try:
            count = min(links.count(), 2_000)
        except PlaywrightError:
            return []

        rows: list[dict[str, Any]] = []
        for index in range(count):
            link = links.nth(index)
            try:
                row = link.evaluate(
                    """
                    (element) => {
                      const card = element.closest(
                        'shreddit-post, article, [data-testid="post-container"], ' +
                        '[data-post-id], [data-thingid], [thingid]'
                      );
                      const timeNode = card?.querySelector(
                        'time[datetime], faceplate-timeago[ts], ' +
                        'faceplate-timeago[datetime]'
                      );
                      const authorNode = card?.querySelector(
                        'a[href^="/user/"], a[href*="reddit.com/user/"]'
                      );
                      const titleNode = card?.querySelector(
                        '[slot="title"], h1, h2, h3, [data-testid="post-title"]'
                      );
                      const rawCardText = card?.innerText || '';
                      const attrs = (node, names) => {
                        if (!node) return '';
                        for (const name of names) {
                          const value = node.getAttribute(name);
                          if (value) return value;
                        }
                        return '';
                      };
                      return {
                        href: element.href || element.getAttribute('href') || '',
                        text: element.innerText || element.textContent || '',
                        aria_label: element.getAttribute('aria-label') || '',
                        title_attr: element.getAttribute('title') || '',
                        card_title: titleNode?.innerText || titleNode?.textContent || '',
                        card_text: rawCardText.slice(0, 4000),
                        post_id: attrs(card, [
                          'thingid', 'thing-id', 'post-id', 'data-post-id',
                          'data-fullname', 'id'
                        ]),
                        author: attrs(card, ['author', 'data-author']) ||
                          authorNode?.textContent || '',
                        comment_count: attrs(card, [
                          'comment-count', 'comment-count-number',
                          'comments-count', 'data-comments-count'
                        ]),
                        created: attrs(card, [
                          'created-timestamp', 'created_timestamp',
                          'timestamp', 'datetime'
                        ]) || timeNode?.getAttribute('datetime') ||
                          timeNode?.getAttribute('ts') || '',
                        subreddit: attrs(card, [
                          'subreddit-prefixed-name', 'subreddit-name', 'subreddit'
                        ]),
                        stickied: attrs(card, [
                          'is-stickied', 'stickied', 'is_stickied'
                        ]),
                      };
                    }
                    """
                )
            except (PlaywrightTimeoutError, PlaywrightError):
                continue
            if isinstance(row, dict) and row.get("href"):
                rows.append(row)

        return rows

    def hydrate_comments(self, *, deadline: float | None = None) -> int:
        """Scroll modern Reddit threads so lazy comment components hydrate."""

        if not self.is_modern_page():
            return 0
        return self._scroll_until_stable(
            selector="shreddit-comment",
            max_scrolls=self.settings.max_comment_scrolls,
            label="comments",
            deadline=deadline,
        )

    def _expand_old_comments(self, *, deadline: float | None = None) -> int:
        if self.page is None:
            raise RuntimeError("Browser session is not open")

        max_clicks = self.settings.max_more_clicks_per_post
        clicks = 0
        stale_rounds = 0
        no_progress_rounds = 0

        while max_clicks <= 0 or clicks < max_clicks:
            self._ensure_deadline(deadline, stage="expanding old Reddit comments")
            links = self.page.locator("span.morecomments a")
            count = links.count()
            clicked = False

            for index in range(count):
                self._ensure_deadline(deadline, stage="expanding old Reddit comments")
                link = links.nth(index)
                try:
                    text = clean_text(link.inner_text(timeout=1200)).casefold()
                    href = (link.get_attribute("href") or "").casefold()
                except (PlaywrightTimeoutError, PlaywrightError):
                    continue

                if "continue this thread" in text:
                    continue

                is_inline_loader = (
                    "load more comment" in text
                    or "more repl" in text
                    or "more comment" in text
                    or href.startswith("javascript:")
                )
                if not is_inline_loader:
                    continue

                before = self._comment_progress_signature()
                try:
                    link.scroll_into_view_if_needed(timeout=2500)
                    link.click(timeout=6000)
                    self._wait_expansion()
                    clicks += 1
                    clicked = True
                    stale_rounds = 0
                    after = self._comment_progress_signature()
                    if after == before:
                        no_progress_rounds += 1
                    else:
                        no_progress_rounds = 0

                    if clicks % self.settings.progress_log_every_clicks == 0:
                        print(
                            f"      expansion progress: {clicks} loader(s), "
                            f"{after[0]} visible comment node(s)"
                        )
                    if no_progress_rounds >= self.settings.max_no_progress_rounds:
                        print(
                            "      [warn] comment expansion made no DOM progress for "
                            f"{no_progress_rounds} consecutive loader(s); moving on"
                        )
                        return clicks
                    break
                except (PlaywrightTimeoutError, PlaywrightError):
                    continue

            if clicked:
                continue

            stale_rounds += 1
            if stale_rounds >= 2:
                break
            self.page.wait_for_timeout(300)

        return clicks

    def _expand_modern_comments(self, *, deadline: float | None = None) -> int:
        if self.page is None:
            raise RuntimeError("Browser session is not open")

        max_clicks = self.settings.max_more_clicks_per_post
        clicks = 0
        stale_rounds = 0
        no_progress_rounds = 0

        self.hydrate_comments(deadline=deadline)

        while max_clicks <= 0 or clicks < max_clicks:
            self._ensure_deadline(deadline, stage="expanding modern Reddit comments")
            controls = self.page.locator("button, a, [role='button']").filter(
                has_text=self.MODERN_MORE_PATTERN
            )
            count = min(controls.count(), 250)
            clicked = False

            for index in range(count):
                self._ensure_deadline(deadline, stage="expanding modern Reddit comments")
                control = controls.nth(index)
                try:
                    if not control.is_visible(timeout=800):
                        continue
                    text = clean_text(control.inner_text(timeout=1000))
                    if not self.MODERN_MORE_PATTERN.search(text):
                        continue

                    href = control.get_attribute("href") or ""
                    if "continue this thread" in text.casefold() and href:
                        continue

                    before = self._comment_progress_signature()
                    control.scroll_into_view_if_needed(timeout=2500)
                    control.click(timeout=6000)
                    self._wait_expansion()
                    clicks += 1
                    clicked = True
                    stale_rounds = 0
                    self.hydrate_comments(deadline=deadline)
                    after = self._comment_progress_signature()
                    if after == before:
                        no_progress_rounds += 1
                    else:
                        no_progress_rounds = 0

                    if clicks % self.settings.progress_log_every_clicks == 0:
                        print(
                            f"      expansion progress: {clicks} loader(s), "
                            f"{after[0]} visible comment node(s)"
                        )
                    if no_progress_rounds >= self.settings.max_no_progress_rounds:
                        print(
                            "      [warn] comment expansion made no DOM progress for "
                            f"{no_progress_rounds} consecutive loader(s); moving on"
                        )
                        return clicks
                    break
                except (PlaywrightTimeoutError, PlaywrightError):
                    continue

            if clicked:
                continue

            stale_rounds += 1
            if stale_rounds >= 2:
                break
            self.hydrate_comments(deadline=deadline)

        return clicks

    def expand_more_comments(self, *, deadline: float | None = None) -> int:
        """Expand inline comment/reply loaders for the active Reddit UI."""

        if self.page is None:
            raise RuntimeError("Browser session is not open")

        clicks = (
            self._expand_modern_comments(deadline=deadline)
            if self.is_modern_page()
            else self._expand_old_comments(deadline=deadline)
        )

        max_clicks = self.settings.max_more_clicks_per_post
        if max_clicks > 0 and clicks >= max_clicks:
            print(
                "      [warn] max_more_clicks_per_post reached; "
                "increase it for unusually large threads"
            )
        return clicks

