"""HTTP surface: dashboard, JSON API, wallet pairing page."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from gembul.core.engine import Engine
from gembul.venues.dex.wallet import WALLET_PAGE, BrowserWalletRelay

log = logging.getLogger(__name__)

DASHBOARD = """<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gembul</title>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;background:#0a0c10;color:#e6e9ef;
      font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
 .wrap{max-width:1080px;margin:0 auto;padding:32px 20px 64px}
 header{display:flex;align-items:baseline;gap:12px;margin-bottom:24px}
 h1{font-size:20px;margin:0;letter-spacing:-.02em;font-weight:650}
 .tag{font-size:11px;padding:3px 8px;border-radius:999px;background:#1a222c;color:#93a1b5;
      border:1px solid #232e3b;font-weight:600;letter-spacing:.04em;text-transform:uppercase}
 .tag.live{background:#3b1219;color:#ff8b9a;border-color:#5c1f2a}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:26px}
 .card{background:#11161d;border:1px solid #1e2833;border-radius:12px;padding:16px}
 .card .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#7d8a9c;margin-bottom:6px}
 .card .v{font-size:22px;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
 .pos{color:#4ade80}.neg{color:#f87171}
 table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
 th{text-align:left;font-weight:600;color:#7d8a9c;font-size:11px;text-transform:uppercase;
    letter-spacing:.05em;padding:8px 10px;border-bottom:1px solid #1e2833}
 td{padding:9px 10px;border-bottom:1px solid #161d25;font-variant-numeric:tabular-nums}
 .mono{font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;color:#9fb0c4}
 h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#8b98ab;
    margin:30px 0 6px;font-weight:650}
 .empty{color:#63707f;font-size:13px;padding:14px 0}
 .bar{height:3px;background:#161d25;border-radius:2px;overflow:hidden;margin-top:10px}
 .bar>i{display:block;height:100%;background:#3b82f6}
</style></head><body>
<div class="wrap">
  <header>
    <h1>gembul</h1>
    <span class="tag" id="mode">-</span>
    <span class="tag" id="halt" style="display:none">halt</span>
  </header>

  <div class="grid">
    <div class="card"><div class="k">ekuitas</div><div class="v" id="equity">-</div></div>
    <div class="card"><div class="k">posisi</div><div class="v" id="npos">-</div></div>
    <div class="card"><div class="k">winrate</div><div class="v" id="wr">-</div></div>
    <div class="card"><div class="k">avg pnl</div><div class="v" id="avg">-</div></div>
    <div class="card"><div class="k">pelajaran</div><div class="v" id="lsn">-</div></div>
  </div>

  <h2>posisi terbuka</h2>
  <table><thead><tr>
    <th>id</th><th>simbol</th><th>venue</th><th>entry</th><th>harga</th><th>upnl</th>
  </tr></thead><tbody id="posbody"></tbody></table>

  <h2>pelajaran terakhir</h2>
  <div id="lessons" class="mono"></div>
</div>
<script>
const f = (n,d=2) => n==null ? '-' : n.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
async function tick(){
  try{
    const s = await (await fetch('/api/status')).json();
    document.getElementById('mode').textContent = s.live ? 'live' : s.mode;
    document.getElementById('mode').className = 'tag' + (s.live?' live':'');
    document.getElementById('equity').textContent = '$' + f(s.equity_usd);
    document.getElementById('npos').textContent = s.positions.length;
    document.getElementById('wr').textContent = (s.stats.win_rate||0) + '%';
    const a = document.getElementById('avg');
    a.textContent = (s.stats.avg_pnl_pct>=0?'+':'') + f(s.stats.avg_pnl_pct) + '%';
    a.className = 'v ' + (s.stats.avg_pnl_pct>=0?'pos':'neg');
    document.getElementById('lsn').textContent = s.stats.lessons || 0;
    const h = document.getElementById('halt');
    h.style.display = s.halted ? '' : 'none';
    h.textContent = 'halt: ' + (s.halt_reason||'');

    const tb = document.getElementById('posbody');
    tb.innerHTML = s.positions.length ? s.positions.map(p => {
      const up = p.upnl_pct;
      return `<tr><td class="mono">#${p.id}</td><td>${p.symbol}</td>
      <td class="mono">${p.chain||p.venue}</td><td class="mono">${p.entry}</td>
      <td class="mono">${p.price==null?'-':p.price}</td>
      <td class="${up>=0?'pos':'neg'}">${up==null?'-':(up>=0?'+':'')+up+'%'}</td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty">tidak ada posisi</td></tr>';

    const ls = await (await fetch('/api/lessons')).json();
    document.getElementById('lessons').innerHTML = ls.lessons.length
      ? ls.lessons.map(l => `[${l.scope}] ${l.rule}`).join('<br>')
      : 'belum ada pelajaran';
  } catch(e){ console.error(e); }
}
tick(); setInterval(tick, 8000);
</script></body></html>"""


class WalletSubmit(BaseModel):
    id: str
    signed: str


class WalletLink(BaseModel):
    id: str
    address: str
    message: str
    signature: str


def _verify_evm(address: str, message: str, signature: str) -> bool:
    from eth_account import Account
    from eth_account.messages import encode_defunct

    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:
        return False
    return recovered.lower() == address.lower()


def _verify_svm(address: str, message: str, signature_b64: str) -> bool:
    import base64

    from solders.pubkey import Pubkey

    try:
        pubkey = Pubkey.from_string(address)
        sig = base64.b64decode(signature_b64)
        return pubkey.verify_message(message.encode(), sig)
    except Exception:
        return False


def build_app(engine: Engine, relay: BrowserWalletRelay) -> FastAPI:
    app = FastAPI(title="gembul", docs_url="/api/docs")

    async def load_req(req_id: str) -> dict | None:
        req = relay.dump(req_id)
        if req:
            return req
        return await engine.db.kv_get(f"wallet_req:{req_id}")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return DASHBOARD

    @app.get("/api/status")
    async def status() -> JSONResponse:
        return JSONResponse(await engine.snapshot())

    @app.get("/api/positions")
    async def positions() -> JSONResponse:
        s = await engine.snapshot()
        return JSONResponse({"positions": s["positions"]})

    @app.get("/api/lessons")
    async def lessons(limit: int = Query(20, ge=1, le=200)) -> JSONResponse:
        rows = await engine.db.fetchall(
            "SELECT scope,rule,weight,hits,wins FROM lessons "
            "ORDER BY weight DESC, updated_at DESC LIMIT ?", (limit,)
        )
        return JSONResponse({"lessons": [dict(r) for r in rows]})

    @app.get("/api/stats")
    async def stats() -> JSONResponse:
        return JSONResponse(await engine.memory.stats())

    @app.get("/api/equity")
    async def equity(limit: int = Query(500, ge=1, le=5000)) -> JSONResponse:
        rows = await engine.db.fetchall(
            "SELECT ts,equity_usd FROM equity ORDER BY id DESC LIMIT ?", (limit,)
        )
        return JSONResponse({"equity": [dict(r) for r in reversed(rows)]})

    @app.get("/api/signals")
    async def signals(limit: int = Query(50, ge=1, le=500)) -> JSONResponse:
        rows = await engine.db.fetchall(
            "SELECT strategy,symbol,chain,score,accepted,reject_reason,created_at "
            "FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        )
        return JSONResponse({"signals": [dict(r) for r in rows]})

    # ---------------------------------------------------------- wallet page

    @app.get("/wallet", response_class=HTMLResponse)
    async def wallet_page(id: str = Query(...)) -> str:
        if not await load_req(id):
            raise HTTPException(404, "permintaan tidak ditemukan")
        return WALLET_PAGE

    @app.get("/wallet/challenge")
    async def wallet_challenge(id: str = Query(...)) -> JSONResponse:
        """One-time message the wallet signs to prove it owns the address."""
        req = await load_req(id)
        if not req:
            raise HTTPException(404, "permintaan tidak ditemukan")
        message = (
            f"gembul akan menyimpan wallet ini.\n"
            f"chain: {req['chain']}\nnonce: {id}:{int(time.time())}\n"
            f"tidak ada transaksi dan tidak ada biaya."
        )
        await engine.db.kv_set(f"wallet_challenge:{id}", message)
        return JSONResponse({"message": message})

    @app.post("/wallet/link")
    async def wallet_link(body: WalletLink) -> JSONResponse:
        req = await load_req(body.id)
        if not req:
            raise HTTPException(404, "permintaan tidak ditemukan")
        expected = await engine.db.kv_get(f"wallet_challenge:{body.id}")
        if expected != body.message:
            raise HTTPException(400, "challenge tidak cocok")

        ok = (
            _verify_svm(body.address, body.message, body.signature)
            if req["chain"] == "solana"
            else _verify_evm(body.address, body.message, body.signature)
        )
        if not ok:
            return JSONResponse({"ok": False, "detail": "tanda tangan tidak valid"})

        wallets = await engine.db.kv_get("wallets", {}) or {}
        wallets[req["chain"]] = body.address
        await engine.db.kv_set("wallets", wallets)
        await engine.db.execute(
            "INSERT INTO episodes(kind,chain,context,action,outcome) "
            "VALUES('wallet_link',?,?,?,'linked')",
            (req["chain"], json.dumps({"address": body.address}), json.dumps({"ok": True})),
        )
        return JSONResponse({"ok": True, "address": body.address})

    @app.get("/api/wallets")
    async def api_wallets() -> JSONResponse:
        return JSONResponse({"wallets": await engine.db.kv_get("wallets", {}) or {}})

    @app.get("/api/llm")
    async def api_llm() -> JSONResponse:
        return JSONResponse(engine.llm.status())

    @app.get("/wallet/tx")
    async def wallet_tx(id: str = Query(...)) -> JSONResponse:
        req = await load_req(id)
        if not req:
            raise HTTPException(404, "permintaan tidak ditemukan")
        return JSONResponse({"tx": req})

    @app.post("/wallet/submit")
    async def wallet_submit(body: WalletSubmit) -> JSONResponse:
        relay.submit(body.id, body.signed)
        return JSONResponse({"ok": True})

    return app
