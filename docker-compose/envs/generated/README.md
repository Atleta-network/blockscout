Generated environment files for GitHub Actions environments:

- `mainnet`
- `testnet_v2`

How these files were produced:

1. Read GitHub environment variables via `gh api`.
2. Applied those values to templates in `docker-compose/envs/common-*.env` using `envsubst`.
3. Kept placeholders where values are not readable from GitHub (secrets) or missing in environment variables.

Important placeholders to fill manually:

- `PUB_RPC_NAME`
- `NEXT_PUBLIC_WALLET_CONNECT_PROJECT_ID`
- `NEXT_PUBLIC_ACCOUNT_DYNAMIC_ENVIRONMENT_ID` (only needed when Dynamic auth provider is enabled)

If `NEXT_PUBLIC_ACCOUNT_AUTH_PROVIDER=none`, `NEXT_PUBLIC_ACCOUNT_DYNAMIC_ENVIRONMENT_ID` can stay empty.
