"""兼容不同 Starlette 版本的 422 常量。"""

from __future__ import annotations

# Starlette 新版把常量改名为 HTTP_422_UNPROCESSABLE_CONTENT；直接用数字最稳。
UNPROCESSABLE: int = 422
