#!/usr/bin/env bash
# Run tools + gateway locally without docker.
cd "$(dirname "$0")/.." || exit 1
if [ ! -f .venv/bin/activate ]; then
  echo "ERROR: .venv missing. Create it with: python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
source .venv/bin/activate
mkdir -p data results
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
# LLM_MODE_OVERRIDE (e.g. nvidia) wins over LLM_MODE from .env
export LLM_MODE="${LLM_MODE_OVERRIDE:-${LLM_MODE:-mock}}"
export GATEWAY_URL=http://localhost:8000
export TOOLS_URL=http://localhost:8001
export GATEWAY_DB_PATH=data/gateway.db
export TOOLS_DB_PATH=data/tools.db
export GATEWAY_KEY_PATH=data/gateway_ed25519.pem
export PYTHONPATH="$PWD"
trap 'kill 0' EXIT
uvicorn tools.mock_tools:default_app --factory --port 8001 --workers 1 &
uvicorn gateway.app:create_app --factory --port 8000 --workers 1 &
for url in http://localhost:8001/health http://localhost:8000/health; do
  for _ in $(seq 1 50); do
    curl -sf "$url" >/dev/null && break
    sleep 0.2
  done
done
echo "WardAIn local stack ready"
wait
