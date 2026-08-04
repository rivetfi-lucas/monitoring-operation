from pathlib import Path

from reddit_scraper.config import load_app_config


def test_relative_paths_resolve_from_project(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "paths:\n  data_dir: custom-data\nbrowser:\n  headless: false\n",
        encoding="utf-8",
    )
    config = load_app_config(config_path, tmp_path)
    assert config.paths.data_dir == (tmp_path / "custom-data").resolve()
    assert config.paths.comments_csv == (
        tmp_path / "custom-data" / "exports" / "reddit_comments.csv"
    ).resolve()
    assert config.browser.headless is False
