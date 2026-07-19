#!/usr/bin/env bash
#
# Palworld Admin Panel — installer.
#
# Idempotent: safe to re-run to pick up code changes or edited config.
# Run from the repo root:  ./deploy/install.sh
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="palworld-admin"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_DIR="${APP_DIR}/.venv"
RUN_USER="${SUDO_USER:-$(id -un)}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "Run as your normal user, not root — the panel should not run as root. It will sudo only where needed."

# --- prerequisites ----------------------------------------------------------
say "Checking prerequisites"
command -v python3 >/dev/null || die "python3 not found"
command -v docker  >/dev/null || die "docker not found — the panel manages a dockerised Palworld server"
python3 -c 'import venv' 2>/dev/null || die "python3-venv missing. Install it:  sudo apt install python3-venv"

if ! sudo -n docker ps >/dev/null 2>&1; then
  warn "Passwordless 'sudo docker' is not working for ${RUN_USER}."
  warn "The panel needs it for the game server's REST API, logs, and compose actions."
  warn "See the 'Permissions' section of README.md for a scoped sudoers snippet."
fi

# --- python environment -----------------------------------------------------
say "Setting up Python environment in ${VENV_DIR}"
[[ -d "$VENV_DIR" ]] || python3 -m venv "$VENV_DIR"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

# --- configuration ----------------------------------------------------------
if [[ ! -f "${APP_DIR}/.env" ]]; then
  say "Creating .env from .env.example"
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
  warn "Edit ${APP_DIR}/.env before starting — PALWORLD_ADMIN_PASSWORD is required."
  NEEDS_CONFIG=1
else
  say ".env already exists — leaving it alone"
  chmod 600 "${APP_DIR}/.env"
  NEEDS_CONFIG=0
fi

# Data directory (pins, trails, event history, map.png) lives outside the
# checkout so `git pull` never touches it.
DATA_DIR="$(grep -E '^PANEL_DATA_DIR=' "${APP_DIR}/.env" | cut -d= -f2- || true)"
DATA_DIR="${DATA_DIR:-$APP_DIR}"
if [[ ! -d "$DATA_DIR" ]]; then
  say "Creating data directory ${DATA_DIR}"
  sudo mkdir -p "$DATA_DIR"
  sudo chown "${RUN_USER}:${RUN_USER}" "$DATA_DIR"
fi

# --- systemd unit -----------------------------------------------------------
say "Installing systemd unit at ${UNIT_PATH}"
sed -e "s|__USER__|${RUN_USER}|g" \
    -e "s|__APP_DIR__|${APP_DIR}|g" \
    -e "s|__PYTHON__|${VENV_DIR}/bin/python|g" \
    "${APP_DIR}/deploy/palworld-admin.service" | sudo tee "$UNIT_PATH" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null

if [[ "$NEEDS_CONFIG" == "1" ]]; then
  say "Install complete — NOT starting yet."
  echo
  echo "  1. Edit config:   \$EDITOR ${APP_DIR}/.env"
  echo "  2. Add a map:     cp your-map.png ${DATA_DIR}/map.png   (see docs/MAP_CALIBRATION.md)"
  echo "  3. Start:         sudo systemctl start ${SERVICE_NAME}"
  echo
else
  say "Restarting ${SERVICE_NAME}"
  sudo systemctl restart "$SERVICE_NAME"
  sleep 2
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    PORT="$(grep -E '^PANEL_PORT=' "${APP_DIR}/.env" | cut -d= -f2- || echo 8300)"
    say "Running — http://localhost:${PORT:-8300}"
  else
    die "Service failed to start. Logs:  sudo journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
  fi
fi
