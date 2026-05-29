.PHONY: dev up down logs build seed test backend-test smoke verify backend-shell frontend-shell clean

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

backend-shell:
	docker compose exec backend bash

frontend-shell:
	docker compose exec frontend sh

clean:
	docker compose down -v
	rm -rf data/*
