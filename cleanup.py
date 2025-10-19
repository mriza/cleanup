import os
import heapq
import time
import yaml
import argparse
import shutil
import stat

def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def rmtree_onerror(func, path, exc_info):
    """Handler yang dipanggil oleh shutil.rmtree jika terjadi error."""
    if not os.access(path, os.W_OK):
        
        try:
            os.chmod(path, stat.S_IWUSR)
            func(path)
        except Exception as e:
            
            raise
    else:
        
        raise

def remove_dir_if_exceeds_depth(root_path, max_depth, remove_flag=True):
    removed_dirs = 0
    for dirpath, dirnames, _ in os.walk(root_path, topdown=True):
        rel_path = os.path.relpath(dirpath, root_path)
        if rel_path == '.':
            depth = 0
        else:
            depth = rel_path.count(os.sep) + 1
        
        if max_depth is not None and depth > max_depth:
            if remove_flag:
                try:
                    
                    shutil.rmtree(dirpath, onerror=rmtree_onerror)
                    print(f"Removed subdirectory exceeding max_depth: {dirpath}")
                except Exception as e:
                    print(f"Failed to remove directory {dirpath}: {e}")
            else:
                print(f"[DRY-RUN] Would remove subdirectory exceeding max_depth: {dirpath}")
            
            removed_dirs += 1
            dirnames[:] = [] 
    return removed_dirs

def fast_file_generator(path):
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                full = os.path.join(dirpath, f)
                stat = os.stat(full)
                yield (stat.st_mtime, stat.st_size, full)
            except Exception:
                continue

def fast_file_generator_with_depth(path, max_depth, remove_flag):
    
    remove_dir_if_exceeds_depth(path, max_depth, remove_flag)
    for dirpath, _, filenames in os.walk(path):
        rel_path = os.path.relpath(dirpath, path)
        if rel_path == '.':
            depth = 0
        else:
            depth = rel_path.count(os.sep) + 1
        
        if max_depth is not None and depth > max_depth:
            continue  
            
        for f in filenames:
            try:
                full = os.path.join(dirpath, f)
                stat = os.stat(full)
                yield (stat.st_mtime, stat.st_size, full)
            except Exception:
                continue

def remove_oldest_files_by_size(target_path, max_bytes, max_file_age_days, remove_flag=True):
    total_size = 0
    heap = []
    now = time.time()

    for mtime, size, path in fast_file_generator(target_path):
        
        if (now - mtime) <= max_file_age_days * 86400:
            total_size += size
            heapq.heappush(heap, (mtime, size, path))
        else:
            
            if remove_flag:
                try:
                    os.remove(path)
                    print(f"Removed old file by age: {path}")
                except Exception as e:
                    print(f"Failed to remove {path}: {e}")
            else:
                print(f"[DRY-RUN] Would remove old file by age: {path}")

    removed = 0
    while total_size > max_bytes and heap:
        mtime, size, path = heapq.heappop(heap)
        if remove_flag:
            try:
                os.remove(path)
                print(f"Removed {path}")
            except Exception as e:
                print(f"Failed to remove {path}: {e}")
        else:
            print(f"[DRY-RUN] Would remove {path}")
        total_size -= size
        removed += 1

    if remove_flag:
        print(f"Done. Actually removed {removed} files in {target_path}. Remaining size: {total_size/(1024*1024*1024):.2f} GB")
    else:
        print(f"DRY-RUN complete. Would have removed {removed} files in {target_path}. Remaining size: {total_size/(1024*1024*1024):.2f} GB")

def remove_old_files_by_age(target_path, max_days, max_depth, remove_flag=True):
    now = time.time()
    cutoff = now - max_days * 86400
    removed = 0

    
    for mtime, size, path in fast_file_generator_with_depth(target_path, max_depth, remove_flag):
        if mtime < cutoff:
            if remove_flag:
                try:
                    os.remove(path)
                    print(f"Removed {path}")
                except Exception as e:
                    print(f"Failed to remove {path}: {e}")
            else:
                print(f"[DRY-RUN] Would remove {path}")
            removed += 1

    print(f"{'Done' if remove_flag else 'DRY-RUN complete'}. Removed {removed} files older than {max_days} days in {target_path}.")

def main():
    parser = argparse.ArgumentParser(description="Directory cleanup script with age and size monitor modes")
    parser.add_argument('--config', default='/opt/cleanup/config.yaml', help='Path to config.yaml')
    args = parser.parse_args()

    config = load_config(args.config)
    folders = config.get('directories', [])
    if not folders:
        print("No directories configured in config.yaml.")
        return

    for folder in folders:
        target_path = folder['target_directory']
        monitor_method = folder.get('monitor_method', 'size')
        remove_flag = folder.get('remove', False)
        max_file_age_days = folder.get('max_file_age_days', 30)
        max_size_bytes = folder.get('max_size_bytes', 400 * 1024 * 1024 * 1024)
        max_depth = folder.get('max_depth', None)

        print(f"=== Processing {target_path} with method={monitor_method}, remove={remove_flag}, max_depth={max_depth} ===")

        if monitor_method == 'size':
            
            if max_depth is not None:
                print(f"Checking for subdirectories exceeding max_depth={max_depth} (in size mode).")
                remove_dir_if_exceeds_depth(target_path, max_depth, remove_flag)
                
            
            remove_oldest_files_by_size(target_path, max_size_bytes, max_file_age_days, remove_flag)
            
        elif monitor_method == 'age':
            
            remove_old_files_by_age(target_path, max_file_age_days, max_depth, remove_flag)
            
        else:
            print(f"Invalid monitor_method '{monitor_method}' for {target_path}. Use 'size' or 'age'.")

if __name__ == "__main__":
    main()