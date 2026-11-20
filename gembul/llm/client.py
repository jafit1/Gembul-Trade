"""OpenAI-compatible LLM client with automatic rotation across free tiers.

OpenRouter's free pool rate-limits per model, per provider, and per account, and
which model is healthy changes hour to hour. So the client keeps an ordered
roster and treats every model as a slot that can be iced for a while:

- a slot that answers gets promoted to the front and stays there (sticky),
- a slot that hits 402/429/5xx or returns empty content is frozen with a
  cooldown that grows with repeated failure,
- the next request starts from the front of the roster again, so recovery is
  automatic once a cooldown expires.

State lives in data/llm_state.json so a restart does not forget which model was
throttling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from gembul.settings import DATA, LlmCfg

log = logging.getLogger(__name__)

STATE_FILE = DATA / "llm_state.json"

# Statuses that mean "this model is unavailable right now", not "the request
# is malformed".
_COOL_STATUS = {401, 402, 403, 404, 429, 500, 502, 503, 504}


class _Rotate(Exception):
    """Raised internally when a slot should be skipped rather than retried."""


@dataclass
class ModelSlot:
    name: str
    cooldown_until: float = 0.0
    calls: int = 0
    wins: int = 0
    fails: int = 0
    last_error: str = ""
    last_latency: float = 0.0
    json_unsupported: bool = False

    @property
    def frozen_for(self) -> float:
        return max(0.0, self.cooldown_until - time.time())

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["frozen_for"] = round(self.frozen_for, 1)
        return d


class LlmClient:
    def __init__(self, cfg: LlmCfg) -> None:
        self.cfg = cfg
        roster = list(cfg.models or [])
        if cfg.model:
            roster.insert(0, cfg.model)
        seen: set[str] = set()
        self.slots: list[ModelSlot] = []
        for name in roster:
            name = (name or "").strip()
            if name and name not in seen:
                seen.add(name)
                self.slots.append(ModelSlot(name))
        self._http = httpx.AsyncClient(timeout=float(cfg.timeout))
        self._lock = asyncio.Lock()
        self._load_state()

    # ------------------------------------------------------------------ state

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = json.loads(STATE_FILE.read_text())
        except Exception:
            return
        saved = raw.get("slots") or {}
        for slot in self.slots:
            data = saved.get(slot.name)
            if not data:
                continue
            for key in ("cooldown_until", "calls", "wins", "fails",
                        "last_error", "last_latency", "json_unsupported"):
                if key in data:
                    setattr(slot, key, data[key])

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "saved_at": time.time(),
                "order": [s.name for s in self.slots],
                "slots": {s.name: asdict(s) for s in self.slots},
            }, indent=1))
            tmp.replace(STATE_FILE)
        except Exception as exc:  # never let bookkeeping kill a trade decision
            log.debug("gagal simpan state llm: %s", exc)

    # ------------------------------------------------------------------- api

    @property
    def ready(self) -> bool:
        return self.cfg.enabled and bool(self.cfg.api_key) and bool(self.slots)

    @property
    def rotation(self) -> list[str]:
        return [s.name for s in self.slots]

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "base_url": self.cfg.base_url,
            "active": self.rotation[0] if self.slots else None,
            "slots": [s.as_dict() for s in self.slots],
        }

    async def close(self) -> None:
        self._save_state()
        await self._http.aclose()

    # ------------------------------------------------------------- rotation

    def _candidates(self) -> list[ModelSlot]:
        live = [s for s in self.slots if not s.frozen_for]
        if live:
            return live
        # everything is iced: try the one that recovers soonest instead of
        # failing outright
        return sorted(self.slots, key=lambda s: s.cooldown_until)[:1]

    def _promote(self, slot: ModelSlot) -> None:
        if self.slots and self.slots[0] is slot:
            return
        self.slots.remove(slot)
        self.slots.insert(0, slot)

    def _freeze(self, slot: ModelSlot, reason: str) -> None:
        slot.fails += 1
        slot.last_error = reason[:180]
        base = float(self.cfg.cooldown_sec)
        ceiling = float(self.cfg.max_cooldown_sec)
        wait = min(ceiling, base * (1 + min(slot.fails - 1, 3)))
        slot.cooldown_until = time.time() + wait
        log.warning("llm %s dibekukan %.0f detik (%s)", slot.name, wait, slot.last_error)
        self._save_state()

    async def _call(self, slot: ModelSlot, system: str, user: str,
                    json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": slot.name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if json_mode and not slot.json_unsupported:
            payload["response_format"] = {"type": "json_object"}

        started = time.time()
        try:
            resp = await self._http.post(
                f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.cfg.api_key}",
                    "HTTP-Referer": "https://gembul-agent.boxd.sh",
                    "X-Title": "gembul",
                },
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise _Rotate(f"jaringan: {type(exc).__name__}") from exc

        slot.last_latency = round(time.time() - started, 2)

        if resp.status_code in _COOL_STATUS:
            detail = resp.text[:160].replace("\n", " ")
            if resp.status_code == 400 and json_mode and "response_format" in detail:
                # this model refuses JSON mode; drop it and retry without
                slot.json_unsupported = True
                return await self._call(slot, system, user, json_mode=False)
            raise _Rotate(f"HTTP {resp.status_code} {detail}")

        if resp.status_code >= 400:
            detail = resp.text[:160].replace("\n", " ")
            raise _Rotate(f"HTTP {resp.status_code} {detail}")

        data = resp.json() if resp.content else {}
        if "error" in data:
            err = data["error"] or {}
            raise _Rotate(f"api {err.get('code')} {str(err.get('message'))[:120]}")

        choices = data.get("choices") or []
        if not choices:
            raise _Rotate("respons kosong")
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()
        if not text:
            finish = choices[0].get("finish_reason")
            raise _Rotate(f"konten kosong (finish={finish})")
        return text

    async def chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if not self.ready:
            raise RuntimeError("LLM belum dikonfigurasi (api_key atau daftar model kosong)")

        async with self._lock:
            errors: list[str] = []
            for slot in self._candidates():
                slot.calls += 1
                try:
                    text = await self._call(slot, system, user, json_mode)
                except _Rotate as exc:
                    self._freeze(slot, str(exc))
                    errors.append(f"{slot.name}: {exc}")
                    continue
                except Exception as exc:
                    slot.fails += 1
                    slot.last_error = f"{type(exc).__name__}: {exc}"[:180]
                    errors.append(f"{slot.name}: {slot.last_error}")
                    continue

                slot.wins += 1
                slot.last_error = ""
                slot.cooldown_until = 0.0
                self._promote(slot)
                self._save_state()
                log.info("llm jawab via %s (%.2fs)", slot.name, slot.last_latency)
                return text

            raise RuntimeError("semua model gagal: " + " | ".join(errors[:5]))

    async def chat_json(self, system: str, user: str) -> dict[str, Any]:
        raw = await self.chat(system, user, json_mode=True)
        raw = raw.strip()
        if raw.startswith("```"):
            body = raw.split("```")
            raw = body[1] if len(body) > 1 else raw
            raw = raw.removeprefix("json").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("LLM mengembalikan JSON tidak valid: %s", raw[:200])
            return {}
