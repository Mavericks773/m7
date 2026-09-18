from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .migrations import migrate

ACTIVE = ("PREPARING", "STARTING", "RUNNING", "WAITING_LOGIN", "STOPPING", "RECONCILING")
TERMINAL = ("EXITED", "FAILED", "CANCELLED", "TIMED_OUT", "MISSED")
ACTIVE_SQL = ",".join(f"'{state}'" for state in ACTIVE)


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        db_path = self.root / "manager.db"
        existed = db_path.exists() and db_path.stat().st_size > 0
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        had_user_tables = bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
        )
        self.db.executescript(f"""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                auth_state TEXT NOT NULL DEFAULT 'UNINITIALIZED',
                image_digest TEXT NOT NULL DEFAULT '',
                timeout_seconds INTEGER NOT NULL DEFAULT 3600,
                pending_config TEXT NOT NULL DEFAULT '{{}}',
                blocked_reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL UNIQUE REFERENCES accounts(id),
                enabled INTEGER NOT NULL DEFAULT 0, local_time TEXT NOT NULL,
                task TEXT NOT NULL DEFAULT 'main', revision INTEGER NOT NULL DEFAULT 1,
                next_run_at REAL NOT NULL, grace_seconds INTEGER NOT NULL DEFAULT 7200
            );
            CREATE TABLE IF NOT EXISTS triggers (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                schedule_id TEXT, task TEXT NOT NULL, source TEXT NOT NULL,
                scheduled_for REAL NOT NULL, expires_at REAL NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'QUEUED', created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, trigger_id TEXT NOT NULL REFERENCES triggers(id),
                account_id TEXT NOT NULL REFERENCES accounts(id), task TEXT NOT NULL,
                state TEXT NOT NULL, container_id TEXT, image_digest TEXT NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                created_at REAL NOT NULL, started_at REAL, deadline_at REAL,
                finished_at REAL, exit_code INTEGER, oom_killed INTEGER DEFAULT 0,
                process_result TEXT NOT NULL DEFAULT '',
                business_result TEXT NOT NULL DEFAULT 'UNCONFIRMED',
                error_code TEXT NOT NULL DEFAULT '', stop_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_account
            ON runs(account_id) WHERE state IN ({ACTIVE_SQL});
            CREATE INDEX IF NOT EXISTS queue_index ON triggers(state, scheduled_for, created_at);
        """)
        migrate(self.db, self.root, existed and had_user_tables)
        self.db.execute(
            "INSERT OR IGNORE INTO settings VALUES ('installation_id', ?)",
            (json.dumps(uuid.uuid4().hex[:12]),),
        )
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('concurrency', '1')")
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params)]

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.transaction() as db:
            db.execute(sql, params)

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def active(self):
        return self.rows(f"SELECT * FROM runs WHERE state IN ({ACTIVE_SQL}) ORDER BY created_at")

    def close(self):
        self.db.close()
