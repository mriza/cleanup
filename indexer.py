#
# SCRIPT: indexer.py
# TASK: Scans the disk and builds a file database (index).
#
import os
import sqlite3
import sys
import time
import yaml
import argparse
import shutil
import stat
import logging
import fcntl # For PID locking

# --- GLOBAL CONFIGURATION ---
PID_FILE_PATH = "/run/cleanupd/indexer.pid" # Default, akan di-override
PROTECTED_PATHS_ABS = [] # Default, akan di-override

# Logger (will be configured in main)
log = logging.getLogger(os.path.basename(__file__))

# --- HELPER FUNCTIONS (General) ---

def load_config_from_yaml(path):
    """Safely loads a YAML file using a shared read lock."""
    try:
        with open(path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            config_data = yaml.safe_load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
            return config_data
    except Exception as e:
        sys.stderr.write(f"FATAL: Error loading config {path} before logging setup: {e}\n")
        sys.exit(1)

def setup_logging(level_name: str):
    """Configures the global logger based on config."""
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        level = logging.INFO
        
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    log.setLevel(level)

def is_path_protected(target_path: str) -> bool:
    """Checks if target_path is OR is INSIDE a protected path (dari config)."""
    try:
        abs_target_path = os.path.normpath(os.path.abspath(target_path))
    except ValueError: return True 

    for protected in PROTECTED_PATHS_ABS:
        if protected == '/' and abs_target_path == '/': return True
        if protected != '/' and (abs_target_path == protected or abs_target_path.startswith(protected + os.sep)):
            return True
    return False

# --- HELPER (BARU): Memuat Konfigurasi Direktori ---
def load_directory_configs(config: dict) -> List[Dict[str, Any]]:
    """
    Memindai 'directories_config_path' dari config global
    dan memuat semua file .yaml di dalamnya.
    """
    g_settings = config.get('global_settings', {})
    dir_path = g_settings.get('directories_config_path', 'directories.d')
    
    if not os.path.isdir(dir_path):
        log.warning(f"Configuration directory '{dir_path}' not found.")
        return []

    configs = []
    for f in os.scandir(dir_path):
        if f.is_file() and f.name.endswith(('.yaml', '.yml')):
            try:
                with open(f.path, 'r') as file:
                    dir_conf = yaml.safe_load(file)
                    if dir_conf and dir_conf.get('target_directory'):
                        configs.append(dir_conf) # Hanya perlu datanya, bukan ID file
                    else:
                        log.warning(f"Config file '{f.name}' skipped: invalid format or no 'target_directory'.")
            except Exception as e:
                log.error(f"Failed to load config file '{f.name}': {e}")
    return configs

# --- HELPER FUNCTIONS (Directory Removal) ---
def handle_rmtree_permission_error(func, path, exc_info):
    if not os.access(path, os.W_OK):
        try:
            os.chmod(path, stat.S_IWUSR); func(path)
        except Exception as e:
            log.error(f"Failed to change permissions & remove {path}: {e}"); raise
    else:
        log.error(f"Failed to remove {path} (not a permission issue): {exc_info}"); raise

def safe_remove_dir(path, reason, dry_run=False):
    if dry_run:
        log.info(f"[DRY-RUN] Would remove directory: {path} (Reason: {reason})")
        return True
    try:
        shutil.rmtree(path, onerror=handle_rmtree_permission_error)
        log.info(f"Removed directory: {path} (Reason: {reason})")
        return True
    except Exception as e:
        log.error(f"Failed to remove directory {path}: {e}"); return False

def remove_deep_directories(root_path, max_depth, dry_run=False):
    if max_depth is None: return 0
    removed_dirs_count = 0
    log.info(f"Checking for directories exceeding depth {max_depth}...")
    for dirpath, dirnames, _ in os.walk(root_path, topdown=True):
        rel_path = os.path.relpath(dirpath, root_path)
        depth = 0 if rel_path == '.' else rel_path.count(os.sep) + 1
        
        if depth > max_depth:
            reason = f"exceeds max_depth ({max_depth})"
            if safe_remove_dir(dirpath, reason, dry_run):
                removed_dirs_count += 1
            dirnames[:] = [] 
            
    if removed_dirs_count > 0:
        log.info(f"Total directories removed (due to depth): {removed_dirs_count}")
    return removed_dirs_count

# --- HELPER FUNCTIONS (File Iterators) ---
def iterate_root_files_only(path):
    log.info(f"Mode: Root files only (non-recursive).")
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                try:
                    file_stat = entry.stat()
                    yield (file_stat.st_mtime, file_stat.st_size, entry.path)
                except (OSError, FileNotFoundError) as e:
                    log.warning(f"Could not stat file {entry.path}: {e}")
    except (OSError, FileNotFoundError) as e:
        log.error(f"Could not scan directory {path}: {e}")

def iterate_recursive_files_with_depth(path, max_depth):
    log.info(f"Mode: Recursive up to depth {max_depth}.")
    for dirpath, dirnames, filenames in os.walk(path, topdown=True):
        rel_path = os.path.relpath(dirpath, path)
        depth = 0 if rel_path == '.' else rel_path.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []; continue
        for f in filenames:
            try:
                full_path = os.path.join(dirpath, f)
                file_stat = os.stat(full_path)
                yield (file_stat.st_mtime, file_stat.st_size, full_path)
            except (OSError, FileNotFoundError) as e:
                log.warning(f"Could not stat file {full_path}: {e}")
                continue

def get_file_iterator(path, max_depth):
    if max_depth is None:
        return iterate_root_files_only(path)
    else:
        return iterate_recursive_files_with_depth(path, max_depth)

# --- HELPER FUNCTIONS (Database Indexing) ---
def initialize_database(db_path):
    log.info(f"Ensuring database index exists at {os.path.abspath(db_path)}...")
    with sqlite3.connect(db_path, timeout=300) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            log.info("Database journal mode set to WAL.")
        except Exception as e:
            log.error(f"Could not set WAL mode: {e}")
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_index (
            target_directory TEXT,
            path TEXT PRIMARY KEY,
            mtime REAL,
            size INTEGER
        )
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS cleanup_history (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            target_directory TEXT,
            status TEXT,
            files_removed_by_age INTEGER,
            files_removed_by_size INTEGER,
            bytes_removed_total INTEGER,
            message TEXT
        )
        """)
        conn.commit()

def run_indexing(target_path, max_depth, db_path):
    log.info(f"Starting indexing for: {target_path}")
    start_time = time.time()
    file_iterator = get_file_iterator(target_path, max_depth)
    
    def file_data_generator():
        for mtime, size, path in file_iterator:
            yield (target_path, path, mtime, size)
    try:
        with sqlite3.connect(db_path, timeout=300) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM file_index WHERE target_directory = ?", (target_path,))
            cursor.executemany("""
                INSERT INTO file_index (target_directory, path, mtime, size)
                VALUES (?, ?, ?, ?)
            """, file_data_generator())
            conn.commit()
    except sqlite3.Error as e:
        log.critical(f"Failed to perform database indexing (possibly locked): {e}"); return False
    except Exception as e:
        log.critical(f"Failed while scanning files: {e}"); return False
    end_time = time.time()
    log.info(f"Indexing finished in {end_time - start_time:.2f} seconds.")
    return True

# --- MAIN FUNCTION (Refactored) ---
def run_main_logic(config: dict, db_path: str):
    """The actual main function, called after lock is acquired."""
    # BARU: Memuat pekerjaan dari direktori config
    directories_to_process = load_directory_configs(config)
    
    if not directories_to_process:
        log.info("No directories configured in 'directories.d'. Exiting."); return

    log.info(f"--- Starting Indexing Session ---")
    for dir_config in directories_to_process:
        target_path = dir_config.get('target_directory')
        if not target_path:
            log.warning("Skipping entry with no 'target_directory'."); continue
        try:
            if not os.path.isdir(target_path):
                log.warning(f"Skipping. 'target_directory' is not a valid directory: {target_path}"); continue
        except Exception as e:
            log.warning(f"Skipping. Could not access 'target_directory' {target_path}: {e}"); continue
            
        if is_path_protected(target_path):
            log.error(f"SKIPPING: Target '{target_path}' is in or IS a forbidden system path. Will not process."); continue
            
        max_depth = dir_config.get('max_depth', None) 
        is_dry_run = not dir_config.get('remove', False)

        log.info(f"== Indexing {target_path} (Max-Depth: {max_depth} | Dry-Run: {is_dry_run}) ==")
        
        remove_deep_directories(target_path, max_depth, is_dry_run)
        success = run_indexing(target_path, max_depth, db_path)
        if not success:
            log.error(f"Failed to index {target_path}.")
            
    log.info(f"--- Indexing Session Finished ---")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="File indexing script for cleanup service")
    parser.add_argument('--config', default='config.yaml', help='Path to config.yaml')
    args = parser.parse_args()
    
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    config = load_config_from_yaml(args.config)
    
    G_SETTINGS = config.get('global_settings', {})
    log_level = G_SETTINGS.get('log_level', 'INFO')
    db_path = os.path.abspath(G_SETTINGS.get('db_path', 'cleanup_index.db'))
    PID_FILE_PATH = G_SETTINGS.get('pid_paths', {}).get('indexer', '/run/cleanupd/indexer.pid')
    PROTECTED_PATHS_ABS = [os.path.normpath(os.path.abspath(p)) for p in G_SETTINGS.get('protected_paths', ['/'])]
    
    setup_logging(log_level)

    # --- PID LOCK ---
    try:
        os.makedirs(os.path.dirname(PID_FILE_PATH), 0o755, exist_ok=True)
        lock_f = open(PID_FILE_PATH, 'w')
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log.info(f"Lock acquired ({PID_FILE_PATH}). Starting.")
        lock_f.write(str(os.getpid()))
        lock_f.flush()
    except (IOError, BlockingIOError):
        log.warning(f"Another instance is already running. Lock file {PID_FILE_PATH} is held. Exiting.")
        sys.exit(0)
    except Exception as e:
        log.critical(f"Failed to create PID lock {PID_FILE_PATH}: {e}")
        sys.exit(1)

    # --- MAIN EXECUTION ---
    try:
        initialize_database(db_path)
        run_main_logic(config, db_path)
    except Exception as e:
        log.critical(f"An unhandled error occurred: {e}", exc_info=True)
    finally:
        log.info("Releasing lock and exiting.")
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()
        try:
            os.remove(PID_FILE_PATH)
        except:
            pass