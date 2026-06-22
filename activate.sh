#!/usr/bin/env bash
#
# setup_service.sh
# -----------------
# Configures and installs the Telegram support bot as a systemd service.
#
# WHAT IT DOES
#   1. Asks for your BotFather token and admin Telegram ID(s) (or accepts
#      them as arguments / existing environment variables).
#   2. Creates a Python virtual environment next to the bot and installs
#      requirements.txt into it.
#   3. Stores the token/admin IDs in a restricted .env file (chmod 600),
#      not directly inside the systemd unit file.
#   4. Writes a systemd unit, enables it, and starts the bot.
#
# USAGE
#   sudo ./setup_service.sh
#       (interactive - you'll be prompted for the token and admin IDs)
#
#   sudo ./setup_service.sh "123456:ABC-DEF..." "111111111,222222222"
#       (non-interactive - pass token and admin IDs as arguments)
#
# REQUIREMENTS
#   - Must be run with sudo/root (it writes to /etc/systemd/system).
#   - support_bot.py and requirements.txt must be in the same directory
#     as this script.
#   - Run this from a normal (non-root) user's checkout via sudo, e.g.:
#       cd ~/telegram-support-bot && sudo ./setup_service.sh
#     so the service can be set up to run as that user rather than root.
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# Pre-flight checks
# --------------------------------------------------------------------------- #

if [[ "${EUID}" -ne 0 ]]; then
    echo "❌ Please run this script with sudo: sudo ./setup_service.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "${SCRIPT_DIR}/support_bot.py" ]]; then
    echo "❌ support_bot.py not found in ${SCRIPT_DIR}"
    echo "   Put this script in the same folder as support_bot.py and try again."
    exit 1
fi

if [[ ! -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    echo "❌ requirements.txt not found in ${SCRIPT_DIR}"
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 is not installed. Install it first."
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "❌ systemctl not found. This script requires a systemd-based Linux distro."
    exit 1
fi

# Run the service as the user who invoked sudo, not as root.
SERVICE_USER="${SUDO_USER:-$(whoami)}"
if [[ "${SERVICE_USER}" == "root" ]]; then
    echo "⚠️  Could not detect a non-root invoking user (SUDO_USER is unset)."
    echo "    The service will run as root. Press Ctrl+C to abort, or Enter to continue."
    read -r _
fi

ENV_FILE="${SCRIPT_DIR}/.env"
SERVICE_NAME="telegram-support-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_DIR="${SCRIPT_DIR}/venv"

# --------------------------------------------------------------------------- #
# Collect BOT_TOKEN and ADMIN_IDS
# --------------------------------------------------------------------------- #

BOT_TOKEN="${1:-${BOT_TOKEN:-}}"
ADMIN_IDS="${2:-${ADMIN_IDS:-}}"

# If an .env already exists from a previous run, offer its values as defaults.
EXISTING_TOKEN=""
EXISTING_ADMINS=""
if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    EXISTING_TOKEN="$(grep -E '^BOT_TOKEN=' "${ENV_FILE}" | cut -d= -f2- || true)"
    EXISTING_ADMINS="$(grep -E '^ADMIN_IDS=' "${ENV_FILE}" | cut -d= -f2- || true)"
fi

if [[ -z "${BOT_TOKEN}" ]]; then
    if [[ -n "${EXISTING_TOKEN}" ]]; then
        read -rp "BotFather token [keep existing]: " BOT_TOKEN
        BOT_TOKEN="${BOT_TOKEN:-${EXISTING_TOKEN}}"
    else
        read -rp "BotFather token: " BOT_TOKEN
    fi
fi

if [[ -z "${ADMIN_IDS}" ]]; then
    if [[ -n "${EXISTING_ADMINS}" ]]; then
        read -rp "Admin Telegram ID(s), comma-separated [keep existing]: " ADMIN_IDS
        ADMIN_IDS="${ADMIN_IDS:-${EXISTING_ADMINS}}"
    else
        read -rp "Admin Telegram ID(s), comma-separated (e.g. 111111111,222222222): " ADMIN_IDS
    fi
fi

if [[ -z "${BOT_TOKEN}" ]]; then
    echo "❌ A bot token is required."
    exit 1
fi

if [[ -z "${ADMIN_IDS}" ]]; then
    echo "⚠️  No admin IDs provided - nobody will be able to approve representatives."
    read -rp "Continue anyway? [y/N] " confirm
    if [[ ! "${confirm}" =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# --------------------------------------------------------------------------- #
# Virtual environment + dependencies
# --------------------------------------------------------------------------- #

echo "📦 Setting up virtual environment in ${VENV_DIR} ..."
if [[ ! -d "${VENV_DIR}" ]]; then
    sudo -u "${SERVICE_USER}" python3 -m venv "${VENV_DIR}"
fi

echo "📦 Installing dependencies ..."
sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"

# --------------------------------------------------------------------------- #
# .env file (token + admin IDs, restricted permissions)
# --------------------------------------------------------------------------- #

echo "🔐 Writing ${ENV_FILE} ..."
cat > "${ENV_FILE}" <<EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
EOF

chown "${SERVICE_USER}:${SERVICE_USER}" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

# --------------------------------------------------------------------------- #
# systemd unit
# --------------------------------------------------------------------------- #

echo "🛠  Writing ${SERVICE_FILE} ..."
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Telegram Customer-Representative Support Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${SCRIPT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python3 ${SCRIPT_DIR}/support_bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${SERVICE_FILE}"

# --------------------------------------------------------------------------- #
# Activate
# --------------------------------------------------------------------------- #

echo "🔄 Reloading systemd ..."
systemctl daemon-reload

echo "✅ Enabling and starting ${SERVICE_NAME} ..."
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 2
systemctl status "${SERVICE_NAME}" --no-pager || true

cat <<EOF

--------------------------------------------------------------
Done. Useful commands:

  View live logs:     sudo journalctl -u ${SERVICE_NAME} -f
  Check status:        sudo systemctl status ${SERVICE_NAME}
  Restart the bot:     sudo systemctl restart ${SERVICE_NAME}
  Stop the bot:         sudo systemctl stop ${SERVICE_NAME}
  Edit token/admins:    sudo nano ${ENV_FILE}  (then: sudo systemctl restart ${SERVICE_NAME})
--------------------------------------------------------------
EOF