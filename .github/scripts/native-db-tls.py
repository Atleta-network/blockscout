#!/usr/bin/env python3
"""Opt-in native TLS rollout for existing Blockscout PostgreSQL containers.

Only a named database service is recreated. Runtime password-bearing service
files and snapshots remain on the server; no role, password or data is changed.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import struct
import subprocess
import tempfile
import time

POSTGRES_IMAGE = "postgres:17-bookworm@sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27"
CERTIFICATE = "/etc/db-access/tls/blockscout-db"
SERVICES = {"db": {"user": "blockscout", "database": "blockscout", "port": 7432, "file": "db.yml"},
            "stats-db": {"user": "stats", "database": "stats", "port": 7433, "file": "stats.yml"}}
ENVIRONMENTS = {"testnet_v2": {"address": "23.162.152.177", "domain": "blockscout.testnet-v2.atleta.network"},
                "mainnet": {"address": "186.190.215.128", "domain": "blockscout.atleta.network"}}


def run(argv, env=None, timeout=75):
    p = subprocess.run(argv, text=True, capture_output=True, env=env, timeout=timeout)
    if p.returncode:
        raise RuntimeError("Command failed; private output withheld")
    return p.stdout.strip()


def inspect(name):
    return json.loads(run(["docker", "inspect", name]))[0]


def atomic(path, content, mode=0o640, uid=0, gid=2000):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".native-tls-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.chown(name, uid, gid)
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def tls_arguments():
    settings = ["ssl=on", "ssl_min_protocol_version=TLSv1.2", "ssl_cert_file=/run/db-tls/current/fullchain.pem",
                "ssl_key_file=/run/db-tls/current/privkey.pem", "hba_file=/run/db-access/pg_hba.conf"]
    return [value for setting in settings for value in ["-c", setting]]


def build_hba(original, networks):
    pattern = r"(?m)^\s*host\s+all\s+all\s+all\s+scram-sha-256\s*(?:#[^\n]*)?$"
    if len(re.findall(pattern, original)) != 1:
        raise RuntimeError("The existing HBA is not the reviewed baseline")
    lines = ["# Native public TLS; preserve existing local administration and local project SCRAM."]
    if not networks:
        raise RuntimeError("No verified Docker network boundary")
    checked = []
    for network, gateway in networks:
        subnet = ipaddress.ip_network(network, strict=True)
        address = ipaddress.ip_address(gateway)
        if address not in subnet or not subnet.is_private or address.is_loopback:
            raise RuntimeError("Unverified Docker network boundary")
        checked.append(str(subnet))
        # Docker's public userland proxy may present an external IPv6 client as this gateway.
        mask = "/32" if address.version == 4 else "/128"
        lines += [f"hostnossl all all {address}{mask} reject", f"hostnossl replication all {address}{mask} reject"]
    lines += [f"host all all {network} scram-sha-256" for network in checked]
    lines += ["hostssl all all 0.0.0.0/0 scram-sha-256", "hostssl all all ::/0 scram-sha-256",
              "hostnossl all all 0.0.0.0/0 reject", "hostnossl all all ::/0 reject"]
    return re.sub(pattern, "\n".join(lines), original)


def patch_service(text, service, previous_command):
    match = re.search(r"(?ms)^  " + re.escape(service) + r":\s*\n.*?(?=^  [A-Za-z0-9_-]+:|\Z)", text)
    if not match:
        raise RuntimeError("The private service layout differs from the reviewed source")
    block = match.group()
    if "/run/db-tls" in block or "hba_file=" in block:
        raise RuntimeError("Native TLS was already configured; inspect before rerunning")
    if len(re.findall(r"(?m)^    command:.*$", block)) != 1 or "\n    volumes:\n" not in block:
        raise RuntimeError("Unexpected command or volume layout")
    if any("password" in word.lower() for word in previous_command):
        raise RuntimeError("Unexpected sensitive command argument")
    command = previous_command + tls_arguments()
    block = re.sub(r"(?m)^    command:.*$", "    command: " + json.dumps(command), block)
    block = block.replace("\n    volumes:\n", "\n    volumes:\n      - " + CERTIFICATE + ":/run/db-tls:ro\n      - /etc/db-access/postgres/blockscout/" + service + ":/run/db-access:ro\n", 1)
    block, count = re.subn(r"(?m)^    image:\s*[^\n]+$", "    image: " + POSTGRES_IMAGE, block)
    if count != 1:
        raise RuntimeError("Unexpected database image definition")
    return text[:match.start()] + block + text[match.end():]


def sql(service, query):
    p = SERVICES[service]
    text = run(["docker", "exec", service, "psql", "-X", "-tA", "-U", p["user"], "-d", p["database"],
        "-v", "ON_ERROR_STOP=1", "-c", "SET default_transaction_read_only=on; SET statement_timeout='5s'; " + query])
    return text.splitlines()[-1]


def fixture(image, hba_directory):
    name = "blockscout-tls-check-" + os.urandom(6).hex()
    try:
        run(["docker", "run", "--detach", "--name", name, "--network", "none", "--read-only", "--user", "2000:2000",
            "--memory", "384m", "--cpus", "0.5", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/var/lib/postgresql/data:rw,uid=2000,gid=2000,mode=0700,size=256m",
            "--tmpfs", "/tmp:rw,mode=1777,size=16m", "--tmpfs", "/var/run/postgresql:rw,uid=2000,gid=2000,mode=0775,size=16m",
            "--env", "POSTGRES_USER=tls_fixture", "--env", "POSTGRES_DB=tls_fixture",
            "--env", "POSTGRES_PASSWORD=fixture-only-not-a-deployment-secret",
            "-v", CERTIFICATE + ":/run/db-tls:ro", "-v", str(hba_directory) + ":/run/db-access:ro",
            image, "postgres", "-c", "shared_buffers=32MB", "-c", "max_connections=20", *tls_arguments()])
        for _ in range(45):
            p = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "tls_fixture", "-d", "tls_fixture"], capture_output=True, timeout=10)
            if p.returncode == 0:
                break
            if not inspect(name)["State"]["Running"]:
                raise RuntimeError("The isolated TLS/HBA fixture failed to start")
            time.sleep(1)
        else:
            raise RuntimeError("The isolated TLS/HBA fixture did not become ready")
        errors = run(["docker", "exec", name, "psql", "-U", "tls_fixture", "-d", "tls_fixture", "-XtA", "-c",
            "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;"])
        if errors != "0":
            raise RuntimeError("The candidate HBA has parse errors")
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)


def verify(environment, service, image, credential, system_identifier):
    target, p = ENVIRONMENTS[environment], SERVICES[service]
    context = ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
    for address in ["127.0.0.1", "::1"]:
        with socket.create_connection((address, p["port"]), timeout=8) as connection:
            connection.sendall(struct.pack("!II", 8, 80877103))
            if connection.recv(1) != b"S":
                raise RuntimeError("The native listener does not accept TLS")
            with context.wrap_socket(connection, server_hostname=target["domain"]):
                pass
        for bad_context, hostname in [(context, "wrong-host.invalid"), (ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), target["domain"])]:
            with socket.create_connection((address, p["port"]), timeout=8) as connection:
                connection.sendall(struct.pack("!II", 8, 80877103))
                if connection.recv(1) != b"S":
                    raise RuntimeError("The native listener does not accept TLS")
                try:
                    bad_context.wrap_socket(connection, server_hostname=hostname)
                except ssl.SSLCertVerificationError:
                    pass
                else:
                    raise RuntimeError("Invalid server identity unexpectedly trusted")
        startup = b"user\0" + p["user"].encode() + b"\0database\0" + p["database"].encode() + b"\0\0"
        with socket.create_connection((address, p["port"]), timeout=8) as connection:
            connection.sendall(struct.pack("!II", len(startup) + 8, 196608) + startup)
            first = connection.recv(8192)
            if not first.startswith(b"E"):
                raise RuntimeError("Native plaintext was not rejected before authentication")
    name = "blockscout-readonly-login-" + os.urandom(6).hex()
    try:
        connection = (f"host={target['domain']} hostaddr={target['address']} port={p['port']} dbname={p['database']} "
            f"user={p['user']} sslmode=verify-full sslrootcert=/etc/ssl/certs/ca-certificates.crt connect_timeout=8")
        identity = run(["docker", "run", "--rm", "--name", name, "--network", "host", "--read-only", "--memory", "128m", "--cpus", "0.25",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--env", "PGPASSWORD", "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
            image, "psql", connection, "-XtA", "-v", "ON_ERROR_STOP=1", "-c", "SELECT system_identifier FROM pg_control_system();"],
            env={**os.environ, "PGPASSWORD": credential}, timeout=40)
        if identity != system_identifier:
            raise RuntimeError("TLS login reached an unexpected PostgreSQL data cluster")
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=20)
    if sql(service, "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;") != "0":
        raise RuntimeError("The running HBA contains parse errors")


def apply(environment, service):
    p = SERVICES[service]
    addresses = json.loads(run(["ip", "-j", "address", "show"]))
    if ENVIRONMENTS[environment]["address"] not in {a["local"] for link in addresses for a in link.get("addr_info", [])}:
        raise RuntimeError("The selected environment does not match this server")
    old = inspect(service)
    if not old["State"]["Running"] or old["Config"].get("User") != "2000:2000":
        raise RuntimeError("Unexpected database identity/state")
    image = json.loads(run(["docker", "image", "inspect", POSTGRES_IMAGE]))[0]
    if image["Id"] != old["Image"]:
        raise RuntimeError("The exact current PostgreSQL image must be retained")
    all_names = run(["docker", "ps", "-q"]).split()
    other_ids = {c["Name"].lstrip("/"): c["Id"] for c in json.loads(run(["docker", "inspect", *all_names])) if c["Name"].lstrip("/") != service}
    roles_before = sql(service, "SELECT md5(string_agg(rolname||':'||coalesce(rolpassword,''),',' ORDER BY rolname)) FROM pg_authid WHERE rolcanlogin;")
    identity = sql(service, "SELECT system_identifier FROM pg_control_system();")
    hba_path = sql(service, "SELECT current_setting('hba_file');")
    root = Path("/proc") / str(old["State"]["Pid"]) / "root"
    original_hba = (root / hba_path.lstrip("/")).read_text()
    networks = []
    for name in old["NetworkSettings"]["Networks"]:
        n = json.loads(run(["docker", "network", "inspect", name]))[0]
        if n["Driver"] != "bridge":
            raise RuntimeError("Only verified single-host Docker bridges are supported")
        for subnet in n["IPAM"]["Config"]:
            networks.append((subnet["Subnet"], subnet["Gateway"]))
    hba = Path("/etc/db-access/postgres/blockscout") / service
    hba.mkdir(parents=True, exist_ok=True)
    os.chmod(hba, 0o750)
    os.chown(hba, 0, 2000)
    atomic(hba / "pg_hba.conf", build_hba(original_hba, networks).encode())
    fixture(old["Image"], hba)
    private = Path("/blockscout/services") / p["file"]
    previous = private.read_bytes()
    stat = private.stat()
    candidate = patch_service(previous.decode(), service, old["Config"]["Cmd"])
    backup = Path("/etc/db-access/rollback/blockscout") / (service + "-" + str(time.time_ns()))
    backup.mkdir(parents=True, mode=0o700)
    atomic(backup / p["file"], previous, 0o600, 0, 0)
    command = ["docker", "compose", "--project-directory", "/blockscout", "-f", "/blockscout/docker-compose.yml"]
    before_model = json.loads(run(command + ["config", "--format", "json"]))["services"][service]
    env = dict(v.split("=", 1) for v in old["Config"].get("Env", []) if "=" in v)
    try:
        atomic(private, candidate.encode(), stat.st_mode & 0o777, stat.st_uid, stat.st_gid)
        after_model = json.loads(run(command + ["config", "--format", "json"]))["services"][service]
        for key in set(before_model) | set(after_model):
            if key not in ["image", "command", "volumes"] and before_model.get(key) != after_model.get(key):
                raise RuntimeError("The Compose patch changes an unrelated database setting")
        if before_model["volumes"] != [v for v in after_model["volumes"] if v["target"] not in ["/run/db-tls", "/run/db-access"]]:
            raise RuntimeError("The original PostgreSQL data mounts changed")
        run(command + ["up", "--detach", "--no-deps", "--pull", "never", service])
        for _ in range(60):
            fresh = inspect(service)
            if fresh["State"]["Running"] and fresh["State"].get("Health", {}).get("Status") == "healthy":
                break
            time.sleep(1)
        else:
            raise RuntimeError("PostgreSQL did not become healthy after the controlled restart")
        verify(environment, service, old["Image"], env["POSTGRES_PASSWORD"], identity)
        if sql(service, "SELECT md5(string_agg(rolname||':'||coalesce(rolpassword,''),',' ORDER BY rolname)) FROM pg_authid WHERE rolcanlogin;") != roles_before:
            raise RuntimeError("Database roles/password verifier material changed")
        if any(inspect(name)["Id"] != value for name, value in other_ids.items()):
            raise RuntimeError("An unrelated container was recreated")
        fields = ["Source", "Destination", "Type", "RW"]
        old_mounts = sorted(tuple(m.get(k) for k in fields) for m in old["Mounts"])
        fresh_mounts = sorted(tuple(m.get(k) for k in fields) for m in fresh["Mounts"] if m["Destination"] not in ["/run/db-tls", "/run/db-access"])
        if fresh["Image"] != old["Image"] or fresh_mounts != old_mounts:
            raise RuntimeError("Unexpected PostgreSQL image or original mount change")
    except Exception:
        atomic(private, previous, stat.st_mode & 0o777, stat.st_uid, stat.st_gid)
        run(command + ["up", "--detach", "--no-deps", "--pull", "never", service])
        raise
    return {"environment": environment, "service": service, "port": p["port"], "old_id": old["Id"], "new_id": fresh["Id"],
        "image_unchanged": True, "data_identity_unchanged": True, "passwords_unchanged": True, "other_container_ids_unchanged": True,
        "verified_tls_login": True, "plaintext_rejected": True, "isolated_candidate_hba_tls_check": True, "backup_directory": str(backup)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("environment", choices=ENVIRONMENTS)
    parser.add_argument("service", choices=SERVICES)
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    if os.getuid() != 0 or args.confirmation != "ENABLE_NATIVE_TLS_ON_" + args.environment:
        raise SystemExit("Explicit root/single-environment confirmation required")
    try:
        print(json.dumps(apply(args.environment, args.service)))
    except RuntimeError as error:
        raise SystemExit("RuntimeError: " + str(error)) from None
    except Exception as error:
        raise SystemExit(type(error).__name__ + ": native TLS rollout failed; private output withheld") from None
