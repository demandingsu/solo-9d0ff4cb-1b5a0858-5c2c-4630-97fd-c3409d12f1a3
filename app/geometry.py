"""网格几何：绕中心旋转与可行方向判定。"""

from __future__ import annotations

from collections.abc import Iterable

ORIENTATIONS: tuple[int, ...] = (0, 90, 180, 270)


def in_bounds(x: int, y: int, grid_size: int) -> bool:
    """坐标采用 0 起的 (x=列, y=行)，整片为 grid_size × grid_size。"""
    return 0 <= x < grid_size and 0 <= y < grid_size


def rotate(x: int, y: int, grid_size: int, degrees: int) -> tuple[int, int]:
    """把台面坐标绕网格中心顺时针旋转 degrees 度，得到首测坐标。"""
    n = grid_size
    if degrees == 0:
        return x, y
    if degrees == 90:
        return n - 1 - y, x
    if degrees == 180:
        return n - 1 - x, n - 1 - y
    if degrees == 270:
        return y, n - 1 - x
    raise ValueError(f"不支持的方向: {degrees}")


def inverse_rotate(
    first_x: int, first_y: int, grid_size: int, degrees: int
) -> tuple[int, int]:
    """把首测坐标按 ``degrees`` 的逆方向转回台面坐标。

    即 ``rotate`` 的逆映射：``inverse_rotate(*rotate(x, y, n, d), n, d) == (x, y)``。
    """
    return rotate(first_x, first_y, grid_size, (-degrees) % 360)


def feasible_orientations(
    grid_size: int,
    first_coordinates: set[tuple[int, int]],
    anchors: Iterable[tuple[int, int, int, int]],
    stage_coordinates: Iterable[tuple[int, int]],
) -> list[int]:
    """返回同时满足定位点且能把全部台面坐标落到首测芯片上的方向。

    anchors 中每项为 (stage_x, stage_y, first_x, first_y)。
    stage_coordinates 是本台历次复测的全部台面坐标。
    """
    anchor_list = list(anchors)
    stage_list = list(stage_coordinates)
    result: list[int] = []
    for degrees in ORIENTATIONS:
        if any(
            rotate(sx, sy, grid_size, degrees) != (fx, fy)
            for sx, sy, fx, fy in anchor_list
        ):
            continue
        if any(
            rotate(x, y, grid_size, degrees) not in first_coordinates
            for x, y in stage_list
        ):
            continue
        result.append(degrees)
    return result
