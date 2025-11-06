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
PROTECTED_PATHS_ABS = [os.path.abspath(p) for p in [
    '/', '/etc', '/usr', '/var', '/lib', '/sbin', '/bin', '/root',
    '/boot', '/dev', '/proc', '/sys', '/run'
]]
PID_FILE_PATH = "/run/cleanupd/indexer.pid"

# Logger (will be configured in main)
log = logging.getLogger(os.path.basename(__file__))

# --- HELPER FUNCTIONS (General) ---

def load_config_from_yaml(path):
    """Safely loads a YAML file using a shared read lock."""
    try:
        with open(path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH) # Shared Lock (Read)
            config_data = yaml.safe_load(f)
            fcntl.flock(f, fcntl.LOCK_UN) # Release
            return config_data
    except Exception as e:
        print(f"FATAL: Error loading config {path} before logging setup: {e}", file=sys.stderr)
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
    """Checks if target_path is INSIDE a protected path."""
    abs_target_path = os.path.abspath(target_path)
    for protected in PROTECTED_PATHS_ABS:
        if os.path.commonpath([abs_target_path, protected]) == protected:
            return True
    return False

# --- HELPER FUNCTIONS (Directory Removal) ---
def handle_rmtree_permission_error(func, path, exc_info):
    """Callback for shutil.rmtree on permission errors."""
    if not os.access(path, os.W_OK):
        try:
            os.chmod(path, stat.S_IWUSR); func(path)
        except Exception as e:
            log.error(f"Failed to change permissions & remove {path}: {e}"); raise
    else:
        log.error(f"Failed to remove {path} (not a permission issue): {exc_info}"); raise

def safe_remove_dir(path, reason, dry_run=False):
    """Safe wrapper for shutil.rmtree."""
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
    """Deletes all directories deeper than max_depth."""
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
    """Logic for max_depth = None: Only FILES in the root, non-recursive."""
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
    """Logic for max_depth = N: Recursive BUT stops if deeper than N."""
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
    """'Factory' function that selects the correct iterator based on max_depth."""
    if max_depth is None:
        return iterate_root_files_only(path)
    else:
        return iterate_recursive_files_with_depth(path, max_depth)

# --- HELPER FUNCTIONS (Database Indexing) ---
def initialize_database(db_path):
    """
    Check if DB exists, create tables (file_index AND cleanup_history),
    and SET WAL MODE.
    """
    log.info(f"Ensuring database index exists at {os.path.abspath(db_path)}...")
    
    with sqlite3.connect(db_path, timeout=300) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            log.info("Database journal mode set to WAL.")
        except Exception as e:
            log.error(f"Could not set WAL mode: {e}")
        
        # Table 1: File Index
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_index (
            target_directory TEXT,
            path TEXT PRIMARY KEY,
            mtime REAL,
            size INTEGER
        )
        """)
        
        # Table 2: Aggregate History Log
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS cleanup_history (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            target_directory TEXT,
            status TEXT, -- 'success', 'dry_run', 'failed'
            files_removed_by_age INTEGER,
            files_removed_by_size INTEGER,
            bytes_removed_total INTEGER,
            message TEXT -- For summary log or error message
        )
        """)
        
        conn.commit()

def run_indexing(target_path, max_depth, db_path):
    """Core script: Scan disk -> Insert into DB."""
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
    directories_to_process = config.get('directories', [])
    if not directories_to_process:
        log.info("No directories configured. Exiting."); return

    log.info(f"--- Starting Indexing Session ---")
    for dir_config in directories_to_process:
        target_path = dir_config.get('target_directory')
        if not target_path or not os.path.isdir(target_path):
            log.warning(f"Skipping. 'target_directory' is invalid: {target_path}"); continue
            
        if is_path_protected(target_path):
            log.critical(f"FATAL: Target '{target_path}' is in or IS a forbidden system path. Halting!"); sys.exit(1)
            
        max_depth = dir_config.get('max_depth', None) 
        is_dry_run = not dir_config.get('remove', False)

        log.info(f"== Indexing {target_path} (Max-Depth: {max_depth} | Dry-Run: {is_dry_run}) ==")
        
        remove_deep_directories(target_path, max_depth, is_dry_run)
        success = run_indexing(target_path, max_depth, db_path)
        if not success:
            log.error(f"Failed to index {target_path}.")
            
    log.info(f"--- Indexing Session Finished ---")

if __name__ == "__main__":
    # --- PRE-MAIN: Load Config for Logging/DB Path ---
    parser = argparse.ArgumentParser(description="File indexing script for cleanup service")
    parser.add_argument('--config', default='/opt/cleanup/config.yaml', help='Path to config.yaml')
    args = parser.parse_args()

    config = load_config_from_yaml(args.config)
    
    log_level = config.get('log_level', 'INFO')
    db_path = config.get('db_path', '/opt/cleanup/cleanup_index.db')
    setup_logging(log_level)

    # --- PID LOCK ---
    try:
        lock_f = open(PID_FILE_PATH, 'w')
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log.info(f"Lock acquired ({PID_FILE_PATH}). Starting.")
        lock_f.write(str(os.getpid()))
        lock_f.flush()
    except (IOError, BlockingIOError):
        log.warning(f"Another instance is already running. Lock file {PID_FILE_PATH} is held. Exiting.")
        sys.exit(0)

    # --- MAIN EXECUTION ---
    try:
        initialize_database(db_path)
        run_main_logic(config, db_path)
    except Exception as e:
        log.critical(f"An unhandled error occurred: {e}", exc_info=True)
    finally:
        # Release the lock
        log.info("Releasing lock and exiting.")
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()
        try:
            os.remove(PID_FILE_PATH)
        except:
            pass