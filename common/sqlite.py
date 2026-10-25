"""Thin thread-safe SQLite wrapper."""
import sqlite3
import threading
from pathlib import Path


class Database:
    def __init__(self, path: str):
        """Open (creating parents) a WAL-mode database."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()

    def executescript(self, sql: str) -> None:
        """Run a multi-statement script."""
        with self.lock:
            self.conn.executescript(sql)
            self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Execute one statement under the lock, commit, return lastrowid."""
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Return all rows as dicts."""
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        """Return the first row as a dict, or None."""
        rows = self.query(sql, params)
        return rows[0] if rows else None
