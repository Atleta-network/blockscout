#!/usr/bin/env bash
set -Eeuo pipefail

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
required_vars=(HOST_1 HOST_1_USERNAME BASE_PATH GITHUB_RUN_ID GITHUB_RUN_ATTEMPT)
for var in "${required_vars[@]}"; do
  [ -n "${!var:-}" ] || { echo "Required upload variable ${var} is missing" >&2; exit 1; }
done
deploy_id="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
[[ "${deploy_id}" =~ ^[0-9]+-[0-9]+$ ]] || { echo "Invalid deployment identity" >&2; exit 1; }
target="${HOST_1_USERNAME}@${HOST_1}"
remote_env=("BASE_PATH=$(printf '%q' "${BASE_PATH}")" "DEPLOY_ID=$(printf '%q' "${deploy_id}")")
receive_script=$(cat <<'REMOTE'
set -Eeuo pipefail
case "${BASE_PATH}" in /*) ;; *) echo "Deployment path must be absolute" >&2; exit 1 ;; esac
[[ "${DEPLOY_ID}" =~ ^[0-9]+-[0-9]+$ ]] || exit 1
umask 077
mkdir -p -- "${BASE_PATH}"
BASE_PATH=$(cd -- "${BASE_PATH}" && pwd -P)
[ "${BASE_PATH}" != / ] || exit 1
[ ! -L "${BASE_PATH}/.incoming" ] || { echo "Unexpected incoming directory" >&2; exit 1; }
mkdir -p -m 700 -- "${BASE_PATH}/.incoming"
chmod 700 "${BASE_PATH}/.incoming"
stage="${BASE_PATH}/.incoming/${DEPLOY_ID}"
mkdir -m 700 -- "${stage}"
tar --extract --file=- --no-same-owner --same-permissions --directory="${stage}"
chmod 600 "${stage}/docker-compose/.env" \
  "${stage}/docker-compose/envs/common-blockscout.env" \
  "${stage}/docker-compose/envs/common-stats.env" \
  "${stage}/docker-compose/envs/common-user-ops-indexer.env"
REMOTE
)

# Only this run's private staging receives files. No live Compose/env file is
# installed before the shared deployment lock is acquired by deploy-remote.sh.
tar -C "${root}" -cf - docker-compose .github/scripts/deploy-remote.sh |
  ssh -o BatchMode=yes "${target}" "${remote_env[*]} bash -c $(printf '%q' "${receive_script}")"
