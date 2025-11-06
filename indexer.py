#
# SCRIPT: indexer.py
# TUGAS: Memindai disk dan membangun database (indeks).
#
import os
import sqlite3
import sys
import time
import yaml
import argparse
import shutil
import stat

# --- KONFIGURASI GLOBAL ---
INDEX_DB_PATH = "cleanup_index.db"
PROTECTED_PATHS = [
    '/', '/etc', '/usr', '/var', '/lib', '/sbin', '/bin', '/root',
    '/boot', '/dev', '/proc', '/sys', '/run'
]

# --- FUNGSI HELPER (Umum) ---
def load_config_from_yaml(path):
    """Biasa, cuma baca file config YAML."""
    with open(path, "r") as f:
        return yaml.safe_load(f)

# --- FUNGSI HELPER (Penghapusan Direktori) ---
def handle_rmtree_permission_error(func, path, exc_info):
    """Callback buat shutil.rmtree."""
    if not os.access(path, os.W_OK):
        try:
            os.chmod(path, stat.S_IWUSR); func(path)
        except Exception as e:
            print(f"ERROR: Gagal ubah izin & hapus {path}: {e}"); raise
    else:
        print(f"ERROR: Gagal hapus {path} (bukan masalah izin): {exc_info}"); raise

def safe_remove_dir(path, reason, dry_run=False):
    """Wrapper aman buat shutil.rmtree (hapus direktori rekursif)."""
    if dry_run:
        print(f"[DRY-RUN] Akan menghapus direktori: {path} (Alasan: {reason})")
        return True
    try:
        shutil.rmtree(path, onerror=handle_rmtree_permission_error)
        print(f"Removed directory: {path} (Alasan: {reason})")
        return True
    except Exception as e:
        print(f"ERROR: Gagal menghapus direktori {path}: {e}"); return False

def remove_deep_directories(root_path, max_depth, dry_run=False):
    """Hapus semua direktori yang lebih dalam dari max_depth."""
    if max_depth is None: return 0
    removed_dirs_count = 0
    print(f"Memeriksa direktori yang melebihi kedalaman {max_depth}...")
    for dirpath, dirnames, _ in os.walk(root_path, topdown=True):
        rel_path = os.path.relpath(dirpath, root_path)
        depth = 0 if rel_path == '.' else rel_path.count(os.sep) + 1
        
        if depth > max_depth:
            reason = f"melebihi max_depth ({max_depth})"
            if safe_remove_dir(dirpath, reason, dry_run):
                removed_dirs_count += 1
            # Pangkas pohonnya, jangan masuk lebih dalam
            dirnames[:] = [] 
            
    if removed_dirs_count > 0:
        print(f"Total direktori yang dihapus karena melebihi max_depth: {removed_dirs_count}")
    return removed_dirs_count

# --- FUNGSI HELPER (Iterator File) ---
def iterate_root_files_only(path):
    """Logika max_depth = None: Hanya FILE di root, non-rekursif."""
    print(f"Mode: Hanya file di root (non-rekursif).")
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                try:
                    file_stat = entry.stat()
                    yield (file_stat.st_mtime, file_stat.st_size, entry.path)
                except (OSError, FileNotFoundError) as e:
                    print(f"WARN: Gak bisa scan stat {entry.path}: {e}")
    except (OSError, FileNotFoundError) as e:
        print(f"ERROR: Gak bisa scan direktori {path}: {e}")

def iterate_recursive_files_with_depth(path, max_depth):
    """Logika max_depth = N: Rekursif TAPI berhenti kalau lebih dalam dari N."""
    print(f"Mode: Rekursif hingga kedalaman {max_depth}.")
    for dirpath, dirnames, filenames in os.walk(path, topdown=True):
        rel_path = os.path.relpath(dirpath, path)
        depth = 0 if rel_path == '.' else rel_path.count(os.sep) + 1

        if depth > max_depth:
            dirnames[:] = []; continue # Pangkas di sini
        
        for f in filenames:
            try:
                full_path = os.path.join(dirpath, f)
                file_stat = os.stat(full_path)
                yield (file_stat.st_mtime, file_stat.st_size, full_path)
            except (OSError, FileNotFoundError) as e:
                print(f"WARN: Gak bisa scan stat {full_path}: {e}")
                continue

def get_file_iterator(path, max_depth):
    """Pabrik' yang milih iterator berdasarkan max_depth."""
    if max_depth is None:
        return iterate_root_files_only(path)
    else:
        return iterate_recursive_files_with_depth(path, max_depth)

# --- FUNGSI HELPER (Database Indexing) ---
def initialize_database(db_path):
    """Cek DB & bikinin tabel kalo belum ada."""
    print(f"Memastikan database indeks ada di {os.path.abspath(db_path)}...")
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_index (
            target_directory TEXT,
            path TEXT PRIMARY KEY,
            mtime REAL,
            size INTEGER
        )
        """)
        conn.commit()

def run_indexing(target_path, max_depth, db_path):
    """Inti skrip: Pindai disk -> Masukkan ke DB."""
    print(f"Memulai pengindeksan untuk: {target_path}")
    start_time = time.time()
    
    file_iterator = get_file_iterator(target_path, max_depth)
    
    def file_data_generator():
        for mtime, size, path in file_iterator:
            yield (target_path, path, mtime, size)

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            # 1. Bersihkan data LAMA
            cursor.execute("DELETE FROM file_index WHERE target_directory = ?", (target_path,))
            # 2. Masukkan data BARU
            cursor.executemany("""
                INSERT INTO file_index (target_directory, path, mtime, size)
                VALUES (?, ?, ?, ?)
            """, file_data_generator())
            conn.commit()
    except sqlite3.Error as e:
        print(f"FATAL: Gagal melakukan pengindeksan database: {e}"); conn.rollback(); return False
    except Exception as e:
        print(f"FATAL: Gagal saat memindai file: {e}"); return False

    end_time = time.time()
    print(f"Pengindeksan selesai dalam {end_time - start_time:.2f} detik.")
    return True

# --- FUNGSI UTAMA (Main) ---
def main():
    parser = argparse.ArgumentParser(description="Script pengindeksan file untuk cleanup")
    parser.add_argument('--config', default='/opt/cleanup/config.yaml', help='Path ke config.yaml')
    args = parser.parse_args()

    try:
        config = load_config_from_yaml(args.config)
    except Exception as e:
        print(f"FATAL: Gagal memuat file konfigurasi {args.config}: {e}"); return

    try:
        initialize_database(INDEX_DB_PATH)
    except Exception as e:
        print(f"FATAL: Gagal inisialisasi database di {INDEX_DB_PATH}: {e}"); return

    directories_to_process = config.get('directories', [])
    if not directories_to_process:
        print("Tidak ada direktori yang dikonfigurasi. Keluar."); return

    print(f"--- Memulai Sesi Pengindeksan ---")
    for dir_config in directories_to_process:
        target_path = dir_config.get('target_directory')
        if not target_path or not os.path.isdir(target_path):
            print(f"WARN: Melewatkan. 'target_directory' tidak valid: {target_path}"); continue
            
        # Safety Guard
        abs_target_path = os.path.abspath(target_path)
        if any(abs_target_path == p for p in PROTECTED_PATHS):
            print(f"FATAL: Target '{target_path}' (ke {abs_target_path}) adalah path sistem terlarang. Batal total!"); sys.exit(1)
            
        max_depth = dir_config.get('max_depth', None) 
        is_dry_run = not dir_config.get('remove', False)

        print(f"\n== Mengindeks {target_path} (Max-Depth: {max_depth} | Dry-Run: {is_dry_run}) ==")
        
        # TAHAP 1A: Hapus direktori > max_depth
        remove_deep_directories(target_path, max_depth, is_dry_run)

        # TAHAP 1B: Bangun indeks
        success = run_indexing(target_path, max_depth, INDEX_DB_PATH)
        if not success:
            print(f"Gagal mengindeks {target_path}.")
            
    print(f"--- Sesi Pengindeksan Selesai ---")

if __name__ == "__main__":
    main()