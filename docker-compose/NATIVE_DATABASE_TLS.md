# Native PostgreSQL TLS: mainnet and testnet_v2

The public ports 7432 (`db`) and 7433 (`stats-db`) use the existing Blockscout
hostname for certificate identity and a direct server address in clients. DNS,
Cloudflare and application passwords are unchanged. Applications and startup
migrations retain SCRAM connections on the existing Docker bridge without TLS.

| Environment | Certificate hostname | Direct address |
|---|---|---|
| testnet_v2 | blockscout.testnet-v2.atleta.network | 23.162.152.177 |
| mainnet | blockscout.atleta.network | 186.190.215.128 |

## Provision before deployment

1. Reuse `/blockscout/services/certbot/www` through the controlled symlink
   `/opt/db-access/webroot/blockscout`. Verify a random HTTP-01 challenge from
   outside the host with the actual hostname; do not change existing website TLS.
2. Use the observability Ansible `db_tls_certificate` role, limited to one host:
   certificate id `blockscout-db`, runtime gid `2000`, dedicated Compose directory
   `/opt/db-access/blockscout-db`, ACME directory `/etc/db-access/acme/blockscout-db`,
   TLS directory `/etc/db-access/tls/blockscout-db`, webroot above, and the reviewed
   pinned certbot image. Initially disable the timer and leave reload/probes empty.
   Run staging issuance before production. Private keys remain on each host.
3. After exact-commit validation succeeds, copy `.github/scripts/native-db-tls.py`
   to the host. As root, first run `python3 native-db-tls.py testnet_v2 stats-db
   --confirmation ENABLE_NATIVE_TLS_ON_testnet_v2`, then `db`. For mainnet use
   `mainnet` and `ENABLE_NATIVE_TLS_ON_mainnet`. Complete all external and app
   checks on one service before proceeding. Do not run a project-wide Compose up.
   The helper checks the host address, retains the exact PostgreSQL 17 image and
   existing tuning/data mounts, tests TLS/HBA on an isolated disposable empty
   cluster, preserves private Compose files in root-only server backups, and
   recreates only the selected database. It rolls back its Compose change if
   verification fails. Existing PostgreSQL data and password verifiers are checked.
4. Verify both public address families from independent hosts: trusted hostname,
   SCRAM authentication, plaintext rejection, wrong-name/unknown-CA refusal. Test
   actual `verify-full` SQL with existing passwords and internal plaintext SCRAM.
   Check backend/stats health, migration paths and unchanged application containers.
5. Enable the certificate timer with reload containers `[db, stats-db]` and native
   PostgreSQL fingerprint probes `127.0.0.1:7432` and `127.0.0.1:7433`. Test renewal,
   HUP and served fingerprints without restarting PostgreSQL.

HBA keeps local administration and allows SCRAM from the verified project bridge.
Its gateway is excluded from plaintext **before** the subnet rule because Docker's
IPv6 userland proxy can present public clients as that gateway. All other remote
addresses require TLS plus SCRAM. No broad `trust` rule or new public pool is added.

Ordinary Deploy renders the same TLS mounts/flags and refuses to copy the bundle
if host certificate/HBA files are absent. No new GitHub environment secret is
needed: migrations run inside the project network. Export public CA and password-
free client profiles only. Example libpq parameters: `host=<hostname>
hostaddr=<direct-address> port=7432 dbname=blockscout user=blockscout
sslmode=verify-full sslrootcert=<public-ca-bundle>`. Passwords stay in the existing
credential store. Keep external administrative-role restriction as a separate phase.
