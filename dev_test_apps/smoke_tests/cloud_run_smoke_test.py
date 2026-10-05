import argparse
import os
import subprocess
import sys

import requests

from smoke_test import run_export_smoke_test


def run_gcloud_command(gcloud, *arguments):
    command = [gcloud, *arguments]
    if os.name == "nt":
        command = ["cmd.exe", "/d", "/c", subprocess.list2cmdline(command)]

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"gcloud {' '.join(arguments)} failed"
            + (f": {detail}" if detail else f" with exit code {result.returncode}")
        )
    return result.stdout.strip()


def get_cloud_run_service_url(gcloud, project, region, service):
    service_url = run_gcloud_command(
        gcloud,
        "run",
        "services",
        "describe",
        service,
        f"--project={project}",
        f"--region={region}",
        "--format=value(status.url)",
    )
    if not service_url.startswith("https://"):
        raise RuntimeError(
            f"gcloud returned an invalid URL for Cloud Run service {service}: "
            f"{service_url or '(empty)'}"
        )
    return service_url


def get_identity_token(gcloud):
    token = run_gcloud_command(gcloud, "auth", "print-identity-token")
    if not token:
        raise RuntimeError("gcloud auth print-identity-token returned an empty token")
    return token


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gcloud", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--format", choices=("CSV", "PARQUET"), required=True)
    args = parser.parse_args()

    os.environ["SMOKE_TEST_SOURCE_PROJECT"] = args.source_project
    os.environ["SMOKE_TEST_FORMAT"] = args.format
    if args.format == "PARQUET":
        os.environ["SMOKE_TEST_COMPRESSION"] = "SNAPPY"
        os.environ["SMOKE_TEST_DESTINATION_OBJECT"] = (
            "example/cloud-run-export-*.parquet"
        )
    else:
        os.environ.pop("SMOKE_TEST_COMPRESSION", None)
        os.environ["SMOKE_TEST_DESTINATION_OBJECT"] = (
            "example/cloud-run-export-*.csv"
        )

    try:
        service_url = get_cloud_run_service_url(
            args.gcloud, args.project, args.region, args.service
        )
        token = get_identity_token(args.gcloud)
        print(
            f"Calling deployed Cloud Run service {args.service} at {service_url}; "
            "no proxy component is required.",
            flush=True,
        )
        return run_export_smoke_test(
            service_url,
            float(os.environ.get("SMOKE_TEST_TIMEOUT_SECONDS", "600")),
            request_headers={"Authorization": f"Bearer {token}"},
        )
    except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"FAIL: Cloud Run smoke test could not authenticate or resolve service: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())