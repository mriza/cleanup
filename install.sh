#!/bin/bash

# This script must be run as root to create users and install services
if [ "$EUID" -ne 0 ]; then
  echo "⚠️ This script must be run as root. Please use: sudo bash $0"
  exit 1
fi

# --- CONFIGURATION ---
INSTALL_DIR="/opt/cleanup"
STATIC_DIR="${INSTALL_DIR}/static"
DIRECTORIES_D="${INSTALL_DIR}/directories.d"
SYSTEMD_DIR="/etc/systemd/system"
VENV_DIR="${INSTALL_DIR}/venv"
SERVICE_USER="cleanupd"

echo "--- 🛠️ Setting up Service User and Directories ---"

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "Creating system user '${SERVICE_USER}'..."
    sudo useradd -r -s /bin/false "${SERVICE_USER}"
else
    echo "System user '${SERVICE_USER}' already exists."
fi

# Buat semua direktori
mkdir -p "${INSTALL_DIR}"
mkdir -p "${VENV_DIR}"
mkdir -p "${STATIC_DIR}"
mkdir -p "${DIRECTORIES_D}"
echo "Installation directories created."

# 1. Copy ALL program files
cp cleanup.py indexer.py configure.py api.py \
   cleanupd-cleaner.service cleanupd-cleaner.timer \
   cleanupd-indexer.service cleanupd-indexer.timer \
   cleanupd-api.service \
   config.yaml requirements.txt \
   index.html example.yaml README.md "${INSTALL_DIR}/"
   
# Pindahkan file frontend dan example config ke subdirektori mereka
mv "${INSTALL_DIR}/index.html" "${STATIC_DIR}/index.html"
mv "${INSTALL_DIR}/example.yaml" "${DIRECTORIES_D}/example.yaml"
echo "Configuration and program files copied to ${INSTALL_DIR}."

# 2. Set ownership
chown root:root "${INSTALL_DIR}"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${VENV_DIR}"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${STATIC_DIR}"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${DIRECTORIES_D}"
chown "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}"/*.py
chown "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}"/config.yaml
chown "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}"/requirements.txt
chown "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_DIR}/README.md"
echo "File ownership set to '${SERVICE_USER}'."

# 3. Secure the API Secret Key
CONFIG_FILE="${INSTALL_DIR}/config.yaml"
DEFAULT_KEY="api_secret_key: \"CHANGE_THIS_TO_A_VERY_LONG_RANDOM_SECRET_STRING\""

if grep -qF "$DEFAULT_KEY" "$CONFIG_FILE"; then
    echo "Generating new random API_SECRET_KEY..."
    NEW_KEY=$(openssl rand -hex 32)
    sudo -u "${SERVICE_USER}" sed -i "s|$DEFAULT_KEY|api_secret_key: \"$NEW_KEY\"|g" "$CONFIG_FILE"
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
rm -f "${SYSTEMD_DIR}/cleanupd-cleaner.service"
rm -f "${SYSTEMD_DIR}/cleanupd-cleaner.timer"
rm -f "${SYSTEMD_DIR}/cleanupd-indexer.service"
rm -f "${SYSTEMD_DIR}/cleanupd-indexer.timer"
rm -f "${SYSTEMD_DIR}/cleanupd-api.service"

ln -s "${INSTALL_DIR}/cleanupd-cleaner.service" "${SYSTEMD_DIR}/cleanupd-cleaner.service"
ln -s "${INSTALL_DIR}/cleanupd-cleaner.timer" "${SYSTEMD_DIR}/cleanupd-cleaner.timer"
ln -s "${INSTALL_DIR}/cleanupd-indexer.service" "${SYSTEMD_DIR}/cleanupd-indexer.service"
ln -s "${INSTALL_DIR}/cleanupd-indexer.timer" "${SYSTEMD_DIR}/cleanupd-indexer.timer"
ln -s "${INSTALL_DIR}/cleanupd-api.service" "${SYSTEMD_DIR}/cleanupd-api.service"
echo "Systemd symlinks for cleanupd-* created."

systemctl daemon-reload
echo "Systemd daemon reloaded."

systemctl enable cleanupd-cleaner.timer
systemctl enable cleanupd-indexer.timer
systemctl enable cleanupd-api.service 

systemctl start cleanupd-cleaner.timer
systemctl start cleanupd-indexer.timer
systemctl start cleanupd-api.service
echo "✅ Timers (cleaner, indexer) and Service (api) enabled and started."

# --- 🪵 Installing Logrotate ---
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
echo "Logrotate config created at ${LOGROTATE_CONF}."

# --- 🎉 Verification & IMPORTANT NEXT STEPS ---
echo "--- 🎉 Verifying Status ---"
systemctl status cleanupd-indexer.timer | grep -E "Active:|Loaded:|service"
systemctl status cleanupd-cleaner.timer | grep -E "Active:|Loaded:|service"
systemctl status cleanupd-api.service | grep -E "Active:|Loaded:|Main PID"
echo ""
echo "Installation complete. API is running on http://<server_ip>:8000"
echo "Frontend is available at http://<server_ip>:8000/"

# --- 💡 New CLI Info ---
echo "--- 💡 CLI Tool (Now an API Client) ---"
echo "To manage the config, use:"
echo "sudo -u ${SERVICE_USER} /opt/cleanup/venv/bin/python /opt/cleanup/configure.py --help"
echo ""
echo "--- 🔐 ACTION REQUIRED: Set Your Admin Password ---"
echo "1. Generate a new hash:"
echo "   sudo -u ${SERVICE_USER} /opt/cleanup/venv/bin/python /opt/cleanup/configure.py hash-password"
echo "2. Copy the generated hash."
echo "3. Paste it into /opt/cleanup/config.yaml, replacing the default 'api_admin_pass_hash'."
echo "4. Restart the