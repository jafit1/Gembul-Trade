"""Memory: episodes -> reflections -> lessons -> retrieval.

This is the part that makes the bot compound instead of repeating mistakes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from gembul.db import Database

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Lesson:
    id: int
    scope: str
    rule: str
    evidence: str
    weight: float
    hits: int
    wins: int

    @property
    def reliability(self) -> float:
        if self.hits <= 0:
            return 0.5
        return self.wins / self.hits


class MemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ episodes

    async def record_episode(self, kind: str, context: dict[str, Any],
                             action: dict[str, Any], *,
                             symbol: str | None = None,
                             chain: str | None = None,
                             outcome: str | None = None,
                             pnl_pct: float | None = None) -> int:
        return await self.db.execute(
            "INSERT INTO episodes(kind,symbol,chain,context,action,outcome,pnl_pct) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                kind,
                symbol,
                chain,
                json.dumps(context, default=str),
                json.dumps(action, default=str),
                outcome,
                pnl_pct,
            ),
        )

    async def pending_reflection(self, limit: int = 10) -> list[Any]:
        return await self.db.fetchall(
            "SELECT * FROM episodes WHERE reflected=0 AND outcome IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    async def mark_reflected(self, episode_id: int) -> None:
        await self.db.execute("UPDATE episodes SET reflected=1 WHERE id=?", (episode_id,))

    # ------------------------------------------------------------ lessons

    async def add_lesson(self, scope: str, rule: str, evidence: str = "",
                         weight: float = 1.0) -> int:
        existing = await self.db.fetchone(
            "SELECT id FROM lessons WHERE scope=? AND rule=?", (scope, rule)
        )
        if existing:
            await self.db.execute(
                "UPDATE lessons SET weight=MIN(weight+0.15, 3.0), "
                "updated_at=datetime('now') WHERE id=?",
                (existing["id"],),
            )
            return int(existing["id"])
        return await self.db.execute(
            "INSERT INTO lessons(scope,rule,evidence,weight) VALUES(?,?,?,?)",
            (scope, rule, evidence, weight),
        )

    async def lessons_for(self, query: str, scope: str, limit: int = 8) -> list[Lesson]:
        """FTS match, then fall back to weighted recent lessons."""
        rows: list[Any] = []
        if query.strip():
            try:
                rows = await self.db.fetchall(
                    "SELECT l.* FROM lessons_fts f JOIN lessons l ON l.id=f.rowid "
                    "WHERE lessons_fts MATCH ? AND l.scope IN (?, 'global') "
                    "ORDER BY bm25(lessons_fts), l.weight DESC LIMIT ?",
                    (_fts_query(query), scope, limit),
                )
            except Exception:
                rows = []

        if len(rows) < limit:
            seen = {r["id"] for r in rows}
            extra = await self.db.fetchall(
                "SELECT * FROM lessons WHERE scope IN (?, 'global') "
                "ORDER BY weight DESC, updated_at DESC LIMIT ?",
                (scope, limit - len(rows) + len(seen)),
            )
            rows.extend([r for r in extra if r["id"] not in seen])

        return [
            Lesson(r["id"], r["scope"], r["rule"], r["evidence"] or "",
                   r["weight"], r["hits"], r["wins"])
            for r in rows[:limit]
        ]

    async def score_penalty(self, query: str, scope: str) -> tuple[float, list[str]]:
        """Turn confirmed losing patterns into a size penalty. Neutral/new lessons never penalize."""
        lessons = await self.lessons_for(query, scope, limit=6)
        if not lessons:
            return 1.0, []
        weight = 0.0
        used: list[str] = []
        for ls in lessons:
            # Only penalize if this pattern has repeatedly failed in real trading
            if ls.hits >= 2 and ls.reliability < 0.40:
                weight += min(ls.weight, 2.0)
                used.append(ls.rule)
        if weight <= 0.0:
            return 1.0, []
        penalty = 1.0 / (1.0 + weight * 0.15)
        return max(0.5, min(1.0, penalty)), used

    async def bump_hit(self, lesson_id: int, won: bool) -> None:
        await self.db.execute(
            "UPDATE lessons SET hits=hits+1, wins=wins+? WHERE id=?",
            (1 if won else 0, lesson_id),
        )

    # ------------------------------------------------------------ stats

    async def stats(self) -> dict[str, Any]:
        trades = await self.db.fetchone(
            "SELECT COUNT(*) n, COALESCE(AVG(pnl_pct),0) avg_pct, "
            "COALESCE(SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END),0) wins "
            "FROM positions WHERE status='closed'"
        )
        lessons = await self.db.fetchone("SELECT COUNT(*) n FROM lessons")
        episodes = await self.db.fetchone("SELECT COUNT(*) n FROM episodes")
        n = int(trades["n"] or 0)
        wins = int(trades["wins"] or 0)
        return {
            "trades": n,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "avg_pnl_pct": round(float(trades["avg_pct"] or 0), 2),
            "lessons": int(lessons["n"] or 0),
            "episodes": int(episodes["n"] or 0),
        }

    # ------------------------------------------------------------ recent ctx

    async def recent_context(self, symbol: str, limit: int = 5) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT * FROM positions WHERE symbol=? ORDER BY id DESC LIMIT ?",
            (symbol, limit),
        )
        return [dict(r) for r in rows]


def _fts_query(text: str) -> str:
    tokens = [t for t in "".join(
        c if c.isalnum() or c in "/-_ " else " " for c in text
    ).split() if len(t) > 2]
    return " OR ".join(tokens[:12]) or '""'
