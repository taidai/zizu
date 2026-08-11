#!/usr/bin/env python3
"""
ZiZu 部署到 1 号机 (e606.xxxx.com:22 / holo)
- 打包 backend/app + frontend/dist + init-db + VERSION
- 上传 -> 解压到 /home/omnithings -> 重启 zizu 容器 -> health 校验
用法: python scripts/deploy_1号机.py
"""
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import paramiko

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION = (REPO_ROOT / "VERSION").read_text().strip()

HOST = "e606.xxxx.com"
PORT = 22
USER = "holo"
PASSWD = "****"
REMOTE_DIR = "/home/omnithings"
SUDO_PROMPT = "****"
CONTAINER = os.environ.get("ZIZU_CONTAINER", "zizu")

# 同步路径：含 init-db 以确保新迁移文件到达远端
PATHS_TO_SYNC = ["backend/app", "frontend/dist", "init-db", "VERSION"]


def log(msg):
    print(f"[DEPLOY-1] {msg}", flush=True)


def make_tarball():
    fd, tar_path = tempfile.mkstemp(suffix=".tar.gz", prefix="zizu-deploy-")
    os.close(fd)
    log(f"Packing tarball: {tar_path}")
    with tarfile.open(tar_path, "w:gz") as tar:
        for rel in PATHS_TO_SYNC:
            src = REPO_ROOT / rel
            if not src.exists():
                raise FileNotFoundError(src)
            tar.add(src, arcname=rel)
    log(f"Tarball size: {os.path.getsize(tar_path)/1024/1024:.2f} MB")
    return tar_path


def sudo_exec(client, command, timeout=120):
    full = f"echo '{SUDO_PROMPT}' | sudo -S bash -c {command!r}"
    stdin, stdout, stderr = client.exec_command(full, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def upload_and_extract(tar_path):
    log("Connecting to 1号机 ...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, password=PASSWD,
                   timeout=20, banner_timeout=40, auth_timeout=40)
    log("SSH connected")

    remote_tar = f"/tmp/zizu-deploy-{VERSION}.tar.gz"
    sftp = client.open_sftp()
    log(f"Uploading {tar_path} -> {remote_tar}")
    sftp.put(tar_path, remote_tar)
    sftp.close()
    log("Upload complete")

    log(f"Extracting to {REMOTE_DIR} ...")
    rc, out, err = sudo_exec(client,
        f"cd {REMOTE_DIR} && rm -rf backend/app frontend/dist init-db VERSION && "
        f"tar -xzf {remote_tar} -C {REMOTE_DIR} && "
        f"rm -rf {REMOTE_DIR}/backend/app/__pycache__ && "
        f"find {REMOTE_DIR}/backend/app -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null; "
        f"echo {VERSION!r} > {REMOTE_DIR}/VERSION && "
        f"echo DONE"
    )
    if rc != 0:
        log(f"Extract failed: rc={rc} err={err}")
        raise RuntimeError("extract failed")
    log("Extract OK: " + out.strip().splitlines()[-1] if out.strip() else "Extract OK")
    return client


def restart_backend(client):
    log(f"Restarting container {CONTAINER} ...")
    rc, out, err = sudo_exec(client, f"docker restart {CONTAINER}", timeout=60)
    if rc != 0:
        log(f"restart {CONTAINER} failed, try omnithings: {err}")
        rc, out, err = sudo_exec(client, "docker restart omnithings", timeout=60)
        if rc != 0:
            raise RuntimeError("restart failed")
    log("Backend restart triggered")
    time.sleep(8)
    rc, out, err = sudo_exec(client,
        "curl -sf http://127.0.0.1:9000/api/v1/health 2>/dev/null || echo HEALTH_PENDING",
        timeout=30)
    log("Health: " + out.strip()[:800])
    rc, out, err = sudo_exec(client, f"docker logs --tail 40 {CONTAINER} 2>&1", timeout=30)
    log("--- recent logs ---")
    for line in out.strip().splitlines()[-25:]:
        log("  " + line)
    return out


def main():
    tar_path = make_tarball()
    client = None
    try:
        client = upload_and_extract(tar_path)
        restart_backend(client)
        log("Deploy to 1号机 complete")
        log(f"Health URL: http://{HOST}:9000/api/v1/health")
        log(f"UI: http://{HOST}:9000")
    finally:
        if client:
            client.close()
        try:
            os.remove(tar_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
