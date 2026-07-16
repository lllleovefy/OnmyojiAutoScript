from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from module.duel_data.models import (
    DuelMatch,
    DuelMatchList,
    DuelMatchPatch,
    DuelPick,
    DuelStrategy,
    DuelSummary,
    DuelTopPick,
    normalize_utc_iso,
)
from module.duel_data.security import sanitize_for_storage


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "config" / "duel" / "duel.sqlite3"


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS duel_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_account_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    user_id TEXT,
    latest_at TEXT,
    latest_score INTEGER,
    latest_star INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, source_account_id)
);

CREATE TABLE IF NOT EXISTS duel_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES duel_accounts(id) ON DELETE CASCADE,
    started_at TEXT,
    score INTEGER,
    star INTEGER,
    self_ban INTEGER,
    opponent_ban INTEGER,
    result TEXT NOT NULL DEFAULT 'unknown' CHECK(result IN ('win', 'loss', 'unknown')),
    duration REAL,
    valid INTEGER NOT NULL DEFAULT 1,
    practice_mode INTEGER NOT NULL DEFAULT 0,
    filename TEXT,
    source TEXT NOT NULL,
    source_record_id TEXT,
    dedupe_key TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_duel_matches_source_record
ON duel_matches(source, source_record_id)
WHERE source_record_id IS NOT NULL AND source_record_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_duel_matches_dedupe
ON duel_matches(account_id, source, dedupe_key);
CREATE INDEX IF NOT EXISTS idx_duel_matches_started_at ON duel_matches(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_duel_matches_result ON duel_matches(result, valid, practice_mode);

CREATE TABLE IF NOT EXISTS duel_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES duel_matches(id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK(side IN ('self', 'opponent')),
    round INTEGER NOT NULL CHECK(round BETWEEN 1 AND 6),
    shishen_id INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    UNIQUE(match_id, side, round)
);
CREATE INDEX IF NOT EXISTS idx_duel_picks_shishen ON duel_picks(shishen_id, side);

CREATE TABLE IF NOT EXISTS duel_strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    content_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    source_strategy_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_duel_strategies_source
ON duel_strategies(source, source_strategy_id)
WHERE source_strategy_id IS NOT NULL AND source_strategy_id <> '';

CREATE TABLE IF NOT EXISTS duel_import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    accounts_imported INTEGER NOT NULL DEFAULT 0,
    matches_imported INTEGER NOT NULL DEFAULT 0,
    matches_skipped INTEGER NOT NULL DEFAULT 0,
    strategies_imported INTEGER NOT NULL DEFAULT 0,
    snapshots_imported INTEGER NOT NULL DEFAULT 0,
    progress_json TEXT NOT NULL DEFAULT '{}',
    errors_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS external_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE(provider, snapshot_type, payload_hash)
);

CREATE TABLE IF NOT EXISTS recommendation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    recommendation_type TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE(provider, recommendation_type, payload_hash)
);

"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(sanitize_for_storage(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class DuelRepository:
    def __init__(self, database_path: str | Path = DEFAULT_DATABASE_PATH):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.database_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._write_lock, self._connect() as connection:
            previous_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            connection.executescript(SCHEMA)
            if previous_version < 2:
                self._normalize_existing_timestamps(connection)
                connection.execute("PRAGMA user_version=2")

    @staticmethod
    def _normalize_existing_timestamps(connection: sqlite3.Connection) -> None:
        for table, column in (
            ("duel_accounts", "latest_at"),
            ("duel_matches", "started_at"),
        ):
            rows = connection.execute(
                f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    normalized = normalize_utc_iso(str(row[column]))
                except ValueError:
                    normalized = None
                if normalized != row[column]:
                    connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE id=?",
                        (normalized, row["id"]),
                    )

    def upsert_account(self, account: dict[str, Any], *, source: str = "oas") -> tuple[int, bool]:
        source_id = str(account.get("id") or account.get("source_account_id") or "").strip()
        if not source_id:
            raise ValueError("account source id is required")
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM duel_accounts WHERE source = ? AND source_account_id = ?",
                (source, source_id),
            ).fetchone()
            values = (
                str(account.get("name") or ""),
                str(account["user_id"]) if account.get("user_id") is not None else None,
                account.get("latest_at") or account.get("latest_ts"),
                account.get("latest_score"),
                account.get("latest_star"),
                now,
                source,
                source_id,
            )
            if existing:
                connection.execute(
                    """UPDATE duel_accounts SET name=?, user_id=?, latest_at=?, latest_score=?, latest_star=?,
                       updated_at=? WHERE source=? AND source_account_id=?""",
                    values,
                )
                return int(existing["id"]), False
            cursor = connection.execute(
                """INSERT INTO duel_accounts
                   (source, source_account_id, name, user_id, latest_at, latest_score, latest_star, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source,
                    source_id,
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    values[4],
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid), True

    @staticmethod
    def match_dedupe_key(account_id: int, match: dict[str, Any]) -> str:
        material = {
            "account_id": account_id,
            "started_at": match.get("started_at"),
            "self_ban": match.get("self_ban"),
            "opponent_ban": match.get("opponent_ban"),
            "picks": sorted(
                [
                    {
                        "side": pick.get("side"),
                        "round": pick.get("round"),
                        "shishen_id": pick.get("shishen_id"),
                        "count": pick.get("count", 1),
                    }
                    for pick in match.get("picks", [])
                ],
                key=lambda item: (str(item["side"]), int(item["round"] or 0)),
            ),
            "result": match.get("result", "unknown"),
            "practice_mode": bool(match.get("practice_mode", False)),
        }
        return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()

    def upsert_match(self, account_id: int, match: dict[str, Any], *, source: str = "oas") -> tuple[int, bool]:
        match = dict(match)
        if match.get("started_at"):
            match["started_at"] = normalize_utc_iso(str(match["started_at"]))
        source_record_id = str(match.get("source_record_id") or "").strip() or None
        dedupe_key = self.match_dedupe_key(account_id, match)
        raw_json = _json(match.get("raw", {}))
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            existing = None
            if source_record_id:
                existing = connection.execute(
                    "SELECT id FROM duel_matches WHERE source=? AND source_record_id=?",
                    (source, source_record_id),
                ).fetchone()
            if existing is None:
                existing = connection.execute(
                    "SELECT id FROM duel_matches WHERE account_id=? AND source=? AND dedupe_key=?",
                    (account_id, source, dedupe_key),
                ).fetchone()

            values = (
                account_id,
                match.get("started_at"),
                match.get("score"),
                match.get("star"),
                match.get("self_ban"),
                match.get("opponent_ban"),
                match.get("result", "unknown"),
                match.get("duration"),
                int(bool(match.get("valid", True))),
                int(bool(match.get("practice_mode", False))),
                match.get("filename"),
                source,
                source_record_id,
                dedupe_key,
                raw_json,
            )
            if existing:
                match_id = int(existing["id"])
                connection.execute(
                    """UPDATE duel_matches SET account_id=?, started_at=?, score=?, star=?, self_ban=?,
                       opponent_ban=?, result=?, duration=?, valid=?, practice_mode=?, filename=?, source=?,
                       source_record_id=?, dedupe_key=?, raw_json=?, updated_at=? WHERE id=?""",
                    values + (now, match_id),
                )
                created = False
            else:
                cursor = connection.execute(
                    """INSERT INTO duel_matches
                       (account_id, started_at, score, star, self_ban, opponent_ban, result, duration, valid,
                        practice_mode, filename, source, source_record_id, dedupe_key, raw_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values + (now, now),
                )
                match_id = int(cursor.lastrowid)
                created = True

            connection.execute("DELETE FROM duel_picks WHERE match_id=?", (match_id,))
            for pick in match.get("picks", []):
                connection.execute(
                    "INSERT INTO duel_picks(match_id, side, round, shishen_id, count) VALUES (?, ?, ?, ?, ?)",
                    (
                        match_id,
                        pick["side"],
                        int(pick["round"]),
                        int(pick["shishen_id"]),
                        int(pick.get("count", 1)),
                    ),
                )
            return match_id, created

    def _match_from_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> DuelMatch:
        pick_rows = connection.execute(
            "SELECT side, round, shishen_id, count FROM duel_picks WHERE match_id=? ORDER BY round, side",
            (row["id"],),
        ).fetchall()
        return DuelMatch(
            id=row["id"],
            account_id=row["account_id"],
            started_at=row["started_at"],
            score=row["score"],
            star=row["star"],
            self_ban=row["self_ban"],
            opponent_ban=row["opponent_ban"],
            picks=[DuelPick(**dict(pick)) for pick in pick_rows],
            result=row["result"],
            duration=row["duration"],
            valid=bool(row["valid"]),
            practice_mode=bool(row["practice_mode"]),
            source=row["source"],
            source_record_id=row["source_record_id"],
        )

    def get_match(self, match_id: int) -> DuelMatch | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM duel_matches WHERE id=?", (match_id,)).fetchone()
            return self._match_from_row(connection, row) if row else None

    def list_matches(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        account_id: int | None = None,
        result: str | None = None,
        practice_mode: bool | None = None,
        valid: bool | None = None,
    ) -> DuelMatchList:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("account_id", account_id), ("result", result)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        for column, value in (("practice_mode", practice_mode), ("valid", valid)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(int(value))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM duel_matches{where}", params).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM duel_matches{where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [page_size, (page - 1) * page_size],
            ).fetchall()
            return DuelMatchList(
                total=total,
                page=page,
                page_size=page_size,
                items=[self._match_from_row(connection, row) for row in rows],
            )

    def patch_match(self, match_id: int, patch: DuelMatchPatch) -> DuelMatch | None:
        changes = patch.model_dump(exclude_unset=True)
        picks = changes.pop("picks", None)
        for required_key in ("result", "valid", "practice_mode"):
            if changes.get(required_key) is None:
                changes.pop(required_key, None)
        if not changes and picks is None:
            return self.get_match(match_id)
        with self._write_lock, self._connect() as connection:
            exists = connection.execute("SELECT id FROM duel_matches WHERE id=?", (match_id,)).fetchone()
            if not exists:
                return None
            if changes:
                for bool_key in ("valid", "practice_mode"):
                    if bool_key in changes:
                        changes[bool_key] = int(changes[bool_key])
                assignments = ", ".join(f"{key}=?" for key in changes)
                connection.execute(
                    f"UPDATE duel_matches SET {assignments}, updated_at=? WHERE id=?",
                    list(changes.values()) + [_utc_now(), match_id],
                )
            if picks is not None:
                connection.execute("DELETE FROM duel_picks WHERE match_id=?", (match_id,))
                for pick in picks:
                    item = pick if isinstance(pick, dict) else pick.model_dump()
                    connection.execute(
                        "INSERT INTO duel_picks(match_id, side, round, shishen_id, count) VALUES (?, ?, ?, ?, ?)",
                        (match_id, item["side"], item["round"], item["shishen_id"], item.get("count", 1)),
                    )
        return self.get_match(match_id)

    def summary(self) -> DuelSummary:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) total, SUM(valid) valid,
                   SUM(CASE WHEN valid=1 AND result='win' THEN 1 ELSE 0 END) wins,
                   SUM(CASE WHEN valid=1 AND result='loss' THEN 1 ELSE 0 END) losses,
                   SUM(CASE WHEN valid=1 AND result='unknown' THEN 1 ELSE 0 END) unknown_count,
                   SUM(practice_mode) practice, MAX(started_at) latest_at FROM duel_matches"""
            ).fetchone()
            top_rows = connection.execute(
                """SELECT p.shishen_id, COUNT(*) count,
                   SUM(CASE WHEN m.result='win' THEN 1 ELSE 0 END) wins
                   FROM duel_picks p JOIN duel_matches m ON m.id=p.match_id
                   WHERE p.side='self' AND m.valid=1
                   GROUP BY p.shishen_id ORDER BY count DESC, p.shishen_id LIMIT 10"""
            ).fetchall()
        total = int(row["total"] or 0)
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        decided = wins + losses
        return DuelSummary(
            total=total,
            valid=int(row["valid"] or 0),
            wins=wins,
            losses=losses,
            unknown=int(row["unknown_count"] or 0),
            practice=int(row["practice"] or 0),
            win_rate=round(wins / decided, 4) if decided else 0.0,
            latest_at=row["latest_at"],
            top_picks=[
                DuelTopPick(
                    shishen_id=item["shishen_id"],
                    count=item["count"],
                    wins=item["wins"],
                    win_rate=round(item["wins"] / item["count"], 4) if item["count"] else 0.0,
                )
                for item in top_rows
            ],
        )

    def upsert_strategy(self, strategy: dict[str, Any], *, source: str = "oas") -> tuple[int, bool]:
        now = _utc_now()
        name = strategy.get("name") or strategy.get("title") or "Unnamed strategy"
        enabled = int(bool(strategy.get("enabled", True)))
        content_json = _json(strategy.get("content", strategy))
        source_id = str(strategy.get("id") or strategy.get("source_strategy_id") or "").strip()
        if not source_id:
            source_id = "hash:" + hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        with self._write_lock, self._connect() as connection:
            existing = None
            if source_id:
                existing = connection.execute(
                    "SELECT id FROM duel_strategies WHERE source=? AND source_strategy_id=?",
                    (source, source_id),
                ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE duel_strategies SET name=?, content_json=?, enabled=?, updated_at=? WHERE id=?",
                    (name, content_json, enabled, now, existing["id"]),
                )
                return int(existing["id"]), False
            cursor = connection.execute(
                """INSERT INTO duel_strategies(name, content_json, enabled, source, source_strategy_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, content_json, enabled, source, source_id, now, now),
            )
            return int(cursor.lastrowid), True

    def list_strategies(self, *, enabled_only: bool = True) -> list[DuelStrategy]:
        where = " WHERE enabled=1" if enabled_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM duel_strategies{where} ORDER BY id"
            ).fetchall()
        strategies: list[DuelStrategy] = []
        for row in rows:
            try:
                content = json.loads(row["content_json"])
            except (TypeError, json.JSONDecodeError):
                content = None
            strategies.append(
                DuelStrategy(
                    id=row["id"],
                    name=row["name"],
                    content=content,
                    enabled=bool(row["enabled"]),
                    source=row["source"],
                    source_strategy_id=row["source_strategy_id"],
                )
            )
        return strategies

    def recommendation_matches(self) -> list[DuelMatch]:
        """Return decided, valid ranked matches used for personal recommendations."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM duel_matches
                   WHERE valid=1 AND practice_mode=0 AND result IN ('win', 'loss')
                   ORDER BY started_at, id"""
            ).fetchall()
            return [self._match_from_row(connection, row) for row in rows]

    def save_snapshot(self, snapshot_type: str, payload: Any, *, recommendation: bool = False, context: Any = None) -> bool:
        payload_json = _json(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = _utc_now()
        table = "recommendation_snapshots" if recommendation else "external_snapshots"
        kind_column = "recommendation_type" if recommendation else "snapshot_type"
        with self._write_lock, self._connect() as connection:
            if recommendation:
                cursor = connection.execute(
                    f"INSERT OR IGNORE INTO {table}(provider, {kind_column}, context_json, payload_json, payload_hash, captured_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("yysrank", snapshot_type, _json(context or {}), payload_json, payload_hash, now),
                )
            else:
                cursor = connection.execute(
                    f"INSERT OR IGNORE INTO {table}(provider, {kind_column}, payload_json, payload_hash, captured_at) VALUES (?, ?, ?, ?, ?)",
                    ("yysrank", snapshot_type, payload_json, payload_hash, now),
                )
            return cursor.rowcount > 0

    def latest_snapshot(self, snapshot_type: str, *, recommendation: bool = False) -> Any | None:
        table = "recommendation_snapshots" if recommendation else "external_snapshots"
        kind_column = "recommendation_type" if recommendation else "snapshot_type"
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {table} WHERE provider=? AND {kind_column}=? ORDER BY id DESC LIMIT 1",
                ("yysrank", snapshot_type),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
