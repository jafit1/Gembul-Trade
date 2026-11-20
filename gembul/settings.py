"""Central configuration. Everything the bot needs comes from env or YAML."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(os.environ.get("GEMBUL_ROOT", "/srv/gembul"))
APP = ROOT / "gembul"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
CONF = ROOT / "config"

for _p in (DATA, LOGS, CONF):
    _p.mkdir(parents=True, exist_ok=True)


class RiskLimits(BaseModel):
    risk_per_trade_pct: float = 4.0
    max_position_pct: float = 12.0
    max_total_exposure_pct: float = 75.0
    max_concurrent_positions: int = 7
    daily_loss_limit_pct: float = 15.0
    weekly_loss_limit_pct: float = 30.0
    max_correlated_positions: int = 4
    stop_loss_pct: float = 4.0
    take_profit_pct: float = 20.0
    trailing_activation_pct: float = 6.0
    trailing_distance_pct: float = 2.5
    max_slippage_bps: int = 300
    min_liquidity_usd: float = 25_000.0
    kill_switch_cooldown_hours: int = 4


class MemeLimits(BaseModel):
    enabled: bool = True
    max_position_usd: float = 150.0
    allow_chains: list[str] = Field(default_factory=lambda: ["solana", "base", "bsc"])
    max_token_age_minutes: int = 720
    min_holders: int = 30
    max_top10_holder_pct: float = 65.0
    max_buy_tax_pct: float = 8.0
    max_sell_tax_pct: float = 8.0
    require_lp_locked: bool = True
    require_no_mint_authority: bool = True
    require_honeypot_pass: bool = True
    cooldown_minutes_per_token: int = 45


class CexLimits(BaseModel):
    enabled: bool = True
    markets: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    max_leverage: float = 2.0
    use_freqtrade: bool = True


class TelegramCfg(BaseModel):
    allowed_user_ids: list[int] = Field(default_factory=list)
    poll_interval: float = 1.0

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: Any) -> Any:
        if isinstance(v, str):
            body = v.strip().strip("[]")
            return [int(p) for p in re.split(r"[,\s]+", body) if p.strip()]
        return v


DEFAULT_MODELS: list[str] = [
    "deepseek-v4.1-flash:free",
    "deepseek-v4-flash:free",
    "mimo-v2.6-flash:free",
    "mimo-v2.5:free",
]


class LlmCfg(BaseModel):
    base_url: str = "https://tokenharbor.ai/v1"
    model: str = "deepseek-v4.1-flash:free"
    models: list[str] = Field(default_factory=lambda: list(DEFAULT_MODELS))
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 1200
    enabled: bool = True
    timeout: float = 90.0
    cooldown_sec: int = 600
    max_cooldown_sec: int = 2400

    @field_validator("models", mode="before")
    @classmethod
    def _split_models(cls, v: Any) -> Any:
        if isinstance(v, str):
            body = v.strip().strip("[]")
            return [p for p in re.split(r"[,\s]+", body) if p.strip()]
        return v


class VenueCfg(BaseModel):
    freqtrade_url: str = "http://127.0.0.1:8080"
    freqtrade_user: str = "freqtrader"
    freqtrade_password: str = ""
    solana_rpc: str = "https://api.mainnet-beta.solana.com"
    helius_api_key: str = ""
    jupiter_api: str = "https://lite-api.jup.ag/swap/v1"
    zerox_api: str = "https://api.0x.org"
    zerox_key: str = ""
    odos_api: str = "https://api.odos.xyz"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEMBUL_", env_file=".env", extra="ignore")

    mode: Literal["paper", "live"] = "paper"
    real_trading_enabled: bool = False
    base_currency: str = "USDT"
    paper_equity_usd: float = 1000.0

    telegram_token: str = ""
    telegram_chat_id: int = 0
    allowed_user_ids: list[int] = Field(default_factory=list)

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: Any) -> Any:
        if isinstance(v, str):
            body = v.strip().strip("[]")
            return [int(p) for p in re.split(r"[,\s]+", body) if p.strip()]
        return v

    db_path: Path = DATA / "gembul.sqlite"
    log_level: str = "INFO"
    public_url: str = "https://gembul-agent.boxd.sh"

    risk: RiskLimits = Field(default_factory=RiskLimits)
    meme: MemeLimits = Field(default_factory=MemeLimits)
    cex: CexLimits = Field(default_factory=CexLimits)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    venues: VenueCfg = Field(default_factory=VenueCfg)

    cex_api_key: str = ""
    cex_secret: str = ""
    cex_password: str = ""
    cex_exchange: str = "binance"

    evm_private_key: str = ""
    solana_private_key: str = ""

    loop_interval_sec: int = 30

    @property
    def live(self) -> bool:
        return self.mode == "live" and self.real_trading_enabled

    @classmethod
    def load(cls) -> "Settings":
        """YAML holds the tuning. Env holds identity and secrets, and env wins.

        Without this, a redeploy of config.yaml silently wipes the telegram
        token and every API key back to empty strings.
        """
        _load_dotenv(CONF / ".env")

        raw: dict[str, Any] = {}
        cfg_file = CONF / "config.yaml"
        if cfg_file.exists():
            raw = yaml.safe_load(cfg_file.read_text()) or {}

        merged: dict[str, Any] = dict(raw)

        for field_name, var in _ENV_TOP.items():
            value = os.environ.get(var)
            if value not in (None, ""):
                merged[field_name] = value

        llm_env = {k: os.environ[v] for k, v in _ENV_LLM.items()
                   if os.environ.get(v) not in (None, "")}
        if llm_env:
            merged["llm"] = {**(raw.get("llm") or {}), **llm_env}

        venue_env = {k: os.environ[v] for k, v in _ENV_VENUES.items()
                     if os.environ.get(v) not in (None, "")}
        if venue_env:
            merged["venues"] = {**(raw.get("venues") or {}), **venue_env}

        tg_env = {k: os.environ[v] for k, v in _ENV_TELEGRAM.items()
                  if os.environ.get(v) not in (None, "")}
        if tg_env:
            merged["telegram"] = {**(raw.get("telegram") or {}), **tg_env}

        return cls.model_validate(merged)


_ENV_TOP = {
    "mode": "GEMBUL_MODE",
    "real_trading_enabled": "GEMBUL_REAL_TRADING_ENABLED",
    "paper_equity_usd": "GEMBUL_PAPER_EQUITY_USD",
    "telegram_token": "GEMBUL_TELEGRAM_TOKEN",
    "telegram_chat_id": "GEMBUL_TELEGRAM_CHAT_ID",
    "allowed_user_ids": "GEMBUL_ALLOWED_USER_IDS",
    "public_url": "GEMBUL_PUBLIC_URL",
    "loop_interval_sec": "GEMBUL_LOOP_INTERVAL_SEC",
    "log_level": "GEMBUL_LOG_LEVEL",
    "cex_api_key": "GEMBUL_CEX_API_KEY",
    "cex_secret": "GEMBUL_CEX_SECRET",
    "cex_password": "GEMBUL_CEX_PASSWORD",
    "cex_exchange": "GEMBUL_CEX_EXCHANGE",
    "evm_private_key": "GEMBUL_EVM_PRIVATE_KEY",
    "solana_private_key": "GEMBUL_SOLANA_PRIVATE_KEY",
}

_ENV_LLM = {
    "api_key": "GEMBUL_LLM_API_KEY",
    "base_url": "GEMBUL_LLM_BASE_URL",
    "models": "GEMBUL_LLM_MODELS",
    "model": "GEMBUL_LLM_MODEL",
    "enabled": "GEMBUL_LLM_ENABLED",
    "temperature": "GEMBUL_LLM_TEMPERATURE",
    "max_tokens": "GEMBUL_LLM_MAX_TOKENS",
}

_ENV_VENUES = {
    "freqtrade_password": "GEMBUL_FREQTRADE_PASSWORD",
    "freqtrade_url": "GEMBUL_FREQTRADE_URL",
    "solana_rpc": "GEMBUL_SOLANA_RPC",
    "helius_api_key": "GEMBUL_HELIUS_API_KEY",
}

_ENV_TELEGRAM = {
    "allowed_user_ids": "GEMBUL_ALLOWED_USER_IDS",
    "poll_interval": "GEMBUL_TELEGRAM_POLL_INTERVAL",
}


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. Real environment variables always win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.load()
