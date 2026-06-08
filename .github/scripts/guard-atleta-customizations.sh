#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  echo "Atleta customization guard failed: $*" >&2
  exit 1
}

require_file() {
  local file="$1"
  [ -f "${file}" ] || fail "missing ${file}"
}

require_pattern() {
  local file="$1"
  local pattern="$2"
  local description="$3"

  require_file "${file}"
  grep -Eq "${pattern}" "${file}" || fail "${description} is missing in ${file}"
}

reject_pattern() {
  local file="$1"
  local pattern="$2"
  local description="$3"

  require_file "${file}"
  if grep -Eq "${pattern}" "${file}"; then
    fail "${description} must not be present in ${file}"
  fi
}

require_file ".github/scripts/render-compose.sh"
require_file ".github/workflows/build-images.yml"
require_file ".github/workflows/deploy.yml"

require_pattern ".github/workflows/build-images.yml" "Atleta-network/blockscout-frontend" "Atleta frontend repository checkout"
require_pattern ".github/workflows/build-images.yml" "docker/login-action@v3" "current Docker registry login action"
require_pattern ".github/workflows/build-images.yml" "docker/setup-buildx-action@v3" "Buildx setup"
require_pattern ".github/workflows/build-images.yml" "blockscout-frontend:\\$\\{\\{ steps\\.service_tag\\.outputs\\.value \\}\\}" "commit-SHA frontend image tag"
require_pattern ".github/workflows/build-images.yml" "blockscout:\\$\\{\\{ steps\\.service_tag\\.outputs\\.value \\}\\}" "commit-SHA backend image tag"

require_pattern ".github/workflows/deploy.yml" "blockscout-deploy-\\$\\{\\{ inputs\\.environment \\}\\}" "environment deploy concurrency"
require_pattern ".github/workflows/deploy.yml" "render-compose\\.sh" "compose render step"
require_pattern ".github/workflows/deploy.yml" "docker compose config --quiet" "compose validation"
require_pattern ".github/workflows/deploy.yml" "docker compose up --detach --remove-orphans" "non-destructive compose up"
require_pattern ".github/workflows/deploy.yml" "flock -w 900" "remote deploy lock"
require_pattern ".github/workflows/deploy.yml" "/api/v2/config" "backend post-deploy API check"
reject_pattern ".github/workflows/deploy.yml" "docker compose down" "destructive compose down"

require_pattern ".github/scripts/render-compose.sh" "required_vars=\\(" "strict render required variables"
require_pattern ".github/scripts/render-compose.sh" "render_file services/frontend.yml" "frontend service rendering"
require_pattern ".github/scripts/render-compose.sh" "render_file services/backend.yml" "backend service rendering"
require_pattern ".github/scripts/render-compose.sh" "render_file proxy/default.conf.template '\\$\\{DOMAIN_NAME\\}'" "limited nginx template substitution"
require_pattern ".github/scripts/render-compose.sh" "docker compose -f docker-compose.yml config --quiet" "render-time compose validation"

require_pattern "docker-compose/envs/common-frontend.env" "NEXT_PUBLIC_API_HOST=\\$\\{DOMAIN_NAME\\}" "environment-driven frontend host"
require_pattern "docker-compose/envs/common-frontend.env" "NEXT_PUBLIC_NETWORK_RPC_URL=https://\\$\\{PUB_RPC_NAME\\}" "environment-driven public RPC"
require_pattern "docker-compose/envs/common-frontend.env" "Atleta-network/blockscout_deprecated" "Atleta frontend assets"
require_pattern "docker-compose/services/backend.yml" "create_and_migrate\\(\\)" "Blockscout create_and_migrate startup"
require_pattern "docker-compose/services/backend.yml" "healthcheck:" "backend healthcheck"
require_pattern "docker-compose/services/nginx.yml" "healthcheck:" "nginx healthcheck"
require_pattern "docker-compose/services/redis.yml" "healthcheck:" "redis healthcheck"
require_pattern "docker-compose/services/db.yml" "pg_isready -U blockscout -d blockscout" "Blockscout DB healthcheck"
require_pattern "docker-compose/services/stats.yml" "pg_isready -U stats -d stats" "stats DB healthcheck"
require_pattern "docker-compose/services/certbot.yml" "CERTBOT_EMAIL=\\$\\{CERTBOT_EMAIL\\}" "environment-driven certbot email"

services=(
  backend
  certbot
  db
  frontend
  nft_media_handler
  nginx
  redis
  sig-provider
  smart-contract-verifier
  stats
  user-ops-indexer
  visualizer
)

for service in "${services[@]}"; do
  case "${service}" in
    nft_media_handler)
      file="docker-compose/services/nft_media_handler.yml"
      ;;
    *)
      file="docker-compose/services/${service}.yml"
      ;;
  esac

  require_pattern "${file}" "com\\.atleta\\.role: \"blockscout\"" "Atleta role label for ${service}"
done

reject_pattern "docker-compose/envs/generated/mainnet/values.env" "stage-blockscout" "stale stage domain in mainnet generated values"
reject_pattern "docker-compose/envs/generated/testnet_v2/values.env" "stage-blockscout" "stale stage domain in testnet_v2 generated values"

echo "Atleta customization guard passed."
