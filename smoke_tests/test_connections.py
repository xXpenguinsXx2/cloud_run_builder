"""PostgreSQL connection smoke tests (live connection).

Config: connections_config.json (or CONNECTIONS_TEST_CONFIG=<path>).

Connects directly to "pg_direct_host" (port "pg_direct_port", default 5432)
and database "pg_db_name" as "pg_db_user" using your own gcloud credentials:
the password is a fresh OAuth access token from
`gcloud auth print-access-token` (PostgreSQL IAM database authentication).
For a user account, pg_db_user is your email address.
"pg_cloud_sql_ip_type" (PUBLIC/PRIVATE) is reserved for Cloud SQL connector
connections and is not used by the direct-host connection.

The live test is skipped when the config is missing or still has placeholder
values ("your-..."). The connect-parameter tests are mocked and always run.

Run: python -m unittest smoke_tests.test_connections -v
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
    path = Path(path)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as config_file:
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


def connect_direct(cfg, password=None):
    import pg8000.dbapi

    if password is None:
        password = get_gcloud_access_token()
    return pg8000.dbapi.connect(**build_connect_params(cfg, password))


class ConnectParamsTests(unittest.TestCase):
    CFG = {
        "pg_direct_host": "10.0.0.5",
        "pg_db_name": "appdb",
        "pg_db_user": "me@example.com",
    }

    def test_default_params_use_direct_host(self):
        params = build_connect_params(self.CFG, "token-123")

        self.assertEqual(params["user"], "me@example.com")
        self.assertEqual(params["password"], "token-123")
        self.assertEqual(params["host"], "10.0.0.5")
        self.assertEqual(params["port"], 5432)
        self.assertEqual(params["database"], "appdb")
        self.assertEqual(params["timeout"], 30)
        self.assertNotIn("ssl_context", params)

    def test_custom_port(self):
        params = build_connect_params({**self.CFG, "pg_direct_port": "6432"}, "t")

        self.assertEqual(params["port"], 6432)

    @patch("subprocess.run")
    def test_connect_posts_gcloud_token_as_password(self, run):
        run.return_value = Mock(stdout="gcloud-token\n")
        with patch("pg8000.dbapi.connect") as connect:
            connect_direct(self.CFG)

        self.assertEqual(run.call_args.args[0][1:], ["auth", "print-access-token"])
        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["password"], "gcloud-token")
        self.assertEqual(kwargs["host"], "10.0.0.5")
        self.assertEqual(kwargs["user"], "me@example.com")

    @patch("subprocess.run")
    def test_empty_token_raises(self, run):
        run.return_value = Mock(stdout="  \n")
        with self.assertRaisesRegex(RuntimeError, "empty token"):
            get_gcloud_access_token()


class DirectPostgresConnectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        if cls.cfg is None:
            raise unittest.SkipTest(f"Config not found: {CONFIG_PATH}")
        missing = [key for key in REQUIRED_KEYS if not _is_set(cls.cfg.get(key))]
        if missing:
            raise unittest.SkipTest(f"Config values not set: {', '.join(missing)}")

    def test_direct_host_connection_with_user_credentials(self):
        conn = connect_direct(self.cfg)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT session_user")
            session_user = cursor.fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(session_user, self.cfg["pg_db_user"])


if __name__ == "__main__":
    unittest.main()
