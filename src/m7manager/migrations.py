from __future__ import annotations

import time
from pathlib import Path

CURRENT_SCHEMA = 2


def _backup_before_migration(db, root: Path):
    backup_dir = root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"manager-db-before-migration-{int(time.time())}.sqlite3"
    target = __import__("sqlite3").connect(destination)
    try:
        db.backup(target)
        target.commit()
    finally:
        target.close()


def migrate(db, root: Path, had_user_tables: bool):
    """Upgrade the metadata schema, preserving a SQLite backup before changes."""

    has_migrations = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    old_version = (
        db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        if has_migrations
        else 0
    )
    if had_user_tables and old_version < CURRENT_SCHEMA:
        _backup_before_migration(db, root)

    db.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, applied_at REAL NOT NULL
        )"""
    )
    if not has_migrations:
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)" ,
            (time.time(),),
        )

    version = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    if version < 2:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT OR IGNORE INTO schema_migrations(version, applied_at)
            VALUES (2, strftime('%s','now'));
            """
        )
    db.execute(f"PRAGMA user_version={CURRENT_SCHEMA}")


__all__ = ["CURRENT_SCHEMA", "migrate"]
