.PHONY: dev up down logs build seed test backend-test backend-shell frontend-shell clean

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

backend-shell:
	docker compose exec backend bash

frontend-shell:
	docker compose exec frontend sh

clean:
	docker compose down -v
	rm -rf data/*
