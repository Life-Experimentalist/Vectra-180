# Vectra-180 development tasks.
#
# Every target is a thin wrapper around a command you could type yourself. The
# point is that CI and a laptop run the identical thing -- if `make gate` is
# green here it is green there.

UV ?= uv
PYTHON_SOURCES = src tests

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

# -- setup -------------------------------------------------------------------

.PHONY: install
install: ## Create the virtualenv and install everything, including dev tools
	$(UV) sync --all-extras

.PHONY: hooks
hooks: ## Install the pre-commit hooks into .git
	$(UV) run pre-commit install

# -- checks ------------------------------------------------------------------

.PHONY: format
format: ## Rewrite source to the project style
	$(UV) run ruff format $(PYTHON_SOURCES)
	$(UV) run ruff check --fix $(PYTHON_SOURCES)

.PHONY: lint
lint: ## Check style and formatting without changing anything
	$(UV) run ruff format --check $(PYTHON_SOURCES)
	$(UV) run ruff check $(PYTHON_SOURCES)

.PHONY: typecheck
typecheck: ## Run mypy over the package and the suite
	$(UV) run mypy $(PYTHON_SOURCES)

.PHONY: test
test: ## Run the full suite with coverage
	$(UV) run pytest

.PHONY: test-fast
test-fast: ## Skip the integration tests, which drive real files and sockets
	$(UV) run pytest -m "not integration"

.PHONY: gate
gate: lint typecheck test ## Everything CI runs, in the order CI runs it

# -- running -----------------------------------------------------------------

.PHONY: doctor
doctor: ## Check that this machine can record
	$(UV) run vectra180 doctor

.PHONY: run
run: ## Record and serve until interrupted
	$(UV) run vectra180 run

.PHONY: view
view: ## Open the desktop control panel
	$(UV) run vectra180 view

# -- packaging ---------------------------------------------------------------

.PHONY: build
build: ## Build the wheel and sdist into dist/
	$(UV) build

.PHONY: docker
docker: ## Build the container image
	docker build -t vectra180:local .

.PHONY: clean
clean: ## Remove build output and tool caches
	rm -rf dist build .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
