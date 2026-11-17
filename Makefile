.PHONY: help install install-dev test lint fmt run demo docker-up docker-down migrate revision

help:
	@echo "Alstom AO - common tasks"
	@echo "  make install      install runtime dependencies"
	@echo "  make install-dev  install with dev dependencies"
	@echo "  make test         run the test suite"
	@echo "  make lint         ruff check"
	@echo "  make fmt          ruff format + fix"
	@echo "  make run          run the API on :8080 (SQLite, mock LLM)"
	@echo "  make demo         run API + mock A2A agents locally"
	@echo "  make docker-up    docker compose up --build"
	@echo "  make migrate      alembic upgrade head"
	@echo "  make revision m= add a migration"

install:
	python -m pip install -e .

install-dev:
	python -m pip install -e ".[dev,postgres]"

test:
	python -m pytest -q

lint:
	python -m ruff check src tests examples

fmt:
	python -m ruff check src tests examples --fix
	python -m ruff format src tests examples

run:
	uvicorn alstom_ao.main:app --host 0.0.0.0 --port 8080 --reload

demo:
	python scripts/run_demo.py

docker-up:
	docker compose up --build

docker-down:
	docker compose down -v

migrate:
	alembic upgrade head

revision:
	alembic revision --autogenerate -m "$(m)"
