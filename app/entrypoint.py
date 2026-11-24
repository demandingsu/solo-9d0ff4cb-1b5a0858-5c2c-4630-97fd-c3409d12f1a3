"""容器入口：准备数据目录权限后降权运行 uvicorn。

以 root 启动时，把数据目录交给非特权用户（默认 uid/gid 10001），
随后 exec uvicorn；若镜像已用非 root 用户启动，则直接 exec。
"""

from __future__ import annotations

import os
import sys

APP_UID = int(os.environ.get("APP_UID", "10001"))
APP_GID = int(os.environ.get("APP_GID", "10001"))


def data_dir() -> str:
    return os.path.dirname(
        os.path.abspath(os.environ.get("DB_PATH", "/data/wafer.db"))
    )


def prepare_data_dir() -> None:
    directory = data_dir()
    os.makedirs(directory, exist_ok=True)
    if os.geteuid() != 0:
        return
    for root, dirs, files in os.walk(directory):
        try:
            os.chown(root, APP_UID, APP_GID)
        except OSError as exc:
            print(f"警告：无法调整目录属主 {root}: {exc}", file=sys.stderr)
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), APP_UID, APP_GID)
            except OSError as exc:
                print(f"警告：无法调整文件属主 {name}: {exc}", file=sys.stderr)


def drop_privileges() -> None:
    if os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(APP_GID)
    os.setuid(APP_UID)


def main() -> None:
    prepare_data_dir()
    drop_privileges()
    host = os.environ.get("HOST", "0.0.0.0")
    port = os.environ.get("PORT", "8000")
    os.execvp(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            host,
            "--port",
            port,
        ],
    )


if __name__ == "__main__":
    main()
