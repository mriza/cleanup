#
# SCRIPT: cleanup.py
# TUGAS: Membaca database indeks dan menghapus file (by age / by size).
#
import os
import sqlite3
import time
import yaml
import argparse
import sys
import shutil
import stat

# --- KONFIGURASI GLOBAL ---
INDEX_DB_PATH = "cleanup_index.db"
PROTECTED_PATHS = [
    '/', '/etc', '/usr', '/var', '/lib', '/sbin', '/bin', '/root',
    '/boot', '/dev', '/proc', '/sys', '/run'
]
SECONDS_IN_A_DAY = 86400

# --- FUNGSI HELPER (Umum) ---
def load_config_from_yaml(path):
    """Biasa, cuma baca file config YAML."""
    with open(path, "r") as f:
        return yaml.safe_load(f)

# --- FUNGSI HELPER (Penghapusan File) ---
def safe_remove_file(path, reason, dry_run=False):
    """Wrapper aman buat os.remove (hapus satu file)."""
    if dry_run:
        print(f"[DRY-RUN] Akan menghapus: {path} (Alasan: {reason})")
        return True
    try:
        os.remove(path)
        print(f"Removed: {path} (Alasan: {reason})")
        return True
    except FileNotFoundError:
        print(f"WARN: Mau hapus {path} tapi filenya udah gak ada.")
        return True # Anggap sukses
    except Exception as e:
        print(f"ERROR: Gagal menghapus {path}: {e}")
        return False

# --- FUNGSI UTAMA CLEANUP ---
def cleanup_directory_by_size(target_path, max_bytes, max_file_age_days, dry_run=False):
    """Logika cleanup 'by size' TAPI pake database indeks."""
    print("Metode 'size': Membersihkan menggunakan indeks.")
    now = time.time(); age_cutoff = now - (max_file_age_days * SECONDS_IN_A_DAY)
    files_removed_by_age = 0; files_removed_by_size = 0; total_size_of_new_files = 0

    try:
        with sqlite3.connect(INDEX_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row; cursor = conn.cursor()

            # 1. Hapus file ketuaan
            print(f"Langkah 1: Hapus file lebih tua dari {max_file_age_days} hari...")
            query_old = "SELECT path FROM file_index WHERE target_directory = ? AND mtime < ?"
            for row in cursor.execute(query_old, (target_path, age_cutoff)):
                if safe_remove_file(row['path'], f"lebih tua dari {max_file_age_days} hari", dry_run):
                    files_removed_by_age += 1
            
            # 2. Hitung total ukuran file yang TERSISA
            query_size = "SELECT SUM(size) as total FROM file_index WHERE target_directory = ? AND mtime >= ?"
            result = cursor.execute(query_size, (target_path, age_cutoff)).fetchone()
            total_size_of_new_files = result['total'] if result['total'] is not None else 0
            
            # 3. Cek apakah ukuran sisa masih ngelewatin batas
            print(f"Langkah 2: Cek ukuran. Sisa: {total_size_of_new_files / 1024**3:.2f} GB. Target: {max_bytes / 1024**3:.2f} GB.")
            if total_size_of_new_files <= max_bytes:
                print(f"Aman! Ukuran sisa sudah di bawah target. Selesai."); return

            size_to_remove = total_size_of_new_files - max_bytes
            print(f"Langkah 3: Ukuran terlampaui. Perlu menghapus {size_to_remove / 1024**3:.2f} GB lagi...")
            
            # 4. Ambil file (yang baru) urut dari yang PALING TUA, lalu hapus
            query_oldest_new_files = "SELECT path, size FROM file_index WHERE target_directory = ? AND mtime >= ? ORDER BY mtime ASC"
            for row in cursor.execute(query_oldest_new_files, (target_path, age_cutoff)):
                if size_to_remove <= 0: break
                if safe_remove_file(row['path'], "terlama untuk mengurangi ukuran", dry_run):
                    size_to_remove -= row['size']; files_removed_by_size += 1
            
            print(f"Selesai. Total dihapus: {files_removed_by_age} (karena tua) + {files_removed_by_size} (karena ukuran).")
    except sqlite3.Error as e:
        print(f"ERROR: Gagal membaca indeks: {e}")

def cleanup_directory_by_age(target_path, max_days, dry_run=False):
    """Logika cleanup 'by age' TAPI pake database indeks."""
    print("Metode 'age': Membersihkan menggunakan indeks.")
    now = time.time(); cutoff_time = now - (max_days * SECONDS_IN_A_DAY)
    removed_files_count = 0
    try:
        with sqlite3.connect(INDEX_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row; cursor = conn.cursor()
            query = "SELECT path FROM file_index WHERE target_directory = ? AND mtime < ?"
            for row in cursor.execute(query, (target_path, cutoff_time)):
                if safe_remove_file(row['path'], f"lebih tua dari {max_days} hari", dry_run):
                    removed_files_count += 1
            print(f"{'DRY-RUN selesai' if dry_run else 'Selesai'}. Menghapus {removed_files_count} file.")
    except sqlite3.Error as e:
        print(f"ERROR: Gagal membaca indeks: {e}")

# --- FUNGSI UTAMA (Main) ---
def main():
    parser = argparse.ArgumentParser(description="Script pembersihan direktori (dari indeks)")
    parser.add_argument('--config', default='/opt/cleanup/config.yaml', help='Path ke config.yaml')
    args = parser.parse_args()

    try:
        config = load_config_from_yaml(args.config)
    except Exception as e:
        print(f"FATAL: Gagal memuat file konfigurasi {args.config}: {e}"); return

    directories_to_process = config.get('directories', [])
    if not directories_to_process:
        print("Tidak ada direktori yang dikonfigurasi. Keluar."); return

    print(f"--- Memulai Sesi Pembersihan (Menggunakan Indeks) ---")
    
    for dir_config in directories_to_process:
        target_path = dir_config.get('target_directory')
        if not target_path:
            print(f"WARN: Melewatkan. 'target_directory' tidak ada di config."); continue
        
        # Safety Guard (Dobel Cek)
        abs_target_path = os.path.abspath(target_path)
        if any(abs_target_path == p for p in PROTECTED_PATHS):
            print(f"FATAL: Target '{target_path}' (ke {abs_target_path}) adalah path sistem terlarang. Batal total!"); sys.exit(1)
            
        monitor_method = dir_config.get('monitor_method', 'size')
        is_dry_run = not dir_config.get('remove', False)
        max_file_age_days = dir_config.get('max_file_age_days', 30)
        max_size_gb = dir_config.get('max_size_bytes', 400 * 1024**3)
        
        print(f"\n=== Memproses {target_path} | Metode: {monitor_method} | DRY-RUN: {is_dry_run} ===")

        if monitor_method == 'size':
            cleanup_directory_by_size(
                target_path, max_size_gb, max_file_age_days, is_dry_run
            )
        elif monitor_method == 'age':
            cleanup_directory_by_age(
                target_path, max_file_age_days, is_dry_run
            )
        else:
            print(f"ERROR: 'monitor_method' tidak valid ('{monitor_method}') untuk {target_path}.")
            
    print(f"--- Sesi Pembersihan Selesai ---")

if __name__ == "__main__":
    main()