.PHONY: up up-browser up-workers up-storage up-monitoring health test test-backend test-frontend lint typecheck migrate logs stop reset

# Combine multiple optional profiles by chaining --profile flags, e.g.:
#   docker compose --profile browser --profile workers up -d --build

up:
	docker compose up -d --build

up-browser:
	docker compose --profile browser up -d --build

up-workers:
	docker compose --profile workers up -d --build

up-storage:
	docker compose --profile storage up -d --build

up-monitoring:
	docker compose --profile monitoring up -d --build

health:
	curl -sf http://localhost:8000/healthz && echo
	curl -sf http://localhost:8000/readyz && echo

test: test-backend test-frontend

test-backend:
	docker compose exec api pytest

test-frontend:
	docker compose exec frontend npm test

lint:
	docker compose exec api ruff check .
	docker compose exec frontend npm run lint

typecheck:
	docker compose exec api mypy app
	docker compose exec frontend npm run typecheck

migrate:
	docker compose exec api alembic upgrade head

logs:
	docker compose logs -f

stop:
	docker compose down

reset:
	./scripts/reset_dev_env.sh
