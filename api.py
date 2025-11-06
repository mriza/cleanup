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
import re # Diperlukan untuk validasi filename
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import uvicorn 

import typer 
from fastapi import FastAPI, HTTPException, Depends, status, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, validator, ValidationError, Literal
from jose import JWTError, jwt
from passlib.context import CryptContext

# --- Konfigurasi Awal: Membaca config untuk setup ---
# PERBAIKAN: Set CWD di awal agar path relatif (CONFIG_PATH) aman
os.chdir(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = "config.yaml"

def load_config_from_yaml(path):
    """Safely loads a YAML file using a shared read lock."""
    try:
        with open(path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            config_data = yaml.safe_load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
            return config_data
    except Exception as e:
        sys.stderr.write(f"FATAL: Error loading config {path}: {e}\n")
        sys.exit(1)

GLOBAL_CONFIG = load_config_from_yaml(CONFIG_PATH)
if not GLOBAL_CONFIG:
    sys.stderr.write(f"FATAL: {CONFIG_PATH} is empty or invalid. API cannot start.\n")
    sys.exit(1)

# PERBAIKAN: Membaca dari struktur config bersarang
G_SETTINGS = GLOBAL_CONFIG.get('global_settings', {})
A_SETTINGS = GLOBAL_CONFIG.get('api_settings', {})

# Setup path dan log level
LOG_LEVEL = G_SETTINGS.get('log_level', 'INFO')
DB_PATH = G_SETTINGS.get('db_path', 'cleanup_index.db')
PID_FILE_PATH = G_SETTINGS.get('pid_paths', {}).get('api', '/run/cleanupd/api.pid')
DIRECTORIES_CONFIG_PATH = G_SETTINGS.get('directories_config_path', 'directories.d')
PROTECTED_PATHS_ABS = [os.path.normpath(os.path.abspath(p)) for p in G_SETTINGS.get('protected_paths', ['/'])]

# Ambil konfigurasi API
API_HOST = A_SETTINGS.get('api_host', '0.0.0.0')
API_PORT = A_SETTINGS.get('api_port', 8000)
API_SECRET_KEY = A_SETTINGS.get('api_secret_key', 'DEFAULT_SECRET_CHANGE_ME')
API_TOKEN_EXPIRE_MINUTES = A_SETTINGS.get('api_token_expire_minutes', 60)
API_ADMIN_USER = A_SETTINGS.get('api_admin_user', 'admin')
API_ADMIN_PASS_HASH = A_SETTINGS.get('api_admin_pass_hash', '')

if API_SECRET_KEY == 'DEFAULT_SECRET_CHANGE_ME' or not API_SECRET_KEY:
    sys.stderr.write("FATAL: api_secret_key has not been changed in config.yaml. API will not start.\n")
    sys.exit(1)
if not API_ADMIN_PASS_HASH:
    sys.stderr.write("FATAL: api_admin_pass_hash is not set in config.yaml. API will not start.\n")
    sys.exit(1)
    
# --- Setup Logging ---
level = logging.getLevelName(LOG_LEVEL.upper())
logging.basicConfig(level=level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(os.path.basename(__file__))
log.setLevel(level)

# --- Setup FastAPI ---
app = FastAPI(title="Cleanup Service API", description="API for managing the cleanup service.", version="2.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- Setup Keamanan (Auth) ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
ALGORITHM = "HS256"

# PERBAIKAN: Logika Pengecekan Path yang Benar
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
        if is_path_protected(v):
            raise ValueError(f"Target path '{v}' is forbidden (is or is inside a system directory).")
        return v
    
    @validator('max_size_bytes')
    def check_max_size_for_size_method(cls, v, values):
        if 'monitor_method' in values and values.get('monitor_method') == 'size' and (v is None or v <= 0):
            raise ValueError("max_size_bytes must be a positive number if monitor_method is 'size'")
        return v

# Model BARU untuk merepresentasikan file config
class DirectoryConfigFile(BaseModel):
    id: str # Nama file, cth: worker.yaml
    config: DirectoryConfig

# Model BARU untuk membuat file
class NewDirectoryConfig(BaseModel):
    filename: str
    config: DirectoryConfig

    @validator('filename')
    def validate_filename(cls, v):
        if not v.endswith(('.yaml', '.yml')):
            raise ValueError("Filename must end with .yaml or .yml")
        if not re.match(r'^[a-zA-Z0-9_.-]+$', v):
            raise ValueError("Filename contains invalid characters.")
        return v

class Token(BaseModel):
    access_token: str
    token_type: str

# --- Helper Keamanan (Tidak Berubah) ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=A_SETTINGS.get('api_token_expire_minutes', 60))
    to_encode.update({"exp": int(expire.timestamp())}) # PERBAIKAN: Gunakan timestamp
    encoded_jwt = jwt.encode(to_encode, API_SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, API_SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None or username != API_ADMIN_USER:
             raise credentials_exception
    except JWTError:
        raise credentials_exception
    return username

# --- Helper Akses File Config (Diperbarui) ---
def load_directory_configs() -> List[Dict[str, Any]]:
    """Memindai 'directories_config_path' dan memuat semua file .yaml."""
    dir_path = DIRECTORIES_CONFIG_PATH
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
                        # Pydantic validasi di sini (opsional tapi bagus)
                        DirectoryConfig(**dir_conf) 
                        configs.append({"id": f.name, "config": dir_conf})
                    else:
                        log.warning(f"Config file '{f.name}' skipped: invalid format or no 'target_directory'.")
            except Exception as e:
                log.error(f"Failed to load config file '{f.name}': {e}")
    return configs

def write_directory_config(filename: str, config_data: DirectoryConfig) -> bool:
    """Menulis/Menimpa satu file config di directories.d."""
    # Validasi filename lagi untuk keamanan
    if not re.match(r'^[a-zA-Z0-9_.-]+$', filename) or '..' in filename:
        log.error(f"Write failed: Invalid filename '{filename}'")
        return False
    
    file_path = os.path.join(DIRECTORIES_CONFIG_PATH, filename)
    
    try:
        # Tulis ke file sementara dulu
        tmp_path = f"{file_path}.tmp"
        with open(tmp_path, 'w') as f:
            # Gunakan .dict() dari Pydantic untuk membuang None
            yaml.dump(config_data.dict(exclude_unset=True), f, default_flow_style=False, sort_keys=False)
        # Ganti file (atomic replace)
        os.replace(tmp_path, file_path)
        return True
    except Exception as e:
        log.error(f"Failed to write config file {filename}: {e}")
        return False

# --- Event Handler (Startup/Shutdown) (Tidak Berubah) ---
@app.on_event("startup")
def startup_event():
    log.info("API service starting up...")
    try:
        os.makedirs(os.path.dirname(PID_FILE_PATH), 0o755, exist_ok=True)
        pid_f = open(PID_FILE_PATH, 'w')
        fcntl.flock(pid_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid_f.write(str(os.getpid()))
        pid_f.flush()
        app.state.pid_file = pid_f
        log.info(f"Lock acquired ({PID_FILE_PATH}). API is running.")
    except (IOError, BlockingIOError):
        log.warning(f"Another instance is already running. Lock file {PID_FILE_PATH} is held. Exiting.")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Failed to create PID lock {PID_FILE_PATH}: {e}")
        sys.exit(1)

@app.on_event("shutdown")
def shutdown_event():
    log.info("API service shutting down...")
    if hasattr(app.state, 'pid_file'):
        fcntl.flock(app.state.pid_file, fcntl.LOCK_UN)
        app.state.pid_file.close()
        try: os.remove(PID_FILE_PATH)
        except: pass
        log.info(f"Lock released ({PID_FILE_PATH}).")

# --- ENDPOINT API (Diperbarui) ---

@app.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    if form_data.username != API_ADMIN_USER or not verify_password(form_data.password, API_ADMIN_PASS_HASH):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    expires_delta = timedelta(minutes=API_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": API_ADMIN_USER}, expires_delta=expires_delta)
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/health")
async def health_check(): return {"status": "ok"}

# --- Endpoint CRUD Config BARU ---

@app.get("/api/config/directories", response_model=List[DirectoryConfigFile])
async def get_all_configs(user: str = Depends(get_current_user)):
    """GET Config: Mengambil SEMUA file config dari directories.d/."""
    log.info(f"User '{user}' requested all config files")
    return load_directory_configs()

@app.post("/api/config/directory", response_model=DirectoryConfigFile)
async def create_config(new_config: NewDirectoryConfig, user: str = Depends(get_current_user)):
    """POST Config: Membuat file config BARU."""
    log.info(f"User '{user}' creating new config file: {new_config.filename}")
    file_path = os.path.join(DIRECTORIES_CONFIG_PATH, new_config.filename)
    
    if os.path.exists(file_path):
        raise HTTPException(status_code=400, detail="A config file with this filename already exists.")
    
    if not write_directory_config(new_config.filename, new_config.config):
        raise HTTPException(status_code=500, detail="Failed to write config file.")
    
    return {"id": new_config.filename, "config": new_config.config}

@app.put("/api/config/directory/{filename}", response_model=DirectoryConfigFile)
async def update_config(filename: str, config_data: DirectoryConfig, user: str = Depends(get_current_user)):
    """PUT Config: Memperbarui file config yang ADA."""
    log.info(f"User '{user}' updating config file: {filename}")
    file_path = os.path.join(DIRECTORIES_CONFIG_PATH, filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Config file not found.")
    
    if not write_directory_config(filename, config_data):
        raise HTTPException(status_code=500, detail="Failed to write config file.")
        
    return {"id": filename, "config": config_data}

@app.delete("/api/config/directory/{filename}", status_code=204)
async def delete_config(filename: str, user: str = Depends(get_current_user)):
    """DELETE Config: Menghapus file config."""
    log.info(f"User '{user}' deleting config file: {filename}")
    
    if not re.match(r'^[a-zA-Z0-9_.-]+$', filename) or '..' in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
        
    file_path = os.path.join(DIRECTORIES_CONFIG_PATH, filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Config file not found.")
        
    try:
        os.remove(file_path)
    except Exception as e:
        log.error(f"Failed to delete config file {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")
    
    return Response(status_code=204)


# --- Endpoint METRICS & HISTORY (Tidak Berubah) ---

@app.get("/api/metrics")
async def get_metrics(user: str = Depends(get_current_user)):
    log.info(f"User '{user}' requested metrics")
    metrics = {"index_stats": {}, "directory_stats": []}
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
    
    # Baca config untuk mendapatkan daftar direktori
    all_configs = load_directory_configs()
    for dir_conf_obj in all_configs:
        path = dir_conf_obj['config']['target_directory']
        try:
            usage = shutil.disk_usage(path)
            metrics['directory_stats'].append({"path": path, "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free})
        except Exception as e:
            metrics['directory_stats'].append({"path": path, "error": f"Failed to get disk usage: {e}"})
    return metrics

@app.get("/api/history")
async def get_history(limit: int = 50, user: str = Depends(get_current_user)):
    log.info(f"User '{user}' requested history (limit {limit})")
    history = []
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row 
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

# --- Menyajikan Frontend Web (Static Files) ---
@app.get("/", response_class=FileResponse, include_in_schema=False)
async def read_index():
    return "static/index.html"

app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    log.info(f"Starting API server on {API_HOST}:{API_PORT}...")
    uvicorn.run("api:app", host=API_HOST, port=API_PORT, reload=False)