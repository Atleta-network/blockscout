#!/usr/bin/env python3
"""Render the real deployment bundle with fake credentials and verify each consumer."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix="blockscout-render-") as tmp:
    work = pathlib.Path(tmp)
    shutil.copytree(root / "docker-compose", work / "docker-compose")
    env = dict(os.environ)
    env.update({
        "CI_REGISTRY": "registry.example.invalid", "CI_REGISTRY_REPO": "test",
        "SERVICE_TAG": "0" * 40, "BACKEND_VERSION": "test",
        "DOMAIN_NAME": "explorer.example.invalid", "CERTBOT_EMAIL": "test@example.invalid",
        "IS_TESTNET": "true", "NETWORK_NAME": "Test", "NETWORK_SHORT_NAME": "Test",
        "NETWORK_CHAIN_ID": "123", "NETWORK_CURRENCY_NAME": "Test",
        "NETWORK_CURRENCY_SYMBOL": "TEST", "NEXT_PUBLIC_WALLET_CONNECT_PROJECT_ID": "test",
        "PRIV_RPC_NAME": "private.example.invalid", "PUB_RPC_NAME": "rpc.example.invalid",
        "WALLETCONNECT": "test-only", "BLOCKSCOUT_DB_PASSWORD": "fake_db_password_123",
        "BLOCKSCOUT_STATS_DB_PASSWORD": "fake_stats_password_456",
        "BLOCKSCOUT_SECRET_KEY_BASE": "a" * 128, "BLOCKSCOUT_RELEASE_COOKIE": "fake_cookie",
    })
    result = subprocess.run(["bash", str(root / ".github/scripts/render-compose.sh")],
                            cwd=work, env=env, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit("Deployment render failed: " + result.stderr)
    rendered = subprocess.run(["docker", "compose", "config", "--format", "json"],
                              cwd=work / "docker-compose", capture_output=True, text=True, check=True)
    services = json.loads(rendered.stdout)["services"]
    assert services["db"]["environment"]["POSTGRES_PASSWORD"] == env["BLOCKSCOUT_DB_PASSWORD"]
    assert services["stats-db"]["environment"]["POSTGRES_PASSWORD"] == env["BLOCKSCOUT_STATS_DB_PASSWORD"]
    assert services["backend"]["environment"]["SECRET_KEY_BASE"] == env["BLOCKSCOUT_SECRET_KEY_BASE"]
    assert services["backend"]["environment"]["RELEASE_COOKIE"] == env["BLOCKSCOUT_RELEASE_COOKIE"]
    assert env["BLOCKSCOUT_DB_PASSWORD"] in services["backend"]["environment"]["DATABASE_URL"]
    assert env["BLOCKSCOUT_STATS_DB_PASSWORD"] in services["stats"]["environment"]["STATS__DB_URL"]
    assert env["BLOCKSCOUT_DB_PASSWORD"] in services["stats"]["environment"]["STATS__BLOCKSCOUT_DB_URL"]
    assert env["BLOCKSCOUT_DB_PASSWORD"] in services["user-ops-indexer"]["environment"]["USER_OPS_INDEXER__DATABASE__CONNECT__URL"]
    assert (work / "docker-compose/.env").stat().st_mode & 0o077 == 0
    missing = dict(env)
    missing.pop("BLOCKSCOUT_DB_PASSWORD")
    result = subprocess.run(["bash", str(root / ".github/scripts/render-compose.sh")],
                            cwd=work, env=missing, capture_output=True, text=True)
    assert result.returncode != 0
    assert "BLOCKSCOUT_DB_PASSWORD" in result.stderr
print("Deployment credential rendering checks passed (fake values only).")
