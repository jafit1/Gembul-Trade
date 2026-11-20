"""Risk engine. Nothing gets sized without passing through here.

Every rejection is written to memory so the reflection loop can learn from it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from gembul.core.models import OrderIntent, Position, RiskDecision, Venue
from gembul.db import Database
from gembul.settings import RiskLimits

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AccountState:
    equity_usd: float
    realized_today: float
    open_positions: list[Position]
    starting_equity_usd: float


class RiskEngine:
    def __init__(self, limits: RiskLimits, db: Database) -> None:
        self.limits = limits
        self.db = db
        self._halted_until: datetime | None = None
        self._halt_reason: str = ""

    # ------------------------------------------------------------------ state

    async def account_state(self, equity_usd: float) -> AccountState:
        rows = await self.db.fetchall(
            "SELECT * FROM positions WHERE status='open'"
        )
        positions = [_row_to_position(r) for r in rows]

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM positions "
            "WHERE status='closed' AND date(closed_at)=?",
            (today,),
        )
        realized = float(row["s"]) if row else 0.0
        start = await self.db.kv_get("day_start_equity", equity_usd)
        return AccountState(
            equity_usd=equity_usd,
            realized_today=realized,
            open_positions=positions,
            starting_equity_usd=float(start),
        )

    # ------------------------------------------------------------------ gates

    async def is_halted(self) -> tuple[bool, str]:
        if self._halted_until and datetime.now(timezone.utc) < self._halted_until:
            return True, self._halt_reason
        if self._halted_until:
            self._halted_until = None
            self._halt_reason = ""
        return False, ""

    async def halt(self, reason: str) -> None:
        until = datetime.now(timezone.utc) + timedelta(
            hours=self.limits.kill_switch_cooldown_hours
        )
        self._halted_until = until
        self._halt_reason = reason
        await self.db.kv_set("kill_switch", {"until": until.isoformat(), "reason": reason})
        await self.db.execute(
            "INSERT INTO episodes(kind,context,action,outcome) VALUES('risk','kill switch',?,?)",
            (reason, "halted"),
        )
        log.error("KILL SWITCH: %s until %s", reason, until.isoformat())

    async def resume(self, current_equity: float | None = None) -> None:
        self._halted_until = None
        self._halt_reason = ""
        await self.db.kv_del("kill_switch")
        if current_equity is not None and current_equity > 0:
            await self.db.kv_set("day_start_equity", current_equity)
        log.info("kill switch dilepas manual (equity=%s)", current_equity)

    # ------------------------------------------------------------------ sizing

    async def evaluate(self, intent: OrderIntent, state: AccountState,
                       extra: dict | None = None) -> RiskDecision:
        extra = extra or {}
        L = self.limits
        checks: dict[str, object] = {}

        halted, why = await self.is_halted()
        if halted:
            return RiskDecision(False, 0, f"kill switch aktif: {why}", checks)

        # drawdown gates
        dd_day = (state.realized_today / max(state.starting_equity_usd, 1e-9)) * 100
        checks["daily_dd_pct"] = round(dd_day, 2)
        if dd_day <= -L.daily_loss_limit_pct:
            await self.halt(f"daily loss {dd_day:.2f}% melewati batas {L.daily_loss_limit_pct}%")
            return RiskDecision(False, 0, "daily loss limit", checks)

        target_scheme = str(extra.get("scheme") or "plan1")
        scheme_open = [p for p in state.open_positions if getattr(p, "scheme", "plan1") in (target_scheme, "scalp" if target_scheme=="plan1" else "")]
        if len(scheme_open) >= 5:
            return RiskDecision(False, 0, f"posisi terbuka {target_scheme} sudah maksimum (5)", checks)

        # duplicate symbol within this scheme
        for p in scheme_open:
            if p.symbol == intent.symbol and p.venue == intent.venue:
                return RiskDecision(False, 0, f"posisi {intent.symbol} sudah terbuka di {target_scheme}", checks)

        scheme_exposure = sum(p.entry_usd for p in scheme_open)
        room_usd = max(0.0, 500.0 - scheme_exposure)

        # correlated cluster (same base asset root)
        root = intent.symbol.split("/")[0].upper()
        same_root = [p for p in state.open_positions
                     if p.symbol.split("/")[0].upper() == root]
        if len(same_root) >= L.max_correlated_positions:
            return RiskDecision(False, 0, f"terlalu banyak posisi berkorelasi pada {root}", checks)

        # size from risk budget
        stop_pct = float(extra.get("stop_pct", L.stop_loss_pct))
        risk_budget = state.equity_usd * (L.risk_per_trade_pct / 100)
        size_by_risk = risk_budget / max(stop_pct / 100, 1e-6)
        size_by_cap = state.equity_usd * (L.max_position_pct / 100)

        usd = min(size_by_risk, size_by_cap, room_usd, intent.usd_amount)
        checks["size_by_risk"] = round(size_by_risk, 2)
        checks["size_by_cap"] = round(size_by_cap, 2)
        checks["room_usd"] = round(room_usd, 2)

        if usd < 10:
            return RiskDecision(False, 0, f"ukuran final ${usd:.2f} di bawah minimum", checks)

        # per-venue hard caps
        if intent.venue is Venue.DEX:
            meme_cap = float(extra.get("meme_cap_usd", 0) or 0)
            hard = float(extra.get("max_position_usd", 1e9))
            usd = min(usd, hard, meme_cap or hard)

        # learned penalty: memories can shrink size
        penalty = float(extra.get("memory_penalty", 1.0))
        if penalty < 1.0:
            checks["memory_penalty"] = penalty
        usd *= max(0.15, min(penalty, 1.0))

        checks["final_usd"] = round(usd, 2)
        return RiskDecision(True, round(usd, 2), "ok", checks)

    # ------------------------------------------------------------------ stops

    async def refresh_stops(self, price_feed: dict[int, float]) -> list[tuple[Position, str, float]]:
        """Update legacy-position trailing stops using per-position, size-aware marks."""
        L = self.limits
        to_close: list[tuple[Position, str, float]] = []
        rows = await self.db.fetchall("SELECT * FROM positions WHERE status='open'")

        for row in rows:
            pos = _row_to_position(row)
            price = price_feed.get(pos.id)
            if not price or price <= 0:
                continue

            peak = max(pos.peak_price or pos.entry_price, price)
            if peak != pos.peak_price:
                await self.db.execute(
                    "UPDATE positions SET peak_price=? WHERE id=?", (peak, pos.id)
                )
                pos.peak_price = peak

            scheme = getattr(pos, "scheme", "plan1")
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0.0

            # ----------------- PLAN 1: TP LADDER (House Money) -----------------
            if scheme in ("plan1", "scalp"):
                meta = {}
                try:
                    if row["meta"]:
                        meta = json.loads(row["meta"])
                except Exception:
                    pass

                stage = int(meta.get("stage", 0))

                # Stop loss check (-6% initial, or BE +1% after TP1)
                if pos.stop_loss and price <= pos.stop_loss:
                    to_close.append((pos, f"Stop Loss ({pnl_pct:+.1f}%)", 1.0))
                    continue

                # Stage 0: waiting for TP1 (+25%) -> sell 50%, move SL to Breakeven (+1%)
                if stage == 0:
                    if price >= pos.entry_price * 1.25:
                        meta["stage"] = 1
                        be_stop = pos.entry_price * 1.01
                        await self.db.execute(
                            "UPDATE positions SET stop_loss=?, meta=? WHERE id=?",
                            (be_stop, json.dumps(meta), pos.id),
                        )
                        pos.stop_loss = be_stop
                        to_close.append((pos, f"TP1 Ladder +25% (Jual 50% & SL BE +1%)", 0.5))
                        continue

                # Stage 1: waiting for TP2 (+50%) -> sell 50% of remainder (25% total)
                elif stage == 1:
                    if price >= pos.entry_price * 1.50:
                        meta["stage"] = 2
                        runner_trail = peak * 0.90
                        await self.db.execute(
                            "UPDATE positions SET trailing_stop=?, meta=? WHERE id=?",
                            (runner_trail, json.dumps(meta), pos.id),
                        )
                        pos.trailing_stop = runner_trail
                        to_close.append((pos, f"TP2 Ladder +50% (Jual 25% & Trailing Runner 10%)", 0.5))
                        continue

                # Stage 2: Sisa 25% runner trailing 10% from peak
                elif stage >= 2:
                    new_trail = peak * 0.90
                    if pos.trailing_stop is None or new_trail > pos.trailing_stop:
                        await self.db.execute("UPDATE positions SET trailing_stop=? WHERE id=?", (new_trail, pos.id))
                        pos.trailing_stop = new_trail
                    if price <= pos.trailing_stop:
                        to_close.append((pos, f"Runner Trailing 10% ({pnl_pct:+.1f}%)", 1.0))
                        continue

            # ----------------- PLAN 2 & PLAN 3: Fixed TP / Trailing -----------------
            else:
                if pos.stop_loss and price <= pos.stop_loss:
                    to_close.append((pos, f"Stop Loss ({pnl_pct:+.1f}%)", 1.0))
                    continue

                if pos.take_profit and price >= pos.take_profit:
                    to_close.append((pos, f"Take Profit ({pnl_pct:+.1f}%)", 1.0))
                    continue

                from gembul.core.schemes import get_scheme
                sch_cfg = get_scheme(scheme)
                gain_pct = (peak - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0.0
                if gain_pct >= sch_cfg.trailing_activation_pct:
                    new_trail = peak * (1 - sch_cfg.trailing_distance_pct / 100)
                    if pos.trailing_stop is None or new_trail > pos.trailing_stop:
                        await self.db.execute("UPDATE positions SET trailing_stop=? WHERE id=?", (new_trail, pos.id))
                        pos.trailing_stop = new_trail
                    if price <= new_trail:
                        to_close.append((pos, f"Trailing Stop ({gain_pct:+.1f}%)", 1.0))
                        continue

        return to_close


def _row_to_position(row) -> Position:
    keys = row.keys()
    return Position(
        id=row["id"],
        venue=Venue(row["venue"]),
        symbol=row["symbol"],
        side=row["side"],
        qty=row["qty"],
        entry_price=row["entry_price"],
        entry_usd=row["entry_usd"],
        chain=row["chain"],
        stop_loss=row["stop_loss"],
        take_profit=row["take_profit"],
        trailing_stop=row["trailing_stop"],
        peak_price=row["peak_price"],
        status=row["status"],
        pnl_usd=row["pnl_usd"],
        pnl_pct=row["pnl_pct"],
        scheme=row["scheme"] if "scheme" in keys else "scalp",
        opened_at=row["opened_at"] if "opened_at" in keys else None,
    )
