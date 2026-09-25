#!/usr/bin/env bash
# Jade local preview: the real backend and the frontend on this machine,
# with the BicycleWorks demonstration data. JDE is SIMULATED throughout --
# nothing here connects to any JD Edwards system.
#
#   scripts/run_local_preview.sh           # start; the first run installs and seeds
#   scripts/run_local_preview.sh --reset   # ONLY when asked: throws the preview data away (asks to confirm)
#
# Everything the preview saves (accounts, password hashes, encrypted
# credentials, setup, frameworks, mappings, maps, records) lives in the
# backend's database and data files under .preview-data/ (git-ignored), and
# is reused on every start. Throwaway demo passwords are generated once into
# .preview-data/credentials.env (readable only by you) and never printed.
set -euo pipefail
BACKEND=$(cd "$(dirname "$0")/.." && pwd)
FRONTEND=${JADE_FRONTEND_DIR:-$BACKEND/../JDE_change_factory_frontend}
DATA=${JADE_PREVIEW_DATA:-$BACKEND/.preview-data}
VENV=${JADE_PREVIEW_VENV:-$BACKEND/.venv}
API_PORT=${JADE_API_PORT:-8000}
UI_PORT=${JADE_UI_PORT:-5173}

if [ "${1:-}" = "--reset" ]; then
  read -r -p "Delete ALL preview data in $DATA (accounts, frameworks, maps, records)? Type yes: " answer
  if [ "$answer" = "yes" ]; then rm -rf "$DATA"; else echo "Nothing deleted."; exit 1; fi
fi
[ -d "$FRONTEND/src" ] || { echo "Frontend not found at $FRONTEND -- clone it next to the backend, or set JADE_FRONTEND_DIR"; exit 1; }

# -- Python 3.11+ in a private virtual environment ---------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then PY=$c; break; fi
done
[ -n "$PY" ] || { echo "Python 3.11 or newer is required (macOS: brew install python@3.12)"; exit 1; }
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating $VENV and installing the backend (first run only)..."
  "$PY" -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c "import fastapi, uvicorn, openpyxl, claude_agent_sdk, jde_mcp_server, jde_api_service" 2>/dev/null; then
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "$BACKEND/api_service" -e "$BACKEND/mcp_server"
fi

# -- Node 18+ for the frontend -------------------------------------------------
command -v node >/dev/null || { echo "Node.js 18 or newer is required (macOS: brew install node)"; exit 1; }
node -e 'process.exit(parseInt(process.versions.node) >= 18 ? 0 : 1)' || { echo "Node.js 18 or newer is required"; exit 1; }
[ -d "$FRONTEND/node_modules" ] || (echo "Installing the frontend (first run only)..." && cd "$FRONTEND" && npm ci --silent)

# -- Throwaway local credentials: created once, then reused ---------------------
mkdir -p "$DATA" && chmod 700 "$DATA"
CRED="$DATA/credentials.env"
if [ ! -f "$CRED" ]; then
  (umask 077; "$VENV/bin/python" - > "$CRED" <<'PY'
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
# Used only if the admin account does not exist yet; an existing account is never reset.
export JDE_BOOTSTRAP_ADMIN_EMAIL=admin@e2e.local JDE_BOOTSTRAP_ADMIN_NAME="E2E Admin" JDE_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PW"
export JADE_E2E_CNC_PASSWORD="$CNC_PW" JADE_E2E_DO_PASSWORD="$DO_PW"

(cd "$BACKEND" && exec "$VENV/bin/uvicorn" jde_api_service.main:app --app-dir api_service --port "$API_PORT" > "$DATA/backend.log" 2>&1) &
BPID=$!
(cd "$FRONTEND" && VITE_USE_MOCK_API=false VITE_API_BASE_URL="http://localhost:$API_PORT" exec npx vite --port "$UI_PORT" --strictPort > "$DATA/frontend.log" 2>&1) &
FPID=$!
trap 'kill $BPID $FPID 2>/dev/null; wait 2>/dev/null; echo "Jade preview stopped (data kept in $DATA)."' EXIT INT TERM

for _ in $(seq 1 90); do
  curl -sf "http://localhost:$API_PORT/health" >/dev/null && curl -sf "http://localhost:$UI_PORT" >/dev/null && break
  sleep 1
done
curl -sf "http://localhost:$API_PORT/health" >/dev/null || { echo "Backend did not start; see $DATA/backend.log"; exit 1; }
curl -sf "http://localhost:$UI_PORT" >/dev/null || { echo "Frontend did not start; see $DATA/frontend.log"; exit 1; }

# -- Demonstration stories: each added once, only if absent (never over existing records) --
if [ -f "$DATA/seeded" ]; then touch "$DATA/seeded-technical" "$DATA/seeded-process"; fi   # earlier preview versions
for seed in technical process functional; do
  if [ ! -f "$DATA/seeded-$seed" ]; then
    echo "Adding the $seed demonstration story (scripted stand-ins, SIMULATED JDE)..."
    (cd "$BACKEND" && "$VENV/bin/python" "scripts/seed_demo_$seed.py" bwm) >> "$DATA/seed.log" 2>&1 \
      || { echo "Seeding failed; see $DATA/seed.log"; exit 1; }
    touch "$DATA/seeded-$seed"
  fi
done

cat <<INFO

  Jade local preview -- JDE is SIMULATED (no JD Edwards system is contacted)

  Open:      http://localhost:$UI_PORT   (in a browser on this computer)
  Company:   BicycleWorks Manufacturing BV

  Demo users (throwaway, local only). Passwords are in: $CRED
    admin@e2e.local  (ADMIN_PW)  admin + product manager: frameworks, reviews, finalising
    do@e2e.local     (DO_PW)     Domain Owner for Customer Service -- try it in a second browser
    cnc@e2e.local    (CNC_PW)    CNC operator only

  Start at Delivery > Process & Maps > S-BW-RETURNS, then follow the journey bar.
  Stop with Ctrl-C; start again to continue with the same data.

INFO
wait $BPID
