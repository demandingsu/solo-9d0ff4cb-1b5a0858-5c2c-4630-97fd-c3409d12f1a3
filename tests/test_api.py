"""端到端接口测试：首测、乱序复测、归并方向、退回与更正。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    db_file = tmp_path / "wafer.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    with TestClient(app) as test_client:
        yield test_client


def full_grid(n: int, passed=lambda x, y: (x + y) % 2 == 0) -> dict[str, list[dict[str, Any]]]:
    return {
        "grid_size": n,
        "chips": [
            {"x": x, "y": y, "passed": passed(x, y)}
            for y in range(n)
            for x in range(n)
        ],
    }


def cell(x: int, y: int, passed: bool) -> dict[str, Any]:
    return {"x": x, "y": y, "passed": passed}


def anchors(*pairs: tuple[int, int, int, int]) -> dict[str, list[dict[str, int]]]:
    return {
        "anchors": [
            {"stage_x": sx, "stage_y": sy, "first_x": fx, "first_y": fy}
            for sx, sy, fx, fy in pairs
        ]
    }


def chip_map(payload: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    return {(c["x"], c["y"]): c for c in payload["chips"]}


def query_map(client: TestClient) -> dict[tuple[int, int], dict[str, Any]]:
    resp = client.get("/query")
    assert resp.status_code == 200, resp.text
    return chip_map(resp.json())


def setup_wafer(client: TestClient, grid_size: int = 4, **kwargs: Any) -> dict[str, Any]:
    body = full_grid(grid_size, **kwargs)
    resp = client.post("/first-test", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------- 首测


def test_first_test_and_empty_query(client: TestClient) -> None:
    body = setup_wafer(client, 4)
    assert body["chip_count"] == 16

    payload = client.get("/query").json()
    assert payload["chip_count"] == 16
    assert payload["from_first"] == 16
    assert payload["from_retest"] == 0
    assert all(c["source"] == "first" for c in payload["chips"])


def test_first_test_is_immutable(client: TestClient) -> None:
    setup_wafer(client, 3)
    assert client.post("/first-test", json=full_grid(3)).status_code == 409


def test_first_test_coordinates_are_validated(client: TestClient) -> None:
    assert (
        client.post(
            "/first-test",
            json={"grid_size": 2, "chips": [cell(2, 0, True)]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/first-test",
            json={"grid_size": 2, "chips": [cell(0, 0, True), cell(0, 0, False)]},
        ).status_code
        == 422
    )


def test_query_before_first_test_conflicts(client: TestClient) -> None:
    assert client.get("/query").status_code == 409


# ---------------------------------------------------------------- 归并与乱序


def test_unique_orientation_merges_and_out_of_order_batches(client: TestClient) -> None:
    setup_wafer(client, 4)
    # 90° 时首测 (3,0) 对应台面 (0,0)。
    resp = client.put("/stations/A/anchors", json=anchors((0, 0, 3, 0)))
    assert resp.status_code == 200, resp.text
    assert resp.json()["orientation"] == 90

    # 先录较新的批次 20，再乱序补录较旧的批次 5。
    newer = client.post(
        "/stations/A/batches",
        json={
            "station_id": "A",
            "batch_seq": 20,
            "cells": [cell(1, 0, False), cell(0, 1, True)],
        },
    )
    assert newer.status_code == 201, newer.text
    assert newer.json()["merge_status"] == "merged"

    older = client.post(
        "/stations/A/batches",
        json={
            "station_id": "A",
            "batch_seq": 5,
            "cells": [cell(0, 0, False), cell(1, 1, False)],
        },
    )
    assert older.status_code == 201, older.text

    # 90°: (1,0)->(3,1), (0,1)->(2,0), (0,0)->(3,0), (1,1)->(2,1)
    chips = query_map(client)
    assert chips[(3, 1)]["source"] == "retest"
    assert chips[(3, 1)]["batch_seq"] == 20
    assert chips[(3, 1)]["passed"] is False
    assert chips[(2, 0)]["batch_seq"] == 20
    assert chips[(3, 0)]["source"] == "retest"
    assert chips[(3, 0)]["batch_seq"] == 5
    assert chips[(3, 0)]["station_id"] == "A"
    assert chips[(2, 1)]["batch_seq"] == 5
    # 未复测的芯片仍用首测。
    first = chip_map(client.get("/first-test").json())
    assert chips[(0, 0)]["source"] == "first"
    assert chips[(0, 0)]["passed"] == first[(0, 0)]["passed"]


def test_latest_batch_sequence_wins_per_chip(client: TestClient) -> None:
    setup_wafer(client, 3)
    client.put("/stations/A/anchors", json=anchors((0, 0, 2, 0)))  # 90°
    assert client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 9, "cells": [cell(1, 1, False)]},
    ).status_code == 201
    # 同一台面格 (1,1) 在更高序号批次里再次复测。
    assert client.post(
        "/stations/A/batches",
        json={
            "station_id": "A",
            "batch_seq": 12,
            "cells": [cell(1, 1, True), cell(0, 1, False)],
        },
    ).status_code == 201

    # 90°: (1,1)->(1,1), (0,1)->(1,0)
    chips = query_map(client)
    assert chips[(1, 1)]["batch_seq"] == 12
    assert chips[(1, 1)]["passed"] is True
    assert chips[(1, 0)]["batch_seq"] == 12
    assert chips[(1, 0)]["passed"] is False


def test_180_and_270_orientations(client: TestClient) -> None:
    setup_wafer(client, 4)
    # 180°: (0,0)->(3,3)
    assert (
        client.put("/stations/B/anchors", json=anchors((0, 0, 3, 3))).json()["orientation"]
        == 180
    )
    client.post(
        "/stations/B/batches",
        json={"station_id": "B", "batch_seq": 1, "cells": [cell(1, 2, False)]},
    )
    chips = query_map(client)
    assert chips[(2, 1)]["source"] == "retest"
    assert chips[(2, 1)]["passed"] is False

    # 270°: (0,0)->(0,3)
    assert (
        client.put("/stations/C/anchors", json=anchors((0, 0, 0, 3))).json()["orientation"]
        == 270
    )
    client.post(
        "/stations/C/batches",
        json={"station_id": "C", "batch_seq": 2, "cells": [cell(1, 2, True)]},
    )
    chips = query_map(client)
    # 270: (1,2)->(2,2)
    assert chips[(2, 2)]["source"] == "retest"
    assert chips[(2, 2)]["passed"] is True


def test_multiple_solutions_withdraws_old_merge(client: TestClient) -> None:
    setup_wafer(client, 3)
    client.put("/stations/A/anchors", json=anchors((0, 0, 2, 0)))
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 7, "cells": [cell(0, 1, False)]},
    )
    assert query_map(client)[(1, 0)]["source"] == "retest"

    # 清空定位点后，全片满格使四个方向都可行 -> 多解，撤下旧归并。
    cleared = client.delete("/stations/A/anchors")
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["merge_status"] == "ambiguous"
    assert body["orientation"] is None
    assert body["candidates"] == [0, 90, 180, 270]

    chips = query_map(client)
    assert chips[(1, 0)]["source"] == "first"

    # 原始批次仍可重读。
    raw = client.get("/stations/A/batches/7")
    assert raw.status_code == 200
    assert raw.json()["cells"] == [cell(0, 1, False)]

    station = client.get("/stations/A").json()
    assert station["merge_status"] == "ambiguous"
    assert station["merged_cell_count"] == 0
    assert station["batch_count"] == 1


def test_center_anchor_leaves_four_directions(client: TestClient) -> None:
    setup_wafer(client, 5)
    resp = client.put("/stations/S/anchors", json=anchors((2, 2, 2, 2)))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["merge_status"] == "ambiguous"
    assert body["candidates"] == [0, 90, 180, 270]


# ---------------------------------------------------------------- 退回


def test_no_direction_rejects_batch_and_keeps_state(client: TestClient) -> None:
    # 首测缺 (2,2)。
    body = {
        "grid_size": 4,
        "chips": [
            {"x": x, "y": y, "passed": True}
            for y in range(4)
            for x in range(4)
            if (x, y) != (2, 2)
        ],
    }
    assert client.post("/first-test", json=body).status_code == 201
    client.put("/stations/A/anchors", json=anchors((0, 0, 3, 0)))  # 固定 90°
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [cell(0, 1, False)]},
    )
    before_query = client.get("/query").json()
    before_station = client.get("/stations/A").json()

    # 90° 下 (2,1) -> (2,2)，而首测没有 (2,2)，因此无可行方向。
    rejected = client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 2, "cells": [cell(2, 1, True)]},
    )
    assert rejected.status_code == 422, rejected.text
    assert "无可行方向" in rejected.json()["detail"]

    assert client.get("/query").json() == before_query
    assert client.get("/stations/A").json() == before_station
    assert client.get("/stations/A/batches/2").status_code == 404


def test_no_direction_rejects_correction_and_keeps_original(
    client: TestClient,
) -> None:
    # 首测缺 (2,2)：90° 下把台面 (2,1) 映射到 (2,2) 即为不可行更正。
    body = {
        "grid_size": 4,
        "chips": [
            {"x": x, "y": y, "passed": True}
            for y in range(4)
            for x in range(4)
            if (x, y) != (2, 2)
        ],
    }
    assert client.post("/first-test", json=body).status_code == 201
    client.put("/stations/A/anchors", json=anchors((0, 0, 3, 0)))
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 3, "cells": [cell(1, 0, False)]},
    )
    before_query = client.get("/query").json()
    before_batch = client.get("/stations/A/batches/3").json()

    rejected = client.put(
        "/stations/A/batches/3",
        json={"cells": [cell(2, 1, False)]},
    )
    assert rejected.status_code == 422, rejected.text
    assert "无可行方向" in rejected.json()["detail"]
    assert client.get("/query").json() == before_query
    assert client.get("/stations/A/batches/3").json() == before_batch


def test_bad_anchor_replacement_is_rejected(client: TestClient) -> None:
    setup_wafer(client, 4)
    client.put("/stations/A/anchors", json=anchors((0, 0, 3, 0)))
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 4, "cells": [cell(0, 1, False)]},
    )
    before = client.get("/stations/A").json()
    before_query = client.get("/query").json()

    # (0,0)->(1,1) 不可能是任何 90° 倍数旋转的结果。
    rejected = client.put("/stations/A/anchors", json=anchors((0, 0, 1, 1)))
    assert rejected.status_code == 422, rejected.text
    assert client.get("/stations/A").json() == before
    assert client.get("/query").json() == before_query


def test_duplicate_cell_in_batch_is_rejected(client: TestClient) -> None:
    setup_wafer(client, 4)
    resp = client.post(
        "/stations/A/batches",
        json={
            "station_id": "A",
            "batch_seq": 1,
            "cells": [cell(0, 0, True), cell(0, 0, False)],
        },
    )
    assert resp.status_code == 422
    assert "重复" in resp.json()["detail"]


def test_batch_seq_is_unique_across_stations(client: TestClient) -> None:
    setup_wafer(client, 4)
    assert client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 8, "cells": [cell(0, 0, True)]},
    ).status_code == 201
    resp = client.post(
        "/stations/B/batches",
        json={"station_id": "B", "batch_seq": 8, "cells": [cell(0, 0, False)]},
    )
    assert resp.status_code == 409
    assert "全片唯一" in resp.json()["detail"]


def test_station_id_mismatch_rejected(client: TestClient) -> None:
    setup_wafer(client, 3)
    resp = client.post(
        "/stations/A/batches",
        json={"station_id": "B", "batch_seq": 1, "cells": [cell(0, 0, True)]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- 更正与读取隔离


def test_correction_applies_with_merge_and_raw_read_is_station_scoped(
    client: TestClient,
) -> None:
    setup_wafer(client, 4)
    client.put("/stations/A/anchors", json=anchors((0, 0, 3, 0)))  # 90°
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 6, "cells": [cell(1, 0, False)]},
    )
    # 90°: (1,0) -> (3,1)
    assert query_map(client)[(3, 1)]["passed"] is False

    corrected = client.put(
        "/stations/A/batches/6",
        json={"cells": [cell(1, 0, True), cell(2, 0, False)]},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["merge_status"] == "merged"
    chips = query_map(client)
    assert chips[(3, 1)]["passed"] is True
    assert chips[(3, 1)]["batch_seq"] == 6
    # 90°: (2,0) -> (3,2)
    assert chips[(3, 2)]["passed"] is False

    raw = client.get("/stations/A/batches/6").json()
    assert raw["cells"] == [cell(1, 0, True), cell(2, 0, False)]

    # 其他台不可读取本台原始批次。
    assert client.get("/stations/B/batches/6").status_code == 404
    # 不存在的台/批次。
    assert client.get("/stations/NOPE").status_code == 404
    assert client.get("/stations/A/batches/999").status_code == 404


def test_correction_and_passed_marks_update_query(client: TestClient) -> None:
    setup_wafer(client, 3)
    client.put("/stations/A/anchors", json=anchors((0, 0, 2, 0)))
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 11, "cells": [cell(0, 0, False)]},
    )
    client.put(
        "/stations/A/batches/11",
        json={"cells": [cell(0, 0, True)]},
    )
    # 90°: (0,0)->(2,0)
    assert query_map(client)[(2, 0)]["passed"] is True


def test_batches_list_returns_raw_batches_in_order(client: TestClient) -> None:
    setup_wafer(client, 3)
    client.put("/stations/A/anchors", json=anchors((0, 0, 2, 0)))
    for seq in (30, 10, 20):
        assert client.post(
            "/stations/A/batches",
            json={"station_id": "A", "batch_seq": seq, "cells": [cell(0, 0, seq == 10)]},
        ).status_code == 201
    listed = client.get("/stations/A/batches").json()
    assert [b["batch_seq"] for b in listed] == [10, 20, 30]
    assert all(b["station_id"] == "A" for b in listed)


# ---------------------------------------------------------------- 补充场景


def test_rejected_batch_does_not_create_station(client: TestClient) -> None:
    body = {
        "grid_size": 3,
        "chips": [{"x": 1, "y": 1, "passed": True}],
    }
    assert client.post("/first-test", json=body).status_code == 201
    # (0,0) 的四个旋转像都不在首测集合里。
    rejected = client.post(
        "/stations/NEW/batches",
        json={"station_id": "NEW", "batch_seq": 1, "cells": [cell(0, 0, True)]},
    )
    assert rejected.status_code == 422
    assert client.get("/stations/NEW").status_code == 404
    assert client.get("/stations").json() == []


def test_rejected_anchor_replacement_does_not_create_station(
    client: TestClient,
) -> None:
    setup_wafer(client, 4)
    rejected = client.put("/stations/Z/anchors", json=anchors((0, 0, 1, 1)))
    assert rejected.status_code == 422
    assert client.get("/stations/Z").status_code == 404


def test_station_merge_isolation_and_cross_station_latest(client: TestClient) -> None:
    setup_wafer(client, 4)
    # A 台 90°，B 台 180°。
    client.put("/stations/A/anchors", json=anchors((0, 0, 3, 0)))
    client.put("/stations/B/anchors", json=anchors((0, 0, 3, 3)))

    # 同一颗首测芯片 (2,1)：A 台 90° 时对应台面 (1,0)->(3,1)? 不，重算：
    # 90°: (x,y)->(n-1-y, x)；(1,1)->(2,1)。
    assert client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 100, "cells": [cell(1, 1, True)]},
    ).status_code == 201
    # 180°: (x,y)->(n-1-x,n-1-y)；(1,2)->(2,1)。
    assert client.post(
        "/stations/B/batches",
        json={"station_id": "B", "batch_seq": 101, "cells": [cell(1, 2, False)]},
    ).status_code == 201

    chips = query_map(client)
    # 两颗台都落在 (2,1)，取序号更大的 101。
    assert chips[(2, 1)]["station_id"] == "B"
    assert chips[(2, 1)]["batch_seq"] == 101
    assert chips[(2, 1)]["passed"] is False

    # A 台多解撤下归并，不影响 B 台。
    client.delete("/stations/A/anchors")
    chips = query_map(client)
    assert chips[(2, 1)]["station_id"] == "B"
    assert chips[(2, 1)]["batch_seq"] == 101
    # A 的原始批次仍可读；B 的归并仍在。
    assert client.get("/stations/A/batches/100").status_code == 200
    assert client.get("/stations/B").json()["merge_status"] == "merged"


def test_batch_body_route_equivalent(client: TestClient) -> None:
    setup_wafer(client, 3)
    resp = client.post(
        "/batches",
        json={"station_id": "A", "batch_seq": 2, "cells": [cell(0, 0, False)]},
    )
    assert resp.status_code == 201, resp.text
    assert client.get("/stations/A/batches/2").json()["cells"] == [cell(0, 0, False)]
    missing = client.post(
        "/batches", json={"batch_seq": 3, "cells": [cell(0, 0, False)]}
    )
    assert missing.status_code == 422


def test_anchor_clear_then_resubmit_regains_unique_merge(client: TestClient) -> None:
    setup_wafer(client, 4)
    client.put("/stations/A/anchors", json=anchors((0, 0, 3, 0)))
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [cell(0, 1, False)]},
    )
    assert query_map(client)[(2, 0)]["source"] == "retest"

    assert client.delete("/stations/A/anchors").json()["merge_status"] == "ambiguous"
    assert query_map(client)[(2, 0)]["source"] == "first"

    # 重新给定同一组定位点，唯一方向恢复，历史批次一起重新归并。
    again = client.put("/stations/A/anchors", json=anchors((0, 0, 3, 0)))
    assert again.json()["orientation"] == 90
    chips = query_map(client)
    assert chips[(2, 0)]["source"] == "retest"
    assert chips[(2, 0)]["batch_seq"] == 1


def test_multiple_anchor_pairs_must_all_match(client: TestClient) -> None:
    setup_wafer(client, 4)
    ok = client.put(
        "/stations/A/anchors",
        json=anchors((0, 0, 3, 0), (1, 0, 3, 1)),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["orientation"] == 90

    conflict = client.put(
        "/stations/A/anchors",
        json=anchors((0, 0, 3, 0), (1, 0, 2, 0)),
    )
    assert conflict.status_code == 422
    # 原定位点保留。
    assert client.get("/stations/A").json()["anchors"] == [
        {"stage_x": 0, "stage_y": 0, "first_x": 3, "first_y": 0},
        {"stage_x": 1, "stage_y": 0, "first_x": 3, "first_y": 1},
    ]


def test_query_counts_and_source_fields(client: TestClient) -> None:
    setup_wafer(client, 3, passed=lambda x, y: True)
    client.put("/stations/A/anchors", json=anchors((0, 0, 2, 0)))
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [cell(0, 0, False)]},
    )
    payload = client.get("/query").json()
    assert payload["chip_count"] == 9
    assert payload["from_retest"] == 1
    assert payload["from_first"] == 8
    assert payload["total_passed"] == 8
    assert payload["total_failed"] == 1
    assert client.get("/query").headers["cache-control"] == "no-store"


def test_docs_and_openapi_available(client: TestClient) -> None:
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "/static/swagger/swagger-ui-bundle.js" in docs.text
    static = client.get("/static/swagger/swagger-ui-bundle.js")
    assert static.status_code == 200
    assert len(static.content) > 100_000
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    for path in (
        "/first-test",
        "/stations/{station_id}/anchors",
        "/stations/{station_id}/batches",
        "/query",
    ):
        assert path in paths


def test_batch_cell_out_of_bounds_rejected(client: TestClient) -> None:
    setup_wafer(client, 3)
    resp = client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [cell(3, 0, True)]},
    )
    assert resp.status_code == 422
    assert "超出" in resp.json()["detail"]


def test_reposting_same_batch_is_conflict(client: TestClient) -> None:
    setup_wafer(client, 3)
    body = {"station_id": "A", "batch_seq": 5, "cells": [cell(0, 0, True)]}
    assert client.post("/stations/A/batches", json=body).status_code == 201
    again = client.post("/stations/A/batches", json=body)
    assert again.status_code == 409
    # 原批次未被覆盖，仍可重读并可用 PUT 更正。
    assert client.get("/stations/A/batches/5").json()["cells"] == [cell(0, 0, True)]


def test_cannot_correct_another_stations_batch(client: TestClient) -> None:
    setup_wafer(client, 3)
    assert client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 5, "cells": [cell(0, 0, True)]},
    ).status_code == 201
    resp = client.put("/stations/B/batches/5", json={"cells": [cell(0, 0, False)]})
    assert resp.status_code == 404
    assert client.get("/stations/A/batches/5").json()["cells"] == [cell(0, 0, True)]


def test_put_empty_anchors_equals_clear(client: TestClient) -> None:
    setup_wafer(client, 3)
    client.put("/stations/A/anchors", json=anchors((0, 0, 2, 0)))
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [cell(0, 1, False)]},
    )
    assert query_map(client)[(1, 0)]["source"] == "retest"
    resp = client.put("/stations/A/anchors", json={"anchors": []})
    assert resp.status_code == 200
    assert resp.json()["merge_status"] == "ambiguous"
    assert client.get("/stations/A").json()["anchors"] == []
    assert query_map(client)[(1, 0)]["source"] == "first"


def test_anchor_clear_keeps_batches_and_allows_reread(client: TestClient) -> None:
    setup_wafer(client, 3)
    client.put("/stations/A/anchors", json=anchors((0, 0, 2, 0)))
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 2, "cells": [cell(0, 1, False)]},
    )
    client.delete("/stations/A/anchors")
    # 清空后仍可重读本台原始批次，坐标保持录入时的台面坐标。
    raw = client.get("/stations/A/batches/2").json()
    assert raw["cells"] == [cell(0, 1, False)]
    station = client.get("/stations/A").json()
    assert station["batch_count"] == 1
    assert station["merge_status"] == "ambiguous"


def test_changing_direction_remaps_existing_batches(client: TestClient) -> None:
    setup_wafer(client, 4)
    client.put("/stations/A/anchors", json=anchors((0, 0, 3, 0)))  # 90°
    client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [cell(1, 0, False)]},
    )
    assert query_map(client)[(3, 1)]["batch_seq"] == 1

    # 换成 180°：同一台面格现在落到 (2, 3)，原落点回退首测。
    resp = client.put("/stations/A/anchors", json=anchors((0, 0, 3, 3)))
    assert resp.json()["orientation"] == 180
    chips = query_map(client)
    assert chips[(2, 3)]["source"] == "retest"
    assert chips[(2, 3)]["batch_seq"] == 1
    assert chips[(3, 1)]["source"] == "first"


def test_correction_can_drop_to_multiple_directions_and_withdraw_merge(
    client: TestClient,
) -> None:
    # 首测只保留少数芯片，配合中心定位点让可行方向随批次内容变化。
    keep = {(2, 2), (0, 0), (4, 4), (0, 4)}
    body = {
        "grid_size": 5,
        "chips": [{"x": x, "y": y, "passed": True} for x, y in sorted(keep)],
    }
    assert client.post("/first-test", json=body).status_code == 201
    client.put("/stations/A/anchors", json=anchors((2, 2, 2, 2)))

    # 三个台面坐标逐步把方向收窄到 0°。
    assert client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [cell(0, 0, False)]},
    ).json()["merge_status"] == "ambiguous"
    assert client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 2, "cells": [cell(4, 4, False)]},
    ).json()["merge_status"] == "ambiguous"
    merged = client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 3, "cells": [cell(0, 4, False)]},
    )
    assert merged.status_code == 201
    assert merged.json()["merge_status"] == "merged"
    assert merged.json()["orientation"] == 0

    # 更正掉最后一个区分的格子，方向回到多解：撤下归并，原始批次仍可读。
    corrected = client.put(
        "/stations/A/batches/3", json={"cells": [cell(2, 2, True)]}
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["merge_status"] == "ambiguous"
    assert corrected.json()["candidates"] == [0, 180]
    assert client.get("/stations/A/batches/3").json()["cells"] == [cell(2, 2, True)]
    assert client.get("/query").json()["from_retest"] == 0


def test_batch_before_first_test_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/stations/A/batches",
        json={"station_id": "A", "batch_seq": 1, "cells": [cell(0, 0, True)]},
    )
    assert resp.status_code == 409
    assert client.get("/stations").status_code == 409
    assert client.get("/stations/A").status_code == 409


def test_concurrent_duplicate_batch_sequence_yields_one_conflict(
    client: TestClient,
) -> None:
    setup_wafer(client, 4)
    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def submit(passed: bool) -> None:
        barrier.wait(timeout=5)
        resp = client.post(
            "/stations/A/batches",
            json={"station_id": "A", "batch_seq": 77, "cells": [cell(0, 1, passed)]},
        )
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=submit, args=(flag,)) for flag in (True, False)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 409], results
    # 只落一份原始批次。
    assert client.get("/stations/A").json()["batch_count"] == 1
