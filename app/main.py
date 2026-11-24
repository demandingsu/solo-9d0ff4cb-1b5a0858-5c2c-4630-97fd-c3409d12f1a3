"""FastAPI 入口。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import db, service
from .http_status import UNPROCESSABLE
from .schemas import (
    AnchorsIn,
    BatchIn,
    BatchOut,
    BatchResultOut,
    CorrectionIn,
    FirstTestIn,
    FirstTestOut,
    HealthOut,
    MergeResultOut,
    QueryOut,
    RecoveryIn,
    RecoveryResultOut,
    StationOut,
)

DB_PATH_ENV = "DB_PATH"
DEFAULT_DB_PATH = "/data/wafer.db"

STATIC_DIR = Path(__file__).resolve().parent / "static"


def db_path() -> str:
    return os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(db_path())
    yield


app = FastAPI(
    title="晶圆复测判定服务",
    description=(
        "同一片晶圆换台复测后的判定查询。\n\n"
        "流程：1) 录入首测网格与芯片；2) 为每台维护定位点对；"
        "3) 按全片唯一批次序号补录复测（可乱序）；"
        "4) 依据定位点判定唯一旋转方向并归并；"
        "5) 从归并数据逐颗取最大批次序号，缺复测时用首测。"
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/docs", include_in_schema=False)
def local_swagger_ui() -> HTMLResponse:
    """Swagger UI 使用随镜像打包的静态资源，离线可打开。"""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{app.title} - API 文档</title>
  <link rel="stylesheet" href="/static/swagger/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="/static/swagger/swagger-ui-bundle.js"></script>
<script src="/static/swagger/swagger-ui-standalone-preset.js"></script>
<script>
  window.ui = SwaggerUIBundle({{
    url: "{app.openapi_url or '/openapi.json'}",
    dom_id: "#swagger-ui",
    deepLinking: true,
    displayRequestDuration: true,
    persistAuthorization: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
    layout: "StandaloneLayout",
  }});
</script>
</body>
</html>"""
    return HTMLResponse(html)


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect(db_path())
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- 健康检查


@app.get("/health", response_model=HealthOut, tags=["系统"])
def health(conn: sqlite3.Connection = Depends(get_conn)) -> HealthOut:
    conn.execute("SELECT 1").fetchone()
    return HealthOut(status="ok", database=db_path())


# ---------------------------------------------------------------- 首测


@app.post(
    "/first-test",
    response_model=FirstTestOut,
    status_code=status.HTTP_201_CREATED,
    tags=["首测"],
    summary="录入首测网格、芯片坐标与合格标记",
)
def create_first_test(
    payload: FirstTestIn, conn: sqlite3.Connection = Depends(get_conn)
) -> FirstTestOut:
    with db.transaction(conn):
        return service.create_first_test(conn, payload.grid_size, payload.chips)


@app.get(
    "/first-test",
    response_model=FirstTestOut,
    tags=["首测"],
    summary="读取首测数据",
)
def read_first_test(conn: sqlite3.Connection = Depends(get_conn)) -> FirstTestOut:
    return service.get_first_test(conn)


# ---------------------------------------------------------------- 台与定位点


@app.get(
    "/stations",
    response_model=list[StationOut],
    tags=["台"],
    summary="列出各台定位点、原始批次、可行方向与归并状态",
)
def read_stations(conn: sqlite3.Connection = Depends(get_conn)) -> list[StationOut]:
    with db.snapshot(conn):
        return service.list_stations(conn)


@app.get(
    "/stations/{station_id}",
    response_model=StationOut,
    tags=["台"],
    summary="读取单台状态",
)
def read_station(
    station_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> StationOut:
    with db.snapshot(conn):
        return service.get_station(conn, station_id)


@app.put(
    "/stations/{station_id}/anchors",
    response_model=MergeResultOut,
    tags=["台与定位点"],
    summary="整组替换台面与首测坐标的定位点对（无可行方向则退回）",
)
def put_anchors(
    station_id: str,
    payload: AnchorsIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> MergeResultOut:
    with db.transaction(conn):
        return service.replace_anchors(conn, station_id, payload.anchors)


@app.delete(
    "/stations/{station_id}/anchors",
    response_model=MergeResultOut,
    tags=["台与定位点"],
    summary="清空该台定位点（无可行方向则退回）",
)
def delete_anchors(
    station_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> MergeResultOut:
    with db.transaction(conn):
        return service.clear_anchors(conn, station_id)


# ---------------------------------------------------------------- 复测批次


@app.post(
    "/batches",
    response_model=BatchResultOut,
    status_code=status.HTTP_201_CREATED,
    tags=["复测批次"],
    summary="按台号提交一个复测批次（等价于 /stations/{station_id}/batches）",
    include_in_schema=True,
)
def post_batch_by_body(
    payload: BatchIn, conn: sqlite3.Connection = Depends(get_conn)
) -> BatchResultOut:
    if payload.station_id is None:
        raise HTTPException(UNPROCESSABLE, detail="缺少台号 station_id")
    station_id = payload.station_id
    with db.transaction(conn):
        return service.submit_batch(conn, station_id, payload.batch_seq, payload.cells)


@app.post(
    "/stations/{station_id}/batches",
    response_model=BatchResultOut,
    status_code=status.HTTP_201_CREATED,
    tags=["复测批次"],
    summary="提交一个复测批次（台号 + 全片唯一批次序号 + 各格坐标与标记）",
)
def post_batch(
    station_id: str,
    payload: BatchIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> BatchResultOut:
    if payload.station_id is not None and payload.station_id != station_id:
        raise HTTPException(
            UNPROCESSABLE,
            detail="路径台号与请求体 station_id 不一致",
        )
    with db.transaction(conn):
        return service.submit_batch(conn, station_id, payload.batch_seq, payload.cells)


@app.get(
    "/stations/{station_id}/batches",
    response_model=list[BatchOut],
    tags=["复测批次"],
    summary="重读本台全部原始批次（不受归并影响）",
)
def read_batches(
    station_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> list[BatchOut]:
    with db.snapshot(conn):
        return service.list_batches(conn, station_id)


@app.get(
    "/stations/{station_id}/batches/{batch_seq}",
    response_model=BatchOut,
    tags=["复测批次"],
    summary="重读本台单个原始批次",
)
def read_batch(
    station_id: str, batch_seq: int, conn: sqlite3.Connection = Depends(get_conn)
) -> BatchOut:
    with db.snapshot(conn):
        return service.get_batch(conn, station_id, batch_seq)


@app.put(
    "/stations/{station_id}/batches/{batch_seq}",
    response_model=BatchResultOut,
    tags=["复测批次"],
    summary="更正本台批次，并与归并一起生效（无可行方向则退回）",
)
def put_batch(
    station_id: str,
    batch_seq: int,
    payload: CorrectionIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> BatchResultOut:
    with db.transaction(conn):
        return service.correct_batch(conn, station_id, batch_seq, payload.cells)


# ---------------------------------------------------------------- 坏片恢复


@app.post(
    "/stations/{station_id}/batches/{batch_seq}/recovery",
    response_model=RecoveryResultOut,
    tags=["坏片恢复"],
    summary="按台面行/列坏片合计恢复本批缺失格（仅唯一归并批次，无解则退回）",
)
def post_recovery(
    station_id: str,
    batch_seq: int,
    payload: RecoveryIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RecoveryResultOut:
    with db.transaction(conn):
        return service.recover_batch(
            conn,
            station_id,
            batch_seq,
            payload.row_failed,
            payload.column_failed,
        )


# ---------------------------------------------------------------- 查询


@app.get(
    "/query",
    response_model=QueryOut,
    tags=["查询"],
    summary="逐颗查询最新有效判定：归并数据取最大批次序号，缺复测用首测",
)
def query(
    response: Response, conn: sqlite3.Connection = Depends(get_conn)
) -> QueryOut:
    response.headers["Cache-Control"] = "no-store"
    with db.snapshot(conn):
        return service.query_wafer(conn)
