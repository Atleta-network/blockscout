#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

required_vars=(
  CI_REGISTRY
  CI_REGISTRY_REPO
  SERVICE_TAG
  BACKEND_VERSION
  DOMAIN_NAME
  CERTBOT_EMAIL
  IS_TESTNET
  NETWORK_NAME
  NETWORK_SHORT_NAME
  NETWORK_CHAIN_ID
  NETWORK_CURRENCY_NAME
  NETWORK_CURRENCY_SYMBOL
  NEXT_PUBLIC_WALLET_CONNECT_PROJECT_ID
  PRIV_RPC_NAME
  PUB_RPC_NAME
  WALLETCONNECT
  BLOCKSCOUT_DB_PASSWORD
  BLOCKSCOUT_STATS_DB_PASSWORD
  BLOCKSCOUT_SECRET_KEY_BASE
  BLOCKSCOUT_RELEASE_COOKIE
)

for var in "${required_vars[@]}"; do
  if [ -z "${!var:-}" ]; then
    echo "Required variable ${var} is not set" >&2
    exit 1
  fi
done

render_file() {
  local file="$1"
  local vars="${2:-}"
  local tmp
  tmp="$(mktemp)"

  if [ -n "${vars}" ]; then
    envsubst "${vars}" < "${file}" > "${tmp}"
  else
    envsubst < "${file}" > "${tmp}"
  fi

  mv "${tmp}" "${file}"
}

cd docker-compose

# Keep environment-specific credentials in the deployment environment, never in Git.
# Hex credentials are deliberately URL-safe; reject ambiguous values before forming DSNs.
for var in BLOCKSCOUT_DB_PASSWORD BLOCKSCOUT_STATS_DB_PASSWORD; do
  [[ "${!var}" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "${var} must be URL-safe" >&2; exit 1; }
done
export STATS__DB_URL="postgres://stats:${BLOCKSCOUT_STATS_DB_PASSWORD}@stats-db:5432/stats"
export STATS__BLOCKSCOUT_DB_URL="postgresql://blockscout:${BLOCKSCOUT_DB_PASSWORD}@db:5432/blockscout"
export USER_OPS_INDEXER__DATABASE__CONNECT__URL="${STATS__BLOCKSCOUT_DB_URL}"
printf '%s\n' \
  "BLOCKSCOUT_DB_PASSWORD=${BLOCKSCOUT_DB_PASSWORD}" \
  "BLOCKSCOUT_STATS_DB_PASSWORD=${BLOCKSCOUT_STATS_DB_PASSWORD}" \
  "STATS__DB_URL=${STATS__DB_URL}" \
  "STATS__BLOCKSCOUT_DB_URL=${STATS__BLOCKSCOUT_DB_URL}" \
  "USER_OPS_INDEXER__DATABASE__CONNECT__URL=${USER_OPS_INDEXER__DATABASE__CONNECT__URL}" > .env

render_file envs/common-frontend.env
render_file envs/common-blockscout.env
render_file envs/common-user-ops-indexer.env
render_file envs/common-stats.env

mkdir -p services/.well-known
printf "%s" "${WALLETCONNECT}" > services/.well-known/walletconnect.txt

render_file services/frontend.yml '${CI_REGISTRY} ${CI_REGISTRY_REPO} ${SERVICE_TAG}'
render_file services/backend.yml '${CI_REGISTRY} ${CI_REGISTRY_REPO} ${SERVICE_TAG}'
render_file services/certbot.yml '${DOMAIN_NAME} ${CERTBOT_EMAIL}'

# Limit proxy substitutions to DOMAIN_NAME so nginx runtime variables stay intact.
render_file proxy/default.conf.template '${DOMAIN_NAME}'
render_file proxy/default.conf.template.new '${DOMAIN_NAME}'
render_file proxy/explorer.conf.template '${DOMAIN_NAME}'
render_file proxy/microservices.conf.template '${DOMAIN_NAME}'

docker compose -f docker-compose.yml config --quiet
