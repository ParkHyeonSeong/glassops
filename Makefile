.PHONY: help build up down logs restart clean dev prod

# Default port
PORT ?= 7440

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

build: ## Build the GlassOps image
	docker compose build

up: ## Start GlassOps (build if needed)
	docker compose up -d --build
	@echo ""
	@echo "  GlassOps is running at http://localhost:$(PORT)"
	@echo ""

down: ## Stop GlassOps
	docker compose down

logs: ## Show logs (follow)
	docker compose logs -f

restart: ## Restart GlassOps
	docker compose restart

clean: ## Stop and remove all data
	docker compose down -v --remove-orphans

dev: ## Start in dev mode (with hot reload)
	docker compose -f docker-compose.dev.yml up -d --build
	@echo ""
	@echo "  GlassOps dev running at http://localhost:$(PORT)"
	@echo ""

prod: ## Production build and start
	docker compose build --no-cache
	docker compose up -d
	@echo ""
	@echo "  GlassOps production running at http://localhost:$(PORT)"
	@echo ""

status: ## Show container status
	@docker compose ps
	@echo ""
	@curl -s http://localhost:$(PORT)/health 2>/dev/null && echo "" || echo "  Not running"
	@curl -s http://localhost:$(PORT)/api/agents 2>/dev/null && echo "" || true
