#!/usr/bin/env python3
"""Export Gembul memory (lessons, tracked_whales, journal_logs, shadow_summary) from SQLite.

Produces clean, secret-free JSON/JSONL files under memory/ for version control and VPS migration.
Usage:
    python scripts/export_memory.py [--db data/gembul.sqlite] [--memory-dir memory]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def export_memory(db_path: Path, memory_dir: Path) -> None:
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    memory_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    lessons = [
        dict(r)
        for r in con.execute(
            "SELECT scope, rule, evidence, weight, hits, wins, created_at, updated_at "
            "FROM lessons ORDER BY id ASC"
        ).fetchall()
    ]
    with (memory_dir / "lessons.jsonl").open("w", encoding="utf-8") as f:
        for row in lessons:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    whales = [
        dict(r)
        for r in con.execute(
            "SELECT chain, address, label, auto_copy, copy_usd, total_trades, wins, losses, "
            "pnl_usd, last_active_at, created_at FROM tracked_whales ORDER BY id ASC"
        ).fetchall()
    ]
    (memory_dir / "tracked_whales.json").write_text(
        json.dumps(whales, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    journals = [
        dict(r)
        for r in con.execute(
            "SELECT period, period_key, scheme, total_trades, win_trades, loss_trades, "
            "win_rate, total_pnl_usd, avg_pnl_pct, best_trade, worst_trade, ai_summary, created_at "
            "FROM journal_logs ORDER BY id ASC"
        ).fetchall()
    ]
    (memory_dir / "journal_logs.json").write_text(
        json.dumps(journals, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    summary = {}
    if "shadow_runs" in tables:
        summary["shadow_runs"] = [
            dict(r) for r in con.execute("SELECT * FROM shadow_runs ORDER BY scheme").fetchall()
        ]
    if "shadow_positions" in tables:
        summary["shadow_positions_summary"] = [
            dict(r)
            for r in con.execute(
                """
                SELECT scheme, status, COUNT(*) n,
                       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
                       ROUND(SUM(COALESCE(pnl_usd, 0)), 4) pnl_usd,
                       ROUND(AVG(COALESCE(pnl_pct, 0)), 2) avg_pct
                FROM shadow_positions
                GROUP BY scheme, status
                """
            ).fetchall()
        ]
    (memory_dir / "shadow_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    con.close()
    print(f"Exported: {len(lessons)} lessons, {len(whales)} whales, {len(journals)} journals -> {memory_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Gembul memory from SQLite.")
    parser.add_argument("--db", type=Path, default=Path("data/gembul.sqlite"))
    parser.add_argument("--memory-dir", type=Path, default=Path("memory"))
    args = parser.parse_args()
    export_memory(args.db, args.memory_dir)


if __name__ == "__main__":
    main()
