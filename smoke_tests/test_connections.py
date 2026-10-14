"""PostgreSQL direct connection check.

Config: connections_config.json (or CONNECTIONS_TEST_CONFIG=<path>).

Connects directly to "pg_direct_host" (port "pg_direct_port", default 5432)
and database "pg_db_name" as "pg_db_user" using your own gcloud credentials:
the password is a fresh OAuth access token from
`gcloud auth print-access-token` (PostgreSQL IAM database authentication).
For a user account, pg_db_user is your email address.
"pg_cloud_sql_ip_type" (PUBLIC/PRIVATE) is reserved for Cloud SQL connector
connections and is not used by the direct-host connection.

Prints whether the login succeeded or failed; exits 0 on success, 1 on failure.

Run: python smoke_tests/test_connections.py
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get(
        "CONNECTIONS_TEST_CONFIG",
        Path(__file__).resolve().parent / "connections_config.json",
    )
)
REQUIRED_KEYS = ("pg_direct_host", "pg_db_name", "pg_db_user")


def _is_set(value):
    return value not in (None, "") and not str(value).lower().startswith("your")


def load_config(path=CONFIG_PATH):
    with Path(path).open(encoding="utf-8") as config_file:
        return json.load(config_file)


def get_gcloud_access_token():
    """Get a fresh OAuth access token for the active gcloud account."""
    gcloud = shutil.which("gcloud") or "gcloud"
    result = subprocess.run(
        [gcloud, "auth", "print-access-token"],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gcloud auth print-access-token returned an empty token")
    return token


def build_connect_params(cfg, password):
    """Build pg8000 connect keyword arguments for a direct-host connection."""
    return {
        "user": cfg["pg_db_user"],
        "password": password,
        "host": cfg["pg_direct_host"],
        "port": int(cfg.get("pg_direct_port", 5432)),
        "database": cfg["pg_db_name"],
        "timeout": 30,
    }


def check_login(cfg):
    """Log in and return the session user reported by PostgreSQL."""
    import pg8000.dbapi

    conn = pg8000.dbapi.connect(**build_connect_params(cfg, get_gcloud_access_token()))
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT session_user")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def main():
    try:
        cfg = load_config()
    except (OSError, ValueError) as exc:
        print(f"FAILED: could not read config {CONFIG_PATH}: {exc}")
        return 1

    missing = [key for key in REQUIRED_KEYS if not _is_set(cfg.get(key))]
    if missing:
        print(f"FAILED: config values not set: {', '.join(missing)}")
        return 1

    target = (
        f"{cfg['pg_db_user']}@{cfg['pg_direct_host']}:"
        f"{cfg.get('pg_direct_port', 5432)}/{cfg['pg_db_name']}"
    )
    try:
        session_user = check_login(cfg)
    except subprocess.CalledProcessError as exc:
        print(f"FAILED: gcloud auth print-access-token failed: {(exc.stderr or '').strip() or exc}")
        return 1
    except Exception as exc:
        print(f"FAILED: login to {target}: {exc}")
        return 1

    print(f"SUCCESS: logged in to {target} as session_user={session_user}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
