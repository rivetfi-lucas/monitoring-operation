from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

OLD_REDDIT_BASE = "https://old.reddit.com"
MODERN_REDDIT_BASE = "https://www.reddit.com"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def parse_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    match = re.search(r"-?[\d,]+", str(value))
    if not match:
        return default
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return default


def parse_iso_datetime(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def to_iso(epoch: Any) -> str:
    if epoch in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def strip_fullname(value: str | None) -> str:
    text = (value or "").strip()
    return text.split("_", 1)[1] if text.startswith(("t1_", "t3_")) else text


def normalize_keyword(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return clean_text(normalized.casefold())


def _reddit_host_url(url: str, *, base: str) -> str:
    if not url:
        return base
    if url.startswith("/"):
        return urljoin(base, url)

    parsed = urlparse(url)
    if parsed.netloc.casefold().endswith("reddit.com"):
        target = urlparse(base)
        return urlunparse(
            parsed._replace(scheme=target.scheme, netloc=target.netloc)
        )
    return url


def old_reddit_url(url: str) -> str:
    return _reddit_host_url(url, base=OLD_REDDIT_BASE)


def modern_reddit_url(url: str) -> str:
    return _reddit_host_url(url, base=MODERN_REDDIT_BASE)


def reddit_url_variants(url: str, *, prefer_old: bool = True) -> list[str]:
    """Return unique old/modern Reddit URL variants in preferred order."""

    old = old_reddit_url(url)
    modern = modern_reddit_url(url)
    variants = [old, modern] if prefer_old else [modern, old]
    return list(dict.fromkeys(variants))


def with_query(url: str, **updates: Any) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in updates.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(query)))


def subreddit_from_url(url: str) -> str:
    match = re.search(r"/r/([^/?#]+)", url, flags=re.I)
    return match.group(1) if match else ""
