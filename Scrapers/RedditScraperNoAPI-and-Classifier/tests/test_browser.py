from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from reddit_scraper.browser import RedditBrowser
from reddit_scraper.config import BrowserConfig


class _Response:
    status = 200


class _BodyLocator:
    def inner_text(self, timeout: int) -> str:
        return "normal reddit page"


class _Page:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.visited: list[str] = []

    def goto(self, url: str, wait_until: str, timeout: int):
        self.visited.append(url)
        if url.startswith("https://old.reddit.com"):
            raise PlaywrightError(
                "Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE"
            )
        self.url = url
        return _Response()

    def wait_for_timeout(self, milliseconds: int) -> None:
        return None

    def locator(self, selector: str) -> _BodyLocator:
        assert selector == "body"
        return _BodyLocator()


def test_navigation_falls_back_from_old_to_modern(tmp_path: Path) -> None:
    settings = BrowserConfig(
        navigation_retries=3,
        request_delay_min_seconds=0,
        request_delay_max_seconds=0,
    )
    browser = RedditBrowser(settings, tmp_path / "profile")
    page = _Page()
    browser.page = page  # type: ignore[assignment]

    result = browser.goto("https://old.reddit.com/r/test/new/?limit=100")

    assert result is page
    assert page.visited == [
        "https://old.reddit.com/r/test/new/?limit=100",
        "https://www.reddit.com/r/test/new/?limit=100",
    ]
    assert browser.current_reddit_ui == "modern"


def test_firefox_launch_configuration(tmp_path: Path) -> None:
    settings = BrowserConfig(headless=True, slow_mo_ms=15)
    browser = RedditBrowser(settings, tmp_path / "profile")

    assert browser.profile_dir == tmp_path / "profile" / "firefox"
    assert browser._build_launch_kwargs() == {
        "headless": True,
        "slow_mo": 15,
    }
    assert "channel" not in browser._build_launch_kwargs()
