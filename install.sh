#!/bin/bash

# This script must be run as root to create users and install services
if [ "$EUID" -ne 0 ]; then
  echo "⚠️ This script must be run as root. Please use: sudo bash $0"
  exit 1
fi

# --- CONFIGURATION ---
INSTALL_DIR="/opt/cleanup"
SYSTEMD_DIR="/etc/systemd/system"
VENV_DIR="${INSTALL_DIR}/venv"
SERVICE_USER="cleanupd"

echo "--- 🛠️ Setting up Service User and Directories ---"

# Create a dedicated system user 'cleanupd'
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "Creating system user '${SERVICE_USER}'..."
    sudo useradd -r -s /bin/false "${SERVICE_USER}"
else
    echo "System user '${SERVICE_USER}' already exists."
fi

mkdir -p "${INSTALL_DIR}"
echo "Installation directory created at: ${INSTALL_DIR}"

# 1. Copy ALL program files (Using new cleanupd-* names)
cp cleanup.py indexer.py configure.py api.py \
   cleanupd-cleaner.service cleanupd-cleaner.timer \
   cleanupd-indexer.service cleanupd-indexer.timer \
   cleanupd-api.service \
   config.yaml requirements.txt "${INSTALL_DIR}/"
echo "Configuration and program files copied to ${INSTALL_DIR}."

# 2. Set ownership
chown root:root "${INSTALL_DIR}"
mkdir -p "${VENV_DIR}"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${VENV_DIR}"
chown "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}"/*.py
chown "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}"/config.yaml
chown "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}"/requirements.txt
echo "File ownership set to '${SERVICE_USER}'."

# 3. Secure the API Secret Key
CONFIG_FILE="${INSTALL_DIR}/config.yaml"
DEFAULT_KEY="CHANGE_THIS_TO_A_VERY_LONG_RANDOM_SECRET_STRING"
if grep -qF "$DEFAULT_KEY" "$CONFIG_FILE"; then
    echo "Generating new random API_SECRET_KEY..."
    # Generate a 32-byte (256-bit) random hex string
    NEW_KEY=$(openssl rand -hex 32)
    # Use sudo to write as the file owner
    sudo -u "${SERVICE_USER}" sed -i "s|$DEFAULT_KEY|$NEW_KEY|g" "$CONFIG_FILE"
    echo "API_SECRET_KEY has been randomized for security."
else
    echo "API_SECRET_KEY already set. Skipping generation."
fi

# --- 🐍 Setting up Virtual Environment ---
echo "--- 🐍 Setting up Virtual Environment ---"
sudo -u "${SERVICE_USER}" /usr/bin/python3 -m venv "${VENV_DIR}"
echo "Virtual Environment created in ${VENV_DIR}."

source "${VENV_DIR}/bin/activate"
echo "Installing dependencies from requirements.txt..."
"${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
deactivate
echo "Dependencies installed."

# --- 🔗 Installing Systemd (Symlink) ---
echo "--- 🔗 Installing Systemd ---"
# Remove old symlinks (using new names)
rm -f "${SYSTEMD_DIR}/cleanupd-cleaner.service"
rm -f "${SYSTEMD_DIR}/cleanupd-cleaner.timer"
rm -f "${SYSTEMD_DIR}/cleanupd-indexer.service"
rm -f "${SYSTEMD_DIR}/cleanupd-indexer.timer"
rm -f "${SYSTEMD_DIR}/cleanupd-api.service"

# Create all 5 Symlinks (using new names)
ln -s "${INSTALL_DIR}/cleanupd-cleaner.service" "${SYSTEMD_DIR}/cleanupd-cleaner.service"
ln -s "${INSTALL_DIR}/cleanupd-cleaner.timer" "${SYSTEMD_DIR}/cleanupd-cleaner.timer"
ln -s "${INSTALL_DIR}/cleanupd-indexer.service" "${SYSTEMD_DIR}/cleanupd-indexer.service"
ln -s "${INSTALL_DIR}/cleanupd-indexer.timer" "${SYSTEMD_DIR}/cleanupd-indexer.timer"
ln -s "${INSTALL_DIR}/cleanupd-api.service" "${SYSTEMD_DIR}/cleanupd-api.service"
echo "Systemd symlinks for cleanupd-* created."

# 8. Reload Systemd daemon
systemctl daemon-reload
echo "Systemd daemon reloaded."

# 9. Enable and start ALL services/timers (using new names)
systemctl enable cleanupd-cleaner.timer
systemctl enable cleanupd-indexer.timer
systemctl enable cleanupd-api.service # api.service (not timer) runs 24/7

systemctl start cleanupd-cleaner.timer
systemctl start cleanupd-indexer.timer
systemctl start cleanupd-api.service
echo "✅ Timers (cleaner, indexer) and Service (api) enabled and started."

# --- 🪵 (NEW) Installing Logrotate ---
echo "--- 🪵 Installing Logrotate ---"
LOGROTATE_CONF="/etc/logrotate.d/cleanupd"
cat << EOF > "${LOGROTATE_CONF}"
/opt/cleanup/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    copytruncate
    su ${SERVICE_USER} ${SERVICE_USER}
}
EOF
echo "Logrotate config created at ${LOGROTATE_CONF} for any future *.log files."

# --- 🎉 Verification & IMPORTANT NEXT STEPS ---
echo "--- 🎉 Verifying Status ---"
systemctl status cleanupd-indexer.timer | grep -E "Active:|Loaded:|service"
systemctl status cleanupd-cleaner.timer | grep -E "Active:|Loaded:|service"
systemctl status cleanupd-api.service | grep -E "Active:|Loaded:|Main PID"
echo ""
echo "Installation complete. API is running on http://<server_ip>:8000"

# --- 💡 New CLI Info ---
echo "--- 💡 CLI Tool (Now an API Client) ---"
echo "To manage the config, use:"
echo "sudo -u ${SERVICE_USER} /opt/cleanup/venv/bin/python /opt/cleanup/configure.py --help"
echo ""
echo "--- 🔐 ACTION REQUIRED: Set Your Admin Password ---"
echo "A default password hash is in config.yaml. CHANGE IT."
echo "1. Generate a new hash:"
echo "   sudo -u ${SERVICE_USER} /opt/cleanup/venv/bin/python /opt/cleanup/configure.py hash-password"
echo "2. Copy the generated hash."
echo "3. Paste it into /opt/cleanup/config.yaml, replacing the default 'api_admin_pass_hash'."
echo "4. Restart the API service: sudo systemctl restart cleanupd-api.service"
echo ""
echo "--- ⚠️ ACTION REQUIRED: Grant Permissions ---"
echo "Remember to grant '${SERVICE_USER}' permissions on your target directories!"
echo "e.g., sudo setfacl -R -m u:${SERVICE_USER}:rwx /home/atcs2/WORKER"