#!/usr/bin/env bash
# Jade local preview: the real backend and the frontend on this machine,
# with the BicycleWorks demonstration data. JDE is SIMULATED throughout --
# nothing here connects to any JD Edwards system.
#
#   scripts/run_local_preview.sh           # start (first run seeds the demo data)
#   scripts/run_local_preview.sh --reset   # throw the preview data away and start fresh
#
# Data and throwaway demo passwords live in .preview-data/ (git-ignored),
# so everything survives a restart of this script. Stop with Ctrl-C.
set -euo pipefail
BACKEND=$(cd "$(dirname "$0")/.." && pwd)
FRONTEND=${JADE_FRONTEND_DIR:-$BACKEND/../JDE_change_factory_frontend}
DATA=${JADE_PREVIEW_DATA:-$BACKEND/.preview-data}
API_PORT=${JADE_API_PORT:-8000}
UI_PORT=${JADE_UI_PORT:-5173}

[ "${1:-}" = "--reset" ] && rm -rf "$DATA"
[ -d "$FRONTEND/src" ] || { echo "Frontend not found at $FRONTEND (set JADE_FRONTEND_DIR)"; exit 1; }
python3 -c "import fastapi, uvicorn, openpyxl, cryptography, bcrypt, claude_agent_sdk, jde_mcp_server" 2>/dev/null || {
  echo "Install the backend first:  pip install -e \"$BACKEND/api_service[test]\" -e \"$BACKEND/mcp_server\""; exit 1; }
[ -d "$FRONTEND/node_modules" ] || (cd "$FRONTEND" && npm ci)

mkdir -p "$DATA" && chmod 700 "$DATA"
CRED="$DATA/credentials.env"
if [ ! -f "$CRED" ]; then
  (umask 077; python3 - > "$CRED" <<'PY'
import secrets
from cryptography.fernet import Fernet
for k in ("ADMIN_PW", "CNC_PW", "DO_PW"):
    print(f"{k}={secrets.token_urlsafe(12)}")
print(f"CREDENTIAL_KEY={Fernet.generate_key().decode()}")
PY
  )
fi
# shellcheck disable=SC1090
source "$CRED"

export JDE_API_DATA_DIR="$DATA/api" JDE_BACKLOG_DIR="$DATA/backlog" JDE_CHANGE_DIR="$DATA/changes" JDE_EVIDENCE_DIR="$DATA/evidence"
export JDE_CREDENTIAL_KEY="$CREDENTIAL_KEY" JDE_API_ALLOWED_ORIGINS="http://localhost:$UI_PORT" JDE_COOKIE_SECURE=false
export JDE_BOOTSTRAP_ADMIN_EMAIL=admin@e2e.local JDE_BOOTSTRAP_ADMIN_NAME="E2E Admin" JDE_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PW"
export JADE_E2E_CNC_PASSWORD="$CNC_PW" JADE_E2E_DO_PASSWORD="$DO_PW"

(cd "$BACKEND" && exec uvicorn jde_api_service.main:app --app-dir api_service --port "$API_PORT" > "$DATA/backend.log" 2>&1) &
BPID=$!
(cd "$FRONTEND" && VITE_USE_MOCK_API=false VITE_API_BASE_URL="http://localhost:$API_PORT" exec npx vite --port "$UI_PORT" --strictPort > "$DATA/frontend.log" 2>&1) &
FPID=$!
trap 'kill $BPID $FPID 2>/dev/null; wait 2>/dev/null; echo "Jade preview stopped."' EXIT INT TERM

for _ in $(seq 1 90); do
  curl -sf "http://localhost:$API_PORT/health" >/dev/null && curl -sf "http://localhost:$UI_PORT" >/dev/null && break
  sleep 1
done
curl -sf "http://localhost:$API_PORT/health" >/dev/null || { echo "Backend did not start; see $DATA/backend.log"; exit 1; }

if [ ! -f "$DATA/seeded" ]; then
  echo "Seeding the BicycleWorks demonstration data (scripted stand-ins, SIMULATED JDE)..."
  (cd "$BACKEND" && python3 scripts/seed_demo_technical.py bwm && python3 scripts/seed_demo_process.py) > "$DATA/seed.log" 2>&1 \
    || { echo "Seeding failed; see $DATA/seed.log"; exit 1; }
  touch "$DATA/seeded"
fi

cat <<INFO

  Jade local preview -- JDE is SIMULATED (no JD Edwards system is contacted)

  Open:      http://localhost:$UI_PORT
  Company:   BicycleWorks Manufacturing BV

  Sign in (throwaway, local-only demo users; also in $CRED):
    admin@e2e.local   $ADMIN_PW   admin, product manager (imports frameworks, reviews, finalises)
    do@e2e.local      $DO_PW   Domain Owner assigned to Customer Service (try a second browser)
    cnc@e2e.local     $CNC_PW   CNC operator only

  Start at Delivery > Process & Maps, story S-BW-RETURNS (then follow the journey bar).
  Framework fixtures to import as a new version: $BACKEND/fixtures/process_framework/
  Stop with Ctrl-C; run again to resume with the same data (--reset starts over).

INFO
wait $BPID
