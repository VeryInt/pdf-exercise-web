from __future__ import annotations

import os
import posixpath
import socket
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import paramiko


HOST = os.environ.get("PDF_EXERCISE_VPS_HOST", "")
PORT = int(os.environ.get("PDF_EXERCISE_VPS_PORT", "4435"))
USER = os.environ.get("PDF_EXERCISE_VPS_USER", "ubuntu")
PASSWORD = os.environ.get("PDF_EXERCISE_VPS_PASSWORD", "")
APP_DIR = os.environ.get("PDF_EXERCISE_APP_DIR", "/opt/pdf-exercise-web")
PUBLIC_PORT = os.environ.get("PDF_EXERCISE_PUBLIC_PORT", "18437")
LOCAL_ROOT = Path(__file__).resolve().parents[1]


EXCLUDE_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv", "cert", "data", "var"}
EXCLUDE_SUFFIXES = {".key", ".pem", ".pyc", ".pyo"}
EXCLUDE_FILES = {".env"}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def connect() -> paramiko.SSHClient:
    if not HOST:
        fail("Set PDF_EXERCISE_VPS_HOST before running this script.")
    if not PASSWORD:
        fail("Set PDF_EXERCISE_VPS_PASSWORD before running this script.")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        port=PORT,
        username=USER,
        password=PASSWORD,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
    )
    return client


def run(client: paramiko.SSHClient, command: str, *, sudo: bool = False, timeout: int = 1200) -> str:
    if sudo:
        command = f"printf '%s\\n' {shell_quote(PASSWORD)} | sudo -S bash -lc {shell_quote(command)}"
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code != 0:
        raise RuntimeError(f"Command failed ({code}): {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out + err


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def sftp_mkdirs(sftp: paramiko.SFTPClient, path: str) -> None:
    parts = [part for part in path.split("/") if part]
    current = ""
    for part in parts:
        current += "/" + part
        try:
            sftp.stat(current)
        except IOError:
            sftp.mkdir(current)


def should_upload(path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return False
    if path.name in EXCLUDE_FILES:
        return False
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    return True


def upload_tree(client: paramiko.SSHClient) -> None:
    remote_tmp = f"/tmp/pdf-exercise-web-upload-{int(time.time())}"
    local_tar = Path(tempfile.gettempdir()) / f"pdf-exercise-web-{int(time.time())}.tar.gz"
    with tarfile.open(local_tar, "w:gz") as archive:
        for local in LOCAL_ROOT.rglob("*"):
            rel = local.relative_to(LOCAL_ROOT)
            if not should_upload(rel):
                continue
            archive.add(local, arcname=rel.as_posix())

    sftp = client.open_sftp()
    try:
        sftp.put(str(local_tar), f"{remote_tmp}.tar.gz")
    finally:
        sftp.close()
        local_tar.unlink(missing_ok=True)
    run(
        client,
        f"mkdir -p {shell_quote(remote_tmp)} {shell_quote(APP_DIR)} && "
        f"tar -xzf {shell_quote(remote_tmp)}.tar.gz -C {shell_quote(remote_tmp)} && "
        f"cp -a {shell_quote(remote_tmp)}/. {shell_quote(APP_DIR)}/ && "
        f"chown -R ubuntu:ubuntu {shell_quote(APP_DIR)} && "
        f"rm -rf {shell_quote(remote_tmp)} {shell_quote(remote_tmp)}.tar.gz",
        sudo=True,
    )


def main() -> None:
    client = connect()
    try:
        print("[inspect] VPS basics")
        inspect = run(
            client,
            "uname -a; echo '---MEM---'; free -h; echo '---DISK---'; df -h; echo '---PORTS---'; ss -tulpn; echo '---OS---'; lsb_release -a 2>/dev/null || cat /etc/os-release",
            timeout=60,
        )
        print(inspect)
        if f":{PUBLIC_PORT} " in inspect or f":{PUBLIC_PORT}\n" in inspect:
            print(f"[inspect] port {PUBLIC_PORT} is already listening; continuing because this may be an existing pdf-exercise nginx deployment.")

        print("[upload] copying project to VPS")
        upload_tree(client)

        print("[install] running VPS installer")
        output = run(
            client,
            f"cd {shell_quote(APP_DIR)} && PDF_EXERCISE_PUBLIC_PORT={shell_quote(PUBLIC_PORT)} bash deploy/install_vps.sh",
            timeout=2400,
        )
        print(output)

        print("[verify] health checks")
        verify = run(
            client,
            f"curl -fsS http://127.0.0.1:8719/health; echo; curl -fsS http://127.0.0.1:{PUBLIC_PORT}/health; echo; systemctl --no-pager --full status pdf-exercise-api | sed -n '1,40p'; systemctl --no-pager --full status pdf-exercise-worker | sed -n '1,40p'",
            timeout=120,
        )
        print(verify)
    finally:
        client.close()


if __name__ == "__main__":
    main()
