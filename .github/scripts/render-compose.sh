#!/usr/bin/env bash
set -Eeuo pipefail

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
