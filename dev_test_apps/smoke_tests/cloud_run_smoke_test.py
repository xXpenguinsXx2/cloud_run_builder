import argparse
import os
import socket
import subprocess
import sys
import time

from smoke_test import run_export_smoke_test


def proxy_is_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def wait_for_proxy(process, port, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                f"Cloud Run proxy exited early with code {process.returncode}"
            )
        if proxy_is_listening(port):
            return
        time.sleep(1)
    raise TimeoutError(f"Cloud Run proxy did not listen on port {port} in time")


def stop_proxy(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gcloud", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--format", choices=("CSV", "PARQUET"), required=True)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--skip-proxy", action="store_true")
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

    proxy = None
    if args.skip_proxy:
        print(
            f"Using existing Cloud Run proxy for {args.service} "
            f"at localhost:{args.port}; no proxy launch performed.",
            flush=True,
        )
    elif proxy_is_listening(args.port):
        print(
            f"Detected existing Cloud Run proxy for {args.service} "
            f"at localhost:{args.port}; reusing it.",
            flush=True,
        )
    else:
        proxy_args = [
            args.gcloud,
            "run",
            "services",
            "proxy",
            args.service,
            f"--project={args.project}",
            f"--region={args.region}",
            f"--port={args.port}",
        ]
        if os.name == "nt":
            proxy_command = ["cmd.exe", "/d", "/c", subprocess.list2cmdline(proxy_args)]
        else:
            proxy_command = proxy_args

        print(
            f"Starting authenticated Cloud Run proxy for {args.service} "
            f"in {args.project}/{args.region} on localhost:{args.port}.",
            flush=True,
        )
        proxy = subprocess.Popen(proxy_command)

    try:
        wait_for_proxy(proxy, args.port, timeout_seconds=60)
        print("Cloud Run proxy is ready; running BigQuery preflight and export.", flush=True)
        return run_export_smoke_test(
            f"http://127.0.0.1:{args.port}",
            float(os.environ.get("SMOKE_TEST_TIMEOUT_SECONDS", "600")),
        )
    except (OSError, RuntimeError, TimeoutError) as exc:
        print(f"FAIL: Cloud Run smoke-test proxy failed: {exc}")
        return 2
    finally:
        if proxy is not None:
            stop_proxy(proxy)


if __name__ == "__main__":
    sys.exit(main())