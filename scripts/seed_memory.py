#!/usr/bin/env python3
"""Seed Gembul SQLite database from exported memory files (lessons, whales, journals).

Zero third-party dependencies (stdlib sqlite3 only).
Idempotent: safe to re-run on a fresh VPS without duplicating existing rows.
Usage:
    python scripts/seed_memory.py [--db data/gembul.sqlite] [--memory-dir memory]
"""
from __future__ import annotations

import argparse
import ast
import json
import sqlite3
from pathlib import Path


def _load_schema() -> str:
    db_py = Path(__file__).resolve().parent.parent / "gembul" / "db.py"
    tree = ast.parse(db_py.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SCHEMA":
                    return ast.literal_eval(node.value)
    raise RuntimeError(f"SCHEMA constant not found in {db_py}")


def seed(db_path: Path, memory_dir: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.executescript(_load_schema())

    lessons_file = memory_dir / "lessons.jsonl"
    inserted_lessons = 0
    if lessons_file.exists():
        existing = {
            (r["scope"], r["rule"])
            for r in con.execute("SELECT scope, rule FROM lessons").fetchall()
        }
        rows_to_insert = []
        with lessons_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                key = (item["scope"], item["rule"])
                if key in existing:
                    continue
                existing.add(key)
                rows_to_insert.append((
                    item["scope"],
                    item["rule"],
                    item.get("evidence", ""),
                    float(item.get("weight", 1.0)),
                    int(item.get("hits", 0)),
                    int(item.get("wins", 0)),
                    item.get("created_at"),
                    item.get("updated_at"),
                ))
        if rows_to_insert:
            con.executemany(
                "INSERT INTO lessons(scope, rule, evidence, weight, hits, wins, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), COALESCE(?, datetime('now')))",
                rows_to_insert,
            )
            inserted_lessons = len(rows_to_insert)

    whales_file = memory_dir / "tracked_whales.json"
    inserted_whales = 0
    if whales_file.exists():
        whales = json.loads(whales_file.read_text(encoding="utf-8"))
        for w in whales:
            cur = con.execute(
                "INSERT OR IGNORE INTO tracked_whales("
                "chain, address, label, auto_copy, copy_usd, total_trades, wins, losses, pnl_usd, last_active_at, created_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), COALESCE(?, datetime('now')))",
                (
                    w.get("chain", "solana"),
                    w["address"],
                    w.get("label"),
                    int(w.get("auto_copy", 1)),
                    float(w.get("copy_usd", 100.0)),
                    int(w.get("total_trades", 0)),
                    int(w.get("wins", 0)),
                    int(w.get("losses", 0)),
                    float(w.get("pnl_usd", 0.0)),
                    w.get("last_active_at"),
                    w.get("created_at"),
                ),
            )
            if cur.rowcount:
                inserted_whales += 1

    journals_file = memory_dir / "journal_logs.json"
    inserted_journals = 0
    if journals_file.exists():
        journals = json.loads(journals_file.read_text(encoding="utf-8"))
        for j in journals:
            cur = con.execute(
                "INSERT OR IGNORE INTO journal_logs("
                "period, period_key, scheme, total_trades, win_trades, loss_trades, "
                "win_rate, total_pnl_usd, avg_pnl_pct, best_trade, worst_trade, ai_summary, created_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
                (
                    j["period"],
                    j["period_key"],
                    j["scheme"],
                    int(j.get("total_trades", 0)),
                    int(j.get("win_trades", 0)),
                    int(j.get("loss_trades", 0)),
                    float(j.get("win_rate", 0.0)),
                    float(j.get("total_pnl_usd", 0.0)),
                    float(j.get("avg_pnl_pct", 0.0)),
                    j.get("best_trade"),
                    j.get("worst_trade"),
                    j.get("ai_summary"),
                    j.get("created_at"),
                ),
            )
            if cur.rowcount:
                inserted_journals += 1

    con.commit()
    con.close()
    print(
        f"Memory seeded into {db_path}: "
        f"+{inserted_lessons} lessons, +{inserted_whales} whales, +{inserted_journals} journal_logs"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Gembul memory into SQLite.")
    parser.add_argument("--db", type=Path, default=Path("data/gembul.sqlite"))
    parser.add_argument("--memory-dir", type=Path, default=Path("memory"))
    args = parser.parse_args()
    seed(args.db, args.memory_dir)


if __name__ == "__main__":
    main()
