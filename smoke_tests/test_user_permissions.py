"""User permission smoke tests (live GCP calls, read-only).

Config: user_permissions_config.json (or PERMISSIONS_TEST_CONFIG=<path>).
Set "impersonate_directly": true (or PERMISSIONS_IMPERSONATE=true|false) to run
every check as the service account in "impersonate_service_account" instead of
the current user. The "can I impersonate this SA" check always runs when the
key is set.

gcs_to_postgresql: checks BigQuery/GCS/Cloud SQL IAM permissions, then (if
cloud_sql_connection_name, db_name and db_iam_user are set) connects with IAM
auth and runs SELECT session_user. With direct impersonation the connection
uses the impersonated service account; db_iam_user must be that SA's database
user (SA email without ".gserviceaccount.com").

Each check records PASS/FAIL/SKIP and testing continues after a failure. A
report is printed at the end and the test fails if any check failed.
Unconfigured placeholder values ("your-...") are skipped.

Run: python -m unittest test_user_permissions -v
"""
import json
import os
import unittest
from pathlib import Path
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
SQLSERVICE_LOGIN_SCOPE = "https://www.googleapis.com/auth/sqlservice.login"
IMPERSONATE_PERMS = ["iam.serviceAccounts.getAccessToken", "iam.serviceAccounts.actAs"]


def _is_set(value):
    return bool(value) and not str(value).startswith("your")


def _env_bool(name, default):
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes")


class Report:
    def __init__(self):
        self.rows = []

    def record(self, section, check, status, note=""):
        self.rows.append((section, check, status, note))

    @property
    def failures(self):
        return [r for r in self.rows if r[2] == "FAIL"]

    def render(self):
        lines = ["", "=== USER PERMISSION REPORT ==="]
        for section, check, status, note in self.rows:
            lines.append(f"[{status}] {section}: {check}" + (f" -- {note}" if note else ""))
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
        cls.report = Report()
        cls.sa = cls.cfg.get("impersonate_service_account")
        cls.impersonate = _env_bool(
            "PERMISSIONS_IMPERSONATE", bool(cls.cfg.get("impersonate_directly"))
        )
        try:
            cls.user_creds, _ = google.auth.default(scopes=SCOPES)
            cls.user_session = AuthorizedSession(cls.user_creds)
        except Exception as exc:
            raise unittest.SkipTest(f"no application default credentials: {exc}")

        cls.session = cls.user_session
        cls.sql_credentials = None  # None -> Cloud SQL connector uses ADC
        cls.identity = "current user"
        if cls.impersonate:
            if not _is_set(cls.sa):
                raise unittest.SkipTest("impersonate_directly set but no impersonate_service_account")
            try:
                creds = impersonated_credentials.Credentials(
                    source_credentials=cls.user_creds,
                    target_principal=cls.sa,
                    target_scopes=SCOPES,
                )
                creds.refresh(Request())
                cls.session = AuthorizedSession(creds)
                cls.sql_credentials = impersonated_credentials.Credentials(
                    source_credentials=cls.user_creds,
                    target_principal=cls.sa,
                    target_scopes=SCOPES + [SQLSERVICE_LOGIN_SCOPE],
                )
                cls.identity = f"service account {cls.sa} (direct impersonation)"
            except Exception as exc:
                cls.report.record("impersonation", "direct impersonation", "FAIL", str(exc))
                cls.impersonate = False

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

    # sections
    def test_01_identity(self):
        try:
            resp = self.session.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": self.session.credentials.token or ""},
                timeout=30,
            )
            who = resp.json().get("email", "unknown") if resp.status_code == 200 else "unknown"
            self.report.record("identity", f"running as {self.identity}", "PASS", who)
        except Exception as exc:
            self.report.record("identity", "resolve identity", "FAIL", str(exc))

    def test_02_bq_to_gcs_required(self):
        s = "bq_to_gcs required"
        if self._need(s, "source_project"):
            p = self.cfg["source_project"]
            self._test_iam(
                s + " (project)",
                f"https://cloudresourcemanager.googleapis.com/v1/projects/{p}:testIamPermissions",
                BQ_PROJECT_PERMS,
            )
            if self._need(s, "dataset_id", "table_id"):
                d, t = self.cfg["dataset_id"], self.cfg["table_id"]
                self._test_iam(
                    s + " (source table)",
                    f"https://bigquery.googleapis.com/bigquery/v2/projects/{p}/datasets/{d}"
                    f"/tables/{t}:testIamPermissions",
                    BQ_TABLE_PERMS,
                )
        if self._need(s, "destination_bucket"):
            b = quote(self.cfg["destination_bucket"], safe="")
            self._gcs_test(s + " (destination bucket)", b, GCS_BUCKET_PERMS)

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
        if not self._need(s, "source_project", "dataset_id", "table_id"):
            return
        p, d, t = (self.cfg[k] for k in ("source_project", "dataset_id", "table_id"))
        try:
            resp = self.session.post(
                f"https://bigquery.googleapis.com/bigquery/v2/projects/{p}/jobs",
                json={
                    "configuration": {
                        "dryRun": True,
                        "query": {
                            "query": f"SELECT * FROM `{p}.{d}.{t}`",
                            "useLegacySql": False,
                        },
                    }
                },
                timeout=30,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            self.report.record(s, "dry-run query on source table", "PASS")
        except Exception as exc:
            self.report.record(s, "dry-run query on source table", "FAIL", str(exc))

    def test_04_common_tasks(self):
        s = "common tasks"
        if self._need(s, "source_project"):
            p = self.cfg["source_project"]
            self._test_iam(
                s + " (project)",
                f"https://cloudresourcemanager.googleapis.com/v1/projects/{p}:testIamPermissions",
                BQ_PROJECT_COMMON_PERMS + PROJECT_COMMON_PERMS,
            )
            if self._need(s, "dataset_id"):
                d = self.cfg["dataset_id"]
                self._test_iam(
                    s + " (dataset)",
                    f"https://bigquery.googleapis.com/bigquery/v2/projects/{p}/datasets/{d}"
                    f":testIamPermissions",
                    BQ_DATASET_COMMON_PERMS,
                )
        if self._need(s, "destination_bucket"):
            self._gcs_test(
                s + " (bucket objects)",
                quote(self.cfg["destination_bucket"], safe=""),
                GCS_COMMON_PERMS,
            )
        if self._need(s, "cloud_run_project", "cloud_run_region", "cloud_run_service"):
            c = self.cfg
            self._test_iam(
                s + " (cloud run service)",
                f"https://run.googleapis.com/v2/projects/{c['cloud_run_project']}/locations/"
                f"{c['cloud_run_region']}/services/{c['cloud_run_service']}:testIamPermissions",
                CLOUD_RUN_PERMS,
            )

    def test_041_gcs_to_postgresql_gcp_permissions(self):
        s = "gcs_to_postgresql required"
        if self._need(s, "source_project", "dataset_id", "table_id"):
            p, d, t = (self.cfg[k] for k in ("source_project", "dataset_id", "table_id"))
            self._test_iam(
                s + " (source table schema)",
                f"https://bigquery.googleapis.com/bigquery/v2/projects/{p}/datasets/{d}"
                f"/tables/{t}:testIamPermissions",
                PG_BQ_TABLE_PERMS,
            )
        if self._need(s, "destination_bucket"):
            self._gcs_test(
                s + " (exported objects)",
                quote(self.cfg["destination_bucket"], safe=""),
                PG_GCS_PERMS,
            )
        if self._need(s, "cloud_sql_connection_name"):
            parts = self.cfg["cloud_sql_connection_name"].split(":")
            if len(parts) != 3:
                self.report.record(
                    s, "cloud_sql_connection_name", "FAIL", "expected project:region:instance"
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
        if not self._need(s, "cloud_sql_connection_name", "db_name", "db_iam_user"):
            return
        try:
            from google.cloud.sql.connector import Connector, IPTypes
        except ImportError as exc:
            self.report.record(s, "import cloud-sql-python-connector", "FAIL", str(exc))
            return
        ip_type = str(self.cfg.get("cloud_sql_ip_type", "PUBLIC")).upper()
        connector = None
        conn = None
        try:
            connector = Connector(credentials=self.sql_credentials)
            conn = connector.connect(
                self.cfg["cloud_sql_connection_name"],
                "pg8000",
                user=self.cfg["db_iam_user"],
                db=self.cfg["db_name"],
                enable_iam_auth=True,
                ip_type=IPTypes.PRIVATE if ip_type == "PRIVATE" else IPTypes.PUBLIC,
            )
            cur = conn.cursor()
            cur.execute("SELECT session_user")
            user = cur.fetchone()[0]
            self.report.record(s, "connect and SELECT session_user", "PASS", f"session_user={user}")
            expected = self.cfg["db_iam_user"]
            if user != expected:
                self.report.record(
                    s, "session_user matches db_iam_user", "FAIL", f"{user} != {expected}"
                )
            schema = self.cfg.get("db_schema", "public")
            cur.execute(
                "SELECT has_schema_privilege(session_user, %s, 'CREATE'), "
                "has_schema_privilege(session_user, %s, 'USAGE')",
                (schema, schema),
            )
            can_create, can_use = cur.fetchone()
            self.report.record(
                s,
                f"CREATE on schema {schema}",
                "PASS" if can_create else "FAIL",
                "" if can_create else "needed for staging table creation",
            )
            self.report.record(
                s, f"USAGE on schema {schema}", "PASS" if can_use else "FAIL"
            )
            cur.execute("SELECT has_database_privilege(session_user, current_database(), 'CREATE')")
            can_db_create = cur.fetchone()[0]
            self.report.record(
                s,
                "CREATE on database (needed if schema must be created)",
                "PASS" if can_db_create else "FAIL",
            )
        except Exception as exc:
            self.report.record(s, "connect and SELECT session_user", "FAIL", str(exc))
        finally:
            if conn is not None:
                conn.close()
            if connector is not None:
                connector.close()

    def test_05_can_impersonate_service_account(self):
        s = "impersonation"
        if not self._need(s, "impersonate_service_account"):
            return
        sa = quote(self.sa, safe="@")
        # Always evaluated as the real user, even in direct impersonation mode
        self._test_iam(
            s + " (permissions on SA)",
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
            self.report.record(s, "generateAccessToken for SA", "PASS")
        except Exception as exc:
            self.report.record(s, "generateAccessToken for SA", "FAIL", str(exc))

    def test_99_no_failures(self):
        failures = self.report.failures
        self.assertFalse(
            failures,
            "permission checks failed:\n"
            + "\n".join(f"  {s}: {c} -- {n}" for s, c, _, n in failures),
        )


if __name__ == "__main__":
    unittest.main()
