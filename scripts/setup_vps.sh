#!/usr/bin/env bash
set -euo pipefail

# Gembul-Trade — One-shot VPS bootstrap (Ubuntu/Debian, Python 3.12)
APP_DIR="${1:-/srv/gembul}"

echo "==> Setting up Gembul-Trade in ${APP_DIR}"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs" "${APP_DIR}/ftuser/logs"

cd "${APP_DIR}"

if [ ! -d ".venv" ]; then
  echo "==> Creating Python 3.12 virtualenv..."
  python3 -m venv .venv
fi

echo "==> Installing Python dependencies..."
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt

if [ ! -f "config/.env" ]; then
  echo "==> Creating config/.env from template (EDIT THIS BEFORE STARTING!)"
  cp config/.env.example config/.env
  chmod 600 config/.env
fi

if [ ! -f "freqtrade_strategy/config.json" ]; then
  echo "==> Creating freqtrade_strategy/config.json from Futures template (swap with config.example.json for Spot)"
  cp freqtrade_strategy/config.futures.example.json freqtrade_strategy/config.json
  chmod 600 freqtrade_strategy/config.json
fi

echo "==> Seeding bot memory (lessons, tracked whales, journals)..."
PYTHONPATH="${APP_DIR}" .venv/bin/python scripts/seed_memory.py --db data/gembul.sqlite --memory-dir memory

echo ""
echo "Done! Next steps:"
echo "  1. Edit ${APP_DIR}/config/.env with your Telegram/LLM/RPC/Exchange keys"
echo "  2. Edit ${APP_DIR}/freqtrade_strategy/config.json (set jwt_secret_key & api_server password)"
echo "  3. Test Gembul:    .venv/bin/python -m gembul run"
echo "  4. Test Freqtrade: .venv/bin/freqtrade trade --config freqtrade_strategy/config.json --strategy GembulFutures --strategy-path freqtrade_strategy --userdir ftuser"
echo "  5. Install systemd units from deploy/ (adjust User= and paths if needed)"
