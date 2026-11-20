# Gembul-Trade

Bot trading crypto hybrid dua pasar (**DEX Solana/EVM** + **CEX Binance via Freqtrade**) yang dilengkapi **shadow paper-trading**, **safety scanner** (GoPlus / RugCheck / Honeypot), **LLM Judge & Reflection**, **memori pelajaran berbasis SQLite FTS5**, dan kontrol penuh lewat **Telegram** + **FastAPI Dashboard**.

Repo ini sudah dibersihkan dari semua `.env`, API key, dan kredensial sensitif, serta menyertakan **snapshot memori bot** (`memory/`) supaya mudah dipindah dan dijalankan ulang di VPS mana saja.

---

## Status & Realitas Performa Saat Ini

> **Transparan apa adanya:** Bot ini sudah berjalan stabil dan mulai **profit tipis** di sisi CEX walk-forward, tapi **masih butuh tuning** agar edge-nya lebih tebal dan konsisten sebelum menyentuh modal real.

| Komponen | Mode Aktif | Status & Catatan |
|---|---|---|
| **CEX — Freqtrade (`GembulSwing`)** | Dry-run (`1h`, 15 liquid USDT pairs di Binance) | Strategi `z20_snapback` (mean-reversion Z-score < -2.0 + RSI14 < 30 + knife guard + 24h time-stop). Validasi walk-forward positif di IS (`Apr–Aug`, bear -21.8%: **+1.10%**, PF 1.28) dan OOS (`Aug–Nov`: **+0.82%**, PF 1.22), Sharpe **1.07**, max DD **1.63%**. Profit tipis karena long-only spot + fee taker 0.1%/side. |
| **DEX — Gembul Shadow (`plan1` / Scalp)** | Paper / Shadow (Jupiter executable quotes) | `plan1` aktif (scalping 1m/5m, TP1 +10% jual 50% → SL BE +1.5%, TP2 +20% jual 25% → SL +10%, runner trail 5%, 45m stale time-stop). `plan2` & `plan3` dipause. Wallet live trading **di-halt** (default aman). |
| **Memori Bot (`memory/`)** | Ter-export bersih | **7.286 lessons** (aturan hasil refleksi trade), **5 tracked whales**, dan **journal logs** siap di-seed otomatis ke VPS baru. |

---

## Arsitektur & Struktur Repo

```text
Gembul-Trade/
├── gembul/                        # Engine utama (Python 3.12, asyncio, aiosqlite)
│   ├── core/
│   │   ├── engine.py              # Main loop: equity -> stops -> scan -> risk -> execute
│   │   ├── shadow.py              # Isolated $1,000 shadow ledgers (quote-aware Jupiter)
│   │   ├── risk.py                # Position sizing, exposure cap, kill switch, lesson penalty
│   │   └── schemes.py             # Konfigurasi skema plan1/plan2/plan3
│   ├── signals/
│   │   ├── meme.py                # Discovery GeckoTerminal + DexScreener + Binance Web3
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
│   ├── telegram/bot.py            # Remote control via Telegram
│   └── api/server.py              # Dashboard & JSON API (port 8000)
├── freqtrade_strategy/
│   ├── GembulSwing.py             # Strategi CEX aktif (1h z20_snapback)
│   ├── GembulSwingV2.py           # Varian eksperimen
│   ├── GembulTrend.py             # Strategi 15m lama (referensi)
│   └── config.example.json        # Template config Freqtrade (tanpa secret)
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
│   └── freqtrade.service          # Systemd unit untuk Freqtrade (GembulSwing)
├── scripts/
│   ├── setup_vps.sh               # Bootstrap 1-klik di VPS Linux baru
│   ├── seed_memory.py             # Import memory/ -> data/gembul.sqlite (idempotent)
│   └── export_memory.py           # Export data/gembul.sqlite -> memory/ sebelum backup/push
├── tests/
│   └── test_shadow_accounting.py  # Unit test akuntansi shadow ledger
├── requirements.txt
└── AGENTS.md                      # Catatan teknis & aturan akuntansi penting
```

---

## Cara Menjalankan di VPS Baru (Quick Start)

Direkomendasikan **Ubuntu 22.04 / 24.04** dengan **Python 3.12** dan RAM minimal **2–4 GB** (lebih lega jika menjalankan Freqtrade + Gembul bersamaan).

### 1. Install prasyarat sistem (Ubuntu/Debian)

```bash
sudo apt update && sudo apt install -y \
  python3.12 python3.12-venv python3-pip git build-essential wget curl
```

*(Opsional jika ingin menjalankan Freqtrade + TA-Lib di VPS yang sama)*:
```bash
# Install TA-Lib C library (dibutuhkan oleh GembulSwing.py)
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
```

Script `setup_vps.sh` otomatis akan:
1. Membuat virtualenv `.venv` dan meng-install dependensi dari `requirements.txt`.
2. Menyalin `config/.env.example` → `config/.env` (permission `600`).
3. Menyalin `freqtrade_strategy/config.example.json` → `freqtrade_strategy/config.json`.
4. Menjalankan `scripts/seed_memory.py` untuk mengisi `data/gembul.sqlite` dengan **7.286 lessons**, **tracked whales**, dan **journal logs**.

Jika ingin memakai **Freqtrade** juga, install ke `.venv` yang sama:
```bash
/srv/gembul/.venv/bin/pip install freqtrade TA-Lib
```

### 3. Isi konfigurasi rahasia (`.env` & `config.json`)

Edit `config/.env`:
```bash
nano /srv/gembul/config/.env
```

Variabel penting:
- `GEMBUL_MODE=paper` dan `GEMBUL_REAL_TRADING_ENABLED=false` (biarkan default sampai siap live).
- `GEMBUL_TELEGRAM_TOKEN` & `GEMBUL_ALLOWED_USER_IDS` — untuk kontrol & notifikasi Telegram.
- `GEMBUL_LLM_API_KEY` & `GEMBUL_LLM_BASE_URL` — API key LLM (TokenHarbor / OpenRouter / OpenAI-compatible). Tanpa ini, judge akan fail-closed dan refleksi jalan di mode mekanis.
- `GEMBUL_SOLANA_RPC` & `GEMBUL_HELIUS_API_KEY` — RPC Solana (disarankan Helius gratis agar lookup decimals & quote tidak kena rate limit).
- `GEMBUL_CEX_API_KEY` / `GEMBUL_CEX_SECRET` — hanya diisi jika ingin live trading CEX.
- `GEMBUL_FREQTRADE_PASSWORD` — samakan dengan password `api_server` di `freqtrade_strategy/config.json`.

Edit `freqtrade_strategy/config.json`:
- Ganti `api_server.jwt_secret_key` dengan string acak panjang.
- Isi `api_server.password` (sama dengan `GEMBUL_FREQTRADE_PASSWORD`).

### 4. Jalankan & Verifikasi

Tes manual dulu:
```bash
# Jalankan unit test akuntansi
/srv/gembul/.venv/bin/pytest -q

# Jalankan Gembul Engine
/srv/gembul/.venv/bin/python -m gembul run

# (Terminal lain) Jalankan Freqtrade CEX
/srv/gembul/.venv/bin/freqtrade trade \
  --config /srv/gembul/freqtrade_strategy/config.json \
  --strategy GembulSwing \
  --strategy-path /srv/gembul/freqtrade_strategy \
  --userdir /srv/gembul/ftuser \
  --logfile /srv/gembul/ftuser/logs/freqtrade.log
```

### 5. Pasang sebagai Systemd Service (24/7)

Sesuaikan `User=` di `deploy/gembul.service` dan `deploy/freqtrade.service` dengan user Linux VPS kamu, lalu:

```bash
sudo cp deploy/gembul.service /etc/systemd/system/
sudo cp deploy/freqtrade.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gembul.service freqtrade.service

# Cek status & log
sudo systemctl status gembul.service freqtrade.service
tail -f /srv/gembul/logs/service.log
tail -f /srv/gembul/ftuser/logs/freqtrade.log
```

Dashboard lokal bisa diakses di `http://127.0.0.1:8000/` (gunakan SSH tunnel: `ssh -L 8000:127.0.0.1:8000 user@vps-ip`).

---

## Manajemen Memori (Backup & Pindah VPS)

Bot menyimpan hasil pembelajaran di SQLite (`data/gembul.sqlite`). Jangan pernah commit file `.sqlite` langsung karena berisi data sesi/profil lokal. Gunakan dua script berikut:

```bash
# Export memori terbaru dari VPS sebelum backup / git push
python scripts/export_memory.py --db data/gembul.sqlite --memory-dir memory

# Import memori ke VPS baru (aman dijalankan berulang kali / idempotent)
python scripts/seed_memory.py --db data/gembul.sqlite --memory-dir memory
```

---

## Perintah Telegram

| Perintah | Fungsi |
|---|---|
| `/status` | Cek ekuitas, status kill switch, dan ringkasan statistik |
| `/pos` | Lihat posisi terbuka + PnL + tombol tutup manual |
| `/scan solana` | Scan kandidat meme coin beserta skor & alasan |
| `/buy solana <mint> <usd>` | Beli manual (tetap melewati safety scan & risk engine) |
| `/sell <id>` | Tutup posisi tertentu |
| `/pause [alasan]` | Aktifkan kill switch darurat |
| `/lessons` | Lihat aturan/pelajaran yang sudah tersimpan di memori |
| `/learn <aturan>` | Tambahkan pelajaran manual ke memori |
| `/ask <pertanyaan>` | Tanya analis AI dengan konteks posisi + memori aktif |
| `/reflect` | Paksa siklus refleksi AI terhadap trade terakhir |

---

## Roadmap Tuning (Menuju Edge Lebih Kuat)

Saat ini sistem baru **profit tipis** di CEX (`GembulSwing` 1h) dan masih fase kalibrasi di DEX shadow (`plan1`). Area prioritas untuk tuning berikutnya:

1. **Tekan Biaya Fee di CEX (`GembulSwing`)**
   - Fee taker 0.1% × 2 (0.2% roundtrip) memakan sebagian besar edge mean-reversion.
   - Uji **limit/maker entry & exit** (`price_side: "same"`, orderbook queue) atau fee discount BNB/VIP tier — bisa menaikkan profit factor secara signifikan.
2. **Aktifkan Dua Arah (Futures + `can_short`)**
   - Saat ini spot **long-only**, sehingga di market bear/downtrend bot lebih banyak menunggu di luar pasar.
   - Tambahkan filter regime HTF (mis. 4h/1d EMA trend + ADX) lalu kembangkan sisi short (`z20 > +2.0` + RSI > 70 rejection) dengan leverage konservatif (1x–2x) dan validasi walk-forward terpisah.
3. **Filter Regime & Volatilitas Dinamis**
   - Mean-reversion paling efektif saat volatilitas cukup tinggi untuk menutup fee, tapi tidak sedang crash vertikal.
   - Tune threshold `z20`, `atr_pct` band (min & max), serta `StoplossGuard` di berbagai regime (bull, sideways, bear) menggunakan walk-forward out-of-sample — hindari overfit satu window saja.
4. **Tuning DEX Scalping (`plan1` di `gembul/core/shadow.py`)**
   - Audit exit reason setelah minimal 200+ closed shadow trades pasca decimals fix.
   - Evaluasi apakah SL 5.5–7.5% terlalu ketat untuk volatilitas meme Solana (sering kena wick sebelum rebound) vs memperketat filter slippage/roundtrip Jupiter (`<= 1.2%`) dan likuiditas minimum.
   - Kurangi noise di `lessons` (saat ini 7.286 baris) dengan pruning/deduplikasi pelajaran berbobot rendah agar retrieval FTS5 lebih tajam.
5. **Aturan Wajib Sebelum Live Trading**
   - Semua perubahan parameter wajib lolos **out-of-sample walk-forward** (CEX) dan minimal **100–200 shadow trades bersih** dengan Profit Factor > 1.3 (DEX) sebelum mematikan kill switch atau menyalakan `GEMBUL_REAL_TRADING_ENABLED=true`.

---

## Keamanan & Batas Risiko

- **Jangan pernah commit** `config/.env`, `freqtrade_strategy/config.json`, `*.sqlite`, atau private key. Semua sudah masuk `.gitignore`.
- **Fail-closed by design**: jika LLM judge error/habis kuota, entry otomatis ditolak.
- **Safety gate DEX**: setiap token meme wajib lolos GoPlus + RugCheck + Honeypot check dan batas slippage/roundtrip Jupiter.
- **Kill switch otomatis**: aktif jika daily loss melewati batas di `config/config.yaml`.
