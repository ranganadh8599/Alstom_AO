.PHONY: help install install-dev lint fmt run docker-up docker-down migrate revision check

help:
	@echo "Alstom AO - common tasks"
	@echo "  make install      install runtime dependencies"
	@echo "  make install-dev  install with dev + postgres dependencies"
	@echo "  make lint         ruff check"
	@echo "  make fmt          ruff format + fix"
	@echo "  make run          run the API on :8080"
	@echo "  make check        lint + boot smoke test"
	@echo "  make docker-up    docker compose up --build"
	@echo "  make docker-down  stop and remove containers + volumes"
	@echo "  make migrate      alembic upgrade head"
	@echo "  make revision m=  add a migration"

install:
	python -m pip install -e .

install-dev:
	python -m pip install -e ".[dev,postgres]"

lint:
	python -m ruff check src examples

fmt:
	python -m ruff check src examples --fix
	python -m ruff format src examples

run:
	uvicorn alstom_ao.main:app --host 0.0.0.0 --port 8080 --reload

check: lint
	python scripts/smoke_check.py

docker-up:
	docker compose up --build

docker-down:
	docker compose down -v

migrate:
	alembic upgrade head

revision:
	alembic revision --autogenerate -m "$(m)"
