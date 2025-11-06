#
# SCRIPT: api.py
# TASK: Runs the 24/7 FastAPI backend API server.
#
import os
import yaml
import fcntl
import sqlite3
import shutil
import logging
import sys
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import uvicorn # Diimpor untuk __main__

import typer # Diperlukan untuk Pydantic
from fastapi import FastAPI, HTTPException, Depends, status, Body
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, validator, ValidationError, Literal
from jose import JWTError, jwt
from passlib.context import CryptContext

# --- Konfigurasi Awal: Membaca config untuk setup ---
CONFIG_PATH = "config.yaml"
# (Path ini disediakan oleh systemd via RuntimeDirectory)
PID_FILE_PATH = "/run/cleanupd/api.pid" 

# Helper khusus untuk memuat config (dengan lock)
def load_config_from_yaml(path):
    """Safely loads a YAML file using a shared read lock."""
    try:
        with open(path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH) # Shared Lock (Read)
            config_data = yaml.safe_load(f)
            fcntl.flock(f, fcntl.LOCK_UN) # Release
            return config_data
    except Exception as e:
        print(f"FATAL: Error loading config {path}: {e}", file=sys.stderr)
        sys.exit(1)

GLOBAL_CONFIG = load_config_from_yaml(CONFIG_PATH)
if not GLOBAL_CONFIG:
    print(f"FATAL: {CONFIG_PATH} is empty or invalid. API cannot start.", file=sys.stderr)
    sys.exit(1)

# Ambil konfigurasi API dari config
API_HOST = GLOBAL_CONFIG.get('api_host', '0.0.0.0')
API_PORT = GLOBAL_CONFIG.get('api_port', 8000)
API_SECRET_KEY = GLOBAL_CONFIG.get('api_secret_key', 'DEFAULT_SECRET_CHANGE_ME')
API_TOKEN_EXPIRE_MINUTES = GLOBAL_CONFIG.get('api_token_expire_minutes', 60)
API_ADMIN_USER = GLOBAL_CONFIG.get('api_admin_user', 'admin')
API_ADMIN_PASS_HASH = GLOBAL_CONFIG.get('api_admin_pass_hash', '')
DB_PATH = GLOBAL_CONFIG.get('db_path', 'cleanup_index.db')
LOG_LEVEL = GLOBAL_CONFIG.get('log_level', 'INFO')

if API_SECRET_KEY == 'DEFAULT_SECRET_CHANGE_ME' or not API_SECRET_KEY:
    print("FATAL: api_secret_key has not been changed in config.yaml. API will not start.", file=sys.stderr)
    sys.exit(1)
if not API_ADMIN_PASS_HASH:
    print("FATAL: api_admin_pass_hash is not set in config.yaml. API will not start.", file=sys.stderr)
    sys.exit(1)
    
# --- Setup Logging ---
level = logging.getLevelName(LOG_LEVEL.upper())
logging.basicConfig(
    level=level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)] # Log ke stdout untuk systemd
)
log = logging.getLogger(os.path.basename(__file__))
log.setLevel(level)

# --- Setup FastAPI ---
app = FastAPI(
    title="Cleanup Service API",
    description="API for managing the cleanup service config, metrics, and history.",
    version="1.1.0"
)

# --- Setup Keamanan (Auth) ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
ALGORITHM = "HS256"
PROTECTED_PATHS_ABS = [os.path.abspath(p) for p in [
    '/', '/etc', '/usr', '/var', '/lib', '/sbin', '/bin', '/root',
    '/boot', '/dev', '/proc', '/sys', '/run'
]]

# --- Model Data (Pydantic) untuk Validasi ---
class DirectoryConfig(BaseModel):
    target_directory: str
    monitor_method: Literal['size', 'age']
    max_size_bytes: Optional[int] = None
    max_file_age_days: int = Field(gt=0)
    max_depth: Optional[int] = Field(default=None, ge=0)
    remove: bool = False

    @validator('target_directory')
    def validate_target_path(cls, v):
        abs_target_path = os.path.abspath(v)
        for protected in PROTECTED_PATHS_ABS:
            if os.path.commonpath([abs_target_path, protected]) == protected:
                raise ValueError(f"Target path '{v}' (to {abs_target_path}) is forbidden.")
        return v
    
    @validator('max_size_bytes')
    def check_max_size_for_size_method(cls, v, values):
        if 'monitor_method' in values and values.get('monitor_method') == 'size' and (v is None or v <= 0):
            raise ValueError("max_size_bytes must be a positive number if monitor_method is 'size'")
        return v

class AppConfig(BaseModel):
    directories: List[DirectoryConfig]

class Token(BaseModel):
    access_token: str
    token_type: str

# --- Helper Keamanan ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, API_SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    """Dependency untuk endpoint yang dilindungi"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, API_SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        if username != API_ADMIN_USER:
             raise credentials_exception
    except JWTError:
        raise credentials_exception
    return username

# --- Helper Akses File (dengan Fcntl Lock) ---
def write_config_safe(config_data: dict):
    """Menulis ke config.yaml dengan 'Exclusive Lock' (aman dari semua)."""
    try:
        with open(CONFIG_PATH, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX) # Lock Eksklusif (Write)
            yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)
            fcntl.flock(f, fcntl.LOCK_UN) # Lepas lock
    except Exception as e:
        log.error(f"Failed to write config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to write config file: {e}")

# --- Event Handler (Startup/Shutdown) ---

@app.on_event("startup")
def startup_event():
    """Saat startup: buat PID lock file."""
    log.info("API service starting up...")
    try:
        # Tulis PID file dengan exclusive lock
        pid_f = open(PID_FILE_PATH, 'w')
        fcntl.flock(pid_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid_f.write(str(os.getpid()))
        pid_f.flush()
        # Simpan file handle agar lock-nya bertahan
        app.state.pid_file = pid_f
        log.info(f"Lock acquired ({PID_FILE_PATH}). API is running.")
    except (IOError, BlockingIOError):
        log.warning(f"Another instance is already running. Lock file {PID_FILE_PATH} is held. Exiting.")
        sys.exit(1) # Keluar jika sudah ada yg jalan

@app.on_event("shutdown")
def shutdown_event():
    """Saat shutdown: lepas lock dan hapus PID file."""
    log.info("API service shutting down...")
    if hasattr(app.state, 'pid_file'):
        fcntl.flock(app.state.pid_file, fcntl.LOCK_UN)
        app.state.pid_file.close()
        try:
            os.remove(PID_FILE_PATH)
        except:
            pass
        log.info(f"Lock released ({PID_FILE_PATH}).")

# --- ENDPOINT API ---

@app.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    """Endpoint untuk mendapatkan token JWT."""
    if form_data.username != API_ADMIN_USER:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )
    if not verify_password(form_data.password, API_ADMIN_PASS_HASH):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )
    access_token_expires = timedelta(minutes=API_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": API_ADMIN_USER}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/health")
async def health_check():
    """Endpoint health check (tidak perlu auth)"""
    return {"status": "ok"}

@app.get("/api/config/directories", response_model=AppConfig)
async def get_config_directories(user: str = Depends(get_current_user)):
    """GET Config: Mengambil HANYA list 'directories' (dilindungi)."""
    log.info(f"User '{user}' requested config directories")
    config = load_config_from_yaml(CONFIG_PATH)
    return AppConfig(directories=config.get('directories', []))

@app.post("/api/config/directories", response_model=AppConfig)
async def set_config_directories(config_updates: AppConfig, user: str = Depends(get_current_user)):
    """POST Config: Mengganti list 'directories' (dilindungi)."""
    log.info(f"User '{user}' is updating config directories")
    
    full_config = load_config_from_yaml(CONFIG_PATH)
    full_config['directories'] = config_updates.dict().get('directories', [])
    write_config_safe(full_config)
    
    log.info("Config directories successfully updated.")
    return config_updates

@app.get("/api/metrics")
async def get_metrics(user: str = Depends(get_current_user)):
    """GET Metrics: Mengambil data dashboard (dilindungi)."""
    log.info(f"User '{user}' requested metrics")
    metrics = {
        "index_stats": {},
        "directory_stats": []
    }

    # 1. Statistik dari Database Indeks (Kondisi Saat Ini)
    try:
        db_mtime = os.path.getmtime(DB_PATH)
        metrics['index_stats']['last_updated_timestamp'] = db_mtime
        
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*), SUM(size) FROM file_index")
            count, total_size = cursor.fetchone()
            metrics['index_stats']['total_files'] = count or 0
            metrics['index_stats']['total_size_bytes'] = total_size or 0
    except FileNotFoundError:
        metrics['index_stats']['error'] = "Index database not found."
    except Exception as e:
        metrics['index_stats']['error'] = f"Failed to read DB: {e}"

    # 2. Statistik Disk Usage
    config = load_config_from_yaml(CONFIG_PATH)
    for dir_conf in config.get('directories', []):
        path = dir_conf.get('target_directory')
        try:
            usage = shutil.disk_usage(path)
            metrics['directory_stats'].append({
                "path": path,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free
            })
        except Exception as e:
            metrics['directory_stats'].append({"path": path, "error": f"Failed to get disk usage: {e}"})

    return metrics

@app.get("/api/history")
async def get_history(limit: int = 50, user: str = Depends(get_current_user)):
    """GET History: Mengambil laporan agregat terakhir (dilindungi)."""
    log.info(f"User '{user}' requested history (limit {limit})")
    history = []
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row # Akses via nama kolom
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM cleanup_history ORDER BY run_timestamp DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            history = [dict(row) for row in rows]
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Index database not found.")
    except sqlite3.Error as e:
        log.error(f"Failed to read history table: {e}")
        if "no such table" in str(e):
            return {"history": [], "message": "History table not found, indexer may need to run."}
        raise HTTPException(status_code=500, detail=f"Failed to read DB: {e}")

    return {"history": history}

if __name__ == "__main__":
    # Pastikan skrip dijalankan dari direktori yang benar
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    log.info(f"Starting API server on {API_HOST}:{API_PORT}...")
    uvicorn.run(app, host=API_HOST, port=API_PORT)