#!/usr/bin/env bash
# Jade on this computer: the real backend and frontend, the latest code of the
# checked-out branches, and your own data. Nothing is simulated and nothing is
# pre-loaded: you sign in with the temporary setup account, create your own
# administrator account, and set up your customers, users and connections
# (AI, Jira, JD Edwards) in Administration.
#
#   scripts/run_local_preview.sh           # start (the first run installs)
#   scripts/run_local_preview.sh --reset   # ONLY when asked: throws this data away (asks to confirm)
#
# Everything Jade saves (accounts, password hashes, encrypted credentials,
# settings, stories, records) lives under .jade-data/ (git-ignored) and is
# reused on every start. The setup password and the credential encryption key
# are generated once into .jade-data/credentials.env (readable only by you)
# and never printed. An older .preview-data/ folder (demo data) is left
# untouched and is not used.
#
# Optional server settings go in .jade-data/server.env (one KEY=value per
# line); only the settings listed below are read from it.
set -euo pipefail
BACKEND=$(cd "$(dirname "$0")/.." && pwd)
FRONTEND=${JADE_FRONTEND_DIR:-$BACKEND/../JDE_change_factory_frontend}
DATA=${JADE_DATA:-$BACKEND/.jade-data}
VENV=${JADE_PREVIEW_VENV:-$BACKEND/.venv}
API_PORT=${JADE_API_PORT:-8000}
UI_PORT=${JADE_UI_PORT:-5173}

if [ "${1:-}" = "--reset" ]; then
  read -r -p "Delete ALL Jade data in $DATA (accounts, settings, stories, records)? Type yes: " answer
  if [ "$answer" = "yes" ]; then rm -rf "$DATA"; else echo "Nothing deleted."; exit 1; fi
fi
[ -d "$FRONTEND/src" ] || { echo "Frontend not found at $FRONTEND -- clone it next to the backend, or set JADE_FRONTEND_DIR"; exit 1; }

# -- Always run the latest code: bring both clones up to date with their branch ---------
# (fast-forward only, and only when there are no local edits; nothing is ever overwritten)
for repo in "$BACKEND" "$FRONTEND"; do
  if [ "${JADE_PREVIEW_NO_UPDATE:-}" != "1" ] && git -C "$repo" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    if [ -z "$(git -C "$repo" status --porcelain --untracked-files=no)" ]; then
      git -C "$repo" pull --ff-only -q 2>/dev/null || echo "Could not update $repo (offline or diverged); running what is there."
    else
      echo "Not updating $repo: it has local edits."
    fi
  fi
  echo "  $(basename "$repo"): $(git -C "$repo" rev-parse --abbrev-ref HEAD) @ $(git -C "$repo" rev-parse --short=10 HEAD)"
done

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
print(f"ADMIN_PW={secrets.token_urlsafe(16)}")
print(f"CREDENTIAL_KEY={Fernet.generate_key().decode()}")
PY
  )
fi
# shellcheck disable=SC1090
source "$CRED"

export JDE_API_DATA_DIR="$DATA/api" JDE_BACKLOG_DIR="$DATA/backlog" JDE_CHANGE_DIR="$DATA/changes" JDE_EVIDENCE_DIR="$DATA/evidence"
export JDE_CREDENTIAL_KEY="$CREDENTIAL_KEY" JDE_API_ALLOWED_ORIGINS="http://localhost:$UI_PORT" JDE_COOKIE_SECURE=false
# The temporary setup account: used only if it does not exist yet; an existing
# account is never reset, and it is switched off once you finish setup.
export JDE_BOOTSTRAP_ADMIN_EMAIL=setup@jade.local JDE_BOOTSTRAP_ADMIN_NAME="Jade setup" JDE_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PW"
# JDE connection settings (address, certificate, credential) are made in the app.
SERVER_SETTINGS="JDE_DISCOVERY_LIVE_ENABLED JDE_DISCOVERY_ALLOWED_HOSTS JDE_DISCOVERY_CA_BUNDLE JDE_DATABASE_URL JDE_DATABASE_SCHEMA JDE_BOOTSTRAP_CUSTOMER_NAME JDE_SMTP_HOST JDE_SMTP_PORT JDE_SMTP_USERNAME JDE_SMTP_PASSWORD JDE_SMTP_STARTTLS JDE_MAIL_FROM JDE_PUBLIC_URL JDE_ANTHROPIC_BASE_URL"
# shellcheck disable=SC2086
unset $SERVER_SETTINGS
if [ -f "$DATA/server.env" ]; then
  while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue ;; esac
    case " $SERVER_SETTINGS " in
      *" $key "*) export "$key=$value" ;;
      *) echo "Ignoring $key in $DATA/server.env (not a permitted server setting)" ;;
    esac
  done < "$DATA/server.env"
fi

(cd "$BACKEND" && exec "$VENV/bin/uvicorn" jde_api_service.main:app --app-dir api_service --port "$API_PORT" > "$DATA/backend.log" 2>&1) &
BPID=$!
(cd "$FRONTEND" && VITE_USE_MOCK_API=false VITE_API_BASE_URL="http://localhost:$API_PORT" exec npx vite --port "$UI_PORT" --strictPort > "$DATA/frontend.log" 2>&1) &
FPID=$!
trap 'kill $BPID $FPID 2>/dev/null; wait 2>/dev/null; echo "Jade stopped (data kept in $DATA)."' EXIT INT TERM

for _ in $(seq 1 90); do
  curl -sf "http://localhost:$API_PORT/health" >/dev/null && curl -sf "http://localhost:$UI_PORT" >/dev/null && break
  sleep 1
done
curl -sf "http://localhost:$API_PORT/health" >/dev/null || { echo "Backend did not start; see $DATA/backend.log"; exit 1; }
curl -sf "http://localhost:$UI_PORT" >/dev/null || { echo "Frontend did not start; see $DATA/frontend.log"; exit 1; }

# -- First sign-in: while the temporary setup account is active, copy its password to the clipboard
#    (never shown) and explain "Finish setup"; afterwards, just point to the owner's own account.
SETUP_ACTIVE=$(JDE_API_DATA_DIR="$DATA/api" "$VENV/bin/python" -c '
import glob, os, sqlite3
active = False
for db in glob.glob(os.path.join(os.environ["JDE_API_DATA_DIR"], "*.sqlite3")) + glob.glob(os.path.join(os.environ["JDE_API_DATA_DIR"], "*.db")):
    try:
        row = sqlite3.connect(db).execute("SELECT is_active FROM users WHERE email = ?", ("setup@jade.local",)).fetchone()
        active = active or bool(row and row[0])
    except sqlite3.Error:
        pass
print("yes" if active else "no")' 2>/dev/null)
if [ "$SETUP_ACTIVE" = "yes" ]; then
  if command -v pbcopy >/dev/null; then printf %s "$ADMIN_PW" | pbcopy; COPIED="on your clipboard now, just paste it"; else COPIED="the ADMIN_PW line in $CRED"; fi
  SIGNIN="  First sign-in: setup@jade.local, the temporary setup account (password: $COPIED).
  Then fill in 'Finish setup' at the top of the page to create your own administrator account;
  the setup account is switched off as soon as yours exists."
else
  SIGNIN="  Sign in with your own administrator account. (The temporary setup account is switched off.)"
fi

cat <<INFO

  Jade -- running on this computer

  Open:      http://localhost:$UI_PORT   (in a browser on this computer)

$SIGNIN

  Set up in Administration:
    Organisation              your customers (the first one is created for you -- rename it)
    Users & Access            Domain Owners, Application Managers, CNC operators
    Agents & AI               the customer's Anthropic API key and model
    Systems & Connections     Jira and JD Edwards (AIS address, certificate, credential, Test connection)

  Data: $DATA   Stop with Ctrl-C; start again to continue with the same data.

INFO
wait $BPID
