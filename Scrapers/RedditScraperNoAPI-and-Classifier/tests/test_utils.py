from reddit_scraper.utils import modern_reddit_url, old_reddit_url, reddit_url_variants


def test_reddit_host_variants_preserve_path_and_query() -> None:
    url = "https://old.reddit.com/r/test/new/?limit=100"
    assert modern_reddit_url(url) == "https://www.reddit.com/r/test/new/?limit=100"
    assert old_reddit_url(modern_reddit_url(url)) == url
    assert reddit_url_variants(url) == [url, modern_reddit_url(url)]
