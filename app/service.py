"""业务规则：首测、复测批次、定位点、方向归并与查询。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException, status

from .geometry import feasible_orientations, in_bounds, inverse_rotate, rotate
from .http_status import UNPROCESSABLE
from .recovery import RecoveryError, plan_recovery
from .schemas import (
    AnchorPairIn,
    BatchOut,
    BatchResultOut,
    BatchSummaryOut,
    CellIn,
    ChipOut,
    CoordinateOut,
    FirstTestOut,
    MergeResultOut,
    QueryOut,
    RecoveryResultOut,
    StageCellOut,
    StationOut,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 读取辅助


def _wafer_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM wafer WHERE id = 1").fetchone()


def require_wafer(conn: sqlite3.Connection) -> sqlite3.Row:
    row = _wafer_row(conn)
    if row is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="尚未录入首测，请先 POST /first-test",
        )
    return row


def _first_coords(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    return {(r["x"], r["y"]) for r in conn.execute("SELECT x, y FROM first_chips")}


def _feasible(
    conn: sqlite3.Connection,
    grid_size: int,
    anchors: list[tuple[int, int, int, int]],
    stage_coordinates: set[tuple[int, int]],
) -> list[int]:
    return feasible_orientations(
        grid_size, _first_coords(conn), anchors, stage_coordinates
    )


def _anchors(conn: sqlite3.Connection, station_id: str) -> list[tuple[int, int, int, int]]:
    return [
        (r["stage_x"], r["stage_y"], r["first_x"], r["first_y"])
        for r in conn.execute(
            "SELECT stage_x, stage_y, first_x, first_y FROM station_anchors "
            "WHERE station_id = ? ORDER BY stage_y, stage_x",
            (station_id,),
        )
    ]


def _stage_coords(
    conn: sqlite3.Connection, station_id: str, exclude_batch: int | None = None
) -> set[tuple[int, int]]:
    if exclude_batch is None:
        rows = conn.execute(
            "SELECT DISTINCT x, y FROM batch_cells WHERE station_id = ?",
            (station_id,),
        )
    else:
        rows = conn.execute(
            "SELECT DISTINCT x, y FROM batch_cells "
            "WHERE station_id = ? AND batch_seq != ?",
            (station_id, exclude_batch),
        )
    return {(r["x"], r["y"]) for r in rows}


def _ensure_station(conn: sqlite3.Connection, station_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO stations (id, created_at) VALUES (?, ?)",
        (station_id, now_iso()),
    )


def _validate_cells(
    cells: list[CellIn], grid_size: int, label: str
) -> list[CellIn]:
    seen: set[tuple[int, int]] = set()
    for cell in cells:
        if not in_bounds(cell.x, cell.y, grid_size):
            raise HTTPException(
                UNPROCESSABLE,
                detail=f"{label}坐标 ({cell.x}, {cell.y}) 超出 {grid_size}×{grid_size} 网格",
            )
        key = (cell.x, cell.y)
        if key in seen:
            raise HTTPException(
                UNPROCESSABLE,
                detail=f"{label}坐标 ({cell.x}, {cell.y}) 重复；批内每格只可报一次",
            )
        seen.add(key)
    return cells


# ---------------------------------------------------------------- 归并


def _candidates(conn: sqlite3.Connection, station_id: str, grid_size: int) -> list[int]:
    return _feasible(
        conn,
        grid_size,
        _anchors(conn, station_id),
        _stage_coords(conn, station_id),
    )


def _apply_merge(
    conn: sqlite3.Connection, station_id: str, grid_size: int
) -> MergeResultOut:
    """按当前定位点与原始批次重算本台归并。

    - 唯一方向：撤下旧归并并按该方向重建归并数据；
    - 零/多方向：撤下旧归并，原始批次保持原样。
    """
    candidates = _candidates(conn, station_id, grid_size)

    conn.execute("DELETE FROM merged_cells WHERE station_id = ?", (station_id,))
    conn.execute("DELETE FROM station_merge WHERE station_id = ?", (station_id,))

    if len(candidates) == 1:
        orientation = candidates[0]
        rows = conn.execute(
            "SELECT batch_seq, x, y, passed FROM batch_cells WHERE station_id = ?",
            (station_id,),
        ).fetchall()
        conn.executemany(
            "INSERT INTO merged_cells (station_id, batch_seq, x, y, passed) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    station_id,
                    r["batch_seq"],
                    *rotate(r["x"], r["y"], grid_size, orientation),
                    r["passed"],
                )
                for r in rows
            ],
        )
        conn.execute(
            "INSERT INTO station_merge (station_id, orientation, merged_at) "
            "VALUES (?, ?, ?)",
            (station_id, orientation, now_iso()),
        )
        return MergeResultOut(
            station_id=station_id,
            candidates=candidates,
            orientation=orientation,
            merge_status="merged",
            message=f"方向唯一（{orientation}°），本台复测已归并",
        )

    if candidates:
        message = (
            f"存在 {len(candidates)} 个可行方向 {candidates}，无法唯一确定；"
            "保留原始批次并撤下该台旧归并"
        )
    else:
        message = "无可行方向；原始批次保留，本台不归并"
    return MergeResultOut(
        station_id=station_id,
        candidates=candidates,
        orientation=None,
        merge_status="ambiguous",
        message=message,
    )


# ---------------------------------------------------------------- 首测


def create_first_test(
    conn: sqlite3.Connection, grid_size: int, cells: list[CellIn]
) -> FirstTestOut:
    if _wafer_row(conn) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="首测已录入，作为基准数据不可覆盖；如需重建请使用全新数据库",
        )
    _validate_cells(cells, grid_size, "首测")
    ts = now_iso()
    conn.execute(
        "INSERT INTO wafer (id, grid_size, created_at, updated_at) VALUES (1, ?, ?, ?)",
        (grid_size, ts, ts),
    )
    conn.executemany(
        "INSERT INTO first_chips (x, y, passed) VALUES (?, ?, ?)",
        [(c.x, c.y, int(c.passed)) for c in cells],
    )
    return get_first_test(conn)


def get_first_test(conn: sqlite3.Connection) -> FirstTestOut:
    wafer = require_wafer(conn)
    rows = conn.execute("SELECT x, y, passed FROM first_chips ORDER BY y, x").fetchall()
    return FirstTestOut(
        grid_size=wafer["grid_size"],
        chip_count=len(rows),
        created_at=wafer["created_at"],
        updated_at=wafer["updated_at"],
        chips=[
            ChipOut(x=r["x"], y=r["y"], passed=bool(r["passed"]), source="first")
            for r in rows
        ],
    )


# ---------------------------------------------------------------- 定位点


def _save_anchors(
    conn: sqlite3.Connection,
    station_id: str,
    pairs: list[tuple[int, int, int, int]],
) -> None:
    conn.execute("DELETE FROM station_anchors WHERE station_id = ?", (station_id,))
    conn.executemany(
        "INSERT INTO station_anchors (station_id, stage_x, stage_y, first_x, first_y) "
        "VALUES (?, ?, ?, ?, ?)",
        [(station_id, *pair) for pair in pairs],
    )


def replace_anchors(
    conn: sqlite3.Connection, station_id: str, pairs: list[AnchorPairIn]
) -> MergeResultOut:
    wafer = require_wafer(conn)
    grid_size = wafer["grid_size"]
    first_coords = _first_coords(conn)

    seen_stage: set[tuple[int, int]] = set()
    seen_first: set[tuple[int, int]] = set()
    for p in pairs:
        if not in_bounds(p.stage_x, p.stage_y, grid_size):
            raise HTTPException(
                UNPROCESSABLE,
                detail=(
                    f"台面定位点 ({p.stage_x}, {p.stage_y}) "
                    f"超出 {grid_size}×{grid_size} 网格"
                ),
            )
        if not in_bounds(p.first_x, p.first_y, grid_size):
            raise HTTPException(
                UNPROCESSABLE,
                detail=(
                    f"首测定位点 ({p.first_x}, {p.first_y}) "
                    f"超出 {grid_size}×{grid_size} 网格"
                ),
            )
        if (p.first_x, p.first_y) not in first_coords:
            raise HTTPException(
                UNPROCESSABLE,
                detail=f"首测定位点 ({p.first_x}, {p.first_y}) 不是首测芯片",
            )
        if (p.stage_x, p.stage_y) in seen_stage:
            raise HTTPException(
                UNPROCESSABLE,
                detail=f"台面定位点 ({p.stage_x}, {p.stage_y}) 重复",
            )
        if (p.first_x, p.first_y) in seen_first:
            raise HTTPException(
                UNPROCESSABLE,
                detail=f"首测定位点 ({p.first_x}, {p.first_y}) 重复",
            )
        seen_stage.add((p.stage_x, p.stage_y))
        seen_first.add((p.first_x, p.first_y))

    new_pairs = [(p.stage_x, p.stage_y, p.first_x, p.first_y) for p in pairs]
    prospective = _feasible(
        conn, grid_size, new_pairs, _stage_coords(conn, station_id)
    )
    if not prospective:
        # 整组替换被退回：不动定位点、不动归并，也不新建台记录。
        raise HTTPException(
            UNPROCESSABLE,
            detail="无可行方向：该组定位点不能把本台历次复测落到首测芯片上；"
            "本次替换已退回，原定位点与归并保持不变",
        )

    _ensure_station(conn, station_id)
    _save_anchors(conn, station_id, new_pairs)
    return _apply_merge(conn, station_id, grid_size)


def clear_anchors(conn: sqlite3.Connection, station_id: str) -> MergeResultOut:
    wafer = require_wafer(conn)
    grid_size = wafer["grid_size"]
    station = conn.execute(
        "SELECT id FROM stations WHERE id = ?", (station_id,)
    ).fetchone()
    if station is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"台 {station_id} 不存在"
        )

    if not _feasible(conn, grid_size, [], _stage_coords(conn, station_id)):
        raise HTTPException(
            UNPROCESSABLE,
            detail="清空定位点后无可行方向；本次清空已退回，"
            "原定位点与归并保持不变",
        )
    _save_anchors(conn, station_id, [])
    return _apply_merge(conn, station_id, grid_size)


# ---------------------------------------------------------------- 批次


def submit_batch(
    conn: sqlite3.Connection,
    station_id: str,
    batch_seq: int,
    cells: list[CellIn],
) -> BatchResultOut:
    wafer = require_wafer(conn)
    grid_size = wafer["grid_size"]
    _validate_cells(cells, grid_size, "复测")

    existing = conn.execute(
        "SELECT station_id FROM batches WHERE batch_seq = ?", (batch_seq,)
    ).fetchone()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"批次序号 {batch_seq} 已被台 {existing['station_id']} 占用；"
                "批次序号须全片唯一"
            ),
        )

    prospective = _stage_coords(conn, station_id) | {(c.x, c.y) for c in cells}
    candidates = _feasible(conn, grid_size, _anchors(conn, station_id), prospective)
    if not candidates:
        raise HTTPException(
            UNPROCESSABLE,
            detail="无可行方向：本批坐标与定位点不匹配，或无法把本台历次复测"
            "都落到首测芯片上；本次录入已退回，原数据与查询结果不变",
        )

    _ensure_station(conn, station_id)
    ts = now_iso()
    conn.execute(
        "INSERT INTO batches (station_id, batch_seq, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (station_id, batch_seq, ts, ts),
    )
    conn.executemany(
        "INSERT INTO batch_cells (station_id, batch_seq, x, y, passed) "
        "VALUES (?, ?, ?, ?, ?)",
        [(station_id, batch_seq, c.x, c.y, int(c.passed)) for c in cells],
    )
    result = _apply_merge(conn, station_id, grid_size)
    return BatchResultOut(
        **result.model_dump(), batch_seq=batch_seq, cell_count=len(cells)
    )


def correct_batch(
    conn: sqlite3.Connection,
    station_id: str,
    batch_seq: int,
    cells: list[CellIn],
) -> BatchResultOut:
    wafer = require_wafer(conn)
    grid_size = wafer["grid_size"]

    batch = conn.execute(
        "SELECT station_id FROM batches WHERE station_id = ? AND batch_seq = ?",
        (station_id, batch_seq),
    ).fetchone()
    if batch is None:
        _raise_batch_not_found(conn, station_id, batch_seq)
    _validate_cells(cells, grid_size, "更正")

    prospective = _stage_coords(conn, station_id, exclude_batch=batch_seq) | {
        (c.x, c.y) for c in cells
    }
    candidates = _feasible(conn, grid_size, _anchors(conn, station_id), prospective)
    if not candidates:
        raise HTTPException(
            UNPROCESSABLE,
            detail="无可行方向：更正后的坐标无法与定位点及本台其他批次共存；"
            "本次更正已退回，原批次与查询结果不变",
        )

    conn.execute(
        "DELETE FROM batch_cells WHERE station_id = ? AND batch_seq = ?",
        (station_id, batch_seq),
    )
    conn.executemany(
        "INSERT INTO batch_cells (station_id, batch_seq, x, y, passed) "
        "VALUES (?, ?, ?, ?, ?)",
        [(station_id, batch_seq, c.x, c.y, int(c.passed)) for c in cells],
    )
    conn.execute(
        "UPDATE batches SET updated_at = ? WHERE station_id = ? AND batch_seq = ?",
        (now_iso(), station_id, batch_seq),
    )
    result = _apply_merge(conn, station_id, grid_size)
    return BatchResultOut(
        **result.model_dump(), batch_seq=batch_seq, cell_count=len(cells)
    )


def _raise_batch_not_found(
    conn: sqlite3.Connection, station_id: str, batch_seq: int
) -> None:
    other = conn.execute(
        "SELECT station_id FROM batches WHERE batch_seq = ?", (batch_seq,)
    ).fetchone()
    if other is not None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"台 {station_id} 没有批次 {batch_seq}；该批次属于台 "
            f"{other['station_id']}，只可读取本台原始批次",
        )
    raise HTTPException(
        status.HTTP_404_NOT_FOUND, detail=f"台 {station_id} 没有批次 {batch_seq}"
    )


def get_batch(conn: sqlite3.Connection, station_id: str, batch_seq: int) -> BatchOut:
    require_wafer(conn)
    row = conn.execute(
        "SELECT * FROM batches WHERE station_id = ? AND batch_seq = ?",
        (station_id, batch_seq),
    ).fetchone()
    if row is None:
        _raise_batch_not_found(conn, station_id, batch_seq)
    cells = [
        CellIn(x=r["x"], y=r["y"], passed=bool(r["passed"]))
        for r in conn.execute(
            "SELECT x, y, passed FROM batch_cells "
            "WHERE station_id = ? AND batch_seq = ? ORDER BY y, x",
            (station_id, batch_seq),
        )
    ]
    return BatchOut(
        station_id=station_id,
        batch_seq=batch_seq,
        cell_count=len(cells),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cells=cells,
    )


def list_batches(conn: sqlite3.Connection, station_id: str) -> list[BatchOut]:
    require_wafer(conn)
    station = conn.execute(
        "SELECT id FROM stations WHERE id = ?", (station_id,)
    ).fetchone()
    if station is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"台 {station_id} 不存在")
    seqs = [
        r["batch_seq"]
        for r in conn.execute(
            "SELECT batch_seq FROM batches WHERE station_id = ? ORDER BY batch_seq",
            (station_id,),
        )
    ]
    return [get_batch(conn, station_id, seq) for seq in seqs]


# ---------------------------------------------------------------- 台信息


def get_station(conn: sqlite3.Connection, station_id: str) -> StationOut:
    require_wafer(conn)
    station = conn.execute(
        "SELECT id FROM stations WHERE id = ?", (station_id,)
    ).fetchone()
    if station is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"台 {station_id} 不存在")

    wafer = _wafer_row(conn)
    assert wafer is not None
    grid_size = wafer["grid_size"]
    anchors = [
        {"stage_x": sx, "stage_y": sy, "first_x": fx, "first_y": fy}
        for sx, sy, fx, fy in _anchors(conn, station_id)
    ]
    batches = [
        BatchSummaryOut(
            batch_seq=r["batch_seq"],
            cell_count=r["cell_count"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in conn.execute(
            "SELECT b.batch_seq, b.created_at, b.updated_at, "
            "       COUNT(c.x) AS cell_count "
            "FROM batches b LEFT JOIN batch_cells c "
            "  ON c.station_id = b.station_id AND c.batch_seq = b.batch_seq "
            "WHERE b.station_id = ? GROUP BY b.batch_seq ORDER BY b.batch_seq",
            (station_id,),
        )
    ]
    candidates = _candidates(conn, station_id, grid_size)
    merge = conn.execute(
        "SELECT orientation FROM station_merge WHERE station_id = ?", (station_id,)
    ).fetchone()
    merged_count = conn.execute(
        "SELECT COUNT(*) AS n FROM merged_cells WHERE station_id = ?", (station_id,)
    ).fetchone()["n"]
    return StationOut(
        station_id=station_id,
        anchors=anchors,
        batch_count=len(batches),
        batches=batches,
        candidates=candidates,
        orientation=merge["orientation"] if merge is not None else None,
        merge_status="merged" if merge is not None else "ambiguous",
        merged_cell_count=merged_count,
    )


def list_stations(conn: sqlite3.Connection) -> list[StationOut]:
    require_wafer(conn)
    return [
        get_station(conn, r["id"])
        for r in conn.execute("SELECT id FROM stations ORDER BY id")
    ]


# ---------------------------------------------------------------- 查询


def query_wafer(conn: sqlite3.Connection) -> QueryOut:
    wafer = require_wafer(conn)
    rows = conn.execute(
        "SELECT f.x, f.y, f.passed AS first_passed, "
        "       m.passed AS retest_passed, m.station_id, m.batch_seq "
        "FROM first_chips f "
        "LEFT JOIN ("
        "    SELECT mc.x, mc.y, mc.passed, mc.station_id, mc.batch_seq "
        "    FROM merged_cells mc "
        "    JOIN ("
        "        SELECT x, y, MAX(batch_seq) AS max_seq "
        "        FROM merged_cells GROUP BY x, y"
        "    ) latest "
        "      ON latest.x = mc.x AND latest.y = mc.y "
        "     AND latest.max_seq = mc.batch_seq"
        ") m ON m.x = f.x AND m.y = f.y "
        "ORDER BY f.y, f.x"
    ).fetchall()

    chips: list[ChipOut] = []
    from_first = from_retest = 0
    for r in rows:
        if r["batch_seq"] is None:
            chips.append(
                ChipOut(
                    x=r["x"],
                    y=r["y"],
                    passed=bool(r["first_passed"]),
                    source="first",
                )
            )
            from_first += 1
        else:
            chips.append(
                ChipOut(
                    x=r["x"],
                    y=r["y"],
                    passed=bool(r["retest_passed"]),
                    source="retest",
                    station_id=r["station_id"],
                    batch_seq=r["batch_seq"],
                )
            )
            from_retest += 1

    total_passed = sum(1 for c in chips if c.passed)
    return QueryOut(
        grid_size=wafer["grid_size"],
        chip_count=len(chips),
        total_passed=total_passed,
        total_failed=len(chips) - total_passed,
        from_first=from_first,
        from_retest=from_retest,
        chips=chips,
    )


# ---------------------------------------------------------------- 坏片恢复


def _batch_stage_cells(
    conn: sqlite3.Connection, station_id: str, batch_seq: int
) -> dict[tuple[int, int], bool]:
    return {
        (r["x"], r["y"]): bool(r["passed"])
        for r in conn.execute(
            "SELECT x, y, passed FROM batch_cells "
            "WHERE station_id = ? AND batch_seq = ?",
            (station_id, batch_seq),
        )
    }


def _recovery_fingerprint(
    conn: sqlite3.Connection, station_id: str, batch_seq: int
) -> tuple[tuple[tuple[int, int, int], ...], int | None]:
    """写入前重核用的状态指纹：本批明细 + 当前归并方向。"""
    cells = tuple(
        sorted(
            (r["x"], r["y"], r["passed"])
            for r in conn.execute(
                "SELECT x, y, passed FROM batch_cells "
                "WHERE station_id = ? AND batch_seq = ?",
                (station_id, batch_seq),
            )
        )
    )
    merge = conn.execute(
        "SELECT orientation FROM station_merge WHERE station_id = ?", (station_id,)
    ).fetchone()
    return cells, None if merge is None else merge["orientation"]


def _recovery_plan(
    conn: sqlite3.Connection,
    station_id: str,
    batch_seq: int,
    grid_size: int,
    orientation: int,
    row_failed: list[int],
    col_failed: list[int],
) -> tuple[dict[tuple[int, int], bool], set[tuple[int, int]]]:
    """按当前方向把首测芯片换算成台面格，并求一致判定的缺失格。"""
    first_coords = _first_coords(conn)
    stage_cells = _batch_stage_cells(conn, station_id, batch_seq)

    # 首测芯片位置按当前方向的逆旋转换算成台面格子（S）。
    stage_scope = {
        inverse_rotate(fx, fy, grid_size, orientation) for fx, fy in first_coords
    }
    off_grid = sorted(
        (x, y) for x, y in stage_cells if (x, y) not in stage_scope
    )
    if off_grid:
        raise HTTPException(
            UNPROCESSABLE,
            detail=(
                "本批已有台面格在当前归并方向下落不到首测芯片上 "
                f"（例如 {off_grid[0]}），无法恢复；请先重新归并"
            ),
        )

    scoped_judgements = {
        (x, y): stage_cells[(x, y)] for x, y in stage_scope if (x, y) in stage_cells
    }
    try:
        determined = plan_recovery(
            grid_size, scoped_judgements, row_failed, col_failed
        )
    except RecoveryError as exc:
        raise HTTPException(UNPROCESSABLE, detail=str(exc)) from exc

    known = set(scoped_judgements)
    pending = {
        (x, y)
        for x, y in stage_scope
        if (x, y) not in known and (x, y) not in determined
    }
    return determined, pending


def recover_batch(
    conn: sqlite3.Connection,
    station_id: str,
    batch_seq: int,
    row_failed: list[int],
    col_failed: list[int],
) -> RecoveryResultOut:
    wafer = require_wafer(conn)
    grid_size = wafer["grid_size"]

    batch = conn.execute(
        "SELECT station_id FROM batches WHERE station_id = ? AND batch_seq = ?",
        (station_id, batch_seq),
    ).fetchone()
    if batch is None:
        _raise_batch_not_found(conn, station_id, batch_seq)

    merge = conn.execute(
        "SELECT orientation FROM station_merge WHERE station_id = ?", (station_id,)
    ).fetchone()
    if merge is None:
        raise HTTPException(
            UNPROCESSABLE,
            detail=(
                f"台 {station_id} 当前没有唯一归并方向（未归并或多解），"
                "恢复只处理已唯一归并的批次；本次恢复已退回"
            ),
        )
    orientation = merge["orientation"]

    # 防御式重核：读-算-写期间若本批明细或方向已变，按新状态重算一次；
    # 再次不一致则退回。正常由 BEGIN IMMEDIATE 串行化，循环只跑一轮。
    fingerprint = _recovery_fingerprint(conn, station_id, batch_seq)
    determined, pending = _recovery_plan(
        conn, station_id, batch_seq, grid_size, orientation, row_failed, col_failed
    )
    if _recovery_fingerprint(conn, station_id, batch_seq) != fingerprint:
        fingerprint = _recovery_fingerprint(conn, station_id, batch_seq)
        merge = conn.execute(
            "SELECT orientation FROM station_merge WHERE station_id = ?",
            (station_id,),
        ).fetchone()
        if merge is None:
            raise HTTPException(
                UNPROCESSABLE,
                detail="归并方向在写入前已变为非唯一，按新状态无法恢复，本次已退回",
            )
        orientation = merge["orientation"]
        determined, pending = _recovery_plan(
            conn,
            station_id,
            batch_seq,
            grid_size,
            orientation,
            row_failed,
            col_failed,
        )
        if _recovery_fingerprint(conn, station_id, batch_seq) != fingerprint:
            raise HTTPException(
                UNPROCESSABLE,
                detail="本批明细或归并方向在写入前持续变化，按新状态重核仍不一致，"
                "本次恢复已退回且未写入任何数据",
            )

    # 补回与归并在同一事务内提交：先补本批缺失格，再整体重建归并。
    conn.executemany(
        "INSERT INTO batch_cells (station_id, batch_seq, x, y, passed) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (station_id, batch_seq, x, y, int(passed))
            for (x, y), passed in determined.items()
        ],
    )
    conn.execute(
        "UPDATE batches SET updated_at = ? WHERE station_id = ? AND batch_seq = ?",
        (now_iso(), station_id, batch_seq),
    )
    merge_result = _apply_merge(conn, station_id, grid_size)
    if merge_result.orientation != orientation:
        # 补格只增加约束，方向理应保持唯一且不变；若变化说明状态已被改动。
        raise HTTPException(
            UNPROCESSABLE,
            detail="归并方向在写入前已变化，按新状态重核未通过，本次恢复已退回",
        )

    recovered_cells = [
        StageCellOut(x=x, y=y, passed=passed)
        for (x, y), passed in sorted(determined.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    ]
    pending_cells = [
        CoordinateOut(x=x, y=y) for x, y in sorted(pending, key=lambda p: (p[1], p[0]))
    ]
    message = (
        f"按 {orientation}° 方向恢复：补回 {len(recovered_cells)} 格判定一致的缺失格，"
        f"另有 {len(pending_cells)} 格判定不确定，仍须复测"
    )
    return RecoveryResultOut(
        station_id=station_id,
        batch_seq=batch_seq,
        orientation=orientation,
        merge_status=merge_result.merge_status,
        candidates=merge_result.candidates,
        row_failed=row_failed,
        column_failed=col_failed,
        recovered_cells=recovered_cells,
        pending_cells=pending_cells,
        recovered_count=len(recovered_cells),
        pending_count=len(pending_cells),
        message=message,
    )
