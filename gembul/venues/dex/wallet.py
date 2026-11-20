"""Signing.

Two modes, chosen per chain:

1. local   - private key from env, signed in-process. Fastest, fully automated.
2. browser - a wallet page served on the VM's HTTPS domain; the user connects
             MetaMask/Phantom and signs there. The bot builds the tx, the wallet
             signs it, the bot broadcasts it.

Both paths end with the same object: a signed raw transaction.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class SigningError(RuntimeError):
    pass


# --------------------------------------------------------------------- EVM


class EvmSigner:
    """Local EVM signer. Requires eth-account + web3."""

    def __init__(self, private_key: str, rpc_url: str) -> None:
        if not private_key:
            raise SigningError("EVM private key kosong")
        from eth_account import Account
        from web3 import Web3

        self.key = private_key if private_key.startswith("0x") else f"0x{private_key}"
        self.acct = Account.from_key(self.key)
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self.address: str = self.acct.address

    def sign_evm_tx(self, tx: dict[str, Any]) -> tuple[str, str]:
        tx = dict(tx)
        tx.setdefault("nonce", self.w3.eth.get_transaction_count(self.address))
        tx.setdefault("chainId", self.w3.eth.chain_id)
        if "gas" not in tx:
            tx["gas"] = int(self.w3.eth.estimate_gas({**tx, "from": self.address}) * 1.25)
        if "gasPrice" not in tx and "maxFeePerGas" not in tx:
            tx["gasPrice"] = int(self.w3.eth.gas_price * 1.2)
        signed = self.acct.sign_transaction(tx)
        raw = signed.raw_transaction.hex()
        if not raw.startswith("0x"):
            raw = "0x" + raw
        h = self.w3.eth.send_raw_transaction(bytes.fromhex(raw[2:]))
        return h.hex(), raw

    async def wait_receipt(self, tx_hash: str, timeout: int = 120) -> dict:
        import asyncio

        deadline = timeout
        while deadline > 0:
            try:
                r = self.w3.eth.get_transaction_receipt(tx_hash)
                if r and r.get("status") is not None:
                    return dict(r)
            except Exception:
                pass
            await asyncio.sleep(2)
            deadline -= 2
        raise SigningError(f"receipt timeout untuk {tx_hash}")

    def balance_native(self) -> float:
        return float(self.w3.from_wei(self.w3.eth.get_balance(self.address), "ether"))


# ------------------------------------------------------------------ Solana


class SolanaSigner:
    """Local Solana signer. Requires solders."""

    def __init__(self, private_key: str, rpc_url: str) -> None:
        if not private_key:
            raise SigningError("Solana private key kosong")
        from solders.keypair import Keypair

        try:
            raw = base64.b64decode(private_key)
            self.kp = Keypair.from_bytes(raw)
        except Exception:
            try:
                import base58
                self.kp = Keypair.from_bytes(base58.b58decode(private_key))
            except Exception as exc:
                raise SigningError(f"format private key solana tidak dikenali: {exc}") from exc

        self.rpc = rpc_url
        self.address = str(self.kp.pubkey())
        self._http = httpx.AsyncClient(timeout=60.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def sign_and_send(self, tx_base64: str) -> str:
        import base64 as b64

        from solders.transaction import VersionedTransaction

        raw = b64.b64decode(tx_base64)
        tx = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [self.kp])
        encoded = b64.b64encode(bytes(signed)).decode()

        resp = await self._http.post(self.rpc, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [encoded, {
                "encoding": "base64",
                "skipPreflight": False,
                "maxRetries": 3,
                "preflightCommitment": "confirmed",
            }],
        })
        data = resp.json()
        if "error" in data:
            raise SigningError(f"solana send gagal: {data['error']}")
        return data["result"]

    async def balance_native(self) -> float:
        resp = await self._http.post(self.rpc, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance", "params": [self.address],
        })
        lamports = resp.json().get("result", {}).get("value", 0)
        return lamports / 1e9

    async def confirm(self, signature: str, timeout: int = 90) -> bool:
        import asyncio

        for _ in range(timeout // 2):
            resp = await self._http.post(self.rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignatureStatuses",
                "params": [[signature], {"searchTransactionHistory": False}],
            })
            val = resp.json().get("result", {}).get("value", [None])[0]
            if val and val.get("confirmationStatus") in ("confirmed", "finalized"):
                return val.get("err") is None
            await asyncio.sleep(2)
        return False


# ------------------------------------------------------- browser wallet relay


@dataclass(slots=True)
class PendingSignature:
    id: str
    chain: str
    kind: str            # "evm_tx" | "solana_tx"
    payload: dict[str, Any]
    created_at: float


class BrowserWalletRelay:
    """Holds unsigned txs until a browser wallet signs them.

    The HTTP layer (gembul.wallet_ui) serves the pairing page and the poll
    endpoint. This object is only the queue + JSON blobs.
    """

    def __init__(self) -> None:
        self.pending: dict[str, PendingSignature] = {}

    def create(self, pid: str, chain: str, kind: str, payload: dict) -> PendingSignature:
        import time

        req = PendingSignature(pid, chain, kind, payload, time.time())
        self.pending[pid] = req
        return req

    def submit(self, pid: str, signed: str) -> None:
        req = self.pending.get(pid)
        if not req:
            raise SigningError("permintaan tanda tangan tidak ditemukan")
        req.payload["signed"] = signed

    def get_signed(self, pid: str) -> str | None:
        req = self.pending.get(pid)
        if not req:
            return None
        return req.payload.get("signed")

    def clear(self, pid: str) -> None:
        self.pending.pop(pid, None)

    def dump(self, pid: str) -> dict | None:
        req = self.pending.get(pid)
        if not req:
            return None
        return {
            "id": req.id,
            "chain": req.chain,
            "kind": req.kind,
            "tx": {k: v for k, v in req.payload.items() if k != "signed"},
        }


WALLET_PAGE = """<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gembul Wallet</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0d10;color:#e8eaed;
      font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
 .card{width:min(440px,92vw);background:#14181d;border:1px solid #232a33;border-radius:16px;padding:24px}
 h1{font-size:17px;margin:0 0 4px;letter-spacing:-.01em}
 p.sub{margin:0 0 20px;color:#8b95a5;font-size:13px}
 button{width:100%;padding:12px;border-radius:10px;border:0;cursor:pointer;
        font:inherit;font-weight:600;background:#3b82f6;color:#fff}
 button:disabled{opacity:.45;cursor:default}
 button.ghost{background:#1e242c;color:#cfd6e0;margin-top:8px}
 .row{font-family:ui-monospace,monospace;font-size:12px;color:#8b95a5;
      background:#0e1116;border:1px solid #1f262f;border-radius:8px;padding:10px;
      margin-top:14px;word-break:break-all;max-height:170px;overflow:auto}
 .ok{color:#4ade80}.err{color:#f87171}
</style></head><body>
<div class="card">
  <h1>Gembul Wallet</h1>
  <p class="sub" id="sub">Menyambungkan...</p>
  <button id="connect">Hubungkan Wallet</button>
  <button id="link" class="ghost" disabled>Simpan wallet ke bot</button>
  <button id="sign" class="ghost" disabled>Tanda tangani transaksi</button>
  <div class="row" id="log">siap</div>
</div>
<script>
const pid = new URLSearchParams(location.search).get('id') || '';
const log = (m, c) => { const e = document.getElementById('log'); e.textContent = m; e.className = 'row ' + (c||''); };
let provider = null, account = null, kind = null;

const b64 = (bytes) => btoa(String.fromCharCode.apply(null, new Uint8Array(bytes)));

function pick() {
  if (window.phantom?.solana) return {p: window.phantom.solana, type: 'svm'};
  if (window.solana) return {p: window.solana, type: 'svm'};
  if (window.ethereum) return {p: window.ethereum, type: 'evm'};
  return null;
}

document.getElementById('connect').onclick = async () => {
  const found = pick();
  if (!found) { log('Wallet tidak terdeteksi. Buka halaman ini di dalam browser MetaMask/Phantom.', 'err'); return; }
  provider = found.p; kind = found.type;
  try {
    if (kind === 'svm') {
      const r = await provider.connect();
      account = r.publicKey.toString();
    } else {
      const accs = await provider.request({method:'eth_requestAccounts'});
      account = accs[0];
    }
    log('tersambung: ' + account, 'ok');
    document.getElementById('sign').disabled = false;
    document.getElementById('link').disabled = false;
    document.getElementById('connect').disabled = true;
  } catch (e) { log('gagal konek: ' + e.message, 'err'); }
};

document.getElementById('link').onclick = async () => {
  try {
    const r = await fetch('/wallet/challenge?id=' + encodeURIComponent(pid));
    const {message} = await r.json();
    let signature;
    if (kind === 'svm') {
      const enc = new TextEncoder().encode(message);
      const res = await provider.signMessage(enc, 'utf8');
      signature = b64(res.signature);
    } else {
      signature = await provider.request({method:'personal_sign', params:[message, account]});
    }
    const resp = await fetch('/wallet/link', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({id: pid, address: account, message, signature})
    });
    const out = await resp.json();
    if (out.ok) log('wallet tersimpan: ' + out.address, 'ok');
    else log('gagal verifikasi: ' + (out.detail || ''), 'err');
  } catch (e) { log('gagal: ' + (e.message||e), 'err'); }
};

document.getElementById('sign').onclick = async () => {
  if (!provider || !account) return;
  try {
    const r = await fetch('/wallet/tx?id=' + encodeURIComponent(pid));
    const req = (await r.json()).tx;
    let signed;
    if (req.kind === 'evm_tx') {
      signed = await provider.request({method:'eth_sendTransaction', params:[{
        from: account, to: req.tx.to, value: req.tx.value, data: req.tx.data,
      }]});
    } else {
      const tx = req.tx_base64;
      const buf = Uint8Array.from(atob(tx), c => c.charCodeAt(0));
      const res = await provider.signAndSendTransaction({message: buf, requireAllSignatures:false});
      signed = res.signature;
    }
    await fetch('/wallet/submit', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({id: pid, signed})
    });
    log('terkirim: ' + signed, 'ok');
  } catch (e) { log('gagal tanda tangan: ' + (e.message||e), 'err'); }
};
</script></body></html>
"""
