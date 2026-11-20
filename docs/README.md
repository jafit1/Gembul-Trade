# gembul

Freqtrade + ccxt, satu engine, dua pasar: CEX lewat ccxt/freqtrade, meme coin
lewat DEX. Telegram jadi remote control. Setiap trade yang ditutup masuk ke
memori, dianalisis, dan berubah jadi aturan yang mempengaruhi ukuran posisi
berikutnya.

## Isi

```
/srv/gembul
├── gembul/                 engine
│   ├── core/engine.py      loop utama: ekuitas -> stop -> sinyal -> risiko -> eksekusi
│   ├── core/risk.py        satu-satunya tempat yang boleh menghitung ukuran posisi
│   ├── memory/store.py     episode, pelajaran, pencarian FTS5
│   ├── memory/reflect.py   refleksi LLM -> aturan yang bisa dipakai ulang
│   ├── signals/meme.py     discovery GeckoTerminal + skoring
│   ├── safety/token_scan.py goplus + rugcheck + honeypot.is
│   ├── venues/cex.py       ccxt async
│   ├── venues/freqtrade_bridge.py  REST ke freqtrade
│   ├── venues/dex/         jupiter (solana), kyber/0x/odos (evm), signer, halaman wallet
│   ├── telegram/bot.py     kontrol penuh lewat chat
│   └── api/server.py       dashboard + JSON API + halaman wallet
├── freqtrade_strategy/     GembulSwing (1h), GembulTrend (15m)
├── vendor/                 freqtrade + ccxt (editable)
├── ftuser/                 database dry-run freqtrade, data historis
├── data/gembul.sqlite      posisi, sinyal, episode, pelajaran
└── config/                 config.yaml, .env
```

## Jalan

```
systemctl status gembul          # engine + API + telegram
journalctl -u gembul -f
curl -s localhost:8000/api/status | jq
```

Dashboard: `http://127.0.0.1:8000/` (lewat ssh tunnel atau reverse proxy).

## Telegram

Isi `GEMBUL_TELEGRAM_TOKEN` dan `GEMBUL_ALLOWED_USER_IDS` di `config/.env`,
lalu `systemctl restart gembul`.

| perintah | fungsi |
|---|---|
| `/status` | ekuitas, halt, statistik |
| `/pos` | posisi terbuka + pnl, tombol tutup |
| `/scan solana` | kandidat meme dengan skor dan alasan |
| `/buy solana <alamat> 50` | beli manual, tetap lewat cek keamanan dan risk engine |
| `/sell <id>` | tutup posisi |
| `/connect solana` | tautan halaman wallet untuk menyambungkan Phantom/MetaMask |
| `/wallet` | wallet tersambung + saldo native |
| `/pause [alasan]` | kill switch |
| `/lessons` | aturan yang sudah dipelajari |
| `/learn <aturan>` | tanam pelajaran manual |
| `/ask <pertanyaan>` | tanya analis dengan konteks posisi + pelajaran |
| `/reflect` | paksa siklus refleksi |

## Angka backtest, apa adanya

Window 2026-05-01 → 2026-11-08. Beli-diam-diam sebagai pembanding:
BTC -7.05%, ETH -4.14%, SOL -8.28%, XRP -21.50%, BNB +4.13% (drawdown
intra-window BTC -33%).

| strategi | trades | hasil | catatan |
|---|---|---|---|
| GembulTrend 15m (v1) | 491 | -9.03% | overtrade, kalah di fee |
| GembulTrend 15m (hyperopt) | 302 | -3.62% | 65.6% win, tetap minus |
| GembulSwing 1h, 5 pair | 26 | +0.48% | positif, tapi sampel kecil |
| GembulSwing 1h, 25 pair | 106 | -1.72% | alt majors tidak punya edge |
| GembulSwing, fee 0 | 106 | -0.76% | **tanpa fee pun tidak untung** |

Kesimpulan jujur: sistem indikator buatan sendiri di majors tidak punya edge
kotor. Yang benar-benar menyelamatkan modal adalah ia sering di luar pasar —
-1.7% sementara XRP -21.5% itu alpha, bukan profit.

Yang menaikkan angka, berurutan menurut dampak:

1. **Fee.** 0.1% × 2 per trade membunuh sistem yang untungnya 0.2%. Maker
   entry atau VIP tier mengubah tanda hasil.
2. **Arah.** Window ini turun. Long-only spot tidak akan untung di pasar turun.
   `can_short` + futures Binance adalah tuas yang sebenarnya.
3. **Regime.** Sistem mean-reversion hanya hidup saat volatilitas cukup besar
   dibanding fee. Filter ATR/ADX wajib, bukan opsional.

## Yang belum diisi

- `GEMBUL_TELEGRAM_TOKEN` — bot belum jalan tanpa ini.
- `GEMBUL_LLM_API_KEY` — refleksi jatuh ke mode mekanis tanpa ini (masih jalan).
- `GEMBUL_CEX_API_KEY` / `SECRET` — untuk data privat dan eksekusi live.
- `GEMBUL_SOLANA_PRIVATE_KEY` / `GEMBUL_EVM_PRIVATE_KEY` — untuk DEX live.
  Tanpa ini DEX tetap bisa dipakai lewat halaman wallet (tanda tangan di HP).
- `GEMBUL_REAL_TRADING_ENABLED=true` + `mode: live` — baru menyentuh uang.

## Batas keamanan yang saya pasang

- Semua order lewat `RiskEngine.evaluate`: batas rugi harian, exposure total,
  jumlah posisi, posisi berkorelasi, dan penalti dari pelajaran lama.
- Kill switch otomatis saat rugi harian lewat batas, dengan cooldown 12 jam.
- Setiap beli meme wajib lewat goplus + rugcheck + honeypot.is. LP tidak
  terkunci atau holder terlalu sedikit = ditolak, apa pun skornya.
- Mode default `paper` dan `real_trading_enabled=false`.
