#
# SCRIPT: cleanup.py
# TASK: Reads the database index and deletes files (by age / by size).
#
import os
import sqlite3
import time
import yaml
import argparse
import sys
import shutil
import stat
import logging
import fcntl # For PID locking

# --- GLOBAL CONFIGURATION ---
PROTECTED_PATHS_ABS = []
SECONDS_IN_A_DAY = 86400
PID_FILE_PATH = "/run/cleanupd/cleaner.pid"

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
                        configs.append(dir_conf) # Hanya perlu datanya
                    else:
                        log.warning(f"Config file '{f.name}' skipped: invalid format or no 'target_directory'.")
            except Exception as e:
                log.error(f"Failed to load config file '{f.name}': {e}")
    return configs

# --- HELPER FUNCTIONS (File Removal & History) ---
def safe_remove_file(path, reason, dry_run=False):
    """Safe wrapper for os.remove (deletes a single file)."""
    if dry_run:
        log.debug(f"[DRY-RUN] Would remove: {path} (Reason: {reason})")
        return True
    try:
        os.remove(path)
        log.debug(f"Removed: {path} (Reason: {reason})")
        return True
    except FileNotFoundError:
        log.warning(f"Wanted to remove {path} but it was already gone.")
        return True
    except Exception as e:
        log.error(f"Failed to remove {path}: {e}")
        return False

def remove_paths_from_index(db_path: str, paths_to_remove: list):
    """Removes a list of paths from the file_index table."""
    if not paths_to_remove:
        return
    log.info(f"Syncing index: Removing {len(paths_to_remove)} deleted file(s) from database...")
    try:
        with sqlite3.connect(db_path, timeout=300) as conn:
            cursor = conn.cursor()
            cursor.executemany("DELETE FROM file_index WHERE path = ?", [(p,) for p in paths_to_remove])
            conn.commit()
            log.info("Database sync complete.")
    except sqlite3.Error as e:
        log.error(f"Failed to remove paths from index (DB may be out of sync!): {e}")

def save_history_log(db_path: str, target_path: str, summary: dict):
    """Saves the aggregate summary to the cleanup_history table."""
    log.info(f"Saving aggregate summary to history: {summary.get('message')}")
    try:
        with sqlite3.connect(db_path, timeout=300) as conn:
            conn.execute("""
                INSERT INTO cleanup_history (target_directory, status, files_removed_by_age, files_removed_by_size, bytes_removed_total, message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                target_path,
                summary.get('status', 'failed'),
                summary.get('files_removed_by_age', 0),
                summary.get('files_removed_by_size', 0),
                summary.get('bytes_removed_total', 0),
                summary.get('message', 'No message.')
            ))
            conn.commit()
    except sqlite3.Error as e:
        log.error(f"Failed to save history summary for {target_path}: {e}")

# --- MAIN CLEANUP FUNCTIONS ---

def cleanup_directory_by_size(db_path: str, target_path: str, max_bytes: int, max_file_age_days: int, dry_run=False) -> dict:
    log.info("Method 'size': Cleaning using index.")
    now = time.time(); age_cutoff = now - (max_file_age_days * SECONDS_IN_A_DAY)
    
    files_removed_by_age = 0
    files_removed_by_size = 0
    total_bytes_removed_by_age = 0
    total_bytes_removed_by_size = 0
    total_size_of_new_files = 0
    paths_to_remove_from_db = []

    try:
        with sqlite3.connect(db_path, timeout=300) as conn:
            conn.row_factory = sqlite3.Row; cursor = conn.cursor()

            log.info(f"Step 1: Deleting files older than {max_file_age_days} days...")
            query_old = "SELECT path, size FROM file_index WHERE target_directory = ? AND mtime < ?"
            for row in cursor.execute(query_old, (target_path, age_cutoff)):
                if safe_remove_file(row['path'], f"older than {max_file_age_days} days", dry_run):
                    files_removed_by_age += 1
                    total_bytes_removed_by_age += row['size']
                    paths_to_remove_from_db.append(row['path'])
            
            query_size = "SELECT SUM(size) as total FROM file_index WHERE target_directory = ? AND mtime >= ?"
            result = cursor.execute(query_size, (target_path, age_cutoff)).fetchone()
            total_size_of_new_files = result['total'] if result['total'] is not None else 0
            
            log.info(f"Step 2: Checking size. Remaining: {total_size_of_new_files / 1024**3:.2f} GB. Target: {max_bytes / 1024**3:.2f} GB.")
            if total_size_of_new_files <= max_bytes:
                msg = f"OK! Size is below target. Removed {files_removed_by_age} file(s) by age."
                log.info(msg)
                if not dry_run:
                    remove_paths_from_index(db_path, paths_to_remove_from_db)
                return {
                    "status": "success" if not dry_run else "dry_run",
                    "files_removed_by_age": files_removed_by_age,
                    "files_removed_by_size": 0,
                    "bytes_removed_total": total_bytes_removed_by_age,
                    "message": msg
                }

            size_to_remove = total_size_of_new_files - max_bytes
            log.info(f"Step 3: Size limit exceeded. Need to remove {size_to_remove / 1024**3:.2f} GB more...")
            
            query_oldest_new_files = "SELECT path, size FROM file_index WHERE target_directory = ? AND mtime >= ? ORDER BY mtime ASC"
            for row in cursor.execute(query_oldest_new_files, (target_path, age_cutoff)):
                if size_to_remove <= 0: break
                if safe_remove_file(row['path'], "oldest to reduce size", dry_run):
                    size_to_remove -= row['size']
                    files_removed_by_size += 1
                    total_bytes_removed_by_size += row['size']
                    paths_to_remove_from_db.append(row['path'])
            
            msg = f"Finished. Removed {files_removed_by_age} (age) + {files_removed_by_size} (size) files."
            log.info(msg)
            if not dry_run:
                remove_paths_from_index(db_path, paths_to_remove_from_db)
            
            return {
                "status": "success" if not dry_run else "dry_run",
                "files_removed_by_age": files_removed_by_age,
                "files_removed_by_size": files_removed_by_size,
                "bytes_removed_total": total_bytes_removed_by_age + total_bytes_removed_by_size,
                "message": msg
            }

    except sqlite3.Error as e:
        log.error(f"Failed to read index (possibly locked): {e}")
        return {"status": "failed", "message": f"Failed to read index: {e}"}

def cleanup_directory_by_age(db_path: str, target_path: str, max_days: int, dry_run=False) -> dict:
    log.info("Method 'age': Cleaning using index.")
    now = time.time(); cutoff_time = now - (max_days * SECONDS_IN_A_DAY)
    removed_files_count = 0
    total_bytes_removed = 0
    paths_to_remove_from_db = []
    
    try:
        with sqlite3.connect(db_path, timeout=300) as conn:
            conn.row_factory = sqlite3.Row; cursor = conn.cursor()
            query = "SELECT path, size FROM file_index WHERE target_directory = ? AND mtime < ?"
            for row in cursor.execute(query, (target_path, cutoff_time)):
                if safe_remove_file(row['path'], f"older than {max_days} days", dry_run):
                    removed_files_count += 1
                    total_bytes_removed += row['size']
                    paths_to_remove_from_db.append(row['path'])
            
            msg = f"Removed {removed_files_count} files."
            log.info(f"{'DRY-RUN complete' if dry_run else 'Finished'}. {msg}")
            
            if not dry_run:
                remove_paths_from_index(db_path, paths_to_remove_from_db)
            
            return {
                "status": "success" if not dry_run else "dry_run",
                "files_removed_by_age": removed_files_count,
                "files_removed_by_size": 0,
                "bytes_removed_total": total_bytes_removed,
                "message": msg
            }
    except sqlite3.Error as e:
        log.error(f"Failed to read index (possibly locked): {e}")
        return {"status": "failed", "message": f"Failed to read index: {e}"}

# --- MAIN FUNCTION (Refactored) ---
def run_main_logic(config: dict, db_path: str):
    # BARU: Memuat pekerjaan dari direktori config
    directories_to_process = load_directory_configs(config)
    
    if not directories_to_process:
        log.info("No directories configured in 'directories.d'. Exiting."); return

    log.info(f"--- Starting Cleanup Session (Using Index) ---")
    
    for dir_config in directories_to_process:
        target_path = dir_config.get('target_directory')
        if not target_path:
            log.warning(f"Skipping. 'target_directory' not in config entry."); continue
        
        if is_path_protected(target_path):
            log.error(f"SKIPPING: Target '{target_path}' is in or IS a forbidden system path. Will not process."); continue
            
        monitor_method = dir_config.get('monitor_method', 'size')
        is_dry_run = not dir_config.get('remove', False)
        max_file_age_days = dir_config.get('max_file_age_days', 30)
        max_size_bytes = dir_config.get('max_size_bytes', 400 * 1024**3)
        
        log.info(f"=== Processing {target_path} | Method: {monitor_method} | DRY-RUN: {is_dry_run} ===")

        summary = {}
        try:
            if monitor_method == 'size':
                summary = cleanup_directory_by_size(
                    db_path, target_path, max_size_bytes, max_file_age_days, is_dry_run
                )
            elif monitor_method == 'age':
                summary = cleanup_directory_by_age(
                    db_path, target_path, max_file_age_days, is_dry_run
                )
            else:
                log.error(f"Invalid 'monitor_method' ('{monitor_method}') for {target_path}.")
                summary = {"status": "failed", "message": f"Invalid monitor_method: {monitor_method}"}
        except Exception as e:
            log.error(f"Unhandled error during cleanup for {target_path}: {e}", exc_info=True)
            summary = {"status": "failed", "message": f"Unhandled error: {e}"}

        save_history_log(db_path, target_path, summary)
            
    log.info(f"--- Cleanup Session Finished ---")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Directory cleanup script (from index)")
    parser.add_argument('--config', default='config.yaml', help='Path to config.yaml')
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    config = load_config_from_yaml(args.config)
    
    G_SETTINGS = config.get('global_settings', {})
    log_level = G_SETTINGS.get('log_level', 'INFO')
    db_path = os.path.abspath(G_SETTINGS.get('db_path', 'cleanup_index.db'))
    PID_FILE_PATH = G_SETTINGS.get('pid_paths', {}).get('cleaner', '/run/cleanupd/cleaner.pid')
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