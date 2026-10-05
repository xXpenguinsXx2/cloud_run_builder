import argparse
import json
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()

    request_path = Path(args.request)
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: could not load import request from {request_path}: {exc}")
        return 2
    if not isinstance(payload, dict):
        print(f"FAIL: import request in {request_path} must be a JSON object.")
        return 2

    try:
        response = requests.post(
            f"{args.url.rstrip('/')}/",
            json=payload,
            timeout=args.timeout,
        )
    except requests.RequestException as exc:
        print(f"FAIL: could not reach local importer at {args.url}: {exc}")
        return 1

    try:
        body = response.json()
    except ValueError:
        body = response.text
    if response.status_code != 200 or not isinstance(body, dict) or body.get("status") != "success":
        print(f"FAIL: import returned HTTP {response.status_code}: {body}")
        return 1

    print(
        f"PASS: imported {body['rows']} row(s) from {body['objects']} GCS object(s) "
        f"into {body['table']} as {body['format']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
