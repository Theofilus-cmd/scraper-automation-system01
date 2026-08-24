.PHONY: bootstrap check-lockfiles up up-browser up-workers up-storage up-monitoring health test test-backend test-frontend lint typecheck migrate logs stop reset

# Combine multiple optional profiles by chaining --profile flags, e.g.:
#   docker compose --profile browser --profile workers up -d --build

# Images used ONLY to generate lockfiles (see `bootstrap` below). Never
# referenced by docker-compose.yml -- those services build from the local
# Dockerfiles instead. Picked to match backend/Dockerfile's and
# frontend/Dockerfile's own base images exactly, so the resolved lockfiles
# match what the real build will use.
UV_IMAGE := ghcr.io/astral-sh/uv:python3.12-bookworm-slim
NODE_IMAGE := node:22-alpine

# One-time setup for a clean clone: no Python/uv/Node/npm required on the
# host, only Docker. Runs uv/npm inside short-lived containers, bind-mounted
# onto the real project directories, so the generated lockfiles land
# directly on disk here -- review and commit them afterward (see README).
bootstrap:
	@test -f .env || cp .env.example .env
	docker run --rm -v "$(CURDIR)/backend:/app" -w /app $(UV_IMAGE) uv lock
	docker run --rm -v "$(CURDIR)/tools/mock-store:/app" -w /app $(UV_IMAGE) uv lock
	docker run --rm -v "$(CURDIR)/frontend:/app" -w /app $(NODE_IMAGE) npm install --package-lock-only
	@echo ""
	@echo "Lockfiles generated. Review, then commit them:"
	@echo "  git add backend/uv.lock tools/mock-store/uv.lock frontend/package-lock.json"
	@echo "  git commit -m 'Add generated lockfiles'"

# Guard so a missing lockfile fails fast with a clear message instead of a
# confusing error partway through `docker compose build`.
check-lockfiles:
	@test -f backend/uv.lock || (echo "Missing backend/uv.lock -- run 'make bootstrap' first (see README)." && exit 1)
	@test -f tools/mock-store/uv.lock || (echo "Missing tools/mock-store/uv.lock -- run 'make bootstrap' first (see README)." && exit 1)
	@test -f frontend/package-lock.json || (echo "Missing frontend/package-lock.json -- run 'make bootstrap' first (see README)." && exit 1)

up: check-lockfiles
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
