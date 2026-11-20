# Gembul-Trade

Bot trading crypto hybrid dua pasar (**DEX Solana/EVM** + **CEX Binance Spot & USDT-M Futures via Freqtrade**) dengan **isolated shadow paper-trading** berbasis quote executable Jupiter, **safety scanner** (GoPlus / RugCheck / Honeypot), **LLM Judge & Reflection**, **memori pelajaran berbasis SQLite FTS5**, dan kontrol penuh lewat **Telegram** + **FastAPI Dashboard**.

Repo ini bebas dari `.env`, API key, maupun database `.sqlite`, serta menyertakan **snapshot memori bot** (`memory/`) agar mudah dipindah dan dijalankan ulang di VPS baru.

---

## Ringkasan Performa & Hasil Tuning Empiris

> **Catatan transparan:** Wallet live trading (`GEMBUL_REAL_TRADING_ENABLED=false` & `kill_switch`) tetap dalam mode paper/dry-run. Seluruh angka di bawah berasal dari backtest resmi Freqtrade (Binance `1h`+`4h`, taker fee realistis `0.10%/side` Spot dan `0.05%/side` Futures) serta replay 5m OHLCV historis Jupiter shadow trades (`2026-05-25` s.d. `2026-11-20`).

### 1. Mesin CEX — Binance (`Freqtrade 1h + Multi-Timeframe 4h`)

Strategi asli `GembulSwing` (fixed `$100` stake, long-only Spot tanpa regime filter) sebelumnya hanya mencatat **`+0.48%`** (26 trades). Setelah upgrade **regime-gating (BTC `1h` EMA200 & `4h` EMA20/EMA50)**, **dynamic ATR stake sizing** (`unlimited` stake terbagi 3 slot `~31%`), dan opsi **2-arah Long+Short di `GembulFutures`**:

| Strategi | Window | Periode (`2026`) | Market Change | Trades | Win% | Total Profit | Profit Factor | Sharpe (Closed / Daily) | Max Drawdown |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| **`GembulFutures` (2x Iso L+S, Aktif)** | W1 Bear | May 25 – Aug 01 | `-18.11%` | 26 | `61.5%` | **`+1.94%`** (`+19.38 USDT`) | `1.15` | `0.43 / 0.73` | `6.05%` |
| **`GembulFutures` (2x Iso L+S, Aktif)** | W2 Bull | Aug 01 – Oct 01 | — | 21 | `81.0%` | **`+16.84%`** (`+168.37 USDT`) | `5.12` | `4.37 / —` | `—` |
| **`GembulFutures` (2x Iso L+S, Aktif)** | W3 Bear (OOS) | Oct 01 – Nov 20 | `-36.57%` | 22 | `81.8%` | **`+13.70%`** (`+136.96 USDT`) | `6.71` | `7.13 / 8.01` | `1.05%` |
| **`GembulFutures` (2x Iso L+S, Aktif)** | **Full 179 Hari** | **May 25 – Nov 20** | **`-10.47%`** | **71** | **`74.6%`** | **`+40.67%` (`+406.71 USDT`)** | **`3.03`** | **`3.53 / 4.04`** | **`6.05%`** |
| **`GembulSwing` (Spot Long-Only)** | W1 Bear | May 25 – Aug 01 | `-18.10%` | 9 | `55.6%` | `-1.10%` (`-11.01 USDT`) | `0.68` | `-0.41 / -1.00` | `1.69%` |
| **`GembulSwing` (Spot Long-Only)** | W2 Bull | Aug 01 – Oct 01 | `+74.42%` | 15 | `93.3%` | **`+8.45%`** (`+84.53 USDT`) | `222.43` | `6.69 / 6.54` | `0.04%` |
| **`GembulSwing` (Spot Long-Only)** | W3 Bear (OOS) | Oct 01 – Nov 20 | `-36.58%` | 5 | `60.0%` | **`+0.39%`** (`+3.94 USDT`) | `1.41` | `0.29 / 0.88` | `0.58%` |
| **`GembulSwing` (Spot Long-Only)** | **Full 179 Hari** | **May 25 – Nov 20** | **`-10.47%`** | **31** | **`77.4%`** | **`+9.31%` (`+93.11 USDT`)** | **`3.07`** | **`1.58 / 2.92`** | **`1.69%`** |

**Breakdown `GembulFutures` Full 179 Hari:**
- **Long trades (30x):** `+19.78%` (`+197.77 USDT`) — aktif optimal saat macro bull / `4h` uptrend / deep capitulation.
- **Short trades (41x):** `+20.89%` (`+208.94 USDT`) — strictly gated hanya ketika **BTC `1h < EMA200` AND BTC `4h < EMA50` AND pair `< EMA200`** untuk fade dead-cat bounce (`z20 > 1.75`, `RSI > 60`).
- Risk controls: isolated `2.0x` leverage, `-8.0%` ROI stop (`-4.0%` underlying move), `+9.5% / +6.0% / +3.5%` tiered ROI, `EMA20` reversion exit, `24h` time-stop, `StoplossGuard`, dan `CooldownPeriod`.

### 2. Mesin DEX — Solana Shadow Scalping (`gembul/core/shadow.py`, `plan1`)

Audit forensik terhadap **143 closed shadow trades** (serta replay 5m OHLCV pada **121 path historis** tervalidasi H1/H2 chronological split) menemukan penyebab kebocoran `-$14.51` pada aturan `plan1` lama:
- Entry dengan **`chg_1h < 0`** menyumbang **86% total kerugian** (`n=49`, 28.6% WR, `-$12.405`).
- Spread **roundtrip Jupiter ≥ 0.8%** merugi `-$8.38` (`n=32`), dan tokenized equities (`TTWO`, `GMEX`, `AMC`, `ANT`, `STONK` dengan ATR < 1.2%) menyumbat 5 slot.
- `11` trade sempat menyentuh puncak `+5%..+10%` namun berbalik rugi **0% WR (`-$9.03`)** karena belum ada proteksi breakeven sebelum TP1 (`+10%`).

Perubahan tervalidasi di `gembul/core/shadow.py`, `gembul/signals/meme.py`, dan `gembul/core/engine.py`:
- **Filter Entry Lebih Tajam:** `chg_1h` dalam rentang `0.0% .. +35.0%`, `buy_ratio >= 1.08`, `MAX_ROUNDTRIP_COST_PCT = 0.90%`, `MAX_PRICE_IMPACT_PCT = 0.35%`, `44 <= rsi14 <= 68`, `1.2% <= atr_pct <= 7.5%`, `ext_pct <= 4.8%`, `vol_ratio >= 0.55`, serta blokir simbol tokenized equity (`_EQUITY_SYMBOLS`).
- **Manajemen Exit Berlapis & Konkuren:**
  - **Early Breakeven Lock:** Begitu `peak >= +6.5%`, `stop_loss` otomatis dinaikkan ke `+0.3%` di SQLite (`stop_loss_be`) tanpa memangkas target runner **TP1 `+10%` (jual 50%) / TP2 `+20%` (jual 25%) / 5% runner trail**.
  - **Two-Tier Stale Exit:** Keluar cepat di **25 menit** (`time_stop_stale_25m`) jika `peak < +1.5%` dan masih merah untuk membebaskan slot, diikuti stale cap `45m`.
  - **Quote Prefetch Konkuren & Mid-Cycle 15s Poll:** Quote Jupiter di-fetch paralel (`asyncio.Semaphore(5)`) dengan pengecekan mid-cycle tiap **15 detik** di `Engine.run()` untuk menekan slippage stop-loss.
- **Candidate Scoring (`meme.py`):** Penalti `chg_1h < 0` (`-0.20`) dan `fdv/liq > 45` (`-0.15`), bonus `0 <= chg_1h <= 20%` dan `smart_money_inflow > 0`.

### 3. Memori Bot (`memory/`)

- **7.286 lessons**, **5 tracked whales**, dan **journal logs** ter-export bersih bebas secret di folder `memory/`.

---

## Arsitektur & Struktur Repo

```text
Gembul-Trade/
├── gembul/                        # Engine utama (Python 3.12, asyncio, aiosqlite)
│   ├── core/
│   │   ├── engine.py              # Loop utama + mid-cycle 15s mark + alert DEX/CEX
│   │   ├── shadow.py              # Isolated $1,000 shadow ledger (concurrent Jupiter quotes)
│   │   ├── risk.py                # Position sizing, exposure cap, kill switch, lesson penalty
│   │   └── schemes.py             # Konfigurasi skema plan1/plan2/plan3
│   ├── signals/
│   │   ├── meme.py                # Discovery GeckoTerminal + DexScreener + Binance Web3 + scoring
│   │   └── whale.py               # Whale wallet tracker
│   ├── safety/
│   │   └── token_scan.py          # GoPlus + RugCheck + Honeypot.is filter
│   ├── llm/
│   │   ├── client.py              # OpenAI-compatible LLM client (fail-closed)
│   │   └── judge.py               # Pre-trade LLM judge
│   ├── memory/
│   │   ├── store.py               # Episodes, lessons, FTS5 retrieval & losing-pattern penalty
│   │   ├── reflect.py             # Post-trade AI reflection -> lesson baru
│   │   └── journal.py             # Daily/weekly AI journal & 3-plan comparison
│   ├── venues/
│   │   ├── dex/                   # Jupiter (Solana), 0x/Odos/Kyber (EVM)
│   │   ├── cex.py                 # CCXT async wrapper
│   │   └── freqtrade_bridge.py    # REST client ke Freqtrade API
│   ├── telegram/bot.py            # Remote control Telegram (/pos, /history Long/Short, dll)
│   └── api/server.py              # Dashboard & JSON API (port 8000)
├── freqtrade_strategy/
│   ├── GembulFutures.py           # Strategi CEX aktif: USDT-M Futures L+S (2x isolated)
│   ├── GembulSwing.py             # Strategi CEX alternatif: Spot Long-Only regime-gated
│   ├── config.futures.example.json# Template config Freqtrade Futures (tanpa secret)
│   └── config.example.json        # Template config Freqtrade Spot (tanpa secret)
├── memory/                        # Snapshot memori bot (bebas secret, siap seed di VPS baru)
│   ├── lessons.jsonl              # 7.286 pelajaran trading hasil refleksi
│   ├── tracked_whales.json        # Daftar wallet whale terpantau
│   ├── journal_logs.json          # Riwayat jurnal AI
│   └── shadow_summary.json        # Ringkasan performa shadow terakhir
├── config/
│   ├── config.yaml                # Konfigurasi runtime (bebas secret)
│   └── .env.example               # Template environment variables
├── deploy/
│   ├── gembul.service             # Systemd unit untuk Gembul Engine
│   └── freqtrade.service          # Systemd unit untuk Freqtrade (GembulFutures)
├── scripts/
│   ├── setup_vps.sh               # Bootstrap 1-klik di VPS Linux baru
│   ├── seed_memory.py             # Import memory/ -> data/gembul.sqlite (idempotent)
│   └── export_memory.py           # Export data/gembul.sqlite -> memory/ sebelum backup/push
├── tests/
│   └── test_shadow_accounting.py  # Unit test akuntansi & filter/exit shadow ledger
├── requirements.txt
└── AGENTS.md                      # Catatan teknis & aturan akuntansi penting
```

---

## Cara Menjalankan di VPS Baru (Quick Start)

Direkomendasikan **Ubuntu 22.04 / 24.04** dengan **Python 3.12** dan RAM minimal **2–4 GB**.

### 1. Install prasyarat sistem & TA-Lib (Ubuntu/Debian)

```bash
sudo apt update && sudo apt install -y \
  python3.12 python3.12-venv python3-pip git build-essential wget curl

# TA-Lib C library (dibutuhkan oleh GembulFutures.py / GembulSwing.py)
wget https://github.com/ta-lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz
tar -xzf ta-lib-0.6.4-src.tar.gz && cd ta-lib-0.6.4
./configure --prefix=/usr && make -j$(nproc) && sudo make install
cd .. && rm -rf ta-lib-0.6.4*
```

### 2. Clone repo & jalankan bootstrap

```bash
sudo mkdir -p /srv/gembul && sudo chown $USER:$USER /srv/gembul
git clone https://github.com/jafit1/Gembul-Trade.git /srv/gembul
cd /srv/gembul

chmod +x scripts/setup_vps.sh
./scripts/setup_vps.sh /srv/gembul
/srv/gembul/.venv/bin/pip install freqtrade TA-Lib
```

Script `setup_vps.sh` otomatis membuat `.venv`, install dependensi `requirements.txt`, menyiapkan `config/.env`, menyalin `freqtrade_strategy/config.futures.example.json` → `freqtrade_strategy/config.json`, dan me-restore **memori 7.286 lessons + whales + journal logs** lewat `scripts/seed_memory.py`.

> *(Opsional mode Spot-only: salin `freqtrade_strategy/config.example.json` ke `freqtrade_strategy/config.json` lalu ganti `--strategy GembulFutures` menjadi `--strategy GembulSwing`.)*

### 3. Isi konfigurasi rahasia (`.env` & `config.json`)

Edit `/srv/gembul/config/.env`:
- `GEMBUL_MODE=paper` dan `GEMBUL_REAL_TRADING_ENABLED=false` (biarkan default sampai siap live).
- `GEMBUL_TELEGRAM_TOKEN` & `GEMBUL_ALLOWED_USER_IDS` — untuk kontrol & notifikasi Telegram.
- `GEMBUL_LLM_API_KEY` & `GEMBUL_LLM_BASE_URL` — API key LLM (TokenHarbor / OpenRouter / OpenAI-compatible).
- `GEMBUL_SOLANA_RPC` & `GEMBUL_HELIUS_API_KEY` — RPC Solana untuk lookup decimals & quote Jupiter.
- `GEMBUL_CEX_API_KEY` / `GEMBUL_CEX_SECRET` — hanya diisi jika nanti live trading CEX.
- `GEMBUL_FREQTRADE_PASSWORD` — samakan dengan `api_server.password` di `freqtrade_strategy/config.json`.

Edit `/srv/gembul/freqtrade_strategy/config.json`:
- Ganti `api_server.jwt_secret_key` dengan string acak panjang, dan samakan `api_server.password`.

### 4. Jalankan Unit Test & Service 24/7

```bash
# Jalankan unit test akuntansi & rule exit DEX
PYTHONPATH=/srv/gembul /srv/gembul/.venv/bin/pytest -v tests/test_shadow_accounting.py

# Pasang systemd units (sesuaikan User= dengan user Linux VPS kamu)
sudo cp deploy/gembul.service deploy/freqtrade.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gembul.service freqtrade.service

# Cek status & log
sudo systemctl status gembul.service freqtrade.service
tail -f /srv/gembul/logs/service.log
tail -f /srv/gembul/ftuser/logs/freqtrade.log
```

Dashboard lokal dapat diakses di `http://127.0.0.1:8000/` (via SSH tunnel: `ssh -L 8000:127.0.0.1:8000 user@vps-ip`).

---

## Manajemen Memori (Backup & Pindah VPS)

Jangan pernah commit file `.sqlite` langsung. Gunakan dua script ini untuk sinkronisasi memori:

```bash
# Export memori terbaru dari VPS sebelum push/backup
PYTHONPATH=/srv/gembul python scripts/export_memory.py --db data/gembul.sqlite --memory-dir memory

# Import memori ke VPS baru (idempotent, aman dijalankan ulang)
PYTHONPATH=/srv/gembul python scripts/seed_memory.py --db data/gembul.sqlite --memory-dir memory
```

---

## Perintah Telegram

| Perintah | Fungsi |
|---|---|
| `/status` | Cek ekuitas, status kill switch, dan ringkasan statistik |
| `/pos` | Lihat posisi terbuka DEX + CEX (LONG/SHORT + leverage) & tombol tutup manual |
| `/history` | Lihat riwayat closed trade DEX & CEX terbaru |
| `/scan solana` | Scan kandidat meme coin beserta skor & alasan |
| `/buy solana <mint> <usd>` | Beli manual (tetap melewati safety scan & risk engine) |
| `/sell <id>` | Tutup posisi tertentu |
| `/pause [alasan]` | Aktifkan kill switch darurat |
| `/lessons` | Lihat aturan/pelajaran yang tersimpan di memori |
| `/learn <aturan>` | Tambahkan pelajaran manual ke memori |
| `/ask <pertanyaan>` | Tanya analis AI dengan konteks posisi + memori aktif |
| `/reflect` | Paksa siklus refleksi AI terhadap trade terakhir |

---

## Keamanan & Batas Risiko

- **Jangan pernah commit** `config/.env`, `freqtrade_strategy/config.json`, `*.sqlite`, atau private key.
- **Fail-closed by design**: jika LLM judge error/habis kuota, entry otomatis ditolak.
- **Safety gate DEX**: wajib lolos GoPlus + RugCheck + Honeypot, batas impact Jupiter `<= 0.35%`, dan spread roundtrip `<= 0.90%`.
- **Kill switch otomatis**: aktif jika daily loss melewati batas di `config/config.yaml`.
- **Syarat sebelum live trading**: pantau performa paper/dry-run live (`GembulFutures` & `plan1` baru) minimal 100+ closed trades dengan Profit Factor stabil > 1.3 sebelum menonaktifkan kill switch atau menyalakan `GEMBUL_REAL_TRADING_ENABLED=true`.
