#!/usr/bin/env bash
# One-shot launcher: installs Python deps (first run), starts the Claude bridge, runs the game.
#   ./start_llm_game.sh              GUI game
#   ./start_llm_game.sh --nogui      headless
#   ./start_llm_game.sh --nogui --max-turns=10
set -euo pipefail
cd "$(dirname "$0")"

# --- 1. API key ---------------------------------------------------------------
if [ -f llm_bridge/.env ]; then set -a; . llm_bridge/.env; set +a; fi
if [ -z "${ANTHROPIC_API_KEY:-}" ] || [[ "$ANTHROPIC_API_KEY" == *"..."* ]]; then
  echo "ERROR: put your real key in llm_bridge/.env  (line: ANTHROPIC_API_KEY=sk-ant-...)"; exit 1
fi

# --- 2. Python deps (installed once into llm_bridge/.venv) ---------------------
if [ ! -x llm_bridge/.venv/bin/python ]; then
  echo "Creating virtualenv and installing fastapi/uvicorn/anthropic ..."
  python3 -m venv llm_bridge/.venv
  llm_bridge/.venv/bin/pip install -q -r llm_bridge/requirements.txt
fi

# --- 3. Bridge (always restarted so code/env changes are picked up) -----------
pkill -f "uvicorn bridge:app" 2>/dev/null || true
sleep 0.5
echo "Starting bridge on http://127.0.0.1:8000 (log: llm_bridge/bridge.log) ..."
( cd llm_bridge && nohup .venv/bin/uvicorn bridge:app --host 127.0.0.1 --port 8000 > bridge.log 2>&1 & )
for i in $(seq 1 40); do curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && break; sleep 0.5; done
if ! curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "ERROR: bridge did not start. Last lines of llm_bridge/bridge.log:"; tail -20 llm_bridge/bridge.log; exit 1
fi
curl -s http://127.0.0.1:8000/health; echo
echo "Tip: in another terminal run   tail -f ~/Documents/Tribes/llm_bridge/bridge.log   to watch Claude's thinking."

# --- 4. Game ------------------------------------------------------------------
exec ./run_llm_match.sh "$@"
