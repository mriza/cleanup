#!/bin/bash

# Pastikan script dijalankan sebagai root (sesuai permintaan)
if [ "$EUID" -ne 0 ]; then
  echo "⚠️ Script ini harus dijalankan sebagai root. Harap gunakan: sudo bash $0"
  exit 1
fi

# --- KONFIGURASI JALUR ---
INSTALL_DIR="/opt/cleanup"
SYSTEMD_DIR="/etc/systemd/system"
VENV_DIR="${INSTALL_DIR}/venv"

echo "--- 🛠️ Setup Direktori dan Izin ---"
mkdir -p "${INSTALL_DIR}"
echo "Direktori instalasi dibuat di: ${INSTALL_DIR}"

# 1. Salin SEMUA file program ke direktori instalasi
# (Ini versi SEBELUM api.py)
cp cleanup.py indexer.py \
   cleanup.service cleanup.timer \
   indexer.service indexer.timer \
   config.yaml "${INSTALL_DIR}/"
echo "File konfigurasi dan program (Indexer, Cleaner) disalin ke ${INSTALL_DIR}."

# 2. Hapus baris User/Group dari KEDUA file service (biar jalan sbg root)
sed -i '/^User=/d' "${INSTALL_DIR}/cleanup.service"
sed -i '/^Group=/d' "${INSTALL_DIR}/cleanup.service"
sed -i '/^User=/d' "${INSTALL_DIR}/indexer.service"
sed -i '/^Group=/d' "${INSTALL_DIR}/indexer.service"
echo "Baris User/Group dihapus dari file .service."

# 3. Tetapkan kepemilikan ke root
chown -R root:root "${INSTALL_DIR}"
echo "Kepemilikan diatur ke root:root."

# --- 🐍 Setup Virtual Environment ---
echo "--- 🐍 Setup Virtual Environment ---"

# 4. Buat Virtual Environment di dalam /opt/cleanup
/usr/bin/python3 -m venv "${VENV_DIR}"
echo "Virtual Environment dibuat di ${VENV_DIR}."

# 5. Aktifkan VENV dan instal dependensi (HANYA pyyaml)
source "${VENV_DIR}/bin/activate"
echo "Menginstal dependensi: pyyaml..."
"${VENV_DIR}/bin/pip" install pyyaml
deactivate
echo "Dependensi PyYAML diinstal."

# --- 🔗 Instalasi Systemd (Symlink) ---
echo "--- 🔗 Instalasi Systemd ---"

# 6. Hapus symlink lama (kalo ada)
rm -f "${SYSTEMD_DIR}/cleanup.service"
rm -f "${SYSTEMD_DIR}/cleanup.timer"
rm -f "${SYSTEMD_DIR}/indexer.service"
rm -f "${SYSTEMD_DIR}/indexer.timer"

# 7. Buat 4 Symlink
ln -s "${INSTALL_DIR}/cleanup.service" "${SYSTEMD_DIR}/cleanup.service"
ln -s "${INSTALL_DIR}/cleanup.timer" "${SYSTEMD_DIR}/cleanup.timer"
ln -s "${INSTALL_DIR}/indexer.service" "${SYSTEMD_DIR}/indexer.service"
ln -s "${INSTALL_DIR}/indexer.timer" "${SYSTEMD_DIR}/indexer.timer"
echo "Symlink Systemd untuk cleanup dan indexer dibuat."

# 8. Muat ulang konfigurasi Systemd
systemctl daemon-reload
echo "Systemd daemon dimuat ulang."

# 9. Aktifkan dan mulai KEDUA timer
systemctl enable cleanup.timer
systemctl enable indexer.timer
systemctl start cleanup.timer
systemctl start indexer.timer
echo "✅ Timer cleanup.timer dan indexer.timer diaktifkan dan dimulai."

# --- 🎉 Verifikasi ---
echo "--- 🎉 Verifikasi Status ---"
echo "Status Indexer (Tahap 1):"
systemctl status indexer.timer | grep -E "Active:|Loaded:|service"
echo ""
echo "Status Cleaner (Tahap 2):"
systemctl status cleanup.timer | grep -E "Active:|Loaded:|service"
echo ""
echo "Instalasi selesai. Semua file berada di ${INSTALL_DIR}."
echo "Database akan dibuat di ${INSTALL_DIR}/cleanup_index.db"