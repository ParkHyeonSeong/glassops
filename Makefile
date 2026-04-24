.PHONY: help build up down logs restart clean dev dev-down shell prod status update agent-up agent-up-gpu agent-down agent-logs

# Default port
PORT ?= 7440

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

build: ## Build the GlassOps image
	docker compose build

up: ## Start GlassOps (build if needed)
	docker compose down --remove-orphans 2>/dev/null || true
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

dev: ## Start in dev mode (backend/agent hot-reload)
	docker compose -f docker-compose.dev.yml up -d --build
	@echo ""
	@echo "  GlassOps dev running at http://localhost:$(PORT)"
	@echo ""

dev-down: ## Stop dev mode
	docker compose -f docker-compose.dev.yml down

shell: ## Open shell in running container
	docker compose exec glassops bash

prod: ## Production build (no cache) and start
	docker compose down --remove-orphans 2>/dev/null || true
	docker compose build --no-cache
	docker compose up -d
	@echo ""
	@echo "  GlassOps production running at http://localhost:$(PORT)"
	@echo ""

status: ## Show container status + agent connection
	@docker compose ps
	@echo ""
	@curl -s http://localhost:$(PORT)/health 2>/dev/null && echo "" || echo "  Not running"
	@curl -s http://localhost:$(PORT)/api/agents 2>/dev/null && echo "" || true

update: ## Pull latest and rebuild
	git pull
	docker compose build --no-cache
	docker compose up -d
	@echo ""
	@echo "  Updated and running at http://localhost:$(PORT)"
	@echo ""

agent-up: ## Start agent only (remote host monitoring, no GPU)
	@test -f agent.env || (echo "  Missing agent.env — copy agent.env.example and edit" && exit 1)
	docker compose -f docker-compose.agent.yml up -d --build
	@echo ""
	@echo "  Agent running — docker logs -f glassops-agent"
	@echo ""

agent-up-gpu: ## Start agent only with NVIDIA GPU access
	@test -f agent.env || (echo "  Missing agent.env — copy agent.env.example and edit" && exit 1)
	docker compose -f docker-compose.agent.yml -f docker-compose.agent.gpu.yml up -d --build
	@echo ""
	@echo "  Agent (GPU) running — docker logs -f glassops-agent"
	@echo ""

agent-down: ## Stop agent
	docker compose -f docker-compose.agent.yml down

agent-logs: ## Tail agent logs
	docker logs -f glassops-agent
