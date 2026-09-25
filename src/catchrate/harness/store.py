"""SQLite results store: runs, reviews, judge matches and hand labels."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    repo TEXT NOT NULL,
    version TEXT NOT NULL,
    model TEXT NOT NULL,
    split TEXT,
    seed INTEGER NOT NULL DEFAULT 0,
    config TEXT NOT NULL,
    benchmark_sha TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    findings TEXT NOT NULL,
    n_findings INTEGER NOT NULL,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    cost_usd REAL,
    seconds REAL,
    tool_calls INTEGER,
    turns INTEGER,
    stop_reason TEXT,
    error TEXT,
    trace TEXT,
    PRIMARY KEY (run_id, case_id)
);
CREATE TABLE IF NOT EXISTS matches (
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    fix_commit TEXT NOT NULL,
    finding_index INTEGER NOT NULL,
    location_ok INTEGER NOT NULL,
    same_bug INTEGER,
    reason TEXT,
    judge_model TEXT,
    PRIMARY KEY (run_id, case_id, fix_commit, finding_index)
);
CREATE TABLE IF NOT EXISTS labels (
    task TEXT NOT NULL,
    item_id TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT,
    labeled_at TEXT NOT NULL,
    PRIMARY KEY (task, item_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    # -- runs and reviews ----------------------------------------------------

    def create_run(self, run_id: str, *, repo: str, version: str, model: str, split: str | None, seed: int,
                   config: dict[str, Any], benchmark_sha: str | None, notes: str = "") -> None:  # fmt: skip
        self._write(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, _now(), repo, version, model, split, seed, json.dumps(config, sort_keys=True), benchmark_sha, notes),
        )

    def runs(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM runs ORDER BY created_at")

    def run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return rows[0] if rows else None

    def save_review(self, run_id: str, case_id: str, kind: str, record: dict[str, Any]) -> None:
        self._write(
            "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, case_id, kind, json.dumps(record["findings"]), len(record["findings"]),
                record["input_tokens"], record["output_tokens"], record["cache_read_tokens"], record["cache_write_tokens"],
                record["cost_usd"], record["seconds"], record["tool_calls"], record["turns"], record["stop_reason"],
                record["error"], json.dumps(record["trace"]),
            ),
        )  # fmt: skip

    def reviews(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM reviews WHERE run_id = ? ORDER BY case_id", (run_id,))
        for row in rows:
            row["findings"] = json.loads(row["findings"])
            row["trace"] = json.loads(row["trace"] or "[]")
        return rows

    def reviewed_cases(self, run_id: str) -> set[str]:
        return {r["case_id"] for r in self._rows("SELECT case_id FROM reviews WHERE run_id = ? AND error IS NULL", (run_id,))}

    def spent(self, run_id: str) -> float:
        rows = self._rows("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM reviews WHERE run_id = ?", (run_id,))
        return float(rows[0]["total"])

    # -- judge matches ---------------------------------------------------------

    def save_match(self, run_id: str, case_id: str, fix_commit: str, finding_index: int, *, location_ok: bool,
                   same_bug: bool | None, reason: str, judge_model: str) -> None:  # fmt: skip
        self._write(
            "INSERT OR REPLACE INTO matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, case_id, fix_commit, finding_index, int(location_ok),
             None if same_bug is None else int(same_bug), reason, judge_model),
        )  # fmt: skip

    def matches(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM matches WHERE run_id = ?", (run_id,))

    # -- hand labels -----------------------------------------------------------

    def save_label(self, task: str, item_id: str, label: str, note: str = "") -> None:
        self._write("INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?, ?)", (task, item_id, label, note, _now()))

    def labels(self, task: str) -> dict[str, dict[str, Any]]:
        return {r["item_id"]: r for r in self._rows("SELECT * FROM labels WHERE task = ?", (task,))}
