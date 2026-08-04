from __future__ import annotations

import csv
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .utils import utc_now_iso

COMMENT_FIELDS = [
    "comment_id",
    "thread_id",
    "thread_title",
    "thread_body",
    "subreddit",
    "intake_mode",
    "matched_keyword",
    "parent_id",
    "root_comment_id",
    "ancestor_ids",
    "parent_body",
    "context_text",
    "author",
    "body",
    "score",
    "depth",
    "created_utc",
    "created_iso",
    "permalink",
    "thread_permalink",
    "fetched_at",
]

POST_FIELDS = [
    "post_id",
    "title",
    "subreddit",
    "intake_mode",
    "matched_keyword",
    "author",
    "body",
    "score",
    "num_comments",
    "fetched_comment_count",
    "created_utc",
    "created_iso",
    "permalink",
    "fetched_at",
]


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 0,
                post_count INTEGER NOT NULL DEFAULT 0,
                new_comment_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS posts (
                post_id TEXT PRIMARY KEY,
                subreddit TEXT,
                intake_mode TEXT,
                matched_keyword TEXT,
                title TEXT,
                body TEXT,
                permalink TEXT,
                reported_comment_count INTEGER NOT NULL DEFAULT 0,
                fetched_comment_count INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_scraped_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS comments (
                comment_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                subreddit TEXT,
                parent_id TEXT,
                root_comment_id TEXT,
                depth INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_comments_thread ON comments(thread_id);
            CREATE INDEX IF NOT EXISTS idx_posts_subreddit ON posts(subreddit);
            """
        )
        self.conn.commit()

    def start_run(self, source_count: int) -> str:
        # A previous process may have been closed or killed before finish_run().
        # Mark those rows explicitly instead of leaving them "running" forever.
        now = utc_now_iso()
        self.conn.execute(
            "UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'",
            (now,),
        )
        run_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO runs(run_id, started_at, status, source_count) VALUES (?, ?, 'running', ?)",
            (run_id, utc_now_iso(), source_count),
        )
        self.conn.commit()
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        post_count: int,
        new_comment_count: int,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at=?, status=?, post_count=?, new_comment_count=?, error=?
            WHERE run_id=?
            """,
            (
                utc_now_iso(),
                status,
                post_count,
                new_comment_count,
                error,
                run_id,
            ),
        )
        self.conn.commit()

    def update_run_progress(
        self,
        run_id: str,
        *,
        post_count: int,
        new_comment_count: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET post_count=?, new_comment_count=?
            WHERE run_id=?
            """,
            (post_count, new_comment_count, run_id),
        )
        self.conn.commit()

    def existing_comment_ids(self, comment_ids: Iterable[str]) -> set[str]:
        """Return IDs already in SQLite without loading the whole table."""

        unique = list(dict.fromkeys(str(value) for value in comment_ids if value))
        existing: set[str] = set()
        for start in range(0, len(unique), 900):
            chunk = unique[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            query = f"SELECT comment_id FROM comments WHERE comment_id IN ({placeholders})"
            existing.update(row[0] for row in self.conn.execute(query, chunk))
        return existing

    def known_comment_ids(self) -> set[str]:
        return {row[0] for row in self.conn.execute("SELECT comment_id FROM comments")}

    def known_post_ids(self) -> set[str]:
        return {row[0] for row in self.conn.execute("SELECT post_id FROM posts")}

    def post_permalink(self, post_id: str) -> str | None:
        """Return the last stored permalink for a post, when available."""

        row = self.conn.execute(
            "SELECT permalink FROM posts WHERE post_id=?",
            (post_id,),
        ).fetchone()
        if not row:
            return None
        permalink = str(row[0] or "").strip()
        return permalink or None

    def record(self, posts: Iterable[dict[str, Any]], comments: Iterable[dict[str, Any]]) -> None:
        now = utc_now_iso()
        with self.conn:
            for post in posts:
                existing = self.conn.execute(
                    "SELECT first_seen_at FROM posts WHERE post_id=?",
                    (post["post_id"],),
                ).fetchone()
                first_seen_at = existing[0] if existing else now
                self.conn.execute(
                    """
                    INSERT INTO posts(
                        post_id, subreddit, intake_mode, matched_keyword, title, body,
                        permalink, reported_comment_count, fetched_comment_count,
                        first_seen_at, last_scraped_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(post_id) DO UPDATE SET
                        subreddit=excluded.subreddit,
                        intake_mode=excluded.intake_mode,
                        matched_keyword=excluded.matched_keyword,
                        title=excluded.title,
                        body=excluded.body,
                        permalink=excluded.permalink,
                        reported_comment_count=excluded.reported_comment_count,
                        fetched_comment_count=excluded.fetched_comment_count,
                        last_scraped_at=excluded.last_scraped_at
                    """,
                    (
                        post["post_id"],
                        post.get("subreddit", ""),
                        post.get("intake_mode", ""),
                        post.get("matched_keyword", ""),
                        post.get("title", ""),
                        post.get("body", ""),
                        post.get("permalink", ""),
                        int(post.get("num_comments", 0) or 0),
                        int(post.get("fetched_comment_count", 0) or 0),
                        first_seen_at,
                        now,
                    ),
                )

            self.conn.executemany(
                """
                INSERT INTO comments(
                    comment_id, thread_id, subreddit, parent_id,
                    root_comment_id, depth, first_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(comment_id) DO UPDATE SET
                    thread_id=excluded.thread_id,
                    subreddit=excluded.subreddit,
                    parent_id=excluded.parent_id,
                    root_comment_id=excluded.root_comment_id,
                    depth=excluded.depth
                """,
                [
                    (
                        row["comment_id"],
                        row["thread_id"],
                        row.get("subreddit", ""),
                        row.get("parent_id", ""),
                        row.get("root_comment_id", ""),
                        int(row.get("depth", 0) or 0),
                        now,
                    )
                    for row in comments
                ],
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _write_rows(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str],
    *,
    append: bool,
) -> Path | None:
    if not rows:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    exists_with_data = path.exists() and path.stat().st_size > 0
    mode = "a" if append else "w"
    encoding = "utf-8" if exists_with_data and append else "utf-8-sig"

    with path.open(mode, encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not exists_with_data or not append:
            writer.writeheader()
        writer.writerows(rows)
    return path


def _upsert_rows(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str],
    *,
    key_field: str,
) -> Path | None:
    """Insert new rows and refresh existing rows in a master CSV.

    Rewriting the master file is intentional: it allows parser improvements to
    repair previously exported rows without deleting SQLite state or creating
    duplicate comments.
    """

    if not rows:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    ordered_keys: list[str] = []
    merged: dict[str, dict[str, Any]] = {}

    if path.exists() and path.stat().st_size > 0:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for existing in reader:
                key = str(existing.get(key_field, "")).strip()
                if not key:
                    continue
                if key not in merged:
                    ordered_keys.append(key)
                merged[key] = existing

    for row in rows:
        key = str(row.get(key_field, "")).strip()
        if not key:
            continue
        if key not in merged:
            ordered_keys.append(key)
        merged[key] = row

    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            writer.writeheader()
            for key in ordered_keys:
                writer.writerow(merged[key])
        temp_path.replace(path)
    except PermissionError as error:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Could not update {path}. Close the CSV in Excel and run the scraper again."
        ) from error

    return path


def write_master_exports(
    *,
    comments_path: Path,
    posts_path: Path,
    comments: list[dict[str, Any]],
    posts: list[dict[str, Any]],
) -> tuple[Path | None, Path | None]:
    return (
        _upsert_rows(
            comments_path,
            comments,
            COMMENT_FIELDS,
            key_field="comment_id",
        ),
        _upsert_rows(
            posts_path,
            posts,
            POST_FIELDS,
            key_field="post_id",
        ),
    )


def write_run_snapshots(
    snapshots_dir: Path,
    *,
    comments: list[dict[str, Any]],
    posts: list[dict[str, Any]],
) -> tuple[Path | None, Path | None]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = snapshots_dir / stamp
    comments_path = _write_rows(
        run_dir / "reddit_comments.csv", comments, COMMENT_FIELDS, append=False
    )
    posts_path = _write_rows(
        run_dir / "reddit_posts.csv", posts, POST_FIELDS, append=False
    )
    return comments_path, posts_path


def _read_key_set(path: Path, key_field: str) -> set[str]:
    if not path.exists() or path.stat().st_size <= 0:
        return set()
    values: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            value = str(row.get(key_field, "")).strip()
            if value:
                values.add(value)
    return values


class IncrementalMasterWriter:
    """Checkpoint master CSVs one post at a time.

    New-post mode appends only unseen IDs and never rewrites the full dataset.
    Repair/manual mode intentionally upserts so existing rows can be refreshed.
    """

    def __init__(
        self,
        *,
        comments_path: Path,
        posts_path: Path,
        update_existing: bool,
    ) -> None:
        self.comments_path = comments_path
        self.posts_path = posts_path
        self.update_existing = update_existing
        self.comment_ids = (
            set() if update_existing else _read_key_set(comments_path, "comment_id")
        )
        self.post_ids = (
            set() if update_existing else _read_key_set(posts_path, "post_id")
        )

    def write(
        self,
        *,
        post: dict[str, Any],
        comments: list[dict[str, Any]],
    ) -> tuple[Path | None, Path | None, int, int]:
        if self.update_existing:
            comment_path, post_path = write_master_exports(
                comments_path=self.comments_path,
                posts_path=self.posts_path,
                comments=comments,
                posts=[post],
            )
            return comment_path, post_path, len(comments), 1

        new_comments = [
            row
            for row in comments
            if row.get("comment_id") and row["comment_id"] not in self.comment_ids
        ]
        new_posts = (
            [post]
            if post.get("post_id") and post["post_id"] not in self.post_ids
            else []
        )
        comment_path = _write_rows(
            self.comments_path,
            new_comments,
            COMMENT_FIELDS,
            append=True,
        )
        post_path = _write_rows(
            self.posts_path,
            new_posts,
            POST_FIELDS,
            append=True,
        )
        self.comment_ids.update(
            str(row["comment_id"]) for row in new_comments if row.get("comment_id")
        )
        self.post_ids.update(
            str(row["post_id"]) for row in new_posts if row.get("post_id")
        )
        return comment_path, post_path, len(new_comments), len(new_posts)


class RunCheckpointWriter:
    """Append per-post run snapshots and failures as the run progresses."""

    ERROR_FIELDS = [
        "post_id",
        "title",
        "permalink",
        "attempt",
        "error",
        "failed_at",
    ]

    def __init__(self, snapshots_dir: Path, run_id: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.run_dir = snapshots_dir / f"{stamp}_{run_id[:8]}"
        self.comments_path = self.run_dir / "reddit_comments.csv"
        self.posts_path = self.run_dir / "reddit_posts.csv"
        self.errors_path = self.run_dir / "failed_posts.csv"
        self.failed_targets_path = self.run_dir / "failed_posts.txt"

    def write_post(
        self,
        *,
        post: dict[str, Any],
        comments: list[dict[str, Any]],
    ) -> tuple[Path | None, Path | None]:
        return (
            _write_rows(
                self.comments_path, comments, COMMENT_FIELDS, append=True
            ),
            _write_rows(self.posts_path, [post], POST_FIELDS, append=True),
        )

    def write_error(
        self,
        *,
        candidate: dict[str, Any],
        attempt: int,
        error: Exception | str,
    ) -> Path:
        row = {
            "post_id": candidate.get("post_id", ""),
            "title": candidate.get("title", ""),
            "permalink": candidate.get("permalink", ""),
            "attempt": attempt,
            "error": str(error),
            "failed_at": utc_now_iso(),
        }
        _write_rows(self.errors_path, [row], self.ERROR_FIELDS, append=True)
        self.failed_targets_path.parent.mkdir(parents=True, exist_ok=True)
        with self.failed_targets_path.open("a", encoding="utf-8") as handle:
            handle.write(str(candidate.get("permalink") or candidate.get("post_id") or ""))
            handle.write("\n")
        return self.errors_path
