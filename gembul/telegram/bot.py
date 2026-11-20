"""Telegram control surface. Full GMGN / Axiom Trade terminal experience with vibrant colors & clean cards."""
from __future__ import annotations

import html
import json
import logging
import re
import secrets
from typing import Any

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from gembul.core.engine import Engine
from gembul.core.models import OrderIntent, Side, Signal, Venue
from gembul.core.schemes import SCHEMES, get_scheme
from gembul.settings import Settings
from gembul.venues.dex.chains import CHAINS, get_chain

log = logging.getLogger(__name__)

DEXS_API = "https://api.dexscreener.com/latest/dex/tokens"

HELP = """⚡️ <b>GEMBUL TRADING TERMINAL</b>
<i>Engine trading multi-chain & whale follower berstandar GMGN / Axiom</i>

📊 <b>Status</b> — Dashboard ekuitas & performa portofolio
🔍 <b>Scan</b> — Radar meme coin multi-chain & safety audit
💰 <b>Posisi</b> — Pantau PnL live & quick sell (50% / 100%)
🐋 <b>Whale</b> — Radar whale GMGN-style, auto-copy & holding scanner
📈 <b>Jurnal</b> — Evaluasi harian/mingguan & head-to-head skema AI
⚙️ <b>Setting</b> — Pilih Skema (⚡️ Scalping vs 🛡️ Safe) & Auto-Trade
👛 <b>Wallet</b> — Saldo onchain & WalletConnect
⏸ <b>Pause / ▶️ Resume</b> — Kill switch darurat

💡 <b>Fitur Cepat:</b>
• <b>Paste Alamat Token (CA)</b> langsung di chat untuk membuka Token Terminal & audit instan!
• <code>/settings</code> — Konfigurasi skema & auto-trade
• <code>/journal</code> — Jurnal performa & perbandingan skema
• <code>/history</code> — Riwayat auto-trade saldo uji (DEX & CEX)
• <code>/adduser &lt;id&gt;</code> — Daftarkan akun Telegram kedua
• <code>/whale</code> — Dashboard pemantauan whale
• <code>/whale scan [page]</code> — Radar whale pool trending
• <code>/buy &lt;chain&gt; &lt;alamat&gt; [usd]</code> — Beli token manual
• <code>/sell &lt;id&gt;</code> — Tutup posisi tertentu
• <code>/resume</code> — Lepas kill switch & reset batas rugi harian
• <code>/ask &lt;tanya&gt;</code> — Analisis pasar via AI"""

BOT_COMMANDS = [
    BotCommand(command="status", description="📊 Dashboard & Ekuitas"),
    BotCommand(command="scan", description="🔍 Radar Meme Coin"),
    BotCommand(command="pos", description="💰 Posisi Terbuka & PnL"),
    BotCommand(command="whale", description="🐋 Radar & Tracker Whale"),
    BotCommand(command="journal", description="📈 Jurnal & Evaluasi AI"),
    BotCommand(command="history", description="📜 Riwayat Auto-Trade Saldo Uji"),
    BotCommand(command="settings", description="⚙️ Skema Scalp/Safe & Auto"),
    BotCommand(command="wallet", description="👛 Saldo & WalletConnect"),
    BotCommand(command="buy", description="🟢 Beli Token Manual"),
    BotCommand(command="sell", description="🔴 Tutup Posisi"),
    BotCommand(command="resume", description="▶️ Lepas Kill Switch"),
    BotCommand(command="pause", description="⏸ Kill Switch"),
    BotCommand(command="ask", description="🧠 Tanya Analis AI"),
    BotCommand(command="lessons", description="📚 Memori & Pelajaran"),
]

MENU_ROWS = [
    ["📊 Status", "🔍 Scan", "💰 Posisi"],
    ["🐋 Whale", "📈 Jurnal", "⚙️ Setting"],
    ["👛 Wallet", "⏸ Pause", "▶️ Resume"],
]

_MENU_MAP = {
    "📊 status": "status",
    "status": "status",
    "st": "status",
    "🔍 scan": "scan_solana",
    "scan": "scan_solana",
    "💰 posisi": "pos",
    "posisi": "pos",
    "pos": "pos",
    "portfolio": "pos",
    "portofolio": "pos",
    "🐋 whale": "whale",
    "whale": "whale",
    "paus": "whale",
    "📈 jurnal": "journal",
    "jurnal": "journal",
    "journal": "journal",
    "log": "journal",
    "riwayat": "history",
    "history": "history",
    "⚙️ setting": "settings",
    "setting": "settings",
    "settings": "settings",
    "skema": "settings",
    "profil": "settings",
    "profile": "settings",
    "👛 wallet": "wallet",
    "wallet": "wallet",
    "dompet": "wallet",
    "⏸ pause": "pause",
    "pause": "pause",
    "stop": "pause",
    "▶️ resume": "resume",
    "resume": "resume",
    "lanjut": "resume",
    # Monochrome aliases
    "▪ status": "status",
    "▪ scan": "scan_solana",
    "▪ posisi": "pos",
    "▪ whale": "whale",
    "▪ jurnal": "journal",
    "▪ setting": "settings",
    "▪ wallet": "wallet",
    "▪ pause": "pause",
    "▪ resume": "resume",
}

MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=t) for t in row] for row in MENU_ROWS],
    resize_keyboard=True,
    is_persistent=True,
)


def _esc(val: Any) -> str:
    return html.escape(str(val or ""))


def _fmt_age(minutes: float) -> str:
    if minutes < 60:
        return f"{int(max(1, minutes))}m"
    hours = int(minutes // 60)
    rem_m = int(minutes % 60)
    if hours < 24:
        return f"{hours}j {rem_m}m"
    days = int(hours // 24)
    rem_h = int(hours % 24)
    return f"{days}h {rem_h}j"


class TelegramUI:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.s = settings
        if not settings.telegram_token:
            raise RuntimeError("telegram token kosong")
        self.bot = Bot(
            token=settings.telegram_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)
        self._http = httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": "gembul/1.0", "Accept": "application/json"}
        )
        self._extra_users: set[int] = set()
        self._register()

    async def _allowed_user(self, msg: Message) -> bool:
        ids = set(self.s.telegram.allowed_user_ids) | set(self.s.allowed_user_ids)
        if not self._extra_users:
            extra = await self.engine.db.kv_get("extra_allowed_users", []) or []
            self._extra_users = {int(x) for x in extra if str(x).isdigit()}
        ids |= self._extra_users
        if not ids:
            return True
        return msg.from_user is not None and msg.from_user.id in ids

    def _register(self) -> None:
        r = self.router
        r.message.register(self.cmd_start, Command("start", "help", "menu"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_positions, Command("pos", "positions"))
        r.message.register(self.cmd_lessons, Command("lessons"))
        r.message.register(self.cmd_scan, Command("scan"))
        r.message.register(self.cmd_whale, Command("whale"))
        r.message.register(self.cmd_journal, Command("journal", "jurnal", "log", "compare", "komparasi", "rencana"))
        r.message.register(self.cmd_history, Command("history", "riwayat"))
        r.message.register(self.cmd_settings, Command("settings", "setting", "skema"))
        r.message.register(self.cmd_adduser, Command("adduser"))
        r.message.register(self.cmd_wallet, Command("wallet"))
        r.message.register(self.cmd_connect, Command("connect"))
        r.message.register(self.cmd_buy, Command("buy"))
        r.message.register(self.cmd_sell, Command("sell"))
        r.message.register(self.cmd_pause, Command("pause"))
        r.message.register(self.cmd_resume, Command("resume"))
        r.message.register(self.cmd_ask, Command("ask"))

        # Callbacks
        r.callback_query.register(self.on_cb_close, F.data.startswith("pos:close:"))
        r.callback_query.register(self.on_cb_shadow_close, F.data.startswith("sh:close:"))
        r.callback_query.register(self.on_cb_buy, F.data.startswith("buy:"))
        r.callback_query.register(self.on_cb_whale, F.data.startswith("whale:"))
        r.callback_query.register(self.on_cb_scan, F.data.startswith("scan:"))
        r.callback_query.register(self.on_cb_token, F.data.startswith("token:"))
        r.callback_query.register(self.on_cb_wallet, F.data.startswith("wlt:"))
        r.callback_query.register(self.on_cb_config, F.data.startswith("cfg:"))
        r.callback_query.register(self.on_cb_refresh, F.data.startswith("rf:"))
        r.message.register(self.on_text, F.text & ~F.text.startswith("/"))

    # ---------------------------------------------------------------- lifecycle

    async def start_polling(self) -> None:
        if not self.s.telegram_token:
            log.warning("telegram token kosong, bot telegram tidak jalan")
            return
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.bot.set_my_commands(BOT_COMMANDS)
        log.info("telegram polling mulai")
        await self.dp.start_polling(self.bot)

    async def stop(self) -> None:
        await self._http.aclose()
        await self.bot.session.close()

    async def notify(self, text: str, **kw: Any) -> None:
        if self.s.telegram_chat_id:
            try:
                await self.bot.send_message(self.s.telegram_chat_id, text, **kw)
            except Exception as exc:
                log.warning("notify ke %s gagal: %s", self.s.telegram_chat_id, exc)

    # ------------------------------------------------------------ start

    async def cmd_start(self, msg: Message) -> None:
        if not await self._allowed_user(msg):
            return
        uid = str(msg.from_user.id if msg.from_user else "default")
        prof = await self.engine.get_user_profile(uid)
        scheme_name = prof.get("scheme", "scalp")
        scheme_cfg = get_scheme(scheme_name)

        onboarding_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"• {scheme_cfg.badge} (Aktif)" if scheme_name == "scalp" else "⚡️ Pilih Mode Scalping",
                    callback_data=f"cfg:scheme:{uid}:scalp",
                ),
                InlineKeyboardButton(
                    text=f"• {scheme_cfg.badge} (Aktif)" if scheme_name == "safe" else "🛡️ Pilih Mode Safe / Swing",
                    callback_data=f"cfg:scheme:{uid}:safe",
                ),
            ],
            [
                InlineKeyboardButton(text="⚙️ Buka Pengaturan", callback_data=f"cfg:refresh:{uid}"),
                InlineKeyboardButton(text="📈 Cek Jurnal AI", callback_data="rf:journal:daily"),
            ],
        ])
        await msg.answer(HELP, reply_markup=MENU)
        await msg.answer(
            f"👋 <b>Selamat Datang di Gembul Terminal!</b>\n"
            f"Akun ID: <code>{uid}</code>\n"
            f"Skema Default: <b>{scheme_cfg.badge}</b>\n\n"
            f"Pilih skema trading yang ingin kamu jalankan di akun ini:",
            reply_markup=onboarding_kb,
        )

    # ------------------------------------------------------------- status card

    async def _status_text(self) -> tuple[str, bool]:
        s = await self.engine.snapshot()
        st = s["stats"]
        is_halted = bool(s["halted"])
        wallet_badge = "🔒 <b>Terproteksi (Shadow-Only)</b>" if is_halted else ("⚡️ <b>LIVE</b>" if s["live"] else "📝 <b>PAPER</b>")
        mode_badge = "⚡️ <b>LIVE</b>" if s["live"] else "📝 <b>SALDO UJI (PAPER)</b>"

        comp = await self.engine.journal.compare_three_plans(period="daily")
        p1, p2, p3 = comp.get("plan1", {}), comp.get("plan2", {}), comp.get("plan3", {})
        tot = comp.get("total", {})

        row_day = await self.engine.db.fetchone("""
            SELECT
                COALESCE(SUM(pnl_usd), 0) AS pnl_usd,
                COUNT(*) AS trades,
                COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(AVG(pnl_pct), 0) AS avg_pct
            FROM shadow_positions
            WHERE status='closed' AND date(closed_at) = date('now')
        """)
        d_pnl = float(row_day["pnl_usd"]) if row_day else 0.0
        d_n = int(row_day["trades"]) if row_day else 0
        d_w = int(row_day["wins"]) if row_day else 0
        d_avg = float(row_day["avg_pct"]) if row_day else 0.0
        d_wr = (d_w / d_n * 100.0) if d_n else 0.0
        d_icon = "🟢" if d_pnl >= 0 else "🔴"

        lines = [
            f"⚡️ <b>GEMBUL TERMINAL STATUS</b> · {mode_badge}",
            f"🤖 Auto-Trade Saldo Uji: 🟢 <b>AKTIF (SCALPING 1m/5m)</b> · Wallet Real: {wallet_badge}",
            "━━━━━━━━━━━━━━━━━━━━",
            f"💵 <b>Saldo Uji DEX Solana:</b>",
            f"   • ⚡️ <b>Scalping (Plan 1):</b> <b>${p1.get('current_equity', 1000):,.2f}</b> ({p1.get('total_pnl_usd', 0):+,.2f}) · {p1.get('open_count', 0)} open",
            f"   • ⏸ Plan 2 & 3: <i>Nonaktif (fokus Scalping)</i>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📅 <b>Auto-Trade DEX Hari Ini:</b>",
            f"   {d_icon} <b>${d_pnl:+,.2f}</b> (Avg: <b>{d_avg:+.1f}%</b>) · {d_n} closed · WR <b>{d_wr:.1f}%</b>",
            f"   📂 Total Posisi Aktif: <b>{tot.get('open_count', 0)}</b> · Total Closed: <b>{tot.get('total_trades', 0)}</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🧠 <b>Memori FTS5:</b> {st.get('lessons', 0)} aturan strategi",
        ]
        ft = s.get("freqtrade") or {}
        if ft.get("online"):
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            lines.append(
                f"⚙️ <b>CEX Freqtrade (Saldo Uji $1,000 · 1h):</b> "
                f"{ft.get('open_trades', 0)} open · {ft.get('closed_trade_count', ft.get('closed_trades', 0))} closed · "
                f"<b>{ft.get('profit_all_pct', 0):+.2f}%</b>"
            )
        return "\n".join(lines), is_halted

    async def cmd_status(self, msg: Message) -> None:
        if not await self._allowed_user(msg):
            return
        txt, _ = await self._status_text()
        rows = [
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="rf:status"),
             InlineKeyboardButton(text="📜 Riwayat Trade", callback_data="rf:history")],
            [InlineKeyboardButton(text="💰 Posisi Aktif", callback_data="rf:pos"),
             InlineKeyboardButton(text="🔍 Radar Scan", callback_data="scan:chain:solana:1")],
        ]
        await msg.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    # ---------------------------------------------------------- positions card

    async def _positions_view(self) -> tuple[str, InlineKeyboardMarkup]:
        shadow_rows = await self.engine.db.fetchall(
            "SELECT * FROM shadow_positions WHERE status='open' ORDER BY id DESC"
        )
        ft_open: list[dict] = []
        try:
            if await self.engine.ft.ping():
                ft_open = await self.engine.ft.open_trades()
        except Exception:
            ft_open = []

        if not shadow_rows and not ft_open:
            return (
                "📭 <b>Tidak ada posisi auto-trade saldo uji yang terbuka saat ini.</b>\n"
                "<i>Bot aktif memindai peluang Scalping Solana DEX tiap 2 menit & Binance CEX tiap candle 1h.</i>",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Refresh", callback_data="rf:pos"),
                     InlineKeyboardButton(text="📜 Riwayat Trade", callback_data="rf:history")],
                    [InlineKeyboardButton(text="🔍 Radar Meme", callback_data="scan:chain:solana:1")],
                ]),
            )

        lines = ["💰 <b>POSISI AUTO-TRADE SALDO UJI AKTIF</b>", "━━━━━━━━━━━━━━━━━━━━"]
        kb = []
        for r in shadow_rows:
            meta = json.loads(r["meta"] or "{}")
            qty = float(r["qty"])
            entry_px = float(r["entry_price"])
            basis = float(r["entry_usd"])
            liq_usd = float(meta.get("last_quote_out_usd") or basis)
            upnl_usd = liq_usd - basis
            up = (upnl_usd / max(basis, 1e-9)) * 100.0
            mark = "🟢" if up >= 0 else "🔴"
            sym = _esc(r["symbol"])
            mint = r["mint"]
            badge = get_scheme(r["scheme"]).badge
            lines.append(
                f"{mark} <b>#{r['id']} {sym}</b> · {badge}\n"
                f"   💵 Nilai Quote: <b>${liq_usd:,.2f}</b> (Modal: ${basis:.2f})\n"
                f"   🎯 Entry: ${entry_px:.6f} · SL: ${float(r['stop_loss']):.6f}\n"
                f"   📈 uPnL: <b>{up:+.2f}%</b> (<b>${upnl_usd:+.2f}</b>)\n"
            )
            kb.append([
                InlineKeyboardButton(text=f"🔴 Tutup #{r['id']} {sym}", callback_data=f"sh:close:{r['id']}"),
                InlineKeyboardButton(text="🔍 GMGN", url=f"https://gmgn.ai/sol/token/{mint}"),
            ])

        for t in ft_open:
            pair = _esc(t.get("pair"))
            prof_pct = float(t.get("profit_pct") or 0.0)
            prof_abs = float(t.get("profit_abs") or 0.0)
            side = "SHORT" if t.get("is_short") else "LONG"
            lev = float(t.get("leverage") or 1.0)
            lev_lbl = f" {lev:g}x" if lev > 1.01 else ""
            mark = "🟢" if prof_pct >= 0 else "🔴"
            lines.append(
                f"{mark} <b>CEX #{t.get('trade_id')} {pair}</b> [{side}{lev_lbl}] · ⚙️ {_esc(t.get('strategy') or 'Freqtrade')} 1h\n"
                f"   💵 Stake: <b>${float(t.get('stake_amount') or 0):.2f}</b> · Entry: ${float(t.get('open_rate') or 0):.4f}\n"
                f"   📈 uPnL: <b>{prof_pct:+.2f}%</b> (<b>${prof_abs:+.2f}</b>)\n"
            )

        kb.append([
            InlineKeyboardButton(text="🔄 Refresh", callback_data="rf:pos"),
            InlineKeyboardButton(text="📜 Riwayat Trade", callback_data="rf:history"),
        ])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)

    async def _history_view(self) -> tuple[str, InlineKeyboardMarkup]:
        rows = await self.engine.db.fetchall(
            "SELECT * FROM shadow_positions WHERE status='closed' ORDER BY id DESC LIMIT 12"
        )
        ft_closed: list[dict] = []
        try:
            if await self.engine.ft.ping():
                ft_closed = await self.engine.ft.closed_trades(limit=8)
        except Exception:
            ft_closed = []

        lines = ["📜 <b>RIWAYAT AUTO-TRADE SALDO UJI</b>", "━━━━━━━━━━━━━━━━━━━━"]
        if rows:
            lines.append("🟣 <b>Solana DEX (Shadow Quotes):</b>")
            for r in rows:
                meta = json.loads(r["meta"] or "{}")
                pnl_u = float(r["pnl_usd"] or 0.0)
                pnl_p = float(r["pnl_pct"] or 0.0)
                icon = "🟢" if pnl_u > 0 else "🔴"
                reason = _esc(meta.get("last_exit_reason") or "closed")
                closed_at = str(r["closed_at"] or "")[:16]
                lines.append(
                    f"{icon} <b>#{r['id']} {_esc(r['symbol'])}</b> [{r['scheme']}] "
                    f"<b>{pnl_p:+.1f}%</b> (${pnl_u:+.2f}) · <i>{reason}</i> ({closed_at} UTC)"
                )
        else:
            lines.append("🟣 <b>Solana DEX:</b> <i>Belum ada riwayat closed trade.</i>")

        lines.append("")
        if ft_closed:
            lines.append("⚙️ <b>Binance CEX (Freqtrade Dry-Run 1h):</b>")
            for t in ft_closed[:8]:
                prof_pct = float(t.get("profit_pct") or 0.0)
                prof_abs = float(t.get("profit_abs") or 0.0)
                side = "S" if t.get("is_short") else "L"
                icon = "🟢" if prof_abs > 0 else "🔴"
                lines.append(
                    f"{icon} <b>{_esc(t.get('pair'))}</b> [{side}] <b>{prof_pct:+.2f}%</b> (${prof_abs:+.2f}) "
                    f"· <i>{_esc(t.get('exit_reason'))}</i>"
                )
        else:
            lines.append("⚙️ <b>Binance CEX:</b> <i>Belum ada closed trade di dry-run 1h (menunggu setup LONG/SHORT).</i>")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh Riwayat", callback_data="rf:history"),
             InlineKeyboardButton(text="💰 Posisi Aktif", callback_data="rf:pos")],
            [InlineKeyboardButton(text="📈 Jurnal Scalping", callback_data="rf:journal:daily")],
        ])
        return "\n".join(lines), kb

    async def cmd_history(self, msg: Message) -> None:
        if not await self._allowed_user(msg):
            return
        txt, kb = await self._history_view()
        await msg.answer(txt, reply_markup=kb)

    async def cmd_positions(self, msg: Message) -> None:
        if not await self._allowed_user(msg):
            return
        try:
            txt, kb = await self._positions_view()
            await msg.answer(txt, reply_markup=kb)
        except Exception as exc:
            log.exception("cmd_positions gagal")
            await msg.answer(f"⚠️ Gagal memuat posisi: {_esc(exc)}")

    # ------------------------------------------------------------- journal / logs

    async def _journal_view(self, period: str = "daily") -> tuple[str, InlineKeyboardMarkup]:
        data = await self.engine.journal.generate_ai_journal(period=period)
        tot = data["total"]
        p1 = data["plan1"]
        p2 = data["plan2"]
        p3 = data["plan3"]
        ai_summary = data.get("summary") or "Belum ada evaluasi."

        period_title = "HARI INI" if period == "daily" else "7 HARI TERAKHIR (MINGGUAN)"

        lines = [
            f"📈 <b>JURNAL SCALPING & SHADOW LOG</b> · <b>{period_title}</b>",
            f"📅 Tanggal / Periode: <code>{tot['period_key']}</code>",
            f"💵 Modal Awal: <b>$1,000.00</b> (Saldo Uji Real-Time)",
            "━━━━━━━━━━━━━━━━━━━━",
            f"⚡️ <b>1. Skema Scalping 1m/5m (Plan 1)</b> — [AKTIF]",
            f"   • Saldo: <b>${p1['current_equity']:,.2f}</b> ({p1['total_pnl_usd']:+,.2f} USD)",
            f"   • Win Rate: <b>{p1['win_rate']:.1f}%</b> ({p1['win_trades']}W / {p1['loss_trades']}L · {p1['total_trades']} trade)",
            f"   • Posisi Aktif: {p1['open_count']} koin (${p1['unrealized_usd']:+,.2f})\n",
            f"⏸ <i>Plan 2 (Breakout: ${p2['current_equity']:,.2f}) & Plan 3 (Sniper: ${p3['current_equity']:,.2f}) dinonaktifkan sesuai mode Scalping.</i>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🧠 <b>EVALUASI & PEMBELAJARAN AI:</b>\n<i>{_esc(ai_summary)}</i>",
        ]

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="• 📅 Hari Ini" if period == "daily" else "📅 Hari Ini",
                    callback_data="rf:journal:daily",
                ),
                InlineKeyboardButton(
                    text="• 🗓 Mingguan" if period == "weekly" else "🗓 Mingguan",
                    callback_data="rf:journal:weekly",
                ),
            ],
            [
                InlineKeyboardButton(text="⚙️ Setting Skema", callback_data="cfg:refresh:default"),
                InlineKeyboardButton(text="🔄 Refresh Jurnal", callback_data=f"rf:journal:{period}"),
            ]
        ])
        return "\n".join(lines), kb

    async def cmd_journal(self, msg: Message, command: CommandObject | None = None) -> None:
        if not await self._allowed_user(msg):
            return
        period = "daily"
        if command and command.args:
            arg = command.args.strip().lower()
            if "week" in arg or "minggu" in arg:
                period = "weekly"
        await msg.answer("📊 <i>Menganalisis jurnal trading & evaluasi AI...</i>")
        txt, kb = await self._journal_view(period=period)
        await msg.answer(txt, reply_markup=kb)

    # ------------------------------------------------------------ settings

    async def _settings_view(self, user_id: int | str) -> tuple[str, InlineKeyboardMarkup]:
        prof = await self.engine.get_user_profile(user_id)
        scheme_name = prof.get("scheme", "scalp")
        scheme_cfg = get_scheme(scheme_name)
        auto_trade = bool(prof.get("auto_trade", 1))
        trade_size = float(prof.get("trade_size", 100.0))

        auto_badge = "🟢 <b>AKTIF (AI Autonomus)</b>" if auto_trade else "⚪ <b>NONAKTIF (Alert Saja)</b>"

        lines = [
            f"⚙️ <b>PENGATURAN STRATEGI & SKEMA TRADING</b>",
            f"👤 Akun ID: <code>{user_id}</code>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🎯 <b>Skema Aktif:</b> <b>{scheme_cfg.badge}</b>",
            f"📝 <i>{scheme_cfg.description}</i>",
            f"• Stop Loss: <b>-{scheme_cfg.stop_loss_pct:.1f}%</b>  • Take Profit: <b>+{scheme_cfg.take_profit_pct:.1f}%</b>",
            f"• Trailing Stop: <b>+{scheme_cfg.trailing_activation_pct:.1f}%</b> (jarak {scheme_cfg.trailing_distance_pct:.1f}%)",
            f"• Min Likuiditas: <b>${scheme_cfg.min_liquidity_usd:,.0f}</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🤖 <b>Mode Auto-Trade AI:</b> {auto_badge}",
            f"💵 <b>Ukuran Order Otomatis:</b> <b>${trade_size:.0f}</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            "💡 <i>Uji coba skema berbeda di akun kedua dan bandingkan performanya di menu Jurnal!</i>",
        ]

        toggle_auto_txt = "⏸ Matikan Auto-Trade" if auto_trade else "▶️ Nyalakan Auto-Trade"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="• ⚡️ Scalping" if scheme_name == "scalp" else "⚡️ Scalping",
                    callback_data=f"cfg:scheme:{user_id}:scalp",
                ),
                InlineKeyboardButton(
                    text="• 🛡️ Safe / Swing" if scheme_name == "safe" else "🛡️ Safe / Swing",
                    callback_data=f"cfg:scheme:{user_id}:safe",
                ),
            ],
            [
                InlineKeyboardButton(text=toggle_auto_txt, callback_data=f"cfg:toggle_auto:{user_id}"),
            ],
            [
                InlineKeyboardButton(text=f"{'✓ ' if abs(trade_size-50)<1 else ''}$50", callback_data=f"cfg:size:{user_id}:50"),
                InlineKeyboardButton(text=f"{'✓ ' if abs(trade_size-75)<1 else ''}$75", callback_data=f"cfg:size:{user_id}:75"),
                InlineKeyboardButton(text=f"{'✓ ' if abs(trade_size-100)<1 else ''}$100", callback_data=f"cfg:size:{user_id}:100"),
                InlineKeyboardButton(text=f"{'✓ ' if abs(trade_size-150)<1 else ''}$150", callback_data=f"cfg:size:{user_id}:150"),
            ],
            [
                InlineKeyboardButton(text="🔄 Refresh Setting", callback_data=f"cfg:refresh:{user_id}"),
                InlineKeyboardButton(text="📈 Cek Jurnal Perbandingan", callback_data="rf:journal:daily"),
            ],
        ])
        return "\n".join(lines), kb

    async def cmd_settings(self, msg: Message) -> None:
        if not await self._allowed_user(msg):
            return
        uid = str(msg.from_user.id if msg.from_user else "default")
        txt, kb = await self._settings_view(uid)
        await msg.answer(txt, reply_markup=kb)

    async def cmd_adduser(self, msg: Message, command: CommandObject) -> None:
        if not await self._allowed_user(msg):
            return
        arg = (command.args or "").strip()
        if not arg or not arg.isdigit():
            await msg.answer("Format: <code>/adduser &lt;telegram_user_id&gt;</code>\nContoh: <code>/adduser 123456789</code>")
            return
        uid = int(arg)
        extra = await self.engine.db.kv_get("extra_allowed_users", []) or []
        if uid not in extra:
            extra.append(uid)
            await self.engine.db.kv_set("extra_allowed_users", extra)
            self._extra_users.add(uid)
        await msg.answer(
            f"✅ <b>Akun Telegram ID <code>{uid}</code> Berhasil Ditambahkan!</b>\n\n"
            f"Akun keduamu sekarang bisa langsung berinteraksi dengan bot dan menguji coba skema trading yang berbeda."
        )

    # ------------------------------------------------------------- lessons

    async def cmd_lessons(self, msg: Message) -> None:
        if not await self._allowed_user(msg):
            return
        rows = await self.engine.db.fetchall(
            "SELECT scope,rule,weight,hits,wins FROM lessons ORDER BY weight DESC LIMIT 10"
        )
        if not rows:
            await msg.answer("📭 <i>Belum ada pelajaran yang tercatat.</i>")
            return
        out = ["📚 <b>DATABASE MEMORI & REFLEKSI AI</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for r in rows:
            wr = (r["wins"] / r["hits"] * 100) if r["hits"] else 0
            out.append(f"🔹 [<b>{_esc(r['scope'])}</b>] {_esc(r['rule'])}\n   <i>Skor: w{r['weight']:.1f} · Win: {wr:.0f}%</i>\n")
        await msg.answer("\n".join(out))

    # ------------------------------------------------------------- scan radar

    async def _scan_view(self, chain: str = "solana", page: int = 1) -> tuple[str, InlineKeyboardMarkup | None]:
        sigs = await self.engine.meme.discover(chain, limit=30, page=page)
        if not sigs:
            return (
                f"📭 <i>Tidak ada sinyal meme yang lolos filter di {chain.upper()} (Hal. {page}).</i>",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Prev", callback_data=f"scan:chain:{chain}:{max(1, page-1)}"),
                     InlineKeyboardButton(text="🔄 Refresh", callback_data=f"scan:chain:{chain}:{page}")]
                ])
            )

        mode_row = [
            InlineKeyboardButton(text="• 🌐 Market", callback_data=f"scan:chain:{chain}:{page}"),
            InlineKeyboardButton(text="🐋 Koin Whale", callback_data="scan:mode:whale"),
            InlineKeyboardButton(text="✍️ Input CA", callback_data="scan:mode:manual"),
        ]

        chain_tabs = [
            InlineKeyboardButton(text=f"• 🟣 Solana" if chain == "solana" else "🟣 Solana", callback_data="scan:chain:solana:1"),
            InlineKeyboardButton(text=f"• 🔵 Base" if chain == "base" else "🔵 Base", callback_data="scan:chain:base:1"),
            InlineKeyboardButton(text=f"• 🟡 BSC" if chain == "bsc" else "🟡 BSC", callback_data="scan:chain:bsc:1"),
        ]

        lines = [
            f"🔍 <b>RADAR MEME COIN</b> · <b>{chain.upper()}</b> (Hal. {page})",
            "<i>Filter likuiditas tebal, volume & momentum otomatis:</i>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        kb = [mode_row, chain_tabs]

        for s in sigs[:5]:
            sym = _esc(s.symbol)
            f = s.features
            addr = _esc(s.address)
            reasons_txt = ", ".join(f.get("reasons") or [])
            px = s.price or 0.0
            age_str = _fmt_age(float(f.get("age_minutes") or 0))

            lines.append(
                f"💊 <b>{sym}</b> · ⏳ <b>Umur: {age_str}</b> · Skor: <b>{s.score:.2f}</b>\n"
                f"   💵 ${px:.8f} · 1h: <b>{f.get('chg_1h', 0):+.1f}%</b>\n"
                f"   💧 Liq: <b>${f['liquidity_usd']:,.0f}</b> · Vol 24h: <b>${f.get('volume_24h', 0):,.0f}</b>\n"
                f"   🏷️ <i>{_esc(reasons_txt)}</i>\n"
                f"   CA: <code>{addr}</code>\n"
            )

            rid10 = secrets.token_urlsafe(6)
            rid25 = secrets.token_urlsafe(6)
            rid50 = secrets.token_urlsafe(6)

            await self.engine.db.kv_set(f"buy_req:{rid10}", {"chain": chain, "address": s.address, "usd": 10.0, "symbol": s.symbol})
            await self.engine.db.kv_set(f"buy_req:{rid25}", {"chain": chain, "address": s.address, "usd": 25.0, "symbol": s.symbol})
            await self.engine.db.kv_set(f"buy_req:{rid50}", {"chain": chain, "address": s.address, "usd": 50.0, "symbol": s.symbol})

            gmgn_url = f"https://gmgn.ai/{'sol' if chain=='solana' else chain}/token/{addr}"
            kb.append([
                InlineKeyboardButton(text="🟢 $10", callback_data=f"buy:{rid10}"),
                InlineKeyboardButton(text="🟢 $25", callback_data=f"buy:{rid25}"),
                InlineKeyboardButton(text="🟢 $50", callback_data=f"buy:{rid50}"),
                InlineKeyboardButton(text="👁 Terminal", callback_data=f"token:view:{chain}:{addr}"),
                InlineKeyboardButton(text="🔍 GMGN", url=gmgn_url),
            ])

        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"scan:chain:{chain}:{page-1}"))
        nav_row.append(InlineKeyboardButton(text=f"Hal {page}", callback_data="noop"))
        nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"scan:chain:{chain}:{page+1}"))
        kb.append(nav_row)

        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)

    async def _scan_whale_coins_view(self) -> tuple[str, InlineKeyboardMarkup]:
        tokens = await self.engine.whale.scan_all_whales_tokens(limit_per_whale=2)
        if not tokens:
            return (
                "🐋 <b>RADAR KOIN DARI WHALE TERDAFTAR</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Belum ada koin baru yang dibeli oleh daftar whale yang sedang dipantau.</i>\n\n"
                "Tambahkan whale di menu Whale atau tap tombol di bawah untuk cari whale baru.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔍 Cari Whale Baru", callback_data="whale:scan:solana:1"),
                     InlineKeyboardButton(text="🌐 Market Scan", callback_data="scan:chain:solana:1")]
                ])
            )

        mode_row = [
            InlineKeyboardButton(text="🌐 Market Scan", callback_data="scan:chain:solana:1"),
            InlineKeyboardButton(text="• 🐋 Koin Whale", callback_data="scan:mode:whale"),
            InlineKeyboardButton(text="✍️ Input CA", callback_data="scan:mode:manual"),
        ]

        lines = [
            "🐋 <b>RADAR KOIN DARI WHALE TERDAFTAR</b>",
            "<i>Token yang baru dibeli oleh whale yang sedang kau pantau:</i>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        kb = [mode_row]

        for t in tokens[:5]:
            sym = _esc(t.get("symbol") or "TOKEN")
            name = _esc(t.get("name") or sym)
            mint = _esc(t["mint"])
            px = t.get("price") or 0.0
            mc = t.get("market_cap") or 0.0
            liq = t.get("liquidity") or 0.0
            chg1 = t.get("chg_1h") or 0.0
            whale_label = _esc(t.get("whale_label") or "Whale")

            lines.append(
                f"💊 <b>{name}</b> (<b>{sym}</b>)\n"
                f"   👤 Diborong oleh: <b>{whale_label}</b>\n"
                f"   💵 Harga: <b>${px:.8f}</b> · 1h: <b>{chg1:+.1f}%</b>\n"
                f"   💎 MC: <b>${mc:,.0f}</b> · 💧 Liq: <b>${liq:,.0f}</b>\n"
                f"   CA: <code>{mint}</code>\n"
            )

            rid10 = secrets.token_urlsafe(6)
            rid25 = secrets.token_urlsafe(6)
            rid50 = secrets.token_urlsafe(6)

            await self.engine.db.kv_set(f"buy_req:{rid10}", {"chain": "solana", "address": t["mint"], "usd": 10.0, "symbol": sym})
            await self.engine.db.kv_set(f"buy_req:{rid25}", {"chain": "solana", "address": t["mint"], "usd": 25.0, "symbol": sym})
            await self.engine.db.kv_set(f"buy_req:{rid50}", {"chain": "solana", "address": t["mint"], "usd": 50.0, "symbol": sym})

            kb.append([
                InlineKeyboardButton(text="🟢 $10", callback_data=f"buy:{rid10}"),
                InlineKeyboardButton(text="🟢 $25", callback_data=f"buy:{rid25}"),
                InlineKeyboardButton(text="🟢 $50", callback_data=f"buy:{rid50}"),
                InlineKeyboardButton(text="👁 Terminal", callback_data=f"token:view:solana:{t['mint']}"),
            ])

        kb.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="scan:mode:whale")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)

    async def cmd_scan(self, msg: Message, command: CommandObject | None = None) -> None:
        if not await self._allowed_user(msg):
            return
        chain = "solana"
        page = 1
        args = (command.args if command else None) or ""
        parts = args.strip().split()

        if parts and parts[0].lower() == "whale":
            await msg.answer("🐋 <i>Memindai koin yang dibeli oleh seluruh whale terdaftar...</i>")
            txt, kb = await self._scan_whale_coins_view()
            await msg.answer(txt, reply_markup=kb)
            return

        if parts and parts[0].lower() in CHAINS:
            chain = parts[0].lower()
            if len(parts) > 1 and parts[1].isdigit():
                page = int(parts[1])

        await msg.answer(f"🔍 <i>Memindai radar meme {chain} (Hal. {page})...</i>")
        txt, kb = await self._scan_view(chain=chain, page=page)
        await msg.answer(txt, reply_markup=kb)

    # -------------------------------------------------------- token terminal view

    async def _token_terminal_view(self, chain: str, address: str) -> tuple[str, InlineKeyboardMarkup]:
        """Renders an Axiom/GMGN-style full Token Terminal card with safety audit & quick buy buttons."""
        ch = get_chain(chain)
        report = await self.engine.scanner.scan(ch, address)

        token_info: dict[str, Any] = {}
        try:
            r = await self._http.get(f"{DEXS_API}/{address}")
            if r.status_code == 200:
                pairs = (r.json() or {}).get("pairs") or []
                if pairs:
                    p = pairs[0]
                    base = p.get("baseToken") or {}
                    token_info = {
                        "name": base.get("name") or "Token",
                        "symbol": (base.get("symbol") or "TOKEN").upper(),
                        "price": float(p.get("priceUsd") or 0.0),
                        "market_cap": float(p.get("marketCap") or p.get("fdv") or 0.0),
                        "liquidity": float((p.get("liquidity") or {}).get("usd") or 0.0),
                        "vol_24h": float((p.get("volume") or {}).get("h24") or 0.0),
                        "chg_5m": float((p.get("priceChange") or {}).get("m5") or 0.0),
                        "chg_1h": float((p.get("priceChange") or {}).get("h1") or 0.0),
                        "chg_24h": float((p.get("priceChange") or {}).get("h24") or 0.0),
                        "dex_url": p.get("url") or f"https://dexscreener.com/{chain}/{address}",
                    }
        except Exception as exc:
            log.debug("dexscreener fetch error: %s", exc)

        name = _esc(token_info.get("name") or address[:6])
        sym = _esc(token_info.get("symbol") or address[:4].upper())
        px = token_info.get("price", 0.0)
        mc = token_info.get("market_cap", 0.0)
        liq = token_info.get("liquidity", 0.0)
        vol = token_info.get("vol_24h", 0.0)
        c5 = token_info.get("chg_5m", 0.0)
        c1 = token_info.get("chg_1h", 0.0)
        c24 = token_info.get("chg_24h", 0.0)

        gp = report.data.get("goplus") or {}
        hp = report.data.get("honeypot") or {}

        mint_status = "🟢 Revoked" if str((gp.get("mintable") or {}).get("status", "0")) != "1" else "🔴 Mintable"
        freeze_status = "🟢 Revoked" if str((gp.get("freezable") or {}).get("status", "0")) != "1" else "🔴 Freezable"
        hp_status = "🟢 Lolos" if not hp.get("isHoneypot") else "🔴 Honeypot"
        safety_badge = "🟢 <b>AMAN</b>" if report.ok else "🔴 <b>BERISIKO</b>"
        reasons_txt = " · ".join(report.reasons) if report.reasons else "Tidak ditemukan celah berbahaya"

        gmgn_url = f"https://gmgn.ai/{'sol' if chain=='solana' else chain}/token/{address}"
        dex_url = token_info.get("dex_url", f"https://dexscreener.com/{chain}/{address}")

        lines = [
            f"⚡️ <b>GMGN TOKEN TERMINAL</b> · <b>{chain.upper()}</b>",
            f"💊 <b>${sym}</b> · <b>{name}</b>",
            f"<code>{address}</code> <i>(tap to copy)</i>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"💵 <b>Harga:</b> <b>${px:.8f}</b>",
            f"💎 <b>Market Cap:</b> <b>${mc:,.0f}</b> · 💧 <b>Liq:</b> <b>${liq:,.0f}</b>",
            f"📊 <b>Vol 24h:</b> <b>${vol:,.0f}</b>",
            f"📈 <b>Kinerja:</b> 5m: <b>{c5:+.1f}%</b> · 1h: <b>{c1:+.1f}%</b> · 24h: <b>{c24:+.1f}%</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🛡️ <b>SAFETY AUDIT:</b> {safety_badge} (Skor: <b>{report.score*10:.1f}/10</b>)",
            f"• Mint: {mint_status}  • Freeze: {freeze_status}",
            f"• Honeypot: {hp_status}",
            f"• Catatan: <i>{_esc(reasons_txt)}</i>",
        ]

        rid10 = secrets.token_urlsafe(6)
        rid25 = secrets.token_urlsafe(6)
        rid50 = secrets.token_urlsafe(6)
        rid100 = secrets.token_urlsafe(6)

        await self.engine.db.kv_set(f"buy_req:{rid10}", {"chain": chain, "address": address, "usd": 10.0, "symbol": sym})
        await self.engine.db.kv_set(f"buy_req:{rid25}", {"chain": chain, "address": address, "usd": 25.0, "symbol": sym})
        await self.engine.db.kv_set(f"buy_req:{rid50}", {"chain": chain, "address": address, "usd": 50.0, "symbol": sym})
        await self.engine.db.kv_set(f"buy_req:{rid100}", {"chain": chain, "address": address, "usd": 100.0, "symbol": sym})

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 $10", callback_data=f"buy:{rid10}"),
                InlineKeyboardButton(text="🟢 $25", callback_data=f"buy:{rid25}"),
                InlineKeyboardButton(text="🟢 $50", callback_data=f"buy:{rid50}"),
                InlineKeyboardButton(text="🟢 $100", callback_data=f"buy:{rid100}"),
            ],
            [
                InlineKeyboardButton(text="🔍 GMGN Chart", url=gmgn_url),
                InlineKeyboardButton(text="📊 DexScreener", url=dex_url),
            ],
            [
                InlineKeyboardButton(text="🔄 Refresh Terminal", callback_data=f"token:view:{chain}:{address}"),
                InlineKeyboardButton(text="🔙 Radar Scan", callback_data=f"scan:chain:{chain}:1"),
            ]
        ])
        return "\n".join(lines), kb

    # ------------------------------------------------------------- wallet terminal

    async def _wallet_view(self) -> tuple[str, InlineKeyboardMarkup]:
        wallets = await self.engine.db.kv_get("wallets", {}) or {}
        lines = [
            "👛 <b>ONCHAIN WALLET TERMINAL</b>",
            "<i>Status saldo & wallet yang terhubung untuk monitoring/eksekusi:</i>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        for name in ("solana", "base", "bsc", "ethereum"):
            addr = wallets.get(name)
            ch = get_chain(name)
            try:
                bal = await self.engine.dex.native_balance(name)
                bal_txt = f"<b>{bal:.4f} {ch.native_symbol}</b>"
            except Exception:
                bal_txt = "<i>N/A</i>"

            flag = "🟣" if name == "solana" else "🔵" if name == "base" else "🟡" if name == "bsc" else "⚪"
            if addr:
                lines.append(f"{flag} <b>{name.upper()}:</b> {bal_txt}\n   <code>{addr}</code>")
            else:
                lines.append(f"{flag} <b>{name.upper()}:</b> {bal_txt} <i>(belum tersambung)</i>")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔗 Konek Solana", callback_data="wlt:connect:solana"),
                InlineKeyboardButton(text="🔗 Konek EVM", callback_data="wlt:connect:base"),
            ],
            [
                InlineKeyboardButton(text="🔄 Refresh Saldo", callback_data="rf:wallet"),
            ]
        ])
        return "\n".join(lines), kb

    async def cmd_wallet(self, msg: Message) -> None:
        if not await self._allowed_user(msg):
            return
        txt, kb = await self._wallet_view()
        await msg.answer(txt, reply_markup=kb)

    async def cmd_connect(self, msg: Message, command: CommandObject) -> None:
        if not await self._allowed_user(msg):
            return
        chain = (command.args or "solana").strip().split()[0].lower()
        if chain not in CHAINS:
            await msg.answer(f"❓ Chain tidak dikenal. Pilihan: {', '.join(CHAINS)}")
            return
        rid = secrets.token_urlsafe(9)
        await self.engine.db.kv_set(
            f"wallet_req:{rid}",
            {"id": rid, "chain": chain, "kind": "link", "tx": {}},
        )
        url = f"{self.engine.s.public_url}/wallet?id={rid}"
        await msg.answer(
            f"🔗 <b>SAMBUNGKAN WALLET {chain.upper()}</b>\n"
            f"Buka tautan ini di browser MetaMask atau Phantom di HP:\n{url}\n\n"
            f"<i>Hanya tanda tangan pembuktian kepemilikan — tanpa biaya & tanpa transaksi.</i>"
        )

    # ------------------------------------------------------------- whale dashboard

    async def _whale_view(self) -> tuple[str, InlineKeyboardMarkup]:
        whales = await self.engine.whale.list_whales()
        if not whales:
            txt = (
                "🐋 <b>TRACKER WHALE & SMART MONEY</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Belum ada wallet whale yang dipantau.</i>\n\n"
                "Tap tombol di bawah untuk mendeteksi whale aktif yang borong token di pool trending, "
                "atau daftarkan alamat manual:\n"
                "<code>/whale add &lt;address&gt; [label]</code>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Deteksi Whale Aktif (Radar)", callback_data="whale:scan:solana:1")]
            ])
            return txt, kb

        lines = [
            "🐋 <b>DAFTAR WHALE DIPANTAU</b>",
            "<i>On-chain RPC monitoring aktif setiap siklus</i>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        kb = []
        for w in whales:
            wid = w["id"]
            addr = _esc(w["address"])
            addr_short = f"{addr[:4]}...{addr[-4:]}"
            label = _esc(w.get("label") or f"Whale-{wid}")
            auto = bool(w.get("auto_copy"))
            nominal = float(w.get("copy_usd") or 100.0)
            status_badge = f"🟢 <b>Auto (${nominal:.0f})</b>" if auto else "⚪ <b>Alert Only</b>"
            trades = w.get("total_trades", 0)

            lines.append(
                f"👤 <b>#{wid} {label}</b> · <code>{addr_short}</code>\n"
                f"   Status: {status_badge} · Transaksi: <b>{trades}</b>\n"
                f"   CA: <code>{addr}</code>\n"
            )

            toggle_lbl = "⏸ Matikan Auto" if auto else "▶️ Nyalakan Auto"
            kb.append([
                InlineKeyboardButton(text=f"💼 Koin Whale #{wid}", callback_data=f"whale:coins:{wid}"),
                InlineKeyboardButton(text=f"💵 Set ${nominal:.0f}", callback_data=f"whale:setmenu:{wid}"),
            ])
            kb.append([
                InlineKeyboardButton(text=toggle_lbl, callback_data=f"whale:toggle:{wid}"),
                InlineKeyboardButton(text="❌ Hapus", callback_data=f"whale:del:{wid}"),
            ])

        kb.append([
            InlineKeyboardButton(text="🔍 Radar Whale Baru", callback_data="whale:scan:solana:1"),
            InlineKeyboardButton(text="🔄 Refresh", callback_data="rf:whale"),
        ])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)

    async def _whale_candidates_view(
        self, chain: str = "solana", page: int = 1
    ) -> tuple[str, InlineKeyboardMarkup]:
        candidates = await self.engine.whale.scan_candidates(
            chain=chain, min_usd=1000.0, top_pools=3, page=page
        )
        if not candidates:
            return (
                f"📭 <i>Tidak ada whale baru di halaman {page}.</i>",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Halaman Sebelumnya", callback_data=f"whale:scan:{chain}:{max(1, page-1)}"),
                     InlineKeyboardButton(text="🔙 Daftar Whale", callback_data="rf:whale")]
                ]),
            )

        lines = [
            f"🔍 <b>RADAR WHALE AKTIF</b> · <b>{chain.upper()}</b> (Hal. {page})",
            "<i>Transaksi beli besar (>= $1k) terdeteksi di pool trending:</i>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        kb = []
        for i, c in enumerate(candidates, 1):
            w = _esc(c["wallet"])
            w_short = f"{w[:4]}...{w[-4:]}"
            pools_txt = _esc(", ".join(c.get("pools", [])[:2]))
            vol = c["total_usd"]
            buys = c["buys"]

            lines.append(
                f"{i}️⃣ <b>Wallet:</b> <code>{w}</code>\n"
                f"   💰 Beli: <b>${vol:,.0f}</b> ({buys} tx) · Pool: <b>{pools_txt}</b>\n"
            )
            kb.append([
                InlineKeyboardButton(
                    text=f"➕ Ikuti {w_short} (${vol:,.0f})",
                    callback_data=f"whale:follow:{c['wallet']}",
                ),
                InlineKeyboardButton(
                    text="🔗 GMGN",
                    url=f"https://gmgn.ai/sol/address/{c['wallet']}",
                ),
            ])

        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"whale:scan:{chain}:{page-1}"))
        nav_row.append(InlineKeyboardButton(text=f"Hal {page}", callback_data="noop"))
        nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"whale:scan:{chain}:{page+1}"))

        kb.append(nav_row)
        kb.append([InlineKeyboardButton(text="🔙 Kembali ke Tracker", callback_data="rf:whale")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)

    async def _whale_coins_view(self, whale_id: int) -> tuple[str, InlineKeyboardMarkup]:
        w = await self.engine.whale.get_whale(whale_id)
        if not w:
            return "❓ Whale tidak ditemukan.", InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Kembali", callback_data="rf:whale")]
            ])

        addr = w["address"]
        label = _esc(w.get("label") or f"Whale-{whale_id}")
        addr_short = f"{addr[:4]}...{addr[-4:]}"

        tokens = await self.engine.whale.get_whale_recent_tokens(addr, limit=3)
        if not tokens:
            return (
                f"📭 <i>Belum mendeteksi transaksi token baru dari <b>{label}</b> (<code>{addr_short}</code>).</i>",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Kembali ke Tracker", callback_data="rf:whale")]
                ]),
            )

        lines = [
            f"💼 <b>KOIN YANG DIBELI WHALE</b> · <b>{label}</b>",
            f"👛 <code>{addr}</code>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        kb = []

        for t in tokens:
            sym = _esc(t.get("symbol") or "TOKEN")
            name = _esc(t.get("name") or sym)
            mint = _esc(t["mint"])
            px = t.get("price") or 0.0
            mc = t.get("market_cap") or 0.0
            liq = t.get("liquidity") or 0.0
            chg1 = t.get("chg_1h") or 0.0

            lines.append(
                f"💊 <b>{name}</b> (<b>{sym}</b>)\n"
                f"   💵 Harga: <b>${px:.8f}</b> · 1h: <b>{chg1:+.1f}%</b>\n"
                f"   💎 MC: <b>${mc:,.0f}</b> · 💧 Liq: <b>${liq:,.0f}</b>\n"
                f"   CA: <code>{mint}</code>\n"
            )

            rid10 = secrets.token_urlsafe(6)
            rid25 = secrets.token_urlsafe(6)
            rid50 = secrets.token_urlsafe(6)

            await self.engine.db.kv_set(f"buy_req:{rid10}", {"chain": "solana", "address": t["mint"], "usd": 10.0, "symbol": sym})
            await self.engine.db.kv_set(f"buy_req:{rid25}", {"chain": "solana", "address": t["mint"], "usd": 25.0, "symbol": sym})
            await self.engine.db.kv_set(f"buy_req:{rid50}", {"chain": "solana", "address": t["mint"], "usd": 50.0, "symbol": sym})

            kb.append([
                InlineKeyboardButton(text="🟢 $10", callback_data=f"buy:{rid10}"),
                InlineKeyboardButton(text="🟢 $25", callback_data=f"buy:{rid25}"),
                InlineKeyboardButton(text="🟢 $50", callback_data=f"buy:{rid50}"),
                InlineKeyboardButton(text="👁 Terminal", callback_data=f"token:view:solana:{t['mint']}"),
            ])

        kb.append([InlineKeyboardButton(text="🔙 Kembali ke Tracker", callback_data="rf:whale")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)

    def _nominal_menu(self, whale_id: int, current_usd: float) -> tuple[str, InlineKeyboardMarkup]:
        txt = (
            f"💵 <b>SETTING NOMINAL AUTO-COPY</b> · #{whale_id}\n"
            f"Nominal sekarang: <b>${current_usd:.0f}</b> per order\n\n"
            "Pilih nominal cepat di bawah atau gunakan perintah:\n"
            f"<code>/whale set {whale_id} &lt;usd&gt;</code>"
        )
        amounts = [10, 25, 50, 100, 200, 300]
        rows = []
        curr_row = []
        for a in amounts:
            btn_txt = f"✓ ${a}" if abs(a - current_usd) < 0.1 else f"${a}"
            curr_row.append(InlineKeyboardButton(text=btn_txt, callback_data=f"whale:setamt:{whale_id}:{a}"))
            if len(curr_row) == 3:
                rows.append(curr_row)
                curr_row = []
        if curr_row:
            rows.append(curr_row)

        rows.append([InlineKeyboardButton(text="🔙 Kembali ke Whale", callback_data="rf:whale")])
        return txt, InlineKeyboardMarkup(inline_keyboard=rows)

    async def cmd_whale(self, msg: Message, command: CommandObject | None = None) -> None:
        if not await self._allowed_user(msg):
            return
        args = (command.args if command else None) or ""
        parts = args.strip().split()
        sub = parts[0].lower() if parts else ""

        if sub == "scan":
            chain = "solana"
            page = 1
            if len(parts) > 1:
                if parts[1].isdigit():
                    page = int(parts[1])
                else:
                    chain = parts[1]
            if len(parts) > 2 and parts[2].isdigit():
                page = int(parts[2])

            await msg.answer(f"🔍 <i>Memindai whale di pool trending {chain} (Hal. {page})...</i>")
            txt, kb = await self._whale_candidates_view(chain, page=page)
            await msg.answer(txt, reply_markup=kb)
            return

        if sub == "add":
            if len(parts) < 2:
                await msg.answer("Format: <code>/whale add &lt;address&gt; [label]</code>")
                return
            addr = parts[1]
            label = " ".join(parts[2:]) if len(parts) > 2 else f"Whale-{addr[:4]}"
            w = await self.engine.whale.add_whale(
                addr, chain="solana", label=label, auto_copy=True, copy_usd=100.0
            )
            await msg.answer(
                f"✅ <b>Whale Berhasil Ditambahkan!</b>\n"
                f"🏷️ Label: <b>{_esc(w.get('label'))}</b>\n"
                f"👛 Alamat: <code>{_esc(addr)}</code>\n"
                f"⚙️ Auto-Copy: <b>$50</b> (Aktif)",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💼 Cek Koin Whale", callback_data=f"whale:coins:{w['id']}"),
                     InlineKeyboardButton(text="🔙 Daftar Whale", callback_data="rf:whale")]
                ]),
            )
            return

        if sub in ("del", "rm"):
            if len(parts) < 2:
                await msg.answer("Format: <code>/whale del &lt;id_atau_address&gt;</code>")
                return
            ok = await self.engine.whale.remove_whale(parts[1])
            await msg.answer("✅ Whale berhasil dihapus." if ok else "❓ Whale tidak ditemukan.")
            return

        if sub == "set":
            if len(parts) < 3 or not parts[1].isdigit():
                await msg.answer("Format: <code>/whale set &lt;id&gt; &lt;usd&gt;</code>")
                return
            wid = int(parts[1])
            try:
                usd = float(parts[2])
            except ValueError:
                await msg.answer("Nominal USD tidak valid.")
                return
            res = await self.engine.whale.set_copy_usd(wid, usd)
            if res:
                await msg.answer(f"✅ Nominal auto-copy untuk <b>#{wid}</b> diubah ke <b>${usd:.0f}</b>.")
            else:
                await msg.answer("❓ Whale tidak ditemukan.")
            return

        if sub == "coins":
            if len(parts) < 2 or not parts[1].isdigit():
                await msg.answer("Format: <code>/whale coins &lt;id&gt;</code>")
                return
            wid = int(parts[1])
            await msg.answer("🔍 <i>Menganalisis transaksi on-chain whale...</i>")
            txt, kb = await self._whale_coins_view(wid)
            await msg.answer(txt, reply_markup=kb)
            return

        txt, kb = await self._whale_view()
        await msg.answer(txt, reply_markup=kb)

    # ----------------------------------------------------------- buy execution

    async def _execute_buy(
        self, chain: str, address: str, usd: float, user_id: str | int = "default"
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        if chain not in CHAINS:
            return "❓ Chain tidak dikenal", None
        ch = get_chain(chain)
        report = await self.engine.scanner.scan(ch, address)
        if not report.ok:
            reasons_txt = ", ".join(report.reasons)
            return f"🚫 <b>Pembelian Ditolak Safety:</b>\n{_esc(reasons_txt)}", None

        price = await self.engine.dex.price(chain, address)
        sym = address[:6].upper()

        prof = await self.engine.get_user_profile(user_id)
        target_scheme = prof.get("scheme", "scalp")
        scheme_cfg = get_scheme(target_scheme)

        sig = Signal(
            strategy="manual", symbol=f"{sym}/USD", score=1.0, venue=Venue.DEX,
            chain=chain, address=address, price=price,
            features={"liquidity_usd": 0, "manual": True, "scheme": target_scheme, "account_id": str(user_id)},
        )
        intent = OrderIntent(
            venue=Venue.DEX, side=Side.BUY, symbol=sig.symbol, usd_amount=usd,
            chain=chain, address=address, slippage_bps=300, reason=f"manual [{target_scheme}]",
        )
        state = await self.engine.risk.account_state(self.engine.equity_usd)
        dec = await self.engine.risk.evaluate(intent, state, extra={
            "max_position_usd": usd, "stop_pct": scheme_cfg.stop_loss_pct,
        })
        if not dec.approved:
            if "kill switch" in dec.reason.lower() or "daily loss" in dec.reason.lower():
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="▶️ Lepas Kill Switch & Reset Baseline", callback_data="rf:resume")]
                ])
                return (
                    f"🚫 <b>Ditolak Risk Engine:</b> {_esc(dec.reason)}\n\n"
                    f"💡 <i>Tekan tombol di bawah untuk melepas kill switch dan melanjutkan trading:</i>",
                    kb,
                )
            return f"🚫 <b>Ditolak Risk Engine:</b> {_esc(dec.reason)}", None

        intent.usd_amount = dec.usd_amount
        fill = await self.engine.dex.execute(intent)
        stop = fill.price * (1 - scheme_cfg.stop_loss_pct / 100)
        tp = fill.price * (1 + scheme_cfg.take_profit_pct / 100)
        trailing = fill.price * (1 + scheme_cfg.trailing_activation_pct / 100)

        pos_id = await self.engine.db.execute(
            "INSERT INTO positions(venue,chain,symbol,side,qty,entry_price,entry_usd,"
            "stop_loss,take_profit,trailing_stop,peak_price,meta,scheme,account_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "dex", chain, sig.symbol, "buy", fill.qty, fill.price, dec.usd_amount,
                stop, tp, trailing, fill.price, json.dumps({"address": address, "tx": fill.tx_hash}),
                target_scheme, str(user_id),
            ),
        )
        return (
            f"✅ <b>ORDER TERISI (PAPER)</b>\n"
            f"💊 Token: <b>{_esc(sig.symbol)}</b> · <b>{scheme_cfg.badge}</b>\n"
            f"💵 Nominal: <b>${dec.usd_amount:.2f}</b> ({fill.qty:,.2f} token)\n"
            f"🎯 Entry: ${fill.price:.8f}\n"
            f"🛑 Stop Loss: ${stop:.8f} (-{scheme_cfg.stop_loss_pct:.0f}%)\n"
            f"🎯 Take Profit: ${tp:.8f} (+{scheme_cfg.take_profit_pct:.0f}%)\n"
            f"📂 Posisi: <b>#{pos_id}</b>",
            None,
        )

    async def cmd_buy(self, msg: Message, command: CommandObject) -> None:
        if not await self._allowed_user(msg):
            return
        parts = (command.args or "").split()
        if len(parts) < 2:
            await msg.answer("Format: <code>/buy &lt;chain&gt; &lt;alamat&gt; [usd]</code>")
            return
        chain, address = parts[0], parts[1]
        usd = float(parts[2]) if len(parts) > 2 else self.engine.s.meme.max_position_usd - 20
        uid = str(msg.from_user.id if msg.from_user else "default")
        await msg.answer(f"🛡️ <i>Menjalankan safety audit {address[:8]}...</i>")
        res_txt, res_kb = await self._execute_buy(chain, address, usd, user_id=uid)
        await msg.answer(res_txt, reply_markup=res_kb)

    async def cmd_sell(self, msg: Message, command: CommandObject) -> None:
        if not await self._allowed_user(msg):
            return
        try:
            pid = int((command.args or "").strip())
        except ValueError:
            await msg.answer("Format: <code>/sell &lt;id&gt;</code>")
            return
        await self._close(pid, msg)

    async def _close(self, pid: int, msg: Message, fraction: float = 1.0, quiet: bool = False) -> None:
        row = await self.engine.db.fetchone("SELECT * FROM positions WHERE id=?", (pid,))
        if not row or row["status"] != "open":
            if not quiet:
                await msg.answer("❓ Posisi tidak ditemukan atau sudah ditutup.")
            return
        from gembul.core.risk import _row_to_position
        pos = _row_to_position(row)
        px = await self.engine._price_for(pos.symbol, pos.chain, pos.venue, pid)
        if not px or px <= 0:
            px = pos.entry_price
        await self.engine.close_position(pid, px, "manual telegram", fraction=fraction)
        if not quiet:
            pnl = (px - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0.0
            icon = "🟢" if pnl >= 0 else "🔴"
            pct_txt = "100%" if fraction >= 0.999 else f"{fraction*100:.0f}%"
            await msg.answer(
                f"{icon} <b>Posisi #{pid} ({pct_txt}) Ditutup</b> @ ${px:.8f}\nPnL: <b>{pnl:+.2f}%</b>"
            )

    async def cmd_pause(self, msg: Message, command: CommandObject | None = None) -> None:
        if not await self._allowed_user(msg):
            return
        reason = (command.args if command else None) or "manual"
        await self.engine.risk.halt(reason)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Lepas Kill Switch", callback_data="rf:resume")]
        ])
        await msg.answer(f"🛑 <b>KILL SWITCH DIAKTIFKAN:</b> {_esc(reason)}", reply_markup=kb)

    async def cmd_resume(self, msg: Message) -> None:
        if not await self._allowed_user(msg):
            return
        await self.engine.risk.resume(current_equity=self.engine.equity_usd)
        await msg.answer("▶️ <b>Kill switch dilepas! Mesin kembali aktif dan batas rugi harian direset ke modal saat ini.</b>")

    async def cmd_ask(self, msg: Message, command: CommandObject) -> None:
        if not await self._allowed_user(msg):
            return
        q = (command.args or "").strip()
        if not q:
            await msg.answer("Format: <code>/ask &lt;pertanyaan&gt;</code>")
            return
        if not self.engine.llm.ready:
            await msg.answer("🧠 <i>LLM belum dikonfigurasi.</i>")
            return
        snap = await self.engine.snapshot()
        lessons = await self.engine.memory.lessons_for(q, "global", 6)
        ctx = "\n".join(f"- {l.rule}" for l in lessons)
        sys = (
            "Kau analis trading kuantitatif & meme coin. Jawab singkat, tajam, bahasa Indonesia, "
            "maksimal 120 kata. Format jawaban rapi dengan bullet points jika relevan."
        )
        user = f"data pasar: {json.dumps(snap, default=str)[:2500]}\naturan memori:\n{ctx}\n\npertanyaan: {q}"
        try:
            ans = await self.engine.llm.chat(sys, user)
            await msg.answer(f"💬 <b>Analisis AI:</b>\n{_esc(ans[:3500])}")
        except Exception as exc:
            await msg.answer(f"❌ Gagal: {_esc(exc)}")

    # ------------------------------------------------------------ callbacks

    async def on_cb_close(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        pos_id = int(parts[2])
        fraction = float(parts[3]) if len(parts) > 3 else 1.0
        await self._close(pos_id, cb.message, fraction=fraction, quiet=False)  # type: ignore[arg-type]
        await cb.answer("Order tutup dikirim.")

    async def on_cb_shadow_close(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        pos_id = int(parts[2])
        try:
            res = await self.engine.shadow.close_manual(pos_id)
            if not res:
                await cb.answer("Posisi tidak ditemukan atau sudah tutup.", show_alert=True)
                return
            icon = "🟢" if res["pnl_usd"] >= 0 else "🔴"
            await cb.answer("Posisi shadow ditutup.")
            await cb.message.answer(  # type: ignore[union-attr]
                f"{icon} <b>Posisi Saldo Uji #{res['id']} ({_esc(res['symbol'])}) Ditutup Manual</b>\n"
                f"📈 Realized: <b>{res['pnl_pct']:+.2f}%</b> (<b>${res['pnl_usd']:+.2f}</b>)"
            )
            txt, kb = await self._positions_view()
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass
        except Exception as exc:
            await cb.answer(f"Gagal tutup: {exc}", show_alert=True)

    async def on_cb_buy(self, cb: CallbackQuery) -> None:
        rid = (cb.data or "").split(":", 1)[-1]
        req = await self.engine.db.kv_get(f"buy_req:{rid}")
        if not req:
            await cb.answer("Permintaan sudah kadaluarsa.", show_alert=True)
            return
        await cb.answer("Memproses order...")
        await self.engine.db.kv_del(f"buy_req:{rid}")
        uid = str(cb.from_user.id if cb.from_user else "default")
        res_txt, res_kb = await self._execute_buy(req["chain"], req["address"], req["usd"], user_id=uid)
        await cb.message.answer(res_txt, reply_markup=res_kb)  # type: ignore[union-attr]

    async def on_cb_scan(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        sub = parts[1] if len(parts) > 1 else "chain"

        if sub == "mode":
            mode = parts[2] if len(parts) > 2 else "market"
            if mode == "whale":
                await cb.answer("Memindai koin whale...")
                txt, kb = await self._scan_whale_coins_view()
                try:
                    await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
                except TelegramBadRequest:
                    pass
            elif mode == "manual":
                await cb.answer()
                await cb.message.answer(  # type: ignore[union-attr]
                    "✍️ <b>INPUT CONTRACT ADDRESS (CA)</b>\n"
                    "Kirim atau paste alamat CA token di chat ini untuk membuka Token Terminal & Audit instan."
                )
            else:
                txt, kb = await self._scan_view("solana", page=1)
                try:
                    await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
                except TelegramBadRequest:
                    pass

        elif sub == "chain":
            chain = parts[2] if len(parts) > 2 else "solana"
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
            await cb.answer(f"Memuat {chain.upper()} hal {page}...")
            txt, kb = await self._scan_view(chain=chain, page=page)
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

    async def on_cb_token(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        action = parts[1] if len(parts) > 1 else "view"
        chain = parts[2] if len(parts) > 2 else "solana"
        addr = parts[3] if len(parts) > 3 else ""

        if action == "view":
            await cb.answer("Menganalisis Token Terminal...")
            txt, kb = await self._token_terminal_view(chain, addr)
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

    async def on_cb_wallet(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        chain = parts[2] if len(parts) > 2 else "solana"

        if action == "connect":
            rid = secrets.token_urlsafe(9)
            await self.engine.db.kv_set(
                f"wallet_req:{rid}",
                {"id": rid, "chain": chain, "kind": "link", "tx": {}},
            )
            url = f"{self.engine.s.public_url}/wallet?id={rid}"
            await cb.answer()
            await cb.message.answer(  # type: ignore[union-attr]
                f"🔗 <b>SAMBUNGKAN WALLET {chain.upper()}</b>\n"
                f"Buka tautan ini di browser MetaMask atau Phantom:\n{url}\n\n"
                f"<i>Tanda tangan pembuktian kepemilikan — tanpa biaya onchain.</i>"
            )

    async def on_cb_config(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        user_id = parts[2] if len(parts) > 2 else str(cb.from_user.id if cb.from_user else "default")

        if action == "scheme":
            target = parts[3] if len(parts) > 3 else "scalp"
            await self.engine.set_user_scheme(user_id, target)
            badge = get_scheme(target).badge
            await cb.answer(f"Skema diubah ke: {badge}", show_alert=True)
            txt, kb = await self._settings_view(user_id)
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

        elif action == "toggle_auto":
            prof = await self.engine.get_user_profile(user_id)
            curr = bool(prof.get("auto_trade", 1))
            await self.engine.set_user_auto_trade(user_id, not curr)
            st = "NONAKTIF (Alert Saja)" if curr else "AKTIF (AI Autonomus)"
            await cb.answer(f"Auto-Trade: {st}", show_alert=True)
            txt, kb = await self._settings_view(user_id)
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

        elif action == "size":
            amt = float(parts[3])
            await self.engine.set_user_trade_size(user_id, amt)
            await cb.answer(f"Ukuran order diubah ke: ${amt:.0f}", show_alert=True)
            txt, kb = await self._settings_view(user_id)
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

        elif action == "refresh":
            await cb.answer("Pengaturan dimuat ulang.")
            txt, kb = await self._settings_view(user_id)
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

    async def on_cb_whale(self, cb: CallbackQuery) -> None:
        data = cb.data or ""
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "scan":
            chain = parts[2] if len(parts) > 2 else "solana"
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
            await cb.answer(f"Memindai whale hal {page}...")
            txt, kb = await self._whale_candidates_view(chain, page=page)
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

        elif action == "follow":
            addr = parts[2]
            await self.engine.whale.add_whale(
                addr, chain="solana", label=f"Whale-{addr[:4]}", auto_copy=True, copy_usd=100.0
            )
            await cb.answer(f"Whale {addr[:4]}... mulai dipantau!", show_alert=True)
            txt, kb = await self._whale_view()
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

        elif action == "toggle":
            wid = int(parts[2])
            res = await self.engine.whale.toggle_whale(wid)
            status_txt = "Auto-Copy AKTIF" if (res or {}).get("auto_copy") else "Alert Only"
            await cb.answer(f"Status diubah: {status_txt}")
            txt, kb = await self._whale_view()
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

        elif action == "del":
            wid = int(parts[2])
            await self.engine.whale.remove_whale(wid)
            await cb.answer("Whale berhasil dihapus.")
            txt, kb = await self._whale_view()
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

        elif action == "coins":
            wid = int(parts[2])
            await cb.answer("Memindai koin yang dibeli whale...")
            txt, kb = await self._whale_coins_view(wid)
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

        elif action == "setmenu":
            wid = int(parts[2])
            whale = await self.engine.whale.get_whale(wid)
            curr = float(whale.get("copy_usd", 100.0)) if whale else 50.0
            txt, kb = self._nominal_menu(wid, curr)
            await cb.answer()
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

        elif action == "setamt":
            wid = int(parts[2])
            amt = float(parts[3])
            await self.engine.whale.set_copy_usd(wid, amt)
            await cb.answer(f"Nominal diubah ke ${amt:.0f}!")
            txt, kb = await self._whale_view()
            try:
                await cb.message.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]
            except TelegramBadRequest:
                pass

    async def on_cb_refresh(self, cb: CallbackQuery) -> None:
        data = cb.data or ""
        parts = data.split(":")
        kind = parts[1] if len(parts) > 1 else ""
        msg = cb.message
        try:
            if kind == "status":
                txt, _ = await self._status_text()
                rows = [
                    [InlineKeyboardButton(text="🔄 Refresh", callback_data="rf:status"),
                     InlineKeyboardButton(text="📜 Riwayat Trade", callback_data="rf:history")],
                    [InlineKeyboardButton(text="💰 Posisi Aktif", callback_data="rf:pos"),
                     InlineKeyboardButton(text="🔍 Radar Scan", callback_data="scan:chain:solana:1")],
                ]
                await msg.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))  # type: ignore[union-attr]

            elif kind == "history":
                txt, kb = await self._history_view()
                await msg.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]

            elif kind == "resume":
                await self.engine.risk.resume(current_equity=self.engine.equity_usd)
                await cb.answer("Kill switch dilepas! Mesin siap trading.", show_alert=True)
                txt, is_halted = await self._status_text()
                rows = [
                    [InlineKeyboardButton(text="🔄 Refresh", callback_data="rf:status"),
                     InlineKeyboardButton(text="🔍 Radar Scan", callback_data="scan:chain:solana:1")]
                ]
                await msg.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))  # type: ignore[union-attr]

            elif kind == "pos":
                txt, kb = await self._positions_view()
                await msg.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]

            elif kind == "whale":
                txt, kb = await self._whale_view()
                await msg.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]

            elif kind == "wallet":
                txt, kb = await self._wallet_view()
                await msg.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]

            elif kind == "journal":
                period = parts[2] if len(parts) > 2 else "daily"
                await cb.answer(f"Memuat jurnal {period}...")
                txt, kb = await self._journal_view(period=period)
                await msg.edit_text(txt, reply_markup=kb)  # type: ignore[union-attr]

        except TelegramBadRequest:
            pass
        await cb.answer("Diperbarui.")

    # ------------------------------------------------------------- text routing

    async def on_text(self, msg: Message) -> None:
        if not await self._allowed_user(msg):
            return
        raw = (msg.text or "").strip()
        key = raw.lower()

        action = _MENU_MAP.get(key)
        try:
            if action == "status":
                await self.cmd_status(msg)
                return
            elif action == "pos":
                await self.cmd_positions(msg)
                return
            elif action == "scan_solana":
                await self.cmd_scan(msg)
                return
            elif action == "whale":
                await self.cmd_whale(msg)
                return
            elif action == "journal":
                await self.cmd_journal(msg)
                return
            elif action == "history":
                await self.cmd_history(msg)
                return
            elif action == "settings":
                await self.cmd_settings(msg)
                return
            elif action == "wallet":
                await self.cmd_wallet(msg)
                return
            elif action == "pause":
                await self.cmd_pause(msg)
                return
            elif action == "resume":
                await self.cmd_resume(msg)
                return
            elif key in ("help", "menu"):
                await msg.answer(HELP, reply_markup=MENU)
                return
        except Exception as exc:
            log.exception("error handling action %s", action)
            await msg.answer(f"⚠️ Gagal memproses perintah: {_esc(exc)}")
            return

        # Instant Token Address (CA) Detection
        is_sol = bool(re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", raw))
        is_evm = bool(re.match(r"^0x[a-fA-F0-9]{40}$", raw))

        if is_sol or is_evm:
            chain = "solana" if is_sol else "base"
            await msg.answer(f"🔍 <i>Menganalisis Contract Address ({chain.upper()})...</i>")
            txt, kb = await self._token_terminal_view(chain, raw)
            await msg.answer(txt, reply_markup=kb)
            return

        await msg.answer(HELP, reply_markup=MENU)
