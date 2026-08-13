#!/usr/bin/env python3
"""以 SSH 密钥部署 ZiZu 到目标主机。

必需环境变量：
  ZIZU_DEPLOY_HOST        远端主机
  ZIZU_DEPLOY_USER        远端部署账户
  ZIZU_DEPLOY_SSH_KEY     私钥绝对路径

可选环境变量：
  ZIZU_DEPLOY_PORT        默认 22
  ZIZU_DEPLOY_KNOWN_HOSTS 默认 ~/.ssh/known_hosts
  ZIZU_DEPLOY_SUDO_PASSWORD
  ZIZU_DEPLOY_REMOTE_DIR  默认 /home/omnithings
  ZIZU_CONTAINER          默认 zizu

部署前必须确认 known_hosts 中已有目标主机的固定指纹。脚本拒绝未知主机，
不接受密码登录，也不会把 sudo 密码放进命令行参数。
"""
from __future__ import annotations

import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import paramiko


REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION = (REPO_ROOT / "VERSION").read_text().strip()
HOST = os.environ.get("ZIZU_DEPLOY_HOST")
PORT = int(os.environ.get("ZIZU_DEPLOY_PORT", "22"))
USER = os.environ.get("ZIZU_DEPLOY_USER")
SSH_KEY_VALUE = os.environ.get("ZIZU_DEPLOY_SSH_KEY")
SSH_KEY = Path(SSH_KEY_VALUE).expanduser() if SSH_KEY_VALUE else None
KNOWN_HOSTS = Path(
    os.environ.get("ZIZU_DEPLOY_KNOWN_HOSTS", "~/.ssh/known_hosts")
).expanduser()
SUDO_PASSWORD = os.environ.get("ZIZU_DEPLOY_SUDO_PASSWORD")
REMOTE_DIR = os.environ.get("ZIZU_DEPLOY_REMOTE_DIR", "/home/omnithings")
CONTAINER = os.environ.get("ZIZU_CONTAINER", "zizu")

PATHS_TO_SYNC = ["backend/app", "frontend/dist", "init-db", "VERSION"]


def log(msg: str) -> None:
    print(f"[DEPLOY-1] {msg}", flush=True)


def validate_deploy_config() -> None:
    missing = []
    if not HOST:
        missing.append("ZIZU_DEPLOY_HOST")
    if not USER:
        missing.append("ZIZU_DEPLOY_USER")
    if not SSH_KEY_VALUE:
        missing.append("ZIZU_DEPLOY_SSH_KEY")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    if SSH_KEY is None or not SSH_KEY.is_file():
        raise FileNotFoundError(f"SSH key not found: {SSH_KEY}")
    if not KNOWN_HOSTS.is_file():
        raise FileNotFoundError(
            f"Known-hosts file not found: {KNOWN_HOSTS}. "
            "Verify and record the host fingerprint before deployment."
        )


def make_tarball() -> str:
    fd, tar_path = tempfile.mkstemp(suffix=".tar.gz", prefix="zizu-deploy-")
    os.close(fd)
    log(f"Packing tarball: {tar_path}")
    with tarfile.open(tar_path, "w:gz") as tar:
        for rel in PATHS_TO_SYNC:
            src = REPO_ROOT / rel
            if not src.exists():
                raise FileNotFoundError(src)
            tar.add(src, arcname=rel)
    log(f"Tarball size: {os.path.getsize(tar_path) / 1024 / 1024:.2f} MB")
    return tar_path


def sudo_exec(client: paramiko.SSHClient, command: str, timeout: int = 120):
    if SUDO_PASSWORD:
        full = f"sudo -S -p '' bash -c {command!r}"
    else:
        full = f"sudo -n bash -c {command!r}"
    stdin, stdout, stderr = client.exec_command(full, timeout=timeout)
    if SUDO_PASSWORD:
        stdin.write(f"{SUDO_PASSWORD}\n")
        stdin.flush()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def upload_and_extract(tar_path: str) -> paramiko.SSHClient:
    log("Connecting to deployment host ...")
    client = paramiko.SSHClient()
    client.load_host_keys(str(KNOWN_HOSTS))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        HOST,
        port=PORT,
        username=USER,
        key_filename=str(SSH_KEY),
        allow_agent=False,
        look_for_keys=False,
        timeout=20,
        banner_timeout=40,
        auth_timeout=40,
    )
    log("SSH connected with a pinned host key")

    remote_tar = f"/tmp/zizu-deploy-{VERSION}.tar.gz"
    sftp = client.open_sftp()
    sftp.put(tar_path, remote_tar)
    sftp.close()
    log("Upload complete")

    log(f"Extracting to {REMOTE_DIR} ...")
    rc, out, err = sudo_exec(
        client,
        f"cd {REMOTE_DIR} && rm -rf backend/app frontend/dist init-db VERSION && "
        f"tar -xzf {remote_tar} -C {REMOTE_DIR} && "
        f"rm -rf {REMOTE_DIR}/backend/app/__pycache__ && "
        f"find {REMOTE_DIR}/backend/app -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null; "
        f"echo {VERSION!r} > {REMOTE_DIR}/VERSION && "
        "echo DONE",
    )
    if rc != 0:
        log(f"Extract failed: rc={rc} err={err}")
        raise RuntimeError("extract failed")
    log(out.strip())
    return client


def restart_backend(client: paramiko.SSHClient) -> str:
    log(f"Restarting container {CONTAINER} ...")
    rc, out, err = sudo_exec(client, f"docker restart {CONTAINER}", timeout=60)
    if rc != 0:
        log(f"Restart by name failed ({err.strip()}), trying compose ...")
        rc, out, err = sudo_exec(
            client,
            f"cd {REMOTE_DIR} && docker compose -f docker-compose.yml "
            "-f docker-compose.e606.yml up -d --force-recreate backend",
            timeout=120,
        )
        if rc != 0:
            raise RuntimeError("restart failed")
    log("Backend restart triggered")
    time.sleep(8)
    rc, out, err = sudo_exec(
        client,
        "curl -sf http://127.0.0.1:9000/api/v1/health/live 2>/dev/null || echo HEALTH_PENDING",
        timeout=30,
    )
    log("Health: " + out.strip()[:800])
    rc, out, err = sudo_exec(client, f"docker logs --tail 40 {CONTAINER} 2>&1", timeout=30)
    log("--- recent logs ---")
    print(out[-6000:])
    return out


def main() -> None:
    validate_deploy_config()
    tar_path = make_tarball()
    client = None
    try:
        client = upload_and_extract(tar_path)
        restart_backend(client)
        log(f"Deployment v{VERSION} complete")
    finally:
        if client:
            client.close()
        try:
            os.unlink(tar_path)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[DEPLOY-1] failed: {exc}", file=sys.stderr)
        raise
