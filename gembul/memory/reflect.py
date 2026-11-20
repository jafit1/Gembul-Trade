"""Reflection worker.

After trades close, it asks the model what went wrong, then distils the answer
into short reusable rules. Those rules later gate and resize new entries.
Without an LLM it still works: it derives mechanical lessons from the numbers.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from gembul.db import Database
from gembul.llm.client import LlmClient
from gembul.memory.store import MemoryStore

log = logging.getLogger(__name__)

SYSTEM = """Kau analis trading kuantitatif yang dingin dan jujur.
Tugasmu: dari satu catatan trade yang sudah ditutup, tarik pelajaran yang bisa dipakai ulang.
Aturan:
- Tulis maksimal 3 pelajaran.
- Tiap pelajaran satu kalimat imperatif, maksimal 20 kata, bahasa Indonesia.
- Fokus pada kondisi masuk (features), bukan pada hasil akhir.
- Jangan mengarang data yang tidak ada.
Balas JSON: {"lessons":[{"rule":"...","evidence":"..."}],"verdict":"win|loss|neutral"}"""


class Reflector:
    def __init__(self, db: Database, memory: MemoryStore, llm: LlmClient) -> None:
        self.db = db
        self.memory = memory
        self.llm = llm

    async def run_once(self, limit: int = 8) -> int:
        rows = await self.memory.pending_reflection(limit)
        done = 0
        for row in rows:
            try:
                await self._reflect(row)
                await self.memory.mark_reflected(row["id"])
                done += 1
            except Exception as exc:  # one bad episode must not stop the loop
                log.warning("reflection gagal untuk episode %s: %s", row["id"], exc)
        return done

    async def _reflect(self, row: Any) -> None:
        context = _loads(row["context"])
        action = _loads(row["action"])
        pnl = float(row["pnl_pct"] or 0.0)
        verdict = "win" if pnl > 0.5 else ("loss" if pnl < -0.5 else "neutral")

        scope = f"{row['chain'] or 'cex'}"
        symbol = row["symbol"] or "?"

        if not self.llm.ready:
            await self._mechanical(scope, symbol, context, action, pnl, verdict)
            return

        payload = {
            "symbol": symbol,
            "chain": row["chain"],
            "kind": row["kind"],
            "pnl_pct": round(pnl, 3),
            "entry": action,
            "features": context,
        }
        try:
            data = await self.llm.chat_json(SYSTEM, json.dumps(payload, default=str))
        except Exception as exc:
            log.warning("LLM reflection error: %s", exc)
            await self._mechanical(scope, symbol, context, action, pnl, verdict)
            return

        lessons = data.get("lessons") or []
        if not lessons:
            await self._mechanical(scope, symbol, context, action, pnl, verdict)
            return

        for item in lessons[:3]:
            rule = str(item.get("rule", "")).strip()
            if len(rule) < 8:
                continue
            evidence = str(item.get("evidence", ""))[:400]
            await self.memory.add_lesson(scope, rule, evidence, weight=1.0)
            log.info("lesson [%s] %s", scope, rule)

    async def _mechanical(self, scope: str, symbol: str, context: dict,
                          action: dict, pnl: float, verdict: str) -> None:
        """Cheap fallback rules distilled from numbers alone."""
        rules: list[tuple[str, str]] = []

        liq = _f(context.get("liquidity_usd"))
        holders = _f(context.get("holders"))
        top10 = _f(context.get("top10_pct"))
        age = _f(context.get("age_minutes"))
        score = _f(context.get("score"))

        if pnl < -3:
            if liq and liq < 120_000:
                rules.append(("Hindari token dengan likuiditas di bawah $120k.", f"liq={liq:.0f} pnl={pnl:.1f}"))
            if holders and holders < 600:
                rules.append(("Butuh minimal 600 holder sebelum masuk.", f"holders={holders:.0f} pnl={pnl:.1f}"))
            if top10 and top10 > 25:
                rules.append(("Lewati token bila top-10 holder di atas 25%.", f"top10={top10:.1f}% pnl={pnl:.1f}"))
            if age and age < 45:
                rules.append(("Jangan masuk 45 menit pertama setelah pool dibuat.", f"age={age:.0f}m pnl={pnl:.1f}"))
            if score and score < 0.6:
                rules.append(("Naikkan ambang skor sinyal ke 0.6 untuk rantai ini.", f"score={score:.2f}"))
        elif pnl > 3 and score and score > 0.7:
            rules.append((
                f"Setup skor>{score - 0.1:.1f} pada {scope} layak diperbesar perlahan.",
                f"pnl=+{pnl:.1f}",
            ))

        if not rules:
            rules.append((
                f"Catat ulang konteks {symbol}: hasil {verdict} pada {scope} belum memberi pola jelas.",
                f"pnl={pnl:.1f}",
            ))

        for rule, evidence in rules[:3]:
            await self.memory.add_lesson(scope, rule, evidence)


def _loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
