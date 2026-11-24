"""网格旋转与可行方向判定的单元测试。"""

from __future__ import annotations

import pytest

from app.geometry import feasible_orientations, rotate


@pytest.mark.parametrize(
    ("degrees", "expected"),
    [
        (0, (1, 2)),
        (90, (1, 1)),
        (180, (2, 1)),
        (270, (2, 2)),
    ],
)
def test_rotate_four_orientations(degrees: int, expected: tuple[int, int]) -> None:
    assert rotate(1, 2, 4, degrees) == expected


def test_rotation_cycles_back() -> None:
    point = (0, 3)
    once = rotate(*point, 4, 90)
    twice = rotate(*once, 4, 90)
    three = rotate(*twice, 4, 90)
    four = rotate(*three, 4, 90)
    assert once == (0, 0)
    assert twice == (3, 0)
    assert four == point


def test_anchor_pins_single_orientation() -> None:
    first = {(0, 0), (1, 0), (0, 1), (1, 1)}
    candidates = feasible_orientations(
        2, first, anchors=[(0, 0, 1, 0)], stage_coordinates={(0, 0)}
    )
    assert candidates == [90]


def test_no_candidates_when_anchor_impossible() -> None:
    first = {(0, 0), (1, 0), (0, 1), (1, 1), (2, 2)}
    assert (
        feasible_orientations(
            3, first, anchors=[(0, 0, 1, 1)], stage_coordinates=set()
        )
        == []
    )


def test_stage_coordinate_must_land_on_first_chip() -> None:
    # 3×3 中 (0, 0) 的四个旋转像都不在首测集合里。
    first = {(1, 1)}
    assert (
        feasible_orientations(
            3, first, anchors=[], stage_coordinates={(0, 0)}
        )
        == []
    )


def test_empty_constraints_allow_all_orientations() -> None:
    first = {(0, 0), (1, 0), (0, 1), (1, 1)}
    assert feasible_orientations(2, first, anchors=[], stage_coordinates=set()) == [
        0,
        90,
        180,
        270,
    ]


def test_center_pin_leaves_multiple_orientations() -> None:
    # 5×5 的中心格在四个方向上都映射到自身，锚点无法定唯一方向。
    first = {(2, 2)}
    assert feasible_orientations(
        5, first, anchors=[(2, 2, 2, 2)], stage_coordinates={(2, 2)}
    ) == [0, 90, 180, 270]
