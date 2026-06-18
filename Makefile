.PHONY: dev up down logs build seed test backend-test smoke smoke-ui smoke-ui-ephemeral verify verify-up screenshots backend-shell frontend-shell clean db-upgrade db-revision db-history

dev: up logs

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

build:
	docker compose build

seed:
	docker compose exec backend python -m app.seed

# Database schema migrations (Alembic). Runs inside the backend container so
# it picks up the compose-time DATABASE_URL automatically.
#
#   make db-upgrade            # apply all pending migrations
#   make db-revision MSG="add foo column"   # generate a new revision
#   make db-history            # show applied vs. pending
db-upgrade:
	docker compose exec backend alembic upgrade head

db-revision:
	@test -n "$(MSG)" || { echo "Pass MSG=... to describe the migration"; exit 1; }
	docker compose exec backend alembic revision --autogenerate -m "$(MSG)"

db-history:
	docker compose exec backend alembic history --verbose

test: backend-test

backend-test:
	docker compose exec -e MOCK_PROVIDERS=true backend pytest -q

smoke:
	docker compose exec -e MOCK_PROVIDERS=true backend python -m scripts.smoke

# Verify a *running* deployment end-to-end over real HTTP.
# Override the target with: make verify BASE_URL=https://avs.example.com MVP_PASSWORD=...
BASE_URL ?= http://localhost:8000
MVP_PASSWORD ?= changeme
verify:
	cd backend && BASE_URL=$(BASE_URL) MVP_PASSWORD=$(MVP_PASSWORD) python -m scripts.verify_deploy

# Ephemeral deploy verifier: boot docker compose, run verify_deploy against
# it, capture logs on failure, tear down. Single command to confirm the whole
# compose stack actually deploys end-to-end.
verify-up:
	./scripts/verify_up.sh

# Capture polished-surface screenshots against a running stack.
# Defaults assume `make up` + `make seed`. Override AVS_BASE_URL / AVS_API_URL /
# AVS_PASSWORD for a different deployment.
screenshots:
	node scripts/capture_screenshots.mjs

# Headless UI smoke: walks every primary route after login, asserts HTTP 200,
# a sentinel string, and zero runtime console/page errors. Exits non-zero on
# any failure so CI can wire it in directly.
smoke-ui:
	node scripts/smoke_ui.mjs

# Same as smoke-ui but boots an ephemeral backend + frontend on temp ports,
# seeds + renders, runs the walk, and tears everything down on exit. Lets a
# developer verify the full UI flow without keeping `make up` running and
# without conflicting with a long-running dev stack on the default ports.
smoke-ui-ephemeral:
	./scripts/smoke_ui_ephemeral.sh

backend-shell:
	docker compose exec backend bash

frontend-shell:
	docker compose exec frontend sh

clean:
	docker compose down -v
	rm -rf data/*
