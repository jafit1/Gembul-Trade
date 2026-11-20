"""Trading performance journal and daily/weekly AI learning synthesizer."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from gembul.core.schemes import SCHEMES, get_scheme
from gembul.db import Database
from gembul.llm.client import LlmClient
from gembul.memory.store import MemoryStore

log = logging.getLogger(__name__)


class JournalManager:
    def __init__(self, db: Database, memory: MemoryStore, llm: LlmClient, shadow: Any = None) -> None:
        self.db = db
        self.memory = memory
        self.llm = llm
        self.shadow = shadow

    async def get_metrics(
        self, period: str = "daily", scheme: str = "all", date_str: str | None = None
    ) -> dict[str, Any]:
        """Calculates closed position performance metrics for a given timeframe & scheme."""
        where_clauses = ["status='closed'"]
        params: list[Any] = []

        if period == "daily":
            d = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            where_clauses.append("date(closed_at) = ?")
            params.append(d)
            period_key = d
        else:
            where_clauses.append("closed_at >= datetime('now', '-7 days')")
            period_key = datetime.now(timezone.utc).strftime("%Y-W%W")

        if scheme in ("plan1", "scalp"):
            where_clauses.append("scheme IN ('plan1', 'scalp')")
        elif scheme in ("plan2", "breakout"):
            where_clauses.append("scheme IN ('plan2', 'breakout')")
        elif scheme in ("plan3", "safe", "sniper"):
            where_clauses.append("scheme IN ('plan3', 'safe', 'sniper')")

        sql = f"SELECT * FROM positions WHERE {' AND '.join(where_clauses)} ORDER BY id ASC"
        rows = await self.db.fetchall(sql, tuple(params))

        # Also get open positions count & unrealized pnls
        open_where = ["status='open'"]
        if scheme in ("plan1", "scalp"):
            open_where.append("scheme IN ('plan1', 'scalp')")
        elif scheme in ("plan2", "breakout"):
            open_where.append("scheme IN ('plan2', 'breakout')")
        elif scheme in ("plan3", "safe", "sniper"):
            open_where.append("scheme IN ('plan3', 'safe', 'sniper')")

        open_rows = await self.db.fetchall(f"SELECT pnl_usd FROM positions WHERE {' AND '.join(open_where)}")
        open_count = len(open_rows)
        unrealized_usd = round(sum((r["pnl_usd"] or 0.0) for r in open_rows), 2)

        total = len(rows)
        if total == 0:
            return {
                "period": period,
                "period_key": period_key,
                "scheme": scheme,
                "total_trades": 0,
                "win_trades": 0,
                "loss_trades": 0,
                "win_rate": 0.0,
                "total_pnl_usd": 0.0,
                "avg_pnl_pct": 0.0,
                "open_count": open_count,
                "unrealized_usd": unrealized_usd,
                "current_equity": round(1000.0 + unrealized_usd, 2),
                "best_trade": "-",
                "worst_trade": "-",
                "trades": [],
            }

        wins = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
        losses = sum(1 for r in rows if (r["pnl_usd"] or 0) <= 0)
        win_rate = round(wins / total * 100, 1)
        total_pnl_usd = round(sum((r["pnl_usd"] or 0) for r in rows), 2)
        avg_pnl_pct = round(sum((r["pnl_pct"] or 0) for r in rows) / total, 2)

        sorted_by_pct = sorted(rows, key=lambda r: (r["pnl_pct"] or 0), reverse=True)
        best = sorted_by_pct[0]
        worst = sorted_by_pct[-1]

        best_str = f"{best['symbol']} ({best['pnl_pct']:+.1f}%)" if best else "-"
        worst_str = f"{worst['symbol']} ({worst['pnl_pct']:+.1f}%)" if worst else "-"
        current_equity = round(1000.0 + total_pnl_usd + unrealized_usd, 2)

        return {
            "period": period,
            "period_key": period_key,
            "scheme": scheme,
            "total_trades": total,
            "win_trades": wins,
            "loss_trades": losses,
            "win_rate": win_rate,
            "total_pnl_usd": total_pnl_usd,
            "avg_pnl_pct": avg_pnl_pct,
            "open_count": open_count,
            "unrealized_usd": unrealized_usd,
            "current_equity": current_equity,
            "best_trade": best_str,
            "worst_trade": worst_str,
            "trades": [
                {
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "scheme": r["scheme"] if "scheme" in r.keys() else "plan1",
                    "pnl_usd": r["pnl_usd"],
                    "pnl_pct": r["pnl_pct"],
                }
                for r in rows
            ],
        }

    async def compare_three_plans(self, period: str = "daily") -> dict[str, Any]:
        """Uses isolated quote-aware shadow ledgers only; contaminated legacy positions are excluded."""
        if getattr(self, "shadow", None) is not None:
            reports = await self.shadow.compare()
            by_name = {r["scheme"]: r for r in reports}
            p1 = by_name.get("plan1", {})
            p2 = by_name.get("plan2", {})
            p3 = by_name.get("plan3", {})
            all_trades = []
            for p in (p1, p2, p3):
                all_trades.extend(p.get("trades") or [])
            tot_n = sum(int(p.get("total_trades") or 0) for p in (p1, p2, p3))
            tot_w = sum(int(p.get("win_trades") or 0) for p in (p1, p2, p3))
            tot_l = sum(int(p.get("loss_trades") or 0) for p in (p1, p2, p3))
            tot_pnl = round(sum(float(p.get("total_pnl_usd") or 0.0) for p in (p1, p2, p3)), 4)
            tot_unreal = round(sum(float(p.get("unrealized_usd") or 0.0) for p in (p1, p2, p3)), 4)
            tot_open = sum(int(p.get("open_count") or 0) for p in (p1, p2, p3))
            best = max(all_trades, key=lambda t: float(t.get("pnl_pct") or 0.0), default=None)
            worst = min(all_trades, key=lambda t: float(t.get("pnl_pct") or 0.0), default=None)
            avg_pct = round(
                sum(float(t.get("pnl_pct") or 0.0) for t in all_trades) / len(all_trades), 4
            ) if all_trades else 0.0
            period_key = p1.get("period_key") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return {
                "period": period,
                "plan1": p1,
                "plan2": p2,
                "plan3": p3,
                "total": {
                    "period_key": period_key,
                    "total_trades": tot_n,
                    "win_trades": tot_w,
                    "loss_trades": tot_l,
                    "win_rate": round(100.0 * tot_w / tot_n, 2) if tot_n else 0.0,
                    "total_pnl_usd": tot_pnl,
                    "avg_pnl_pct": avg_pct,
                    "open_count": tot_open,
                    "unrealized_usd": tot_unreal,
                    "current_equity": round(3000.0 + tot_pnl, 4),
                    "best_trade": f"{best['symbol']} ({float(best['pnl_pct']):+.1f}%)" if best else "-",
                    "worst_trade": f"{worst['symbol']} ({float(worst['pnl_pct']):+.1f}%)" if worst else "-",
                },
                "source": "isolated_shadow_ledgers",
            }
        # Explicitly mark historical trade-table comparisons as contaminated legacy data.
        return {
            "period": period,
            "plan1": {}, "plan2": {}, "plan3": {}, "total": {},
            "source": "legacy_not_comparable",
        }

    async def generate_ai_journal(self, period: str = "daily") -> dict[str, Any]:
        """Synthesizes 3-way plan metrics into an evaluation and saves to journal_logs."""
        comp = await self.compare_three_plans(period=period)
        tot = comp["total"]
        p1 = comp["plan1"]
        p2 = comp["plan2"]
        p3 = comp["plan3"]

        if tot["total_trades"] == 0:
            summary = "Eksperimen baru dimulai dari $1,000. Belum ada trade yang ditutup pada periode ini."
        elif not self.llm.ready:
            summary = (
                f"Hasil Realtime (Modal Awal: $1000):\n"
                f"• Scalping 1m/5m (Plan 1): {p1['total_trades']} trade, WR {p1['win_rate']}%, PnL ${p1['total_pnl_usd']:+.2f} (Saldo: ${p1['current_equity']:,.2f})"
            )
        else:
            prompt_data = {
                "period": period,
                "plan1_ladder": {
                    "trades": p1["total_trades"], "win_rate": p1["win_rate"],
                    "realized_pnl": p1["total_pnl_usd"], "equity": p1["current_equity"],
                },
                "plan2_breakout": {
                    "trades": p2["total_trades"], "win_rate": p2["win_rate"],
                    "realized_pnl": p2["total_pnl_usd"], "equity": p2["current_equity"],
                },
                "plan3_sniper": {
                    "trades": p3["total_trades"], "win_rate": p3["win_rate"],
                    "realized_pnl": p3["total_pnl_usd"], "equity": p3["current_equity"],
                },
            }
            system = (
                "Kau mentor trading kuantitatif. Bandingkan performa 3 skema trading meme coin ini: "
                "Rencana 1 (TP Ladder House Money), Rencana 2 (Breakout Momentum), dan Rencana 3 (Volume-Delta Sniper). "
                "Modal awal disamakan $1,000 masing-masing. Berikan evaluasi tajam dan strategi mana yang paling unggul. "
                "Bahasa Indonesia, padat, maksimal 130 kata."
            )
            try:
                summary = await self.llm.chat(system, json.dumps(prompt_data))
                lesson_text = f"[{period.upper()} 3-PLAN REVIEW] {summary[:240].replace(chr(10), ' ')}"
                await self.memory.add_lesson("journal", lesson_text, "ai_review", weight=1.5)
            except Exception as exc:
                log.warning("generate_ai_journal llm error: %s", exc)
                summary = (
                    f"Rangkuman: P1 ${p1['total_pnl_usd']:+.2f} | P2 ${p2['total_pnl_usd']:+.2f} | "
                    f"P3 ${p3['total_pnl_usd']:+.2f}. (AI review pending: {exc})"
                )

        # Save to journal_logs
        await self.db.execute(
            "INSERT INTO journal_logs(period, period_key, scheme, total_trades, win_trades, "
            "loss_trades, win_rate, total_pnl_usd, avg_pnl_pct, best_trade, worst_trade, ai_summary) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(period, period_key, scheme) DO UPDATE SET "
            "total_trades=excluded.total_trades, win_trades=excluded.win_trades, "
            "loss_trades=excluded.loss_trades, win_rate=excluded.win_rate, "
            "total_pnl_usd=excluded.total_pnl_usd, avg_pnl_pct=excluded.avg_pnl_pct, "
            "best_trade=excluded.best_trade, worst_trade=excluded.worst_trade, "
            "ai_summary=excluded.ai_summary, created_at=datetime('now')",
            (
                period,
                tot["period_key"],
                "all",
                tot["total_trades"],
                tot["win_trades"],
                tot["loss_trades"],
                tot["win_rate"],
                tot["total_pnl_usd"],
                tot["avg_pnl_pct"],
                tot["best_trade"],
                tot["worst_trade"],
                summary,
            ),
        )

        return {
            **comp,
            "summary": summary,
        }
