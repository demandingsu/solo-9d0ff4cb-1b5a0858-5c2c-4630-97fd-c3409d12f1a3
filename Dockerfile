FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv \
    DB_PATH=/data/wafer.db \
    APP_UID=10001 \
    APP_GID=10001

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 以 root 启动，入口脚本修正卷属主后降到 APP_UID 运行；避免命名卷
# 初次创建为 root 属主导致非特权用户无法写库。
RUN useradd --create-home --uid 10001 wafer \
    && mkdir -p /data \
    && chown -R wafer:wafer /data /srv

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app.entrypoint"]
