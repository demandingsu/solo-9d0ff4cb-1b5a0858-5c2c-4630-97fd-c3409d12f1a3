"""端到端接口测试：坏片恢复 API。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "wafer.db"))
    with TestClient(app) as test_client:
        yield test_client


def full_grid(n: int) -> dict[str, Any]:
    return {
        "grid_size": n,
        "chips": [
            {"x": x, "y": y, "passed": (x + y) % 2 == 0}
            for y in range(n)
            for x in range(n)
        ],
    }


def setup(client: TestClient, n: int = 4, anchor=(0, 0, 3, 0)) -> None:
    assert client.post("/first-test", json=full_grid(n)).status_code == 201
    resp = client.put(
        f"/stations/A/anchors",
        json={"anchors": [
            {"stage_x": anchor[0], "stage_y": anchor[1],
             "first_x": anchor[2], "first_y": anchor[3]}
        ]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["merge_status"] == "merged"


def recovery_path(seq: int, station: str = "A") -> str:
    return f"/stations/{station}/batches/{seq}/recovery"


# ---------------------------------------------------------------- 主场景


def test_recovery_mixed_recoverable_and_pending_cells(client: TestClient) -> None:
    setup(client)  # 4×4，90° 唯一归并。
    # 本批只找回 3 颗：(0,0) 坏、(1,0) 好、(2,1) 坏。
    resp = client.post(
        "/stations/A/batches",
        json={
            "station_id": "A",
            "batch_seq": 10,
            "cells": [
                {"x": 0, "y": 0, "passed": False},
                {"x": 1, "y": 0, "passed": True},
                {"x": 2, "y": 1, "passed": False},
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    rows = [2, 2, 1, 1]
    cols = [1, 1, 1, 3]
    rec = client.post(recovery_path(10), json={"row_failed": rows, "column_failed": cols})
    assert rec.status_code == 200, rec.text
    body = rec.json()

    assert body["station_id"] == "A"
    assert body["batch_seq"] == 10
    assert body["orientation"] == 90
    assert body["merge_status"] == "merged"
    assert body["candidates"] == [90]
    assert body["row_failed"] == rows
    assert body["column_failed"] == cols

    recovered = {(c["x"], c["y"]): c["passed"] for c in body["recovered_cells"]}
    # 强制坏片 1 格 + 强制好片 6 格；已知 3 格不在补回列表里。
    assert body["recovered_count"] == 7
    assert recovered[(3, 0)] is False
    assert all(
        passed is True
        for (x, y), passed in recovered.items()
        if (x, y) != (3, 0)
    )
    assert (0, 0) not in recovered and (1, 0) not in recovered and (2, 1) not in recovered

    pending = {(c["x"], c["y"]) for c in body["pending_cells"]}
    assert body["pending_count"] == 6
    assert pending == {(1, 1), (3, 1), (1, 2), (3, 2), (1, 3), (3, 3)}
    # 待复测格不返回判定。
    assert all(set(c.keys()) == {"x", "y"} for c in body["pending_cells"])
    assert body["recovered_count"] + body["pending_count"] + 3 == 16
    # 补回已写入原批：7 格判定一致的缺失格入批，6 个歧义格保留待复测不入批；
    # 原批 3 颗找回记录的判定保持不变。
    raw = client.get("/stations/A/batches/10").json()
    assert raw["cell_count"] == 10
    raw_map = {(c["x"], c["y"]): c["passed"] for c in raw["cells"]}
    assert raw_map[(0, 0)] is False
    assert raw_map[(2, 1)] is False
    assert raw_map[(1, 0)] is True
    assert raw_map[(3, 0)] is False
    # 待复测格没有被写入。
    for x, y in pending:
        assert (x, y) not in raw_map

    # 归并已按同一方向重建：/query 中可查到补回格的复测判定。
    chips = {(c["x"], c["y"]): c for c in client.get("/query").json()["chips"]}
    # 90°: 台面 (3,0) -> 首测 (3,3)
    assert chips[(3, 3)]["source"] == "retest"
    assert chips[(3, 3)]["passed"] is False
    assert chips[(3, 3)]["batch_seq"] == 10
    # 台面 (2,0) 补为好片 -> 首测 (3,2)
    assert chips[(3, 2)]["passed"] is True
    assert chips[(3, 2)]["source"] == "retest"
    # 待复测的台面格没有归并记录，对应首测位置回退首测判定。
    # 90°: 台面 (1,1) -> 首测 (2,1)
    assert chips[(2, 1)]["source"] == "first"


def test_recovery_margins_are_satisfied_exactly(client: TestClient) -> None:
    setup(client)
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [
            {"x": 0, "y": 0, "passed": False},
            {"x": 1, "y": 0, "passed": True},
            {"x": 2, "y": 1, "passed": False},
        ]},
    )
    # 情形一：所有缺失格判定都一致（待复测为 0），补回后行列坏片数须与小票分毫不差。
    rec = client.post(
        recovery_path(1),
        json={"row_failed": [1, 1, 0, 0], "column_failed": [1, 0, 1, 0]},
    )
    assert rec.status_code == 200, rec.text
    assert rec.json()["pending_count"] == 0
    raw = client.get("/stations/A/batches/1").json()
    grid = [[None] * 4 for _ in range(4)]
    for c in raw["cells"]:
        grid[c["y"]][c["x"]] = c["passed"]
    assert [sum(1 for v in row if v is False) for row in grid] == [1, 1, 0, 0]
    assert [
        sum(1 for y in range(4) if grid[y][x] is False) for x in range(4)
    ] == [1, 0, 1, 0]

    # 情形二：存在待复测格时，已确定格的坏片数不超过小票，且任一相容填法
    # （由算法保证）都能把剩余坏片放进待复测格凑齐小票。
    rec2 = client.post(
        "/stations/A/batches/1/recovery",
        json={"row_failed": [2, 2, 1, 1], "column_failed": [1, 1, 1, 3]},
    )
    # 批次已在上一次恢复中补满，重复提交按“无缺失格”退回；
    # 先用更正回到 3 颗再恢复。
    assert rec2.status_code == 422
    client.put(
        "/stations/A/batches/1",
        json={"cells": [
            {"x": 0, "y": 0, "passed": False},
            {"x": 1, "y": 0, "passed": True},
            {"x": 2, "y": 1, "passed": False},
        ]},
    )
    rec2 = client.post(
        recovery_path(1),
        json={"row_failed": [2, 2, 1, 1], "column_failed": [1, 1, 1, 3]},
    )
    assert rec2.status_code == 200, rec2.text
    body = rec2.json()
    assert body["pending_count"] == 6
    raw = client.get("/stations/A/batches/1").json()
    grid = [[None] * 4 for _ in range(4)]
    for c in raw["cells"]:
        grid[c["y"]][c["x"]] = c["passed"]
    known_fail_rows = [sum(1 for v in row if v is False) for row in grid]
    known_fail_cols = [
        sum(1 for y in range(4) if grid[y][x] is False) for x in range(4)
    ]
    # 已确定坏片不超过小票；差额恰好要由待复测格补足。
    assert all(a <= b for a, b in zip(known_fail_rows, [2, 2, 1, 1]))
    assert all(a <= b for a, b in zip(known_fail_cols, [1, 1, 1, 3]))
    pending = {(c["x"], c["y"]) for c in body["pending_cells"]}
    row_slack = [b - a for a, b in zip(known_fail_rows, [2, 2, 1, 1])]
    col_slack = [b - a for a, b in zip(known_fail_cols, [1, 1, 1, 3])]
    for x, y in pending:  # 待复测格在批次里确实仍空缺。
        assert grid[y][x] is None
    assert sum(row_slack) == sum(col_slack)
    assert sum(row_slack) <= len(pending)


def test_recovery_with_zero_orientation(client: TestClient) -> None:
    setup(client, anchor=(0, 0, 0, 0))  # 0°
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 7, "cells": [
            {"x": 0, "y": 0, "passed": False},
        ]},
    )
    rec = client.post(
        recovery_path(7),
        json={"row_failed": [1, 0, 0, 0], "column_failed": [1, 0, 0, 0]},
    )
    assert rec.status_code == 200, rec.text
    body = rec.json()
    assert body["orientation"] == 0
    recovered = {(c["x"], c["y"]): c["passed"] for c in body["recovered_cells"]}
    # 坏片配额已被 (0,0) 用完，其余 15 格全部补为好片，无待复测。
    assert body["pending_count"] == 0
    assert len(recovered) == 15
    assert all(recovered.values())
    chips = {(c["x"], c["y"]): c for c in client.get("/query").json()["chips"]}
    # 0° 下台面对齐首测，台面 (1,0) 即首测 (1,0)。
    assert chips[(1, 0)]["source"] == "retest"
    assert chips[(1, 0)]["passed"] is True


def test_recovery_180_orientation_remaps_coordinates(client: TestClient) -> None:
    setup(client, anchor=(0, 0, 3, 3))  # 180°
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 4, "cells": [
            {"x": 0, "y": 0, "passed": False},
        ]},
    )
    rec = client.post(
        recovery_path(4),
        json={"row_failed": [1, 0, 0, 0], "column_failed": [1, 0, 0, 0]},
    )
    assert rec.status_code == 200, rec.text
    assert rec.json()["orientation"] == 180
    # 台面 (1,0) 补为好片；180° 下 -> 首测 (2,3)。
    chips = {(c["x"], c["y"]): c for c in client.get("/query").json()["chips"]}
    assert chips[(2, 3)]["source"] == "retest"
    assert chips[(2, 3)]["passed"] is True


# ---------------------------------------------------------------- 退回场景


def test_recovery_infeasible_margins_rolls_back(client: TestClient) -> None:
    setup(client)
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 2, "cells": [
            {"x": 0, "y": 0, "passed": False},
        ]},
    )
    before_batch = client.get("/stations/A/batches/2").json()
    before_query = client.get("/query").json()

    # 第 0 行已有 1 颗坏片，小票却只给 0：矛盾，无解退回。
    rec = client.post(
        recovery_path(2),
        json={"row_failed": [0, 0, 0, 0], "column_failed": [0, 0, 0, 0]},
    )
    assert rec.status_code == 422
    assert "无解" in rec.json()["detail"]
    assert client.get("/stations/A/batches/2").json() == before_batch
    assert client.get("/query").json() == before_query


def test_recovery_rejects_when_totals_disagree(client: TestClient) -> None:
    setup(client)
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 3, "cells": [
            {"x": 0, "y": 0, "passed": True},
        ]},
    )
    rec = client.post(
        recovery_path(3),
        json={"row_failed": [1, 0, 0, 0], "column_failed": [0, 0, 0, 0]},
    )
    assert rec.status_code == 422
    assert "两组合计不一致" in rec.json()["detail"]


def test_recovery_rejects_unmerged_station(client: TestClient) -> None:
    # 稀疏首测 + 中心定位点：方向多解，不唯一归并。
    body = {
        "grid_size": 5,
        "chips": [{"x": x, "y": y, "passed": True}
                  for x, y in {(2, 2), (0, 0), (4, 4), (0, 4)}],
    }
    client.post("/first-test", json=body)
    client.put(
        "/stations/A/anchors",
        json={"anchors": [{"stage_x": 2, "stage_y": 2, "first_x": 2, "first_y": 2}]},
    )
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [
            {"x": 2, "y": 2, "passed": False},
        ]},
    )
    assert client.get("/stations/A").json()["merge_status"] == "ambiguous"
    rec = client.post(
        recovery_path(1),
        json={"row_failed": [0] * 5, "column_failed": [0] * 5},
    )
    assert rec.status_code == 422
    assert "唯一归并" in rec.json()["detail"]


def test_recovery_unknown_batch_and_station_404(client: TestClient) -> None:
    setup(client)
    rec = client.post(
        recovery_path(99),
        json={"row_failed": [0] * 4, "column_failed": [0] * 4},
    )
    assert rec.status_code == 404
    # 批次属于别的台。
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 5, "cells": [
            {"x": 0, "y": 0, "passed": True},
        ]},
    )
    other = client.post(
        "/stations/B/batches/5/recovery",
        json={"row_failed": [0] * 4, "column_failed": [0] * 4},
    )
    assert other.status_code == 404


def test_recovery_validates_array_lengths_and_signs(client: TestClient) -> None:
    setup(client)
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 6, "cells": [
            {"x": 0, "y": 0, "passed": True},
        ]},
    )
    bad_length = client.post(
        recovery_path(6),
        json={"row_failed": [0, 0, 0], "column_failed": [0, 0, 0, 0]},
    )
    assert bad_length.status_code == 422
    negative = client.post(
        recovery_path(6),
        json={"row_failed": [-1, 0, 0, 0], "column_failed": [0, 0, 0, 0]},
    )
    assert negative.status_code == 422
    too_big = client.post(
        recovery_path(6),
        json={"row_failed": [0] * 33, "column_failed": [0] * 33},
    )
    assert too_big.status_code == 422
    extra = client.post(
        recovery_path(6),
        json={
            "row_failed": [0] * 4,
            "column_failed": [0] * 4,
            "surprise": 1,
        },
    )
    assert extra.status_code == 422


def test_recovery_then_correction_re_merge_and_second_recovery(client: TestClient) -> None:
    setup(client)
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 8, "cells": [
            {"x": 0, "y": 0, "passed": False},
            {"x": 1, "y": 0, "passed": True},
            {"x": 2, "y": 1, "passed": False},
        ]},
    )
    rec1 = client.post(
        recovery_path(8),
        json={"row_failed": [2, 2, 1, 1], "column_failed": [1, 1, 1, 3]},
    )
    assert rec1.status_code == 200
    assert rec1.json()["recovered_count"] == 7

    # 更正本批：回到 3 颗记录，恢复结果随原批一起被替换。
    corrected = client.put(
        "/stations/A/batches/8",
        json={"cells": [{"x": 0, "y": 0, "passed": False}]},
    )
    assert corrected.status_code == 200
    assert client.get("/stations/A/batches/8").json()["cell_count"] == 1

    # 用新小票重新恢复：坏片配额已用完，其余全部补为好片。
    rec2 = client.post(
        recovery_path(8),
        json={"row_failed": [1, 0, 0, 0], "column_failed": [1, 0, 0, 0]},
    )
    assert rec2.status_code == 200, rec2.text
    assert rec2.json()["recovered_count"] == 15
    assert rec2.json()["pending_count"] == 0
    assert client.get("/stations/A/batches/8").json()["cell_count"] == 16


def test_recovery_before_first_test_conflicts(client: TestClient) -> None:
    rec = client.post(
        recovery_path(1),
        json={"row_failed": [0] * 3, "column_failed": [0] * 3},
    )
    assert rec.status_code == 409


def test_recovery_endpoint_in_openapi(client: TestClient) -> None:
    setup(client)
    spec = client.get("/openapi.json").json()
    assert "/stations/{station_id}/batches/{batch_seq}/recovery" in spec["paths"]
    docs = client.get("/docs")
    assert docs.status_code == 200


def test_recovery_fingerprint_detects_detail_and_orientation_changes(
    client: TestClient,
) -> None:
    # 重核指纹须同时覆盖本批明细与归并方向，任一变化都应可检出。
    import os

    from app import db, service

    setup(client)
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 9, "cells": [
            {"x": 0, "y": 0, "passed": False},
        ]},
    )
    conn = db.connect(os.environ["DB_PATH"])
    try:
        with db.snapshot(conn):
            fp1 = service._recovery_fingerprint(conn, "A", 9)
        assert fp1[1] == 90

        # 本批明细变化 -> 指纹变化。
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO batch_cells (station_id, batch_seq, x, y, passed) "
                "VALUES ('A', 9, 1, 0, 1)"
            )
        with db.snapshot(conn):
            assert service._recovery_fingerprint(conn, "A", 9) != fp1

        # 归并方向变化（换定位点重归并到 180°）-> 指纹变化。
        with db.transaction(conn):
            service._save_anchors(conn, "A", [(0, 0, 3, 3)])
            service._apply_merge(conn, "A", 4)
        with db.snapshot(conn):
            fp2 = service._recovery_fingerprint(conn, "A", 9)
            assert fp2 != fp1
            assert fp2[1] == 180
    finally:
        conn.close()
