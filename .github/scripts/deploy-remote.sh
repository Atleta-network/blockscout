#!/usr/bin/env bash
set -Eeuo pipefail

required_vars=(CI_REGISTRY CI_REGISTRY_REPO SERVICE_TAG BASE_PATH DEPLOY_ID)
for var in "${required_vars[@]}"; do
  [ -n "${!var:-}" ] || { echo "Required deployment variable ${var} is missing" >&2; exit 1; }
done
case "${BASE_PATH}" in /*) ;; *) echo "Deployment path must be absolute" >&2; exit 1 ;; esac
[[ "${DEPLOY_ID}" =~ ^[0-9]+-[0-9]+$ ]] || { echo "Invalid deployment identity" >&2; exit 1; }
BASE_PATH=$(cd -- "${BASE_PATH}" && pwd -P)
[ "${BASE_PATH}" != / ] || exit 1
DEPLOY_STAGE="${BASE_PATH}/.incoming/${DEPLOY_ID}"
[ -d "${DEPLOY_STAGE}" ] && [ ! -L "${DEPLOY_STAGE}" ] || { echo "Deployment staging is missing" >&2; exit 1; }
[ "$(cd -- "${DEPLOY_STAGE}" && pwd -P)" = "${DEPLOY_STAGE}" ] || { echo "Unexpected staging path" >&2; exit 1; }
bundle="${DEPLOY_STAGE}/docker-compose"
[ -f "${bundle}/docker-compose.yml" ] && [ -f "${bundle}/.env" ] || { echo "Incomplete staged bundle" >&2; exit 1; }

# Canonical physical path agrees with native-db-tls.py for trailing slashes,
# repeated separators and symlink aliases. There is no unlocked fallback.
lock_name=$(printf '%s' "${BASE_PATH}" | LC_ALL=C tr -cs 'A-Za-z0-9_.-' '_')
lock_file="/tmp/blockscout-deploy-${lock_name}.lock"
command -v flock >/dev/null 2>&1 || { echo "flock is required" >&2; exit 1; }
[ ! -L "${lock_file}" ] || { echo "Unexpected deployment lock" >&2; exit 1; }
[ ! -e "${lock_file}" ] || [ -f "${lock_file}" ] || { echo "Deployment lock must be regular" >&2; exit 1; }
umask 007
exec 9>>"${lock_file}"
umask 077
flock -w 900 9

private_command() {
  if ! "$@" >>"${DEPLOY_STAGE}/deploy-private.log" 2>&1; then
    echo "Deployment command failed; private output remains in the protected staging directory" >&2
    return 1
  fi
}

# Installation, including secret-bearing env files, is inside the same lock as
# native database cutovers and the complete Compose application rollout.
for path in /etc/db-access/tls/blockscout-db/current/fullchain.pem \
            /etc/db-access/tls/blockscout-db/current/privkey.pem \
            /etc/db-access/postgres/blockscout/db/pg_hba.conf \
            /etc/db-access/postgres/blockscout/stats-db/pg_hba.conf; do
  private_command sudo -n test -s "${path}"
done
cd -- "${bundle}"
frontend_image="${CI_REGISTRY}/${CI_REGISTRY_REPO}/blockscout-frontend:${SERVICE_TAG}"
backend_image="${CI_REGISTRY}/${CI_REGISTRY_REPO}/blockscout:${SERVICE_TAG}"
private_command sed -i "s|image: .*blockscout-frontend:.*|image: ${frontend_image}|" services/frontend.yml
private_command sed -i "s|image: .*blockscout:.*|image: ${backend_image}|" services/backend.yml
chmod 600 .env envs/common-blockscout.env envs/common-stats.env envs/common-user-ops-indexer.env
private_command docker compose config --quiet
private_command cp -a -- "${bundle}/." "${BASE_PATH}/"
cd -- "${BASE_PATH}"
private_command docker compose config --quiet
private_command docker compose pull
private_command docker compose up --detach --remove-orphans

wait_for_containers() {
  local containers=(db redis-db backend frontend stats-db stats visualizer sig-provider user-ops-indexer proxy certbot)
  local attempt container state health failed
  for attempt in $(seq 1 60); do
    failed=0
    for container in "${containers[@]}"; do
      if ! docker inspect "${container}" >/dev/null 2>&1; then
        failed=1
        break
      fi
      state=$(docker inspect -f '{{.State.Status}}' "${container}")
      health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container}")
      if [ "${state}" != running ] || { [ "${health}" != none ] && [ "${health}" != healthy ]; }; then
        failed=1
        break
      fi
    done
    [ "${failed}" -ne 0 ] || return 0
    sleep 5
  done
  echo "Containers did not become healthy in time; inspect protected host logs" >&2
  return 1
}

wait_for_containers
private_command docker exec proxy nginx -t
private_command docker exec proxy nginx -s reload
private_command docker exec backend curl -fsS --max-time 10 http://127.0.0.1:4000/api/health/liveness
# Only this invocation's staged copy is removed after success; live files and
# service data are not removed. Failed deployments retain protected diagnostics.
rm -rf -- "${DEPLOY_STAGE}"
echo "Deployment completed under the shared lock"
