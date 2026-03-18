#!/usr/bin/env bash
#
# install.sh — Set up ventoy-sync: venv, deps, systemd user units.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

echo "=== Ventoy Sync Installer ==="
echo ""

# 1. Create / update venv
if [ ! -d "${VENV_DIR}" ]; then
    echo "[1/4] Creating virtual environment..."
    python3 -m venv "${VENV_DIR}"
else
    echo "[1/4] Virtual environment already exists."
fi

# 2. Install dependencies
echo "[2/4] Installing Python dependencies..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"

# 3. Install systemd user units
echo "[3/4] Installing systemd user units..."
mkdir -p "${SYSTEMD_USER_DIR}"

# Remove old symlink if present (previous installs used symlinks)
rm -f "${SYSTEMD_USER_DIR}/ventoy-sync.service"

# Template the service file with the actual project path
sed "s|__SCRIPT_DIR__|${SCRIPT_DIR}|g" "${SCRIPT_DIR}/ventoy-sync.service" \
    > "${SYSTEMD_USER_DIR}/ventoy-sync.service"
ln -sf "${SCRIPT_DIR}/ventoy-sync.timer" "${SYSTEMD_USER_DIR}/ventoy-sync.timer"
systemctl --user daemon-reload

# 4. Enable & start the timer
echo "[4/4] Enabling ventoy-sync.timer..."
systemctl --user enable ventoy-sync.timer
systemctl --user start ventoy-sync.timer

echo ""
echo "Done. The timer is now active:"
systemctl --user status ventoy-sync.timer --no-pager || true

echo ""
echo "Useful commands:"
echo "  Manual sync:     ${SCRIPT_DIR}/ventoy-sync.py"
echo "  Dry run:         ${SCRIPT_DIR}/ventoy-sync.py --dry-run"
echo "  Check one ISO:   ${SCRIPT_DIR}/ventoy-sync.py --check archlinux"
echo "  Timer status:    systemctl --user status ventoy-sync.timer"
echo "  Run now:         systemctl --user start ventoy-sync.service"
echo "  View logs:       journalctl --user -u ventoy-sync.service"
echo "  Disable timer:   systemctl --user disable --now ventoy-sync.timer"
