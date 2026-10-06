import json
import fnmatch
import os
import sys
from pathlib import Path

import google.auth
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from google.cloud import bigquery
from google.cloud import storage
import requests

EXPORT_FIELDS = {
    "source_project": "SMOKE_TEST_SOURCE_PROJECT",
    "dataset_id": "SMOKE_TEST_DATASET_ID",
    "table_id": "SMOKE_TEST_TABLE_ID",
    "destination_bucket": "SMOKE_TEST_DESTINATION_BUCKET",
    "destination_object": "SMOKE_TEST_DESTINATION_OBJECT",
    "location": "SMOKE_TEST_LOCATION",
    "format": "SMOKE_TEST_FORMAT",
    "compression": "SMOKE_TEST_COMPRESSION",
}
OPTIONAL_EXPORT_FIELDS = ("compression",)
REQUIRED_EXPORT_FIELDS = (
    "source_project",
    "dataset_id",
    "table_id",
    "destination_bucket",
)


def response_body(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def find_matching_export_blobs(storage_client, bucket_name, object_pattern):
    prefix = object_pattern.split("*", 1)[0]
    matching_blobs = [
        blob
        for blob in storage_client.list_blobs(bucket_name, prefix=prefix)
        if (
            fnmatch.fnmatchcase(blob.name, object_pattern)
            if "*" in object_pattern
            else blob.name == object_pattern
        )
    ]
    return sorted(matching_blobs, key=lambda blob: blob.name)


def run_validation_smoke_test(function_url, timeout_seconds):
    print(f"Checking local function readiness at {function_url.rstrip('/')}/")
    response = requests.post(
        f"{function_url.rstrip('/')}/", json={}, timeout=timeout_seconds
    )
    body = response_body(response)
    expected_error = "missing required parameter(s)"

    if (
        response.status_code != 400
        or not isinstance(body, dict)
        or expected_error not in body.get("error", "")
    ):
        print(
            f"FAIL: expected HTTP 400 with the missing-parameters error; "
            f"received HTTP {response.status_code}: {body}"
        )
        return 1

    print("PASS: function is reachable and validates missing export parameters.")
    return 0


def load_export_request():
    payload_path = Path(
        os.environ.get(
            "SMOKE_TEST_REQUEST_FILE",
            str(Path(__file__).with_name("export-request.json")),
        )
    )
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: could not load export request JSON from {payload_path}: {exc}")
        return 2

    if not isinstance(payload, dict):
        print(f"FAIL: export request JSON in {payload_path} must be an object.")
        return 2

    for key, setting in EXPORT_FIELDS.items():
        value = os.environ.get(setting, "").strip()
        if value:
            payload[key] = value

    invalid_fields = [
        key
        for key in EXPORT_FIELDS
        if key not in OPTIONAL_EXPORT_FIELDS
        if not isinstance(payload.get(key), str) or not payload[key].strip()
    ]
    invalid_fields.extend(
        key
        for key in OPTIONAL_EXPORT_FIELDS
        if key in payload
        and (not isinstance(payload[key], str) or not payload[key].strip())
    )
    invalid_fields.extend(
        key
        for key in REQUIRED_EXPORT_FIELDS
        if isinstance(payload.get(key), str)
        and payload[key].startswith("REPLACE_WITH_")
    )
    if invalid_fields:
        print(
            "FAIL: set these string fields in "
            f"{payload_path} before export mode: "
            + ", ".join(dict.fromkeys(invalid_fields))
        )
        return 2

    unexpected_fields = sorted(set(payload) - set(EXPORT_FIELDS))
    if unexpected_fields:
        print("FAIL: unexpected export request fields: " + ", ".join(unexpected_fields))
        return 2

    print("Export request JSON:")
    print(json.dumps(payload, indent=2))
    return payload


def run_export_smoke_test(function_url, timeout_seconds, request_headers=None):
    payload = load_export_request()
    if payload is None:
        return 2

    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        client = bigquery.Client(
            project=payload["source_project"], credentials=credentials
        )

        dataset_id = f"{payload['source_project']}.{payload['dataset_id']}"
        table_id = f"{dataset_id}.{payload['table_id']}"

        print(f"Checking BigQuery dataset: {dataset_id}", flush=True)
        dataset = client.get_dataset(dataset_id)
        print(f"PASS: dataset is reachable in location {dataset.location}.", flush=True)

        if dataset.location.casefold() != payload["location"].casefold():
            print(
                f"FAIL: request location {payload['location']} does not match "
                f"dataset location {dataset.location}."
            )
            return 2

        print(f"Checking BigQuery table: {table_id}", flush=True)
        table = client.get_table(table_id)
        print(
            f"PASS: table is reachable (type={table.table_type}, "
            f"rows={table.num_rows}).",
            flush=True,
        )

        identity_job = client.query("SELECT SESSION_USER() AS principal")
        principal = next(identity_job.result(timeout=timeout_seconds)).principal
    except (GoogleAuthError, GoogleAPIError, StopIteration) as exc:
        print(f"FAIL: Google authentication/BigQuery preflight failed: {exc}")
        if isinstance(exc, GoogleAuthError):
            print("Run `make auth-google-adc` and try the export smoke test again.")
        return 2

    print(f"Google Cloud authenticated as: {principal}")
    print(f"BigQuery project: {payload['source_project']}")
    print(f"Calling export function at {function_url.rstrip('/')}/")

    response = requests.post(
        f"{function_url.rstrip('/')}/",
        json=payload,
        timeout=timeout_seconds,
        headers=request_headers,
    )
    body = response_body(response)
    object_pattern = payload.get("destination_object", f"{payload['table_id']}.csv")
    expected_destination = (
        f"gs://{payload['destination_bucket']}/"
        f"{object_pattern}"
    )

    if (
        response.status_code != 200
        or not isinstance(body, dict)
        or body.get("status") != "success"
        or body.get("destination") != expected_destination
    ):
        print(f"FAIL: export request returned HTTP {response.status_code}: {body}")
        return 1

    print(
        f"PASS: function reports BigQuery extract completed for {body['exported']} "
        f"to {body['destination']} as {principal}."
    )

    try:
        storage_client = storage.Client(
            project=payload["source_project"], credentials=credentials
        )
        matching_blobs = find_matching_export_blobs(
            storage_client, payload["destination_bucket"], object_pattern
        )
    except (GoogleAuthError, GoogleAPIError) as exc:
        print(f"FAIL: could not verify exported GCS objects: {exc}")
        return 2

    if not matching_blobs:
        print(
            "FAIL: no GCS objects match "
            f"gs://{payload['destination_bucket']}/{object_pattern}"
        )
        return 1

    print(
        f"PASS: found {len(matching_blobs)} exported object(s) at "
        f"gs://{payload['destination_bucket']}/: "
        + ", ".join(blob.name for blob in matching_blobs)
    )
    return 0


def run_local_export_smoke_test(function_url, timeout_seconds):
    readiness_result = run_validation_smoke_test(function_url, timeout_seconds)
    if readiness_result != 0:
        return readiness_result

    return run_export_smoke_test(function_url, timeout_seconds)


def main():
    function_url = os.environ.get("FUNCTION_URL", "http://localhost:8080")
    mode = os.environ.get("SMOKE_TEST_MODE", "validation").strip().lower()

    try:
        timeout_seconds = float(os.environ.get("SMOKE_TEST_TIMEOUT_SECONDS", "600"))
        if mode == "validation":
            return run_validation_smoke_test(function_url, timeout_seconds)
        if mode == "export":
            return run_export_smoke_test(function_url, timeout_seconds)
        if mode == "local-export":
            return run_local_export_smoke_test(function_url, timeout_seconds)
        print(
            "FAIL: SMOKE_TEST_MODE must be 'validation', 'export', "
            "or 'local-export'."
        )
        return 2
    except requests.RequestException as exc:
        print(f"FAIL: could not call the function at {function_url}: {exc}")
        return 1
    except ValueError as exc:
        print(f"FAIL: invalid timeout setting: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
