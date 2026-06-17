.PHONY: dev up down logs build seed test backend-test smoke verify verify-up screenshots backend-shell frontend-shell clean

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

backend-shell:
	docker compose exec backend bash

frontend-shell:
	docker compose exec frontend sh

clean:
	docker compose down -v
	rm -rf data/*
