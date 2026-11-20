"""SQLite layer. WAL mode, single writer, all state lives here."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    venue         TEXT NOT NULL,
    chain         TEXT,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           REAL NOT NULL,
    entry_price   REAL NOT NULL,
    entry_usd     REAL NOT NULL,
    stop_loss     REAL,
    take_profit   REAL,
    trailing_stop REAL,
    peak_price    REAL,
    status        TEXT NOT NULL DEFAULT 'open',
    opened_at     TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at     TEXT,
    exit_price    REAL,
    pnl_usd       REAL,
    pnl_pct       REAL,
    fees_usd      REAL DEFAULT 0,
    signal_id     INTEGER,
    meta          TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions(id) ON DELETE CASCADE,
    side        TEXT NOT NULL,
    qty         REAL NOT NULL,
    price       REAL NOT NULL,
    fee_usd     REAL DEFAULT 0,
    tx_hash     TEXT,
    venue       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    chain       TEXT,
    score       REAL NOT NULL,
    features    TEXT,
    accepted    INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS equity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    equity_usd REAL NOT NULL,
    realized   REAL NOT NULL DEFAULT 0,
    unrealized REAL NOT NULL DEFAULT 0,
    venue      TEXT DEFAULT 'all'
);

CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    symbol      TEXT,
    chain       TEXT,
    context     TEXT NOT NULL,
    action      TEXT NOT NULL,
    outcome     TEXT,
    pnl_pct     REAL,
    reflected   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lessons (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    scope     TEXT NOT NULL,
    rule      TEXT NOT NULL,
    evidence  TEXT,
    weight    REAL NOT NULL DEFAULT 1.0,
    hits      INTEGER NOT NULL DEFAULT 0,
    wins      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(
    rule, evidence, content='lessons', content_rowid='id', tokenize='porter'
);

CREATE TRIGGER IF NOT EXISTS lessons_ai AFTER INSERT ON lessons BEGIN
    INSERT INTO lessons_fts(rowid, rule, evidence) VALUES (new.id, new.rule, new.evidence);
END;
CREATE TRIGGER IF NOT EXISTS lessons_au AFTER UPDATE ON lessons BEGIN
    INSERT INTO lessons_fts(lessons_fts, rowid, rule, evidence)
    VALUES ('delete', old.id, old.rule, old.evidence);
    INSERT INTO lessons_fts(rowid, rule, evidence) VALUES (new.id, new.rule, new.evidence);
END;

CREATE TABLE IF NOT EXISTS tokens_seen (
    chain       TEXT NOT NULL,
    address     TEXT NOT NULL,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now')),
    score       REAL,
    verdict     TEXT,
    payload     TEXT,
    PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS tracked_whales (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chain         TEXT NOT NULL DEFAULT 'solana',
    address       TEXT NOT NULL,
    label         TEXT,
    auto_copy     INTEGER NOT NULL DEFAULT 1,
    copy_usd      REAL NOT NULL DEFAULT 100.0,
    last_sig      TEXT,
    total_trades  INTEGER NOT NULL DEFAULT 0,
    wins          INTEGER NOT NULL DEFAULT 0,
    losses        INTEGER NOT NULL DEFAULT 0,
    pnl_usd       REAL NOT NULL DEFAULT 0.0,
    last_active_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chain, address)
);
CREATE INDEX IF NOT EXISTS idx_tracked_whales_chain ON tracked_whales(chain);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id      TEXT PRIMARY KEY,
    scheme       TEXT NOT NULL DEFAULT 'scalp',
    auto_trade   INTEGER NOT NULL DEFAULT 1,
    trade_size   REAL NOT NULL DEFAULT 100.0,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS journal_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    period        TEXT NOT NULL,
    period_key    TEXT NOT NULL,
    scheme        TEXT NOT NULL,
    total_trades  INTEGER NOT NULL DEFAULT 0,
    win_trades    INTEGER NOT NULL DEFAULT 0,
    loss_trades   INTEGER NOT NULL DEFAULT 0,
    win_rate      REAL NOT NULL DEFAULT 0.0,
    total_pnl_usd REAL NOT NULL DEFAULT 0.0,
    avg_pnl_pct   REAL NOT NULL DEFAULT 0.0,
    best_trade    TEXT,
    worst_trade   TEXT,
    ai_summary    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(period, period_key, scheme)
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.executescript(SCHEMA)
            for col in (
                "ALTER TABLE positions ADD COLUMN scheme TEXT DEFAULT 'scalp'",
                "ALTER TABLE positions ADD COLUMN account_id TEXT DEFAULT 'default'",
                "ALTER TABLE tracked_whales ADD COLUMN wins INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE tracked_whales ADD COLUMN losses INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE tracked_whales ADD COLUMN pnl_usd REAL NOT NULL DEFAULT 0.0",
                "ALTER TABLE tracked_whales ADD COLUMN last_active_at TEXT NOT NULL DEFAULT (datetime('now'))",
            ):
                try:
                    await self._conn.execute(col)
                except Exception:
                    pass
            await self._conn.commit()
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[aiosqlite.Cursor]:
        conn = await self.connect()
        cur = await conn.cursor()
        try:
            yield cur
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await cur.close()

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        conn = await self.connect()
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        conn = await self.connect()
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        conn = await self.connect()
        cur = await conn.execute(sql, params)
        await conn.commit()
        last = cur.lastrowid or 0
        await cur.close()
        return last

    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT v FROM kv WHERE k=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except json.JSONDecodeError:
            return row["v"]

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value, default=str)),
        )

    async def kv_del(self, key: str) -> None:
        await self.execute("DELETE FROM kv WHERE k=?", (key,))
