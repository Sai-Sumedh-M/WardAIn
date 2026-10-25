cd ~/github_projects/WardAIn
source .venv/bin/activate
set -a; source .env; set +a
export GATEWAY_URL=http://localhost:8000 TOOLS_URL=http://localhost:8001 LLM_MODE=${LLM_MODE_OVERRIDE:-mock}
echo "Local env ready -> $GATEWAY_URL"
