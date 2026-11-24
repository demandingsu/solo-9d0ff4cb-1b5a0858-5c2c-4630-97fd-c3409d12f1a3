"""坏片恢复算法单元测试：流网络/SCC 结论与暴力枚举交叉验证。"""

from __future__ import annotations

import itertools
import random
import time

import pytest

from app.recovery import MAX_GRID_SIZE, RecoveryError, plan_recovery


def brute_force(
    n: int,
    stage_cells: dict[tuple[int, int], bool],
    row_failed: list[int],
    col_failed: list[int],
) -> dict[tuple[int, int], set[bool]] | None:
    """枚举所有相容填法，返回每个缺失格出现过的判定集合；无解返回 None。"""
    known = set(stage_cells)
    missing = [
        (x, y)
        for y in range(n)
        for x in range(n)
        if (x, y) not in known
    ]
    row_need = list(row_failed)
    col_need = list(col_failed)
    for (x, y), passed in stage_cells.items():
        if not passed:
            row_need[y] -= 1
            col_need[x] -= 1
    if any(v < 0 for v in row_need) or any(v < 0 for v in col_need):
        return None
    total = sum(row_need)
    if total != sum(col_need) or total > len(missing):
        return None

    verdicts: dict[tuple[int, int], set[bool]] = {c: set() for c in missing}
    found = False
    for fail_combo in itertools.combinations(range(len(missing)), total):
        fails = {missing[i] for i in fail_combo}
        r_need = list(row_need)
        c_need = list(col_need)
        for x, y in fails:
            r_need[y] -= 1
            c_need[x] -= 1
        if any(v != 0 for v in r_need) or any(v != 0 for v in c_need):
            continue
        found = True
        for cell in missing:
            verdicts[cell].add(cell not in fails)  # True=合格, False=坏片
    return verdicts if found else None


def cross_check(
    n: int,
    stage_cells: dict[tuple[int, int], bool],
    row_failed: list[int],
    col_failed: list[int],
) -> None:
    expected = brute_force(n, stage_cells, row_failed, col_failed)
    if expected is None:
        with pytest.raises(RecoveryError):
            plan_recovery(n, stage_cells, row_failed, col_failed)
        return
    determined = plan_recovery(n, stage_cells, row_failed, col_failed)
    for cell, options in expected.items():
        if len(options) == 1:
            assert determined[cell] == next(iter(options))
        else:
            assert cell not in determined


def test_mixed_determined_and_pending_case() -> None:
    # 4×4，0° 方向；已知 (0,0) 坏、(1,0) 好、(2,1) 坏。
    stage = {(0, 0): False, (1, 0): True, (2, 1): False}
    rows = [1, 2, 1, 1]
    cols = [1, 1, 1, 2]
    determined = plan_recovery(4, stage, rows, cols)

    forced_fail = {cell for cell, passed in determined.items() if not passed}
    forced_pass = {cell for cell, passed in determined.items() if passed}
    # (3,1) 虽被某个填法占用，但残量环可翻转，仍属待复测而非强制坏片。
    assert forced_fail == set()
    assert forced_pass == {
        (2, 0), (3, 0), (0, 1),
        (0, 2), (2, 2), (0, 3), (2, 3),
    }

    known = set(stage)
    pending = {
        (x, y)
        for y in range(4)
        for x in range(4)
        if (x, y) not in known and (x, y) not in determined
    }
    # 6 格可在相容填法间翻转（r1 与 r2/r3 经残量环连通），仍须复测。
    assert pending == {(1, 1), (3, 1), (1, 2), (3, 2), (1, 3), (3, 3)}
    # 交叉验证与暴力枚举一致。
    cross_check(4, stage, rows, cols)


def test_all_missing_permutation_margins_everything_pending() -> None:
    n = 3
    # 每行每列恰好 1 颗坏片：多种置换填法，没有一格判定一致。
    determined = plan_recovery(n, {}, [1, 1, 1], [1, 1, 1])
    assert determined == {}
    cross_check(n, {}, [1, 1, 1], [1, 1, 1])


def test_zero_remaining_marks_all_missing_as_pass() -> None:
    n = 3
    stage = {(0, 0): False, (2, 2): False}
    determined = plan_recovery(n, stage, [1, 0, 1], [1, 0, 1])
    assert determined == {
        (x, y): True
        for y in range(n)
        for x in range(n)
        if (x, y) not in stage
    }
    cross_check(n, stage, [1, 0, 1], [1, 0, 1])


def test_known_fail_exceeding_total_is_infeasible() -> None:
    with pytest.raises(RecoveryError, match="超过小票合计"):
        plan_recovery(3, {(0, 0): False}, [0, 1, 1], [1, 1, 0])
    with pytest.raises(RecoveryError, match="超过小票合计"):
        plan_recovery(3, {(0, 0): False}, [1, 1, 0], [0, 1, 1])


def test_unsatisfiable_margins_rejected() -> None:
    # 第 0 行还缺 1 颗坏片，但该行缺失格所在列都已满额。
    stage = {(1, 0): True, (2, 0): True}
    with pytest.raises(RecoveryError, match="没有相容填法"):
        plan_recovery(3, stage, [1, 1, 0], [0, 1, 1])
    cross_check(3, stage, [1, 1, 0], [0, 1, 1])


def test_totals_must_agree() -> None:
    with pytest.raises(RecoveryError, match="两组合计不一致"):
        plan_recovery(3, {}, [1, 0, 0], [1, 1, 0])
    # 扣除已有坏片后若两组合计被拉成不一致，同样无解。
    with pytest.raises(RecoveryError):
        plan_recovery(
            3, {(0, 0): False, (1, 1): False}, [1, 1, 0], [2, 0, 0]
        )


def test_negative_and_length_validation() -> None:
    with pytest.raises(RecoveryError, match="不能为负数"):
        plan_recovery(2, {}, [0, -1], [0, 0])
    with pytest.raises(RecoveryError, match="都必须给出"):
        plan_recovery(3, {}, [0, 0], [0, 0, 0])


def test_grid_size_capped_at_32() -> None:
    with pytest.raises(RecoveryError, match="至多 32"):
        plan_recovery(33, {}, [0] * 33, [0] * 33)
    assert MAX_GRID_SIZE == 32


def test_no_missing_cell_rejected() -> None:
    full = {(x, y): True for y in range(2) for x in range(2)}
    with pytest.raises(RecoveryError, match="没有可恢复的缺失格"):
        plan_recovery(2, full, [0, 0], [0, 0])


def test_32x32_performance_forced_row() -> None:
    n = 32
    # 第 0 行整行坏片，其余全好：1024 条格边，要求快速求解。
    rows = [n] + [0] * (n - 1)
    cols = [1] * n
    start = time.perf_counter()
    determined = plan_recovery(n, {}, rows, cols)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    assert len(determined) == n * n
    assert all(not passed for (x, y), passed in determined.items() if y == 0)
    assert all(passed for (x, y), passed in determined.items() if y != 0)


def test_32x32_permutation_runs_without_enumeration() -> None:
    n = 32
    start = time.perf_counter()
    determined = plan_recovery(n, {}, [1] * n, [1] * n)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    # 完全二分图上所有格都可在置换间切换，全部待复测。
    assert determined == {}


@pytest.mark.parametrize("seed", range(24))
def test_random_small_cases_match_brute_force(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(2, 4)
    all_cells = [(x, y) for y in range(n) for x in range(n)]
    # 随机保留一部分作为已知判定。
    known_count = rng.randint(0, min(4, n * n - 1))
    known_cells = rng.sample(all_cells, known_count)
    stage = {cell: rng.choice([True, False]) for cell in known_cells}

    # 以一组随机可行的“真实坏片布局”生成小票合计，保证至少有解；
    # 再随机扰动制造一部分无解场景。
    truth_fail = {
        cell
        for cell in all_cells
        if cell not in stage and rng.random() < 0.35
    }
    rows = [0] * n
    cols = [0] * n
    for (x, y), passed in stage.items():
        if not passed:
            rows[y] += 1
            cols[x] += 1
    for x, y in truth_fail:
        rows[y] += 1
        cols[x] += 1
    if rng.random() < 0.3:
        rows[rng.randrange(n)] += 1  # 扰动：大概率无解
    cross_check(n, stage, rows, cols)
