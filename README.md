# Cleanup Service

An automated storage housekeeping service for Linux servers. It monitors specified directories and cleans up old files based on flexible rules (age or size quota) to manage disk space.

The entire system is managed via a central **FastAPI backend** (`api.py`), which provides:
1.  A **Web Interface** (frontend) for dashboard monitoring and configuration.
2.  A **CLI Tool** (`configure.py`) for server-side management.
3.  A secure, token-based API for all operations.

## 🏛️ Architecture

This project uses an API-first, three-service architecture managed by `systemd`:

1.  **`cleanupd-api.service` (24/7 API & Web Frontend)**
    * Runs a **FastAPI** server (`api.py`) that acts as the central "brain".
    * **Serves the `index.html` frontend** at the root URL (`/`).
    * Provides API endpoints (`/api/*`, `/token`) for all configuration, metrics, and history.
    * Secured by JWT (token-based) authentication.
    * Uses PID locking to ensure only one instance runs.

2.  **`cleanupd-indexer.service` (Periodic Job)**
    * Runs hourly (via `cleanupd-indexer.timer`).
    * Reads `config.yaml` (for global settings) and scans the `/opt/cleanup/directories.d/` directory for all `.yaml` job files.
    * Builds (or rebuilds) the `cleanup_index.db` SQLite database.
    * Uses a PID lock to prevent concurrent runs.

3.  **`cleanupd-cleaner.service` (Periodic Job)**
    * Runs hourly, 30 minutes offset from the indexer (via `cleanupd-cleaner.timer`).
    * Reads configs from `directories.d/`.
    * Reads the `cleanup_index.db` and deletes files that match the rules.
    * Writes a one-line **aggregate summary** to the `cleanup_history` table in the database.
    * Uses a PID lock to prevent concurrent runs.

## ✨ Key Features

* **Modular Configuration**: Global settings are in `config.yaml`, but each target directory is defined in its own `.yaml` file inside `/opt/cleanup/directories.d/`, eliminating config file conflicts.
* **Unified Interface**: Manage the system via a modern **Web UI** or a powerful **CLI**. Both are clients for the same central API.
* **Secure by Default**:
    * Runs as a non-privileged system user (`cleanupd`).
    * API is secured with JWT, passwords are hashed (`passlib`).
    * `api_secret_key` is randomized on install.
    * Includes a **Protected Path** check to prevent configuring critical system directories (e.g., `/etc`, `/var`).
* **Robust & Concurrent**:
    * Uses **PID locking** (`fcntl`) to prevent services from running multiple times.
    * Uses **SQLite WAL mode** to prevent database locks between the indexer (writer) and cleaner (reader).
* **Aggregate Reporting**: File deletions are logged as aggregates to a `cleanup_history` table, keeping `journalctl` logs clean and providing rich reports.

## 💾 Installation

1.  Place all project files (`.py`, `.sh`, `.yaml`, `.txt`, `.service`, `.timer`, `.html`, `README.md`) in a single directory.
2.  Run the installer **as root**:
    ```bash
    sudo bash ./install.sh
    ```
3.  The installer will:
    * Create the `cleanupd` system user.
    * Copy files to `/opt/cleanup`, creating `static/` and `directories.d/` subdirectories.
    * Set correct file ownership (`cleanupd`).
    * Create a Python virtual environment in `/opt/cleanup/venv`.
    * Install dependencies from `requirements.txt`.
    * **Randomize the `api_secret_key`** in `config.yaml`.
    * Create a `logrotate` config file.
    * Symlink the `.service` and `.timer` files to `/etc/systemd/system`.
    * Reload `systemd` and start all three services.

### ⚠️ Post-Install Actions (Required)

1.  **Set Admin Password:**
    * Generate a new hash:
        ```bash
        sudo -u cleanupd /opt/cleanup/venv/bin/python /opt/cleanup/configure.py hash-password
        ```
    * Copy the resulting hash.
    * Paste it into `/opt/cleanup/config.yaml` to replace the value of `api_admin_pass_hash`.
    * Restart the API:
        ```bash
        sudo systemctl restart cleanupd-api.service
        ```

2.  **Grant Directory Permissions:** The service runs as `cleanupd` and needs permission to read/write in your target directories.
    * **Recommended (using ACLs):**
        ```bash
        # Give permissions to existing files/dirs
        sudo setfacl -R -m u:cleanupd:rwx /home/atcs2/WORKER
        # Give permissions to future files/dirs
        sudo setfacl -dR -m u:cleanupd:rwx /home/atcs2/WORKER
        ```

## 💻 Usage

### Web Interface
Access the server's IP address and port (e.g., `http://10.0.1.5:8000`) in your browser. Log in with the `api_admin_user` and the password you set.

### CLI (Command Line)
Run all CLI commands as the `cleanupd` user.

**Recommended Alias:**
```bash
# Add to your .bashrc or .zshrc
alias cleanup-config="sudo -u cleanupd /opt/cleanup/venv/bin/python /opt/cleanup/configure.py"
```

* `cleanup-config list`: Show all currently monitored directories (from directories.d/).
* `cleanup-config add`: Interactively create a new .yaml file in directories.d/.
* `cleanup-config edit`: Interactively edit an existing .yaml file.
* `cleanup-config remove`: Remove an existing .yaml file.
* `cleanup-config metrics`: Show live dashboard metrics.
* `cleanup-config history --limit 5`: Show the summary of the last 5 cleanup runs.

## 📊 Monitoring (Logs)

Logs are handled by `systemd-journald`.

```bash
# View the API server logs
journalctl -u cleanupd-api.service -f

# View the Indexer (disk scanner) logs
journalctl -u cleanupd-indexer.service -f

# View the Cleaner (file deletion) logs
journalctl -u cleanupd-cleaner.service -f
```