#!/bin/bash

# Pastikan script dijalankan sebagai root (karena ini adalah persyaratan Anda)
if [ "$EUID" -ne 0 ]; then
  echo "⚠️ Script ini harus dijalankan sebagai root. Harap gunakan: sudo bash $0"
  exit 1
fi

# --- KONFIGURASI JALUR ---
INSTALL_DIR="/opt/cleanup"
SYSTEMD_DIR="/etc/systemd/system"
VENV_DIR="${INSTALL_DIR}/venv"

echo "--- 🛠️ Setup Direktori dan Izin ---"

# 1. Buat direktori instalasi
mkdir -p "${INSTALL_DIR}"
echo "Direktori instalasi dibuat di: ${INSTALL_DIR}"

# 2. Salin file program ke direktori instalasi
cp cleanup.py cleanup.service cleanup.timer config.yaml "${INSTALL_DIR}/"
echo "File konfigurasi dan program disalin."

# 3. Hapus baris User dan Group dari cleanup.service (agar service berjalan sebagai root)
# Menggunakan sed untuk menghapus baris yang dimulai dengan 'User=' atau 'Group='
sed -i '/^User=/d' "${INSTALL_DIR}/cleanup.service"
sed -i '/^Group=/d' "${INSTALL_DIR}/cleanup.service"
echo "Baris User dan Group dihapus dari cleanup.service. Service akan berjalan sebagai root."

# 4. Tetapkan kepemilikan ke root (meskipun sudah root, ini memastikan)
chown -R root:root "${INSTALL_DIR}"
echo "Kepemilikan diatur ke root:root."

# --- 🐍 Setup Virtual Environment ---
echo "--- 🐍 Setup Virtual Environment ---"

# 5. Buat Virtual Environment
/usr/bin/python3 -m venv "${VENV_DIR}"
echo "Virtual Environment dibuat."

# 6. Aktifkan VENV dan instal dependensi (PyYAML)
# Karena dijalankan sebagai root, VENV akan diakses oleh root
source "${VENV_DIR}/bin/activate"
"${VENV_DIR}/bin/pip" install pyyaml
deactivate
echo "Dependensi PyYAML diinstal."

# --- 🔗 Instalasi Systemd (Symlink) ---
echo "--- 🔗 Instalasi Systemd ---"

# 7. Hapus symlink lama jika ada
rm -f "${SYSTEMD_DIR}/cleanup.service"
rm -f "${SYSTEMD_DIR}/cleanup.timer"

# 8. Buat Symlink ke direktori Systemd
ln -s "${INSTALL_DIR}/cleanup.service" "${SYSTEMD_DIR}/cleanup.service"
ln -s "${INSTALL_DIR}/cleanup.timer" "${SYSTEMD_DIR}/cleanup.timer"
echo "Symlink Systemd dibuat di ${SYSTEMD_DIR}."

# 9. Muat ulang konfigurasi Systemd
systemctl daemon-reload
echo "Systemd daemon dimuat ulang."

# 10. Aktifkan dan mulai timer
systemctl enable cleanup.timer
systemctl start cleanup.timer
echo "✅ Timer cleanup.timer diaktifkan dan dimulai. Service akan berjalan sebagai root."

# --- 🎉 Verifikasi ---
echo "--- 🎉 Verifikasi Status ---"
systemctl status cleanup.timer | grep -E "Active:|Loaded:|service"
echo ""
echo "Instalasi selesai. Timer akan berjalan pertama kali 5 menit setelah start ini."
echo "Untuk melihat log, gunakan: journalctl -u cleanup.service"