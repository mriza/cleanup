#!/usr/bin/env python
#
# SCRIPT: configure.py
# TASK: A command-line (CLI) tool to manage config.yaml VIA THE API
#
import typer
import yaml
import os
import sys
import fcntl
import re
import httpx
from typing import Optional, List, Dict, Any
from passlib.context import CryptContext # Untuk hash-password

# --- KONFIGURASI GLOBAL ---
CONFIG_PATH = "config.yaml"
# Konteks Hashing (harus sama dengan api.py)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Inisialisasi Typer
app = typer.Typer(
    help="A CLI tool to manage the cleanup service via its API."
)

# --- FUNGSI HELPER (Aman & Terkunci) ---
def load_connection_config() -> Dict[str, Any]:
    """Hanya membaca config.yaml untuk info koneksi API (read-only, shared lock)."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            # Perbaikan: Tambahkan read lock untuk konsistensi
            fcntl.flock(f, fcntl.LOCK_SH)
            config_data = yaml.safe_load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
            return {
                "port": config_data.get('api_port', 8000),
                "user": config_data.get('api_admin_user', 'admin')
            }
    except Exception as e:
        typer.secho(f"Failed to load {CONFIG_PATH} for connection info: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

# --- FUNGSI HELPER (API Client) ---
class ApiClient:
    """Helper class untuk menangani otentikasi dan panggilan API."""
    def __init__(self, port, user):
        self.base_url = f"http://127.0.0.1:{port}"
        self.user = user
        self.token = None

    def login(self):
        """Mendapatkan token JWT dari API."""
        password = typer.prompt(f"Enter password for admin user '{self.user}'", hide_input=True)
        try:
            with httpx.Client() as client:
                response = client.post(
                    f"{self.base_url}/token",
                    data={"username": self.user, "password": password}
                )
                if response.status_code == 401:
                    typer.secho("Authentication failed. Incorrect username or password.", fg=typer.colors.RED)
                    raise typer.Exit(code=1)
                response.raise_for_status() # Gagal jika error 500, dll.
                self.token = response.json()["access_token"]
                typer.secho("Login successful, token acquired.", fg=typer.colors.GREEN, dim=True)
        except httpx.ConnectError:
            typer.secho(f"Connection Error: Could not connect to API at {self.base_url}. Is it running?", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        except Exception as e:
            typer.secho(f"An error occurred during login: {e}", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    def _get_headers(self):
        if not self.token:
            self.login()
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, endpoint):
        """Membuat panggilan GET (terotentikasi) ke API."""
        try:
            with httpx.Client() as client:
                response = client.get(f"{self.base_url}{endpoint}", headers=self._get_headers())
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            typer.secho(f"API Error: {e.response.status_code} - {e.response.json().get('detail', 'Unknown error')}", fg=typer.colors.RED)
            raise typer.Exit(1)
        except Exception as e:
            typer.secho(f"An error occurred: {e}", fg=typer.colors.RED); raise typer.Exit(1)

    def post(self, endpoint, data):
        """Membuat panggilan POST (terotentikasi) ke API."""
        try:
            with httpx.Client() as client:
                response = client.post(f"{self.base_url}{endpoint}", json=data, headers=self._get_headers())
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            typer.secho(f"API Error: {e.response.status_code} - {e.response.json().get('detail', 'Unknown error')}", fg=typer.colors.RED)
            raise typer.Exit(1)
        except Exception as e:
            typer.secho(f"An error occurred: {e}", fg=typer.colors.RED); raise typer.Exit(1)

# Inisialisasi API client
@app.callback()
def main_callback(ctx: typer.Context):
    """Callback untuk menginisialisasi state, jika diperlukan."""
    if ctx.invoked_subcommand == "hash-password":
        return
    
    conn_config = load_connection_config()
    ctx.obj = ApiClient(port=conn_config['port'], user=conn_config['user'])


# --- FUNGSI HELPER (Lama, masih dipakai oleh CLI) ---
def parse_size_to_bytes(size_str: str) -> int:
    size_str = str(size_str).strip().upper()
    if not size_str: return 0
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMGT]?)$', size_str)
    if not match: match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMGT]?)B$', size_str)
    if not match: raise ValueError(f"Size format '{size_str}' is invalid. Use '100G', '500M', '1T'.")
    num, unit = match.groups()
    val = float(num)
    if unit == 'K': val *= 1024
    elif unit == 'M': val *= 1024**2
    elif unit == 'G': val *= 1024**3
    elif unit == 'T': val *= 1024**4
    return int(val)

def human_readable_size(size: int, default_val="N/A") -> str:
    if size is None or size == 0: return default_val
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"

PROTECTED_PATHS_ABS = [os.path.abspath(p) for p in [
    '/', '/etc', '/usr', '/var', '/lib', '/sbin', '/bin', '/root',
    '/boot', '/dev', '/proc', '/sys', '/run'
]]
def is_path_protected(target_path: str) -> bool:
    abs_target_path = os.path.abspath(target_path)
    for protected in PROTECTED_PATHS_ABS:
        if os.path.commonpath([abs_target_path, protected]) == protected:
            return True
    return False

def _ask_for_dir_details(defaults: Dict[str, Any] = None) -> Dict[str, Any]:
    if defaults is None: defaults = {}
    typer.secho("--- Enter Directory Details ---", fg=typer.colors.CYAN)
    while True:
        default_path = defaults.get('target_directory', "/home/user")
        target_dir = typer.prompt("Target directory path", default=default_path)
        if is_path_protected(target_dir):
            typer.secho(f"ERROR: Path '{target_dir}' is inside a protected system directory! Forbidden.", fg=typer.colors.RED)
        elif not os.path.isdir(os.path.abspath(target_dir)):
            typer.secho(f"WARNING: Path '{target_dir}' does not currently exist. Please ensure it is correct.", fg=typer.colors.YELLOW)
            if typer.confirm("Continue even though the path does not exist?"):
                break
        else: break
    method = typer.prompt("Monitor method (size/age)", default=defaults.get('monitor_method', 'size'))
    while method not in ['size', 'age']:
        typer.secho("Invalid input. Please choose 'size' or 'age'.", fg=typer.colors.RED)
        method = typer.prompt("Monitor method (size/age)", default=defaults.get('monitor_method', 'size'))
    new_dir = {"target_directory": target_dir, "monitor_method": method}
    if method == 'size':
        default_size_str = human_readable_size(defaults.get('max_size_bytes'), "400GB")
        size_str = typer.prompt(f"Max size (e.g., 100GB, 1T)", default=default_size_str)
        try: new_dir['max_size_bytes'] = parse_size_to_bytes(size_str)
        except ValueError as e: typer.secho(f"Error: {e}", fg=typer.colors.RED); raise typer.Exit(1)
    new_dir['max_file_age_days'] = typer.prompt("Max file age (days)", default=defaults.get('max_file_age_days', 15), type=int)
    new_dir['max_depth'] = typer.prompt("Max depth (leave empty for 'None'/unlimited at root)", default=defaults.get('max_depth', 'None'))
    if isinstance(new_dir['max_depth'], str) and new_dir['max_depth'].strip().lower() in ['none', '']:
        new_dir['max_depth'] = None
    else: new_dir['max_depth'] = int(new_dir['max_depth'])
    default_remove = defaults.get('remove', False)
    new_dir['remove'] = typer.confirm(f"Enable actual Deletion Mode? (False=Dry-Run)", default=default_remove)
    return new_dir

# --- PERINTAH-PERINTAH CLI (Diperbarui ke API) ---

@app.command(name="list")
def list_targets(ctx: typer.Context):
    """
    Lists the currently monitored directories (via API).
    """
    client: ApiClient = ctx.obj
    typer.secho("Fetching configuration from API...", fg=typer.colors.DIM)
    config_data = client.get("/api/config/directories")
    dirs = config_data.get('directories', [])
    
    if not dirs:
        typer.secho("No directories are configured yet.", fg=typer.colors.YELLOW)
        typer.echo(f"Use 'configure.py add' to add one.")
        return

    typer.secho("--- Monitored Directories (from API) ---", fg=typer.colors.CYAN, bold=True)
    for i, d in enumerate(dirs):
        typer.secho(f"[{i+1}] Path: {d.get('target_directory')}", bold=True)
        typer.echo(f"    Mode      : {d.get('monitor_method')}")
        if d.get('monitor_method') == 'size':
            size_str = human_readable_size(d.get('max_size_bytes'))
            typer.echo(f"    Max Size  : {size_str}")
        typer.echo(f"    Max Age   : {d.get('max_file_age_days')} days")
        typer.echo(f"    Max Depth : {d.get('max_depth', 'None')}")
        remove_mode = d.get('remove', False)
        if remove_mode: typer.secho(f"    Mode      : ACTUAL DELETE", fg=typer.colors.RED, bold=True)
        else: typer.secho(f"    Mode      : DRY-RUN (Simulation)", fg=typer.colors.GREEN)
        typer.echo("-" * 20)

@app.command(name="add")
def add_target(ctx: typer.Context):
    """Interactively adds a new target directory (via API)."""
    client: ApiClient = ctx.obj
    try:
        new_dir_details = _ask_for_dir_details()
    except typer.Abort:
        typer.secho("Cancelled.", fg=typer.colors.YELLOW); return

    typer.secho("Fetching current config from API...", fg=typer.colors.DIM)
    config = client.get("/api/config/directories")
    config['directories'].append(new_dir_details)
    
    typer.secho("Sending updated config to API...", fg=typer.colors.DIM)
    client.post("/api/config/directories", data=config)
    
    typer.secho(f"\nSuccess! Directory '{new_dir_details['target_directory']}' has been added.", fg=typer.colors.GREEN, bold=True)
    list_targets(ctx)

@app.command(name="edit")
def edit_target(ctx: typer.Context):
    """Edits an existing directory configuration (via API)."""
    client: ApiClient = ctx.obj
    typer.secho("Fetching current config from API...", fg=typer.colors.DIM)
    config = client.get("/api/config/directories")
    dirs = config.get('directories', [])
    if not dirs:
        typer.secho("No directories to edit.", fg=typer.colors.YELLOW); return
    
    list_targets(ctx)
    try:
        num = typer.prompt("Enter the number of the directory to edit", type=int)
        idx = num - 1
        if not (0 <= idx < len(dirs)):
            typer.secho(f"Error: Number '{num}' is not valid.", fg=typer.colors.RED); return
            
        existing_details = dirs[idx]
        typer.secho(f"--- Editing '{existing_details['target_directory']}' ---", fg=typer.colors.CYAN)
        new_dir_details = _ask_for_dir_details(defaults=existing_details)
        
        config['directories'][idx] = new_dir_details
        
        typer.secho("Sending updated config to API...", fg=typer.colors.DIM)
        client.post("/api/config/directories", data=config)
        
        typer.secho(f"\nSuccess! Directory '{new_dir_details['target_directory']}' has been updated.", fg=typer.colors.GREEN, bold=True)
    except typer.Abort:
        typer.secho("Cancelled.", fg=typer.colors.YELLOW)

@app.command(name="remove")
def remove_target(ctx: typer.Context):
    """Removes a target directory from the configuration (via API)."""
    client: ApiClient = ctx.obj
    typer.secho("Fetching current config from API...", fg=typer.colors.DIM)
    config = client.get("/api/config/directories")
    dirs = config.get('directories', [])
    if not dirs:
        typer.secho("No directories to remove.", fg=typer.colors.YELLOW); return
    
    list_targets(ctx)
    try:
        num = typer.prompt("Enter the number of the directory to remove", type=int)
        idx = num - 1
        if not (0 <= idx < len(dirs)):
            typer.secho(f"Error: Number '{num}' is not valid.", fg=typer.colors.RED); return
            
        removed_dir = config['directories'].pop(idx)
        
        if typer.confirm(f"Are you sure you want to remove '{removed_dir['target_directory']}'?"):
            typer.secho("Sending updated config to API...", fg=typer.colors.DIM)
            client.post("/api/config/directories", data=config)
            typer.secho(f"\nSuccess! Directory '{removed_dir['target_directory']}' has been removed.", fg=typer.colors.GREEN, bold=True)
        else:
            typer.secho("Cancelled.", fg=typer.colors.YELLOW)
    except typer.Abort:
        typer.secho("Cancelled.", fg=typer.colors.YELLOW)

# --- PERINTAH BARU: Utilitas & Laporan ---

@app.command(name="hash-password")
def hash_password(
    password: str = typer.Argument(..., help="Password to hash", prompt=True, hide_input=True, confirmation_prompt=True)
):
    """(Utility) Hashes a password for use in config.yaml."""
    typer.echo("Hashing password...")
    hashed_password = pwd_context.hash(password)
    typer.secho("\nSuccess! Copy this hash into your config.yaml 'api_admin_pass_hash':", fg=typer.colors.GREEN)
    typer.echo(hashed_password)

@app.command(name="metrics")
def get_metrics(ctx: typer.Context):
    """(Report) Fetches live metrics/dashboard data from the API."""
    client: ApiClient = ctx.obj
    typer.secho("Fetching live metrics from API...", fg=typer.colors.DIM)
    data = client.get("/api/metrics")

    typer.secho("\n--- Index Statistics ---", fg=typer.colors.CYAN, bold=True)
    istats = data.get('index_stats', {})
    if 'error' in istats:
        typer.secho(f"Error: {istats['error']}", fg=typer.colors.RED)
    else:
        typer.echo(f"Total Files Indexed : {istats.get('total_files')}")
        typer.echo(f"Total Size Indexed  : {human_readable_size(istats.get('total_size_bytes'))}")
        last_upd = time.ctime(istats.get('last_updated_timestamp', 0))
        typer.echo(f"Index Last Updated  : {last_upd}")

    typer.secho("\n--- Monitored Directory Disk Usage ---", fg=typer.colors.CYAN, bold=True)
    dstats = data.get('directory_stats', [])
    for d in dstats:
        typer.secho(f"Path: {d['path']}", bold=True)
        if 'error' in d:
            typer.secho(f"  Error: {d['error']}", fg=typer.colors.RED)
        else:
            typer.echo(f"  Usage: {human_readable_size(d.get('used_bytes'))} / {human_readable_size(d.get('total_bytes'))}")
            free_pct = (d.get('free_bytes', 0) / d.get('total_bytes', 1)) * 100
            typer.echo(f"  Free : {human_readable_size(d.get('free_bytes'))} ({free_pct:.1f}%)")

@app.command(name="history")
def get_history(
    ctx: typer.Context, 
    limit: int = typer.Option(10, "--limit", "-n", help="Number of recent runs to show")
):
    """(Report) Fetches the recent cleanup run history from the API."""
    client: ApiClient = ctx.obj
    typer.secho(f"Fetching last {limit} history logs from API...", fg=typer.colors.DIM)
    data = client.get(f"/api/history?limit={limit}")
    
    history = data.get('history', [])
    if not history:
        typer.secho("No history found.", fg=typer.colors.YELLOW)
        return

    typer.secho("\n--- Recent Cleanup History ---", fg=typer.colors.CYAN, bold=True)
    for run in history:
        ts = run.get('run_timestamp').replace('T', ' ') # Format
        typer.secho(f"[{ts}] {run.get('target_directory')}", bold=True)
        
        status = run.get('status')
        if status == 'success': color = typer.colors.GREEN
        elif status == 'dry_run': color = typer.colors.BLUE
        else: color = typer.colors.RED
        typer.secho(f"  Status: {status.upper()}", fg=color, bold=True)

        typer.echo(f"  Message : {run.get('message')}")
        typer.echo(f"  Removed : {run.get('files_removed_by_age', 0)} (age) + {run.get('files_removed_by_size', 0)} (size) files")
        typer.echo(f"  Data Freed: {human_readable_size(run.get('bytes_removed_total'))}")
        typer.echo("-" * 20)

if __name__ == "__main__":
    # Set working directory to the script's location
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    app()