ifeq ($(OS),Windows_NT)
SHELL := cmd.exe
.SHELLFLAGS := /C
else
SHELL := /bin/sh
endif

DEV_TEST_APP ?= smoke_tests
IMAGE_TAG ?= local
IMAGE_PREFIX ?= cloud-run-builder/dev
DEV_TEST_IMAGE = $(IMAGE_PREFIX)-$(DEV_TEST_APP):$(IMAGE_TAG)
CONTAINER_NAME = cloud-run-builder-dev-$(DEV_TEST_APP)
DEV_TEST_APP_DIRS := $(foreach dir,$(wildcard */),$(if $(wildcard $(dir)Dockerfile),$(patsubst %/,%,$(dir))))
DEV_TEST_APP_NAMES := $(DEV_TEST_APP_DIRS)
BUILD_TARGETS := $(addprefix build-app-,$(DEV_TEST_APP_NAMES))

ifeq ($(DEV_TEST_APP),smoke_tests)
DEV_TEST_RUN_ARGS ?= --add-host host.docker.internal:host-gateway -e FUNCTION_URL=http://host.docker.internal:8080
endif

ifeq ($(OS),Windows_NT)
PYTHON ?= ..\..\.venv\Scripts\python.exe
else
PYTHON ?= ../../.venv/bin/python
endif

POSTGRES_IMPORT_COMPOSE = docker compose -f gcs_to_postgresql.compose.yaml

.PHONY: help list build build-all run smoke-test-export-local smoke-test-export-remote \
	local-import-postgres-up local-import-postgres-down local-import $(BUILD_TARGETS)

help:
	@echo "Targets: list, build DEV_TEST_APP=name, build-all, run DEV_TEST_APP=name [DEV_TEST_RUN_ARGS=...], smoke-test-export-local, smoke-test-export-remote"
	@echo "smoke-test-export-local checks local readiness and the BigQuery dataset/table, then exports CSV and Parquet."
	@echo "smoke-test-export-remote checks the BigQuery dataset/table, then exports CSV and Parquet via deployed Cloud Run."
	@echo "local-import-postgres-up starts local PostgreSQL and the GCS importer at localhost:5433 and localhost:8081."
	@echo "local-import triggers CSV and Parquet imports from the configured GCS requests into the local PostgreSQL container."

list:
	@echo $(DEV_TEST_APP_NAMES)

build:
	docker build --tag "$(DEV_TEST_IMAGE)" "$(DEV_TEST_APP)"

build-all: $(BUILD_TARGETS)

run: build
	docker run --rm --name "$(CONTAINER_NAME)" $(DEV_TEST_RUN_ARGS) "$(DEV_TEST_IMAGE)"

smoke-test-export-local:
	$(MAKE) -C smoke_tests smoke-test-export-local

smoke-test-export-remote:
	$(MAKE) -C smoke_tests smoke-test-export-remote

local-import-postgres-up:
	$(POSTGRES_IMPORT_COMPOSE) up -d --build --wait

local-import-postgres-down:
	$(POSTGRES_IMPORT_COMPOSE) down

local-import: local-import-postgres-up
	$(PYTHON) smoke_tests/local_import_smoke_test.py --request smoke_tests/import-request-csv.json
	$(PYTHON) smoke_tests/local_import_smoke_test.py --request smoke_tests/import-request-parquet.json
	$(POSTGRES_IMPORT_COMPOSE) exec -T postgres psql -U testuser -d testdb -c "SELECT 'example' AS table_name, COUNT(*) AS rows FROM public.example UNION ALL SELECT 'parq_example', COUNT(*) FROM public.parq_example;"

$(BUILD_TARGETS):
	docker build --tag "$(IMAGE_PREFIX)-$(patsubst build-app-%,%,$@):$(IMAGE_TAG)" "$(patsubst build-app-%,%,$@)"