ifeq ($(OS),Windows_NT)
SHELL := cmd.exe
.SHELLFLAGS := /C
PYTHON ?= ..\..\..\.venv\Scripts\python.exe
GCLOUD ?= gcloud.cmd
else
PYTHON ?= ../../../.venv/bin/python
GCLOUD ?= gcloud
endif

.DEFAULT_GOAL := help
PROJECT_ID ?= jag-pgsql-gke
REGION ?= us-central1
SERVICE_NAME ?= bigquery-gcs-export
SOURCE_PROJECT ?= jag-pgsql-gke
FORMAT ?= CSV
LOCAL_FUNCTION_URL ?= http://127.0.0.1:8080

.PHONY: help export smoke-test-export-local smoke-test-export-local-csv \
	smoke-test-export-local-parquet smoke-test-export-remote \
	smoke-test-export-remote-csv smoke-test-export-remote-parquet \
	auth auth-google-adc test test-deps

help:
	@echo "Targets: export, smoke-test-export-local, smoke-test-export-remote, auth, auth-google-adc, test, test-deps"
	@echo "Remote export calls the deployed Cloud Run URL with a gcloud identity token; no proxy component is needed."
	@echo "Override PROJECT_ID, REGION, SERVICE_NAME, SOURCE_PROJECT, FORMAT as needed."
	@echo "smoke-test-export-local checks local function readiness and the BigQuery dataset/table, then exports CSV and Parquet."
	@echo "Override LOCAL_FUNCTION_URL if the local function is not at http://127.0.0.1:8080."
	@echo "smoke-test-export-remote checks the BigQuery dataset/table, then exports CSV and Parquet via deployed Cloud Run."

export:
	$(PYTHON) cloud_run_smoke_test.py --gcloud "$(GCLOUD)" --project "$(PROJECT_ID)" --region "$(REGION)" --service "$(SERVICE_NAME)" --source-project "$(SOURCE_PROJECT)" --format "$(FORMAT)"

smoke-test-export-local: smoke-test-export-local-csv smoke-test-export-local-parquet
smoke-test-export-local-parquet: smoke-test-export-local-csv

smoke-test-export-local-csv: export SMOKE_TEST_MODE := local-export
smoke-test-export-local-csv: export FUNCTION_URL := $(LOCAL_FUNCTION_URL)
smoke-test-export-local-csv: export SMOKE_TEST_REQUEST_FILE := export-request.json
smoke-test-export-local-parquet: export SMOKE_TEST_MODE := local-export
smoke-test-export-local-parquet: export FUNCTION_URL := $(LOCAL_FUNCTION_URL)
smoke-test-export-local-parquet: export SMOKE_TEST_REQUEST_FILE := export-request-parquet.json
smoke-test-export-local-csv smoke-test-export-local-parquet:
	$(PYTHON) smoke_test.py

smoke-test-export-remote: smoke-test-export-remote-csv smoke-test-export-remote-parquet
smoke-test-export-remote-parquet: smoke-test-export-remote-csv

smoke-test-export-remote-csv:
	$(MAKE) export FORMAT=CSV SMOKE_TEST_REQUEST_FILE=export-request.json

smoke-test-export-remote-parquet:
	$(MAKE) export FORMAT=PARQUET SMOKE_TEST_REQUEST_FILE=export-request-parquet.json

auth:
	$(GCLOUD) auth login
	$(GCLOUD) auth application-default login --project="$(PROJECT_ID)"

auth-google-adc:
	$(GCLOUD) auth application-default login --project="$(PROJECT_ID)"

test:
	$(PYTHON) -m unittest discover -s . -p "test_*.py" -v

test-deps:
	$(PYTHON) -m pip install -r requirements.txt