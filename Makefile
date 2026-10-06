ifeq ($(OS),Windows_NT)
SHELL := cmd.exe
.SHELLFLAGS := /C
PYTHON ?= ..\.venv\Scripts\python.exe
GCLOUD ?= gcloud.cmd
else
PYTHON ?= ../.venv/bin/python
GCLOUD ?= gcloud
endif

APP ?= bq_to_gcs_helpers
PROJECT_ID ?= jag-pgsql-gke
IMAGE_TAG ?= local
REGION ?= us-central1
AR_REPOSITORY ?= cloud-run-source-deploy
# No default: pass explicitly, e.g. `make deploy RUNTIME_SERVICE_ACCOUNT=name@project.iam.gserviceaccount.com`.
# This is independent of cloudbuild.yaml's _RUNTIME_SERVICE_ACCOUNT substitution, which only applies to
# Cloud Build-triggered deploys, not this local/manual `make` workflow.
RUNTIME_SERVICE_ACCOUNT ?=
RUN_ENV_VARS ?=
RUN_SECRETS ?=
CLOUD_SQL_CONNECTION_NAME ?=
RUN_TIMEOUT_SECONDS ?=
comma := ,
DEPLOY_ENV_VARS = $(strip $(RUN_ENV_VARS)$(if $(strip $(CLOUD_SQL_CONNECTION_NAME)),$(if $(strip $(RUN_ENV_VARS)),$(comma))CLOUD_SQL_CONNECTION_NAME=$(CLOUD_SQL_CONNECTION_NAME)))

DEPLOY_ENV ?= dev

SERVICE_NAME ?= $(subst _,-,$(APP))-$(DEPLOY_ENV)
LOCAL_IMAGE := $(APP):$(IMAGE_TAG)
REMOTE_IMAGE = $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(AR_REPOSITORY)/$(APP):$(IMAGE_TAG)
APP_DIRS := $(foreach dir,$(wildcard apps/*),$(if $(wildcard $(dir)/Dockerfile),$(dir)))
APP_NAMES := $(patsubst apps/%,%,$(APP_DIRS))
BUILD_APP_TARGETS := $(addprefix build-app-,$(APP_NAMES))

.PHONY: help list-apps build build-all push deploy release test test-deps check-project check-deploy $(BUILD_APP_TARGETS)

help:
	@echo "Targets: list-apps, build APP=name, build-all, push, deploy, release, test, test-deps"

list-apps:
	@echo $(APP_NAMES)

build:
	docker build --tag "$(LOCAL_IMAGE)" "apps/$(APP)"

build-all: $(BUILD_APP_TARGETS)

$(BUILD_APP_TARGETS):
	docker build --tag "$(patsubst build-app-%,%,$@):$(IMAGE_TAG)" "apps/$(patsubst build-app-%,%,$@)"

push: check-project build
	$(if $(strip $(PROJECT_ID)),,$(error PROJECT_ID is required, for example make push PROJECT_ID=my-project))
	$(GCLOUD) auth configure-docker "$(REGION)-docker.pkg.dev" --quiet
	docker tag "$(LOCAL_IMAGE)" "$(REMOTE_IMAGE)"
	docker push "$(REMOTE_IMAGE)"

deploy: check-deploy push
	$(GCLOUD) run deploy "$(SERVICE_NAME)" --project="$(PROJECT_ID)" --region="$(REGION)" --image="$(REMOTE_IMAGE)" --service-account="$(RUNTIME_SERVICE_ACCOUNT)" --no-allow-unauthenticated --quiet $(if $(strip $(DEPLOY_ENV_VARS)),--update-env-vars="$(DEPLOY_ENV_VARS)") $(if $(strip $(RUN_SECRETS)),--update-secrets="$(RUN_SECRETS)") $(if $(strip $(CLOUD_SQL_CONNECTION_NAME)),--add-cloudsql-instances="$(CLOUD_SQL_CONNECTION_NAME)") $(if $(strip $(RUN_TIMEOUT_SECONDS)),--timeout="$(RUN_TIMEOUT_SECONDS)")

check-project:
	$(if $(strip $(PROJECT_ID)),,$(error PROJECT_ID is required, for example make push PROJECT_ID=my-project))

check-deploy:
	$(if $(strip $(PROJECT_ID)),,$(error PROJECT_ID is required, for example make deploy PROJECT_ID=my-project))
	$(if $(strip $(RUNTIME_SERVICE_ACCOUNT)),,$(error RUNTIME_SERVICE_ACCOUNT is required for Cloud Run deploy))

release: deploy

test:
	$(PYTHON) -m unittest discover -s smoke_tests -p "test_*.py" -v

test-deps:
	$(PYTHON) -m pip install -r smoke_tests/requirements.txt