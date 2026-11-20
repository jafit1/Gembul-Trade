"""Hybrid AI Judge: LLM-powered decision maker for trade entries & exits."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from gembul.core.models import Position, Signal
from gembul.llm.client import LlmClient
from gembul.safety.token_scan import SafetyReport

log = logging.getLogger(__name__)


@dataclass(slots=True)
class EntryVerdict:
    approved: bool
    confidence: float
    reasoning: str
    custom_tp_pct: float | None = None
    custom_sl_pct: float | None = None


@dataclass(slots=True)
class ExitVerdict:
    should_close: bool
    fraction: float
    reasoning: str


class AiJudge:
    def __init__(self, llm: LlmClient) -> None:
        self.llm = llm

    async def evaluate_entry(
        self,
        sig: Signal,
        report: SafetyReport | None = None,
        lessons: list[str] | None = None,
        default_tp: float = 25.0,
        default_sl: float = 6.0,
    ) -> EntryVerdict:
        """Asks LLM to evaluate candidate token fundamentals, safety, and order flow."""
        if not self.llm.ready:
            return EntryVerdict(approved=False, confidence=0.0, reasoning="AI unavailable; entry ditahan (fail-closed)")

        f = sig.features or {}
        context = {
            "token": sig.symbol,
            "chain": sig.chain or "solana",
            "price_usd": sig.price,
            "liquidity_usd": f.get("liquidity_usd"),
            "volume_24h": f.get("volume_24h"),
            "price_change_1h": f.get("chg_1h"),
            "age_minutes": f.get("age_minutes"),
            "buys_1h": f.get("buys_1h"),
            "sells_1h": f.get("sells_1h"),
            "strategy": sig.strategy,
            "safety_passed": report.ok if report else True,
            "safety_score": report.score if report else 1.0,
            "safety_notes": report.reasons if report else [],
            "relevant_lessons": lessons[:4] if lessons else [],
        }
        if "whale_label" in f:
            context["whale_tracking"] = {
                "whale": f.get("whale_label"),
                "wallet": f.get("whale"),
            }

        system = (
            "Kau adalah AI Trader Ahli Meme Coin Solana (GMGN / BullX style). "
            "Tugasmu: hindari HANYA scam/rugpull murni (honeypot atau dev cabut likuiditas). "
            "Jangan terlalu kaku: lonjakan harga tinggi (+100% s/d +3000%) adalah hal LUMRAH dan PELUANG BESAR di meme coin! "
            "Jika token punya transaksi aktif, ada pembeli, dan kontraknya tidak berbahaya, BERIKAN KEPUTUSAN LAYAK (approved: true). "
            "Keluarkan HANYA JSON valid dengan format: "
            '{"approved": bool, "confidence": float_0_to_1, "reasoning": "kalimat singkat bahasa indonesia", '
            f'"target_tp_pct": float, "target_sl_pct": float}}'
        )

        try:
            res = await self.llm.chat_json(system, json.dumps(context, default=str))
            approved = bool(res.get("approved", False))
            conf = float(res.get("confidence", 0.5))
            reason = str(res.get("reasoning") or "Evaluasi AI selesai.")
            tp = float(res.get("target_tp_pct", default_tp))
            sl = float(res.get("target_sl_pct", default_sl))
            return EntryVerdict(
                approved=approved,
                confidence=conf,
                reasoning=reason,
                custom_tp_pct=tp if tp > 0 else default_tp,
                custom_sl_pct=sl if sl > 0 else default_sl,
            )
        except Exception as exc:
            log.warning("AiJudge evaluate_entry gagal; entry ditahan: %s", exc)
            return EntryVerdict(approved=False, confidence=0.0, reasoning=f"AI unavailable; entry ditahan: {type(exc).__name__}")

    async def evaluate_exit(
        self,
        pos: Position,
        current_price: float,
        pnl_pct: float,
        peak_price: float,
        age_minutes: float,
    ) -> ExitVerdict:
        """Asks LLM whether to hold a running position or close now."""
        if not self.llm.ready:
            return ExitVerdict(should_close=False, fraction=0.0, reasoning="Hold kuantitatif")

        context = {
            "token": pos.symbol,
            "entry_price": pos.entry_price,
            "current_price": current_price,
            "peak_price": peak_price,
            "pnl_pct": round(pnl_pct, 2),
            "hold_minutes": round(age_minutes, 1),
            "scheme": pos.scheme,
        }

        system = (
            "Kau AI Trading Manager. Evaluasi posisi terbuka ini. "
            "Tentukan apakah harus HOLD (should_close: false) atau TUTUP/TAKE PROFIT (should_close: true). "
            "Keluarkan HANYA JSON: "
            '{"should_close": bool, "fraction": 1.0_atau_0.5, "reasoning": "alasan singkat bahasa indonesia"}'
        )

        try:
            res = await self.llm.chat_json(system, json.dumps(context, default=str))
            should_close = bool(res.get("should_close", False))
            frac = float(res.get("fraction", 1.0))
            reason = str(res.get("reasoning") or "Evaluasi AI exit.")
            return ExitVerdict(should_close=should_close, fraction=frac, reasoning=reason)
        except Exception as exc:
            log.debug("AiJudge evaluate_exit fallback: %s", exc)
            return ExitVerdict(should_close=False, fraction=0.0, reasoning=f"Hold (AI fallback: {exc})")
