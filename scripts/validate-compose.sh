#!/bin/sh
set -eu

created_env=0
created_agent_env=0

cleanup() {
  if [ "$created_env" -eq 1 ]; then
    rm -f .env
  fi
  if [ "$created_agent_env" -eq 1 ]; then
    rm -f agent.env
  fi
}

trap cleanup EXIT HUP INT TERM

if [ ! -e .env ] && [ ! -L .env ]; then
  cp .env.example .env
  created_env=1
fi

if [ ! -e agent.env ] && [ ! -L agent.env ]; then
  cp agent.env.example agent.env
  created_agent_env=1
fi

docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.dev.yml config --quiet
docker compose -f docker-compose.agent.yml config --quiet
docker compose -f docker-compose.agent.yml -f docker-compose.agent.gpu.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.socket-proxy.yml config --quiet

sh -n deploy/entrypoint.sh
sh -n backend/entrypoint.sh
