# Blockscout deployment and native PostgreSQL TLS safety

Application deployment and the opt-in `scripts/native-db-tls.py` cutover use the
same remote lock. Canonicalize `BASE_PATH` to its physical absolute directory
before deriving `/tmp/blockscout-deploy-<sanitized-path>.lock`; trailing slashes,
repeated separators and symlink aliases must not create different locks.
The native helper acquires this lock before reading the running baseline and
keeps it through verification or rollback. Root-created locks retain the private
Compose project's owner/group and mode 0660 so the existing SSH deployment user
can open them. Symlink/nonregular lock files are refused.

The workflow's upload step transfers only into
`BASE_PATH/.incoming/<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>/`, mode 0700. It does not
replace live Compose files, dotenv files, certificates or data. Each run has its
own directory, and an existing run directory is not overwritten. Public bundle
permissions are preserved; credential-bearing environment files are mode 0600.

The staged remote deployer takes the shared lock before checking host TLS/HBA
prerequisites, validating the staged bundle, installing files into the live
directory, pulling images or running Compose. There is no unlocked fallback.
Database TLS cutovers therefore cannot race with the workflow's file copy or
service recreation. Compose/nginx/curl output is retained only in protected
staging logs, rather than printing dotenv or private application diagnostics into
Actions. On success only the exact run's staging directory is removed; a failure
retains it for authorized host-side investigation. Live files and data are never
removed by staging cleanup.

The native TLS helper preserves the original command, environment/passwords,
image identity and data mounts. A candidate Compose validation failure restores
the private file without calling Compose up. Once database recreation has been
attempted, an error restores the original private file and performs a single
service rollback with `up --timeout 120 --no-deps --pull never` and a 180-second
command timeout. Rollback acceptance checks health, cluster system identifier,
the original Compose/runtime model and image, an authenticated SCRAM `SELECT 1`
using the original password, and the original role/password-verifier digest.
No passwords, roles or data are regenerated or migrated.

`test-native-db-tls.py` covers the TLS/HBA boundary, preserved configuration,
pre-recreation refusal, rollback authentication checks and lock ownership.
`test-deployment-transaction.py` exercises the actual upload with fake SSH and
uses Linux flock to prove publication/Compose wait for the native helper lock,
including a symlink/trailing-slash alias. It also verifies that invalid staged
configuration does not overwrite live credentials or print private output.
These tests use temporary bundles, fake commands and fake credentials only.
The self-hosted Linux CI gate requires flock and cannot silently skip its
interoperability checks.
