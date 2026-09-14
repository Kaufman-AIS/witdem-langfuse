"""Host-local shared request pacing for independent Duckle backfill processes."""

import math
import os
import sqlite3
import time
from pathlib import Path


class SharedBudget:
    def __init__(self, path, group, *, requests_per_minute=30, clock=time.time):
        if (
            not group
            or not math.isfinite(requests_per_minute)
            or requests_per_minute <= 0
        ):
            raise ValueError(
                "budget group and positive finite request rate are required"
            )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.group, self.clock = group, clock
        self.interval = 60 / requests_per_minute
        with sqlite3.connect(self.path) as db:
            os.chmod(self.path, 0o600)
            db.execute(
                "CREATE TABLE IF NOT EXISTS budgets (group_id TEXT PRIMARY KEY, interval REAL NOT NULL, next_at REAL NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO budgets VALUES (?, ?, 0)", (group, self.interval)
            )
            configured = db.execute(
                "SELECT interval FROM budgets WHERE group_id=?", (group,)
            ).fetchone()[0]
            if configured != self.interval:
                raise ValueError("quota group already has a different request rate")

    def acquire(self):
        """Return 0 when reserved, otherwise the earliest retry timestamp."""
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            deadline = db.execute(
                "SELECT next_at FROM budgets WHERE group_id=?", (self.group,)
            ).fetchone()[0]
            now = self.clock()
            if deadline > now:
                return deadline
            db.execute(
                "UPDATE budgets SET next_at=? WHERE group_id=?",
                (now + self.interval, self.group),
            )
            return 0

    def defer_until(self, deadline):
        if not math.isfinite(deadline):
            raise ValueError("quota deadline must be finite")
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute(
                "UPDATE budgets SET next_at=max(next_at, ?) WHERE group_id=?",
                (deadline, self.group),
            )
