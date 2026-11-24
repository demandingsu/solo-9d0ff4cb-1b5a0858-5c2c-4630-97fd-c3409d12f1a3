"""容器健康检查：GET /health 并校验 status == ok。"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

URL = os.environ.get("HEALTHCHECK_URL", "http://127.0.0.1:8000/health")


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=3) as response:
            payload = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 - 健康检查失败即非零退出
        print(f"健康检查失败: {exc}", file=sys.stderr)
        return 1
    if payload.get("status") != "ok":
        print(f"健康检查状态异常: {payload}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
