.DEFAULT_GOAL := help

VENV   := .venv
BIN    := $(VENV)/bin
PYTHON := $(BIN)/python
IMAGE  := ai-agent-service:latest
PORT   ?= 8000
COMPOSE     := docker compose
COMPOSE_ENV := docker compose -f docker-compose.yml -f docker-compose.docker-env.yml

.PHONY: help venv install run run-log test test-unit cov lint format typecheck check \
        docker-build up up-env down logs restart health clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---------- Environment ----------
venv:  ## Create the virtual environment
	python3 -m venv $(VENV)

install: venv  ## Install the project with dev dependencies
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

# ---------- Run ----------
run:  ## Run the service locally (host and port come from the settings)
	$(PYTHON) main.py

# tee -i ignores Ctrl+C, so it keeps reading until the service has logged its shutdown;
# without it tee dies first and every shutdown log line fails with BrokenPipeError.
# 2>&1 also captures what is printed before logging is configured (e.g. invalid settings).
run-log:  ## Run locally and also save the logs to logs/agent-<timestamp>.log
	@mkdir -p logs
	$(PYTHON) main.py 2>&1 | tee -i logs/agent-$$(date +%Y%m%d-%H%M%S).log

# ---------- Quality ----------
test:  ## Run the whole test suite (includes the in-process MCP integration tests)
	$(PYTHON) -m pytest

test-unit:  ## Run only the unit tests (no local server is started)
	$(PYTHON) -m pytest -m "not integration"

cov:  ## Run tests with a coverage report (fails under the configured minimum)
	$(PYTHON) -m pytest --cov

lint:  ## Lint and check formatting with ruff
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:  ## Format and auto-fix with ruff
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

typecheck:  ## Static type checking with pyright (strict)
	$(BIN)/pyright --pythonpath $(PYTHON)

check: lint typecheck test  ## Run lint, typecheck and tests (use before a commit)

# ---------- Docker ----------
docker-build:  ## Build the Docker image
	docker build -t $(IMAGE) .

up:  ## Start standalone: agent + its own Ollama and Jaeger (MCP server on the host)
	$(COMPOSE) --profile standalone up -d --build

up-env:  ## Start on the shared docker-env network (docker-env must be running)
	$(COMPOSE_ENV) up -d --build

down:  ## Stop and remove the container (either mode)
	$(COMPOSE_ENV) down

logs:  ## Follow the container logs
	$(COMPOSE) logs -f agent

restart:  ## Rebuild and restart on the docker-env network
	$(COMPOSE_ENV) up -d --build --force-recreate

health:  ## Check the liveness and readiness endpoints
	@curl -fsS http://localhost:$(PORT)/health/live && echo
	@curl -sS http://localhost:$(PORT)/health/ready && echo

# ---------- Cleanup ----------
clean:  ## Remove caches and build artifacts
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) \
		-not -path "./$(VENV)/*" -prune -exec rm -rf {} +
	rm -rf build dist *.egg-info .coverage htmlcov
