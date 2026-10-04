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

ifeq ($(DEV_TEST_APP),postgres)
DEV_TEST_RUN_ARGS ?= -p 5432:5432 -e POSTGRES_USER=testuser -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=testdb
else ifeq ($(DEV_TEST_APP),smoke_tests)
DEV_TEST_RUN_ARGS ?= --add-host host.docker.internal:host-gateway -e FUNCTION_URL=http://host.docker.internal:8080
endif

.PHONY: help list build build-all run $(BUILD_TARGETS)

help:
	@echo "Targets: list, build DEV_TEST_APP=name, build-all, run DEV_TEST_APP=name [DEV_TEST_RUN_ARGS=...]"

list:
	@echo $(DEV_TEST_APP_NAMES)

build:
	docker build --tag "$(DEV_TEST_IMAGE)" "$(DEV_TEST_APP)"

build-all: $(BUILD_TARGETS)

run: build
	docker run --rm --name "$(CONTAINER_NAME)" $(DEV_TEST_RUN_ARGS) "$(DEV_TEST_IMAGE)"

$(BUILD_TARGETS):
	docker build --tag "$(IMAGE_PREFIX)-$(patsubst build-app-%,%,$@):$(IMAGE_TAG)" "$(patsubst build-app-%,%,$@)"