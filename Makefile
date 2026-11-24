# jwt-proxy
#
#   make build              build the Docker image
#   make run                run the proxy (HTTP_PORT=9090 make run  -> http://127.0.0.1:9090)
#   make test               run the test suite inside Docker
#
# Targets suffixed -local do the same with a local Python 3.11+ instead of Docker.

.DEFAULT_GOAL := help

COMPOSE ?= docker compose
PYTHON  ?= python3
VENV    ?= .venv

# Only hand HTTP_PORT to Compose when the caller set it, so a value in .env still applies.
ifdef HTTP_PORT
export HTTP_PORT
endif

.PHONY: help build run stop logs test clean run-local test-local \
        deploy-ec2 status-ec2 tunnel-ec2 update-ec2 destroy-ec2 docs diagrams

help:
	@echo "Docker (default):"
	@echo "  make build        build the application image"
	@echo "  make run          start the proxy        (HTTP_PORT=8080 by default)"
	@echo "  make test         run the tests in a container"
	@echo "  make stop         stop and remove the containers"
	@echo "  make logs         follow the proxy logs"
	@echo "Without Docker (needs Python 3.11+):"
	@echo "  make run-local    start the proxy from a local virtualenv"
	@echo "  make test-local   run the tests from a local virtualenv"
	@echo "  make clean        remove containers, images, virtualenv and caches"
	@echo "AWS, one EC2 instance via CloudFormation (docs/deploy-ec2.md):"
	@echo "  make deploy-ec2   create/update the stack   (ALLOWED_CIDR=x.x.x.x/32 to open the port)"
	@echo "  make tunnel-ec2   http://localhost:8080 -> instance over SSM (no inbound ports)"
	@echo "  make update-ec2   pull the latest commit on the instance and restart"
	@echo "  make status-ec2   stack status and outputs"
	@echo "  make destroy-ec2  delete the stack"
	@echo "Documentation (needs Google Chrome; draw.io desktop for the diagrams):"
	@echo "  make docs         render docs/deliverables/*.md to PDF  (DOCS_LIVE=1 fills AWS values)"
	@echo "  make diagrams     export docs/architecture.drawio pages to docs/img/*.png"

# Compose reads variables from .env; create it from the example on first use.
.env:
	cp .env.example .env
	@echo "created .env from .env.example"

build: .env
	$(COMPOSE) build proxy

run: .env
	$(COMPOSE) up --build proxy

stop: .env
	$(COMPOSE) down --remove-orphans

logs: .env
	$(COMPOSE) logs --follow proxy

test: .env
	$(COMPOSE) --profile test run --build --rm tests

# ---------------------------------------------------------------------------
# Local alternative (no Docker)
# ---------------------------------------------------------------------------
$(VENV): pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --quiet --upgrade pip
	$(VENV)/bin/python -m pip install --quiet --editable ".[test]"
	@touch $(VENV)

run-local: .env $(VENV)
	set -a; . ./.env; set +a; $(if $(HTTP_PORT),HTTP_PORT=$(HTTP_PORT)) $(VENV)/bin/python -m jwt_proxy

test-local: $(VENV)
	$(VENV)/bin/python -m pytest -q

# ---------------------------------------------------------------------------
# AWS: a single EC2 instance (see docs/deploy-ec2.md)
# ---------------------------------------------------------------------------
deploy-ec2: .env
	deploy/ec2/deploy.sh up

status-ec2:
	deploy/ec2/deploy.sh status

tunnel-ec2:
	deploy/ec2/deploy.sh tunnel

update-ec2:
	deploy/ec2/deploy.sh update

destroy-ec2:
	deploy/ec2/deploy.sh down

# ---------------------------------------------------------------------------
# Documentation: PDF deliverables and diagrams (see docs/deliverables/README.md)
# ---------------------------------------------------------------------------
docs: $(VENV)
	$(VENV)/bin/python -m pip install --quiet markdown pygments pypdf reportlab
	$(VENV)/bin/python docs/deliverables/build.py $(if $(DOCS_LIVE),--live) $(DOCS_ARGS)

diagrams: $(VENV)
	$(VENV)/bin/python docs/deliverables/build.py --only none

clean:
	-$(COMPOSE) --profile test down --remove-orphans --rmi local 2>/dev/null
	rm -rf $(VENV) .pytest_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
