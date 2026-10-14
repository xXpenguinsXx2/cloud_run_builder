"""PostgreSQL direct connection check.

Config: connections_config.json (or CONNECTIONS_TEST_CONFIG=<path>).

Connects directly to "pg_direct_host" (port "pg_direct_port", default 5432)
and database "pg_db_name" as "pg_db_user".

Password:
- If "pg_secret_name" is set, the secret value is read from Secret Manager with
  Application Default Credentials and used as the password. Use a short name
  with "pg_secret_project", or a full projects/<p>/secrets/<name> path.
  "pg_secret_version" defaults to latest.
- Otherwise a fresh OAuth access token from `gcloud auth print-access-token`
  is used (PostgreSQL IAM database authentication; for a user account,
  pg_db_user is your email address).
"pg_cloud_sql_ip_type" (PUBLIC/PRIVATE) is reserved for Cloud SQL connector
connections and is not used by the direct-host connection.

Prints whether the login succeeded or failed; exits 0 on success, 1 on failure.

Run: python smoke_tests/test_connections.py
"""
import base64
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


def read_secret(cfg):
    """Return the Secret Manager payload for pg_secret_name using default credentials."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    name = str(cfg["pg_secret_name"]).strip()
    if name.startswith("projects/"):
        resource = name.split("/versions/")[0]
    elif _is_set(cfg.get("pg_secret_project")):
        resource = f"projects/{cfg['pg_secret_project']}/secrets/{name}"
    else:
        raise ValueError("set pg_secret_project (or use a full projects/... secret name)")
    version = cfg.get("pg_secret_version") or "latest"

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    resp = AuthorizedSession(credentials).get(
        f"https://secretmanager.googleapis.com/v1/{resource}/versions/{version}:access",
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Secret Manager HTTP {resp.status_code}: {resp.text[:200]}")
    return base64.b64decode(resp.json()["payload"]["data"]).decode("utf-8").strip()


def get_password(cfg):
    if _is_set(cfg.get("pg_secret_name")):
        return read_secret(cfg), "secret"
    return get_gcloud_access_token(), "gcloud access token"


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


def check_login(cfg, password):
    """Log in and return the session user reported by PostgreSQL."""
    import pg8000.dbapi

    conn = pg8000.dbapi.connect(**build_connect_params(cfg, password))
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
        password, source = get_password(cfg)
    except subprocess.CalledProcessError as exc:
        print(f"FAILED: gcloud auth print-access-token failed: {(exc.stderr or '').strip() or exc}")
        return 1
    except Exception as exc:
        print(f"FAILED: could not get password: {exc}")
        return 1

    try:
        session_user = check_login(cfg, password)
    except Exception as exc:
        print(f"FAILED: login to {target} using {source}: {exc}")
        return 1

    print(f"SUCCESS: logged in to {target} using {source} as session_user={session_user}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
