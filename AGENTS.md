# gembul — Solana meme-coin paper/shadow trading bot

Runtime lives on VPS `gembul-agent.boxd` at `/srv/gembul` (systemd `gembul.service`, venv `.venv`).
This folder is the synced copy of that source tree. Edit here, deploy with tar+scp, never edit on the server.

## What it is

- Python 3.12, asyncio, aiosqlite (single-writer SQLite at `/srv/gembul/data/gembul.sqlite`).
- Signal scanners: DexScreener meme screener, whale follower tracker.
- Safety: GoPlus / RugCheck token scan (`gembul/safety/token_scan.py`).
- LLM judge: TokenHarbor `deepseek-v4.1-flash:free`, fail-closed (no LLM → no entry).
- Venues: `gembul/venues/dex` (Jupiter quotes on Solana), `gembul/venues/cex` (ccxt, Binance).
- Telegram control surface: `gembul/telegram/bot.py`.
- Shadow research: `gembul/core/shadow.py` — three isolated $1,000 ledgers (plan1/plan2/plan3).

## Current state (2026-11-18)

- **Wallet trading halt is ON**: `kv.kill_switch` until 2099, `user_profiles.auto_trade=0` for every row.
  Clearing requires deleting the kv row. Do not re-enable without fresh out-of-sample evidence.
- `Engine.hunt()` runs shadow research only. The legacy order executor below the `return` in `hunt()`
  is unreachable dead code kept for reference.
- Old `positions` table data (43 closed trades, realized ≈ -$561) is **contaminated** and not comparable:
  ticker collisions, a decimals bug, and DexScreener marks instead of executable quotes. Kept for audit only.
- Every shadow sample collected before 2026-11-18 was voided by the decimals fix. Ledgers restarted at $1,000.

## Accounting rules (do not break these)

- Token identity = `chain` + `mint` (+ pool). Never ticker.
- Paper fills use size-aware executable quotes: buy `$X` in USDC, then quote the whole position back.
  Reject entry when roundtrip cost > 4% or price impact > 1%.
- Raw token amounts are `qty * 10**decimals`, decimals resolved per mint via RPC and cached in
  `shadow_positions.meta.decimals`. Assuming 6 decimals fabricates -99.9% marks (this happened once).
- Partial exits (plan1 TP1/TP2) re-quote the exact partial raw amount and reduce `qty`/`entry_usd` basis.
- PnL is only ever computed from fills/quotes, never from a chart mark.

## Deploy

```powershell
cd "C:\Users\T14s G1 AMD R5 4560U\gembul-src"
$n = "up" + (Get-Random) + ".tgz"
tar -czf $n gembul config
scp $n "gembul-agent.boxd:/tmp/$n"
ssh gembul-agent.boxd "cd /srv/gembul && tar -xzf /tmp/$n && rm -f /tmp/$n"
# restart — note the bracketed pkill pattern, a plain one kills its own shell
ssh gembul-agent.boxd "sudo pkill -9 -f '[g]embul run'; sleep 1; sudo systemctl restart gembul.service"
```

Always use a fresh random tgz name; `scp` to an existing remote path fails with `Operation unsupported`.

Remote one-off scripts: write them locally, `scp`, then run. PowerShell mangles inline heredocs and
quotes over ssh — do not pipe python with quotes inside the ssh command.

## Layout

```
gembul/core/      engine.py (loop), risk.py (sizing/caps), shadow.py (isolated ledgers), schemes.py
gembul/venues/    dex/{solana,evm,__init__}.py, cex.py, freqtrade_bridge.py
gembul/signals/   meme.py, whale.py
gembul/safety/    token_scan.py
gembul/llm/       client.py, judge.py
gembul/memory/    journal.py, reflect.py, store.py
gembul/telegram/  bot.py
config/           config.yaml (runtime), .env (secrets, gitignored)
deploy/           gembul.service
```

## Known weak points & recent upgrades (2026-11-18)

- **Shadow accounting & throttle fixes**: Fixed 4 critical bugs in `shadow.py` & `journal.py`:
  1. Removed premature `_last_scan` throttle inside `ShadowBook.consider()` that previously dropped signals #2..#12 whenever signal #1 failed a filter.
  2. Enforced `row["stop_loss"]` in `plan1` (so BE +1.5% after TP1 and +20% after TP2 actually trigger).
  3. Computed final `pnl_pct` against `initial_entry_usd` ($10) instead of remaining `entry_usd` (which previously inflated partial-exit ROIs 2-4x, e.g. +35.7% reported as +142.8%).
  4. Added `"total"` aggregation in `JournalManager.compare_three_plans()` so `generate_ai_journal()` never KeyErrors.
- **DEX entry/exit upgrades**: Multi-source discovery (`GeckoTerminal` p1-2 + `DexScreener` boosts/m5 enrichment + `Binance Web3` trending & `smart-money-inflow`), 5m OHLCV gate (EMA20, RSI 42-73, volume ratio >=0.8x, extension <=15%), tightened Jupiter roundtrip cost <=1.6% / impact <=0.6%, persistent 4h loss cooldown per mint, ATR-scaled SL (8-11% for `plan1`), and 90-120m stale time-stop.
- **CEX Freqtrade (`GembulSwing`)**: Fixed `config.json` `timeframe` override (`15m` -> `1h`), fixed `minimal_roi` units (minutes, not hours), replaced dip-in-uptrend with walk-forward validated `z20_snapback` (`Z < -2.0`, `RSI14 < 30`, green reversal, `vol > 1.2x`, `ema20 > ema50 * 0.965`, `ema20` reversion exit, `-4%` SL, `24h` time-stop). Validated positive in both IS (`Apr-Aug` bear -21.8%: +1.10%, PF 1.28) and OOS (`Aug-Nov`: +0.82%, PF 1.22), full 7m Sharpe 1.07, max DD 1.63%.
- Free LLM quota exhaustion means legacy `pursue()` entries are denied (fail-closed by design). Check quota before concluding the strategy is idle.
- Helius free plan: one RPC call per mint decimals lookup, cached. Keep the cache warm, don't beeline to RPC per cycle.