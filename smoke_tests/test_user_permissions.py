"""User permission smoke tests (live GCP calls, read-only).

Config: user_permissions_config.json (or PERMISSIONS_TEST_CONFIG=<path>).
Set "bigquery_project" to the project containing the source table and where
BigQuery jobs run. Set "destination_bucket_project" to label the project that
owns destination_bucket; Storage permissions are checked on the bucket itself.
Set "impersonate_directly": true (or PERMISSIONS_IMPERSONATE=true|false) to run
BigQuery/export checks as "impersonate_service_account" and PostgreSQL/import
checks (including their GCS checks) as "pg_impersonate_service_account". The
impersonation checks always run for configured service accounts.

gcs_to_postgresql: checks BigQuery/GCS/Cloud SQL IAM permissions, then (if
pg_direct_host, pg_db_name and pg_db_user are set) connects directly to
pg_direct_host (port "pg_direct_port", default 5432, no SSL) as pg_db_user and
runs SELECT session_user. The password is the "pg_secret_name" value from
Secret Manager (project "pg_secret_project", optional "pg_secret_version",
default latest; or a full projects/.../secrets/... name), read as the active
identity. Without a secret, a fresh OAuth access token from
`gcloud auth print-access-token` is used (IAM database authentication; with
direct impersonation gcloud impersonates "pg_impersonate_service_account").

Behaviour switches are boolean "is_" flags in the config, so what is tested can be
changed without editing the string values: is_impersonate_directly,
is_check_bq_to_gcs, is_check_bq_dry_run, is_check_common_tasks,
is_check_cloud_run, is_check_gcs_to_postgresql, is_check_cloud_sql_instance,
is_check_postgresql_connection, is_check_impersonation. When a flag is absent it
defaults to the old behaviour (on when the related value is configured; sections on).

Each check records PASS/FAIL/SKIP and testing continues after a failure. A
report is printed at the end and the test fails if any check failed.
Unconfigured placeholder values ("your-...") are skipped.

Run: python -m unittest test_user_permissions -v
"""
import base64
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

import google.auth
from google.auth import impersonated_credentials
from google.auth.transport.requests import AuthorizedSession, Request

CONFIG_PATH = Path(
    os.environ.get(
        "PERMISSIONS_TEST_CONFIG",
        Path(__file__).resolve().parent / "user_permissions_config.json",
    )
)
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Permissions needed for bq_to_gcs to run to completion
BQ_TABLE_PERMS = ["bigquery.tables.get", "bigquery.tables.getData", "bigquery.tables.export"]
BQ_PROJECT_PERMS = ["bigquery.jobs.create"]
GCS_BUCKET_PERMS = ["storage.objects.create", "storage.buckets.get"]
# Other common tasks
GCS_COMMON_PERMS = ["storage.objects.get", "storage.objects.list", "storage.objects.delete"]
BQ_DATASET_COMMON_PERMS = ["bigquery.datasets.get", "bigquery.tables.list"]
BQ_PROJECT_COMMON_PERMS = ["bigquery.jobs.list", "bigquery.readsessions.create"]
CLOUD_RUN_PERMS = ["run.services.get", "run.services.list", "run.routes.invoke"]
PROJECT_COMMON_PERMS = ["resourcemanager.projects.get", "serviceusage.services.use"]
# gcs_to_postgresql: reads BQ schema, lists/reads GCS objects, connects to Cloud SQL
PG_BQ_TABLE_PERMS = ["bigquery.tables.get"]
PG_GCS_PERMS = ["storage.objects.list", "storage.objects.get"]
CLOUD_SQL_PERMS = ["cloudsql.instances.connect", "cloudsql.instances.get", "cloudsql.instances.login"]
IMPERSONATE_PERMS = ["iam.serviceAccounts.getAccessToken", "iam.serviceAccounts.actAs"]


def get_gcloud_access_token(service_account=None):
    """Get a fresh OAuth access token from gcloud for PostgreSQL IAM auth."""
    command = ["gcloud", "auth", "print-access-token"]
    if service_account:
        command.extend(["--impersonate-service-account", service_account])
    result = subprocess.run(
        command,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gcloud auth print-access-token returned an empty token")
    return token


class GcloudAccessTokenTests(unittest.TestCase):
    @patch("subprocess.run")
    def test_generates_and_returns_access_token(self, run):
        run.return_value = Mock(stdout=" fresh-token\n")

        token = get_gcloud_access_token()

        self.assertEqual(token, "fresh-token")
        run.assert_called_once_with(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )

    @patch("subprocess.run")
    def test_generates_token_as_impersonated_service_account(self, run):
        run.return_value = Mock(stdout="service-account-token\n")

        token = get_gcloud_access_token("pg-importer@example.iam.gserviceaccount.com")

        self.assertEqual(token, "service-account-token")
        self.assertEqual(
            run.call_args.args[0],
            [
                "gcloud",
                "auth",
                "print-access-token",
                "--impersonate-service-account",
                "pg-importer@example.iam.gserviceaccount.com",
            ],
        )

    @patch("subprocess.run")
    def test_rejects_empty_access_token(self, run):
        run.return_value = Mock(stdout=" \n")

        with self.assertRaisesRegex(
            RuntimeError, "gcloud auth print-access-token returned an empty token"
        ):
            get_gcloud_access_token()


def _is_set(value):
    return bool(value) and not str(value).startswith("your")


def _env_bool(name, default):
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes")


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


class Report:
    def __init__(self, accounts, projects):
        self.accounts = accounts
        self.current_account = "current ADC user (email not resolved)"
        self.projects = projects
        self.rows = []

    def record(self, section, check, status, note="", account=None):
        self.rows.append((section, check, status, note, account or self.current_account))

    @property
    def failures(self):
        return [r for r in self.rows if r[2] == "FAIL"]

    def render(self):
        lines = [
            "",
            "=== USER PERMISSION REPORT ===",
            "Accounts: " + "; ".join(
                f"{label}={account}" for label, account in self.accounts.items()
            ),
            "Projects: " + "; ".join(
                f"{label}={project}" for label, project in self.projects.items()
            ),
        ]
        for section, check, status, note, account in self.rows:
            lines.append(
                f"[{status}] [{account}] {section}: {check}" + (f" -- {note}" if note else "")
            )
        lines.append(
            f"Totals: {sum(r[2] == 'PASS' for r in self.rows)} pass, "
            f"{len(self.failures)} fail, {sum(r[2] == 'SKIP' for r in self.rows)} skip"
        )
        return "\n".join(lines)


class UserPermissionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CONFIG_PATH.exists():
            raise unittest.SkipTest(f"config not found: {CONFIG_PATH}")
        cls.cfg = json.loads(CONFIG_PATH.read_text())
        cls.impersonate = _env_bool(
            "PERMISSIONS_IMPERSONATE",
            cls._flag("is_impersonate_directly", cls.cfg.get("impersonate_directly", False)),
        )
        accounts = {
            "BigQuery/GCS": cls.cfg.get("impersonate_service_account")
            if cls.impersonate
            else "current ADC user",
            "PostgreSQL/GCS": cls.cfg.get("pg_impersonate_service_account")
            if cls.impersonate
            else "current ADC user",
        }
        accounts = {
            label: (value if _is_set(value) else "not configured")
            for label, value in accounts.items()
        }
        projects = {
            label: str(cls.cfg.get(key, "unspecified")).strip() or "unspecified"
            for label, key in (
                ("BigQuery", "bigquery_project"),
                ("bucket", "destination_bucket_project"),
            )
        }
        cls.report = Report(accounts, projects)
        try:
            cls.user_creds, _ = google.auth.default(scopes=SCOPES)
            cls.user_session = AuthorizedSession(cls.user_creds)
        except Exception as exc:
            raise unittest.SkipTest(f"no application default credentials: {exc}")

        cls.session = cls.user_session
        cls.identity = "current ADC user"
        cls.current_adc_account = "current ADC user (email not resolved)"
        cls._identity_cache = {}

    @classmethod
    def _flag(cls, name, default):
        """Boolean switch from the config; `default` applies when the key is absent."""
        return _as_bool(cls.cfg[name]) if name in cls.cfg else _as_bool(default)

    def _use_identity(self, role, section):
        if not self.impersonate:
            self.session = self.user_session
            self.identity = "current ADC user"
            self.report.current_account = self.current_adc_account
            return True

        config_key = (
            "impersonate_service_account"
            if role == "BigQuery/GCS"
            else "pg_impersonate_service_account"
        )
        account = self.cfg.get(config_key)
        if not _is_set(account):
            self.report.current_account = "current ADC user"
            self.report.record(section, "impersonation config", "SKIP", f"unset: {config_key}")
            return False

        try:
            if account not in self._identity_cache:
                credentials = impersonated_credentials.Credentials(
                    source_credentials=self.user_creds,
                    target_principal=account,
                    target_scopes=SCOPES,
                )
                credentials.refresh(Request())
                self._identity_cache[account] = AuthorizedSession(credentials)
            self.session = self._identity_cache[account]
            self.identity = f"service account {account}"
            self.report.current_account = account
            return True
        except Exception as exc:
            self.report.current_account = account
            self.report.record(section, "direct impersonation", "FAIL", str(exc))
            return False

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "report"):
            print(cls.report.render())

    # helpers
    def _test_iam(self, section, url, perms, session=None):
        """Call a testIamPermissions endpoint and record each permission."""
        try:
            resp = (session or self.session).post(url, json={"permissions": perms}, timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            granted = set(resp.json().get("permissions", []))
        except Exception as exc:
            for perm in perms:
                self.report.record(section, perm, "FAIL", f"check errored: {exc}")
            return
        for perm in perms:
            if perm in granted:
                self.report.record(section, perm, "PASS")
            else:
                self.report.record(section, perm, "FAIL", f"missing for {self.identity}")

    def _need(self, section, *names):
        missing = [n for n in names if not _is_set(self.cfg.get(n))]
        if missing:
            self.report.record(section, "config", "SKIP", f"unset: {', '.join(missing)}")
            return False
        return True

    def _read_secret(self):
        """Return the payload of pg_secret_name (latest, or pg_secret_version) as text."""
        name = str(self.cfg["pg_secret_name"]).strip()
        version = str(self.cfg.get("pg_secret_version", "latest"))
        if name.startswith("projects/"):
            resource = name.split("/versions/")[0]
        else:
            project = next(
                (
                    self.cfg[k]
                    for k in ("pg_secret_project", "pg_project", "bigquery_project")
                    if _is_set(self.cfg.get(k))
                ),
                None,
            )
            if project is None:
                raise ValueError("set pg_secret_project (or use a full projects/... name)")
            resource = f"projects/{project}/secrets/{name}"
        resp = self.session.get(
            f"https://secretmanager.googleapis.com/v1/{resource}/versions/{version}:access",
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return base64.b64decode(resp.json()["payload"]["data"]).decode("utf-8").strip()

    # sections
    def test_01_identity(self):
        self.session = self.user_session
        self.identity = "current ADC user"
        self.report.current_account = self.identity
        try:
            resp = self.session.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": self.session.credentials.token or ""},
                timeout=30,
            )
            who = (resp.json().get("email") or "unknown") if resp.status_code == 200 else "unknown"
            if who != "unknown":
                self.report.accounts["ADC user"] = who
                self.current_adc_account = who
            self.report.current_account = self.current_adc_account
            self.report.record("identity", f"running as {self.identity}", "PASS", who)
        except Exception as exc:
            self.report.record("identity", "resolve identity", "FAIL", str(exc))

    def test_02_bq_to_gcs_required(self):
        s = "bq_to_gcs required"
        if not self._flag("is_check_bq_to_gcs", True):
            return self.report.record(s, "section", "SKIP", "is_check_bq_to_gcs is false")
        if not self._use_identity("BigQuery/GCS", s):
            return
        if self._need(s, "bigquery_project"):
            project = self.cfg["bigquery_project"]
            self._test_iam(
                s + f" (BigQuery project: {project})",
                f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions",
                BQ_PROJECT_PERMS,
            )
        if self._need(s, "bigquery_project"):
            p = self.cfg["bigquery_project"]
            if self._need(s, "dataset_id", "table_id"):
                d, t = self.cfg["dataset_id"], self.cfg["table_id"]
                self._test_iam(
                    s + f" (BigQuery source table: {p}.{d}.{t})",
                    f"https://bigquery.googleapis.com/bigquery/v2/projects/{p}/datasets/{d}"
                    f"/tables/{t}:testIamPermissions",
                    BQ_TABLE_PERMS,
                )
        if self._need(s, "destination_bucket"):
            b = quote(self.cfg["destination_bucket"], safe="")
            bucket_project = self.cfg.get("destination_bucket_project")
            bucket_label = (
                f" (destination bucket: gs://{self.cfg['destination_bucket']}"
                f" in project {bucket_project})"
                if _is_set(bucket_project)
                else f" (destination bucket: gs://{self.cfg['destination_bucket']})"
            )
            self._gcs_test(s + bucket_label, b, GCS_BUCKET_PERMS)

    def _gcs_test(self, section, bucket, perms):
        try:
            resp = self.session.get(
                f"https://storage.googleapis.com/storage/v1/b/{bucket}/iam/testPermissions",
                params=[("permissions", p) for p in perms],
                timeout=30,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            granted = set(resp.json().get("permissions", []))
        except Exception as exc:
            for perm in perms:
                self.report.record(section, perm, "FAIL", f"check errored: {exc}")
            return
        for perm in perms:
            if perm in granted:
                self.report.record(section, perm, "PASS")
            else:
                self.report.record(section, perm, "FAIL", f"missing for {self.identity}")

    def test_03_bq_to_gcs_dry_run_access(self):
        """Practical end-to-end read check: dry-run query against the source table."""
        s = "bq_to_gcs required"
        if not self._flag("is_check_bq_dry_run", True):
            return self.report.record(s, "dry run", "SKIP", "is_check_bq_dry_run is false")
        if not self._use_identity("BigQuery/GCS", s):
            return
        if not self._need(s, "bigquery_project", "dataset_id", "table_id"):
            return
        project, d, t = (self.cfg[k] for k in ("bigquery_project", "dataset_id", "table_id"))
        try:
            resp = self.session.post(
                f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/jobs",
                json={
                    "configuration": {
                        "dryRun": True,
                        "query": {
                            "query": f"SELECT * FROM `{project}.{d}.{t}`",
                            "useLegacySql": False,
                        },
                    }
                },
                timeout=30,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            self.report.record(
                s,
                f"dry-run query: BigQuery project {project}, source table {project}.{d}.{t}",
                "PASS",
            )
        except Exception as exc:
            self.report.record(
                s,
                f"dry-run query: BigQuery project {project}, source table {project}.{d}.{t}",
                "FAIL",
                str(exc),
            )

    def test_04_common_tasks(self):
        s = "common tasks"
        if not self._flag("is_check_common_tasks", True):
            return self.report.record(s, "section", "SKIP", "is_check_common_tasks is false")
        if not self._use_identity("BigQuery/GCS", s):
            return
        if self._need(s, "bigquery_project"):
            p = self.cfg["bigquery_project"]
            self._test_iam(
                s + f" (BigQuery project: {p})",
                f"https://cloudresourcemanager.googleapis.com/v1/projects/{p}:testIamPermissions",
                BQ_PROJECT_COMMON_PERMS + PROJECT_COMMON_PERMS,
            )
            if self._need(s, "dataset_id"):
                d = self.cfg["dataset_id"]
                self._test_iam(
                    s + f" (BigQuery dataset: {p}.{d})",
                    f"https://bigquery.googleapis.com/bigquery/v2/projects/{p}/datasets/{d}"
                    f":testIamPermissions",
                    BQ_DATASET_COMMON_PERMS,
                )
        if self._need(s, "destination_bucket"):
            bucket_project = self.cfg.get("destination_bucket_project")
            bucket_label = (
                f" (bucket objects: gs://{self.cfg['destination_bucket']}"
                f" in project {bucket_project})"
                if _is_set(bucket_project)
                else f" (bucket objects: gs://{self.cfg['destination_bucket']})"
            )
            self._gcs_test(
                s + bucket_label,
                quote(self.cfg["destination_bucket"], safe=""),
                GCS_COMMON_PERMS,
            )
        if not self._flag("is_check_cloud_run", True):
            self.report.record(s, "cloud run service", "SKIP", "is_check_cloud_run is false")
        elif self._need(s, "cloud_run_project", "cloud_run_region", "cloud_run_service"):
            c = self.cfg
            self._test_iam(
                s + " (cloud run service)",
                f"https://run.googleapis.com/v2/projects/{c['cloud_run_project']}/locations/"
                f"{c['cloud_run_region']}/services/{c['cloud_run_service']}:testIamPermissions",
                CLOUD_RUN_PERMS,
            )

    def test_041_gcs_to_postgresql_gcp_permissions(self):
        s = "gcs_to_postgresql required"
        if not self._flag("is_check_gcs_to_postgresql", True):
            return self.report.record(s, "section", "SKIP", "is_check_gcs_to_postgresql is false")
        if not self._use_identity("PostgreSQL/GCS", s):
            return
        if self._need(s, "bigquery_project", "dataset_id", "table_id"):
            p, d, t = (self.cfg[k] for k in ("bigquery_project", "dataset_id", "table_id"))
            self._test_iam(
                s + " (source table schema)",
                f"https://bigquery.googleapis.com/bigquery/v2/projects/{p}/datasets/{d}"
                f"/tables/{t}:testIamPermissions",
                PG_BQ_TABLE_PERMS,
            )
        if self._need(s, "destination_bucket"):
            bucket_project = self.cfg.get("destination_bucket_project")
            bucket_label = (
                f" (exported objects: gs://{self.cfg['destination_bucket']}"
                f" in project {bucket_project})"
                if _is_set(bucket_project)
                else f" (exported objects: gs://{self.cfg['destination_bucket']})"
            )
            self._gcs_test(
                s + bucket_label,
                quote(self.cfg["destination_bucket"], safe=""),
                PG_GCS_PERMS,
            )
        if not self._flag("is_check_cloud_sql_instance", True):
            self.report.record(
                s, "cloud sql instance", "SKIP", "is_check_cloud_sql_instance is false"
            )
        elif self._need(s, "pg_cloud_sql_connection_name"):
            parts = self.cfg["pg_cloud_sql_connection_name"].split(":")
            if len(parts) != 3:
                self.report.record(
                    s,
                    "pg_cloud_sql_connection_name",
                    "FAIL",
                    "expected project:region:instance",
                )
                return
            project, _, instance = parts
            self._test_iam(
                s + " (cloud sql project)",
                f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions",
                CLOUD_SQL_PERMS,
            )
            try:
                resp = self.session.get(
                    f"https://sqladmin.googleapis.com/v1/projects/{project}/instances/{instance}",
                    timeout=30,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                flags = {
                    f["name"]: f.get("value")
                    for f in resp.json().get("settings", {}).get("databaseFlags", [])
                }
                if flags.get("cloudsql.iam_authentication") == "on":
                    self.report.record(s, "instance IAM database authentication enabled", "PASS")
                else:
                    self.report.record(
                        s,
                        "instance IAM database authentication enabled",
                        "FAIL",
                        "flag cloudsql.iam_authentication is not 'on'",
                    )
            except Exception as exc:
                self.report.record(s, "describe Cloud SQL instance", "FAIL", str(exc))

    def test_042_postgresql_connection(self):
        s = "postgresql connection"
        if not self._flag("is_check_postgresql_connection", True):
            return self.report.record(
                s, "section", "SKIP", "is_check_postgresql_connection is false"
            )
        if not self._use_identity("PostgreSQL/GCS", s):
            return
        if not self._need(s, "pg_direct_host", "pg_db_name", "pg_db_user"):
            return
        try:
            if _is_set(self.cfg.get("pg_secret_name")):
                password = self._read_secret()
                self.report.record(s, "read password from Secret Manager", "PASS")
            else:
                service_account = (
                    self.cfg.get("pg_impersonate_service_account") if self.impersonate else None
                )
                password = get_gcloud_access_token(service_account)
                self.report.record(s, "generated access token with gcloud", "PASS")
        except Exception as exc:
            return self.report.record(s, "get password", "FAIL", str(exc))

        import pg8000.dbapi

        conn = None
        try:
            conn = pg8000.dbapi.connect(
                user=self.cfg["pg_db_user"],
                password=password,
                host=self.cfg["pg_direct_host"],
                port=int(self.cfg.get("pg_direct_port", 5432)),
                database=self.cfg["pg_db_name"],
                timeout=30,
            )
            cur = conn.cursor()
            cur.execute("SELECT session_user")
            user = cur.fetchone()[0]
            self.report.record(s, "connect and SELECT session_user", "PASS", f"session_user={user}")
        except Exception as exc:
            self.report.record(s, "connect and SELECT session_user", "FAIL", str(exc))
        finally:
            if conn is not None:
                conn.close()

    def test_05_can_impersonate_service_account(self):
        s = "impersonation (current ADC user)"
        if not self._flag("is_check_impersonation", True):
            return self.report.record(s, "section", "SKIP", "is_check_impersonation is false")
        self.session = self.user_session
        self.identity = "current ADC user"
        self.report.current_account = self.current_adc_account
        for role, config_key in (
            ("BigQuery/GCS", "impersonate_service_account"),
            ("PostgreSQL/GCS", "pg_impersonate_service_account"),
        ):
            target = self.cfg.get(config_key)
            if not _is_set(target):
                self.report.record(
                    s, f"{role} service account", "SKIP", f"unset: {config_key}"
                )
                continue
            sa = quote(target, safe="@")
            section = f"{s} ({role}: {target})"
            self._test_iam(
                section + " permissions on SA",
                f"https://iam.googleapis.com/v1/projects/-/serviceAccounts/{sa}:testIamPermissions",
                IMPERSONATE_PERMS,
                session=self.user_session,
            )
            try:
                resp = self.user_session.post(
                    f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:generateAccessToken",
                    json={"scope": SCOPES},
                    timeout=30,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                self.report.record(section, "generateAccessToken", "PASS")
            except Exception as exc:
                self.report.record(section, "generateAccessToken", "FAIL", str(exc))

    def test_99_no_failures(self):
        failures = self.report.failures
        self.assertFalse(
            failures,
            "permission checks failed:\n"
            + "\n".join(f"  [{account}] {s}: {c} -- {n}" for s, c, _, n, account in failures),
        )


if __name__ == "__main__":
    unittest.main()
