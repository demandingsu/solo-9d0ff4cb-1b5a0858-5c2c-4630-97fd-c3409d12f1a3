"""坏片恢复：按台面行/列坏片合计，求所有相容填法中判定一致的缺失格。

不枚举整片填法。把问题建成二分图 b-匹配（最大流）：

- 行节点 -> 列节点的容量 1 边对应一个**缺失格**（坏片候选）；
- 源点 -> 行节点容量为该行还缺的坏片数，列节点 -> 汇点同理；
- 已有判定的格子不进网络：坏片先从合计里扣除，合格格直接占位。

网络有满流当且仅当存在相容填法。满流后的残量图上用 Tarjan 求强连通
分量（SCC），对每条缺失格边 ``r -> c``：

- 边被满流占用（填坏）且 ``r``、``c`` 不在同一 SCC：任何满流都占用它，
  即所有相容填法里该格都是坏片（判定一致 -> 补回，passed=False）；
- 边未被占用（填好）且两端不在同一 SCC：任何满流都不占用它，
  即所有相容填法里都是好片（判定一致 -> 补回，passed=True）；
- 两端在同一 SCC：沿残量环可切换占用状态，存在判定相反的相容填法
  （判定不一致 -> 保留为待复测）。

网格每边至多 32 格，节点数不超过 66、边数不超过 1024 量级，
Dinic 与 Tarjan 均为多项式复杂度。
"""

from __future__ import annotations

from collections import deque

MAX_GRID_SIZE = 32


class RecoveryError(ValueError):
    """恢复前置条件不满足；路由层统一转成 HTTP 422。"""


class _Edge:
    __slots__ = ("to", "rev", "cap", "init_cap")

    def __init__(self, to: int, rev: int, cap: int) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap
        # 记录初始容量，便于满流后反推这条格边是否被占用（cell 边容量为 1）。
        self.init_cap = cap


class _Dinic:
    def __init__(self, n: int) -> None:
        self.n = n
        self.g: list[list[_Edge]] = [[] for _ in range(n)]
        self.level = [-1] * n
        self.it = [0] * n

    def add_edge(self, fr: int, to: int, cap: int) -> _Edge:
        forward = _Edge(to, len(self.g[to]), cap)
        backward = _Edge(fr, len(self.g[fr]), 0)
        self.g[fr].append(forward)
        self.g[to].append(backward)
        return forward

    def _bfs(self, s: int, t: int) -> bool:
        self.level = [-1] * self.n
        self.level[s] = 0
        q: deque[int] = deque([s])
        g = self.g
        while q:
            v = q.popleft()
            for e in g[v]:
                if e.cap > 0 and self.level[e.to] < 0:
                    self.level[e.to] = self.level[v] + 1
                    q.append(e.to)
        return self.level[t] >= 0

    def _dfs(self, v: int, t: int, pushed: int) -> int:
        if v == t:
            return pushed
        g = self.g
        for i in range(self.it[v], len(g[v])):
            self.it[v] = i
            e = g[v][i]
            if e.cap > 0 and self.level[e.to] == self.level[v] + 1:
                d = self._dfs(e.to, t, min(pushed, e.cap))
                if d > 0:
                    e.cap -= d
                    g[e.to][e.rev].cap += d
                    return d
        return 0

    def max_flow(self, s: int, t: int) -> int:
        flow = 0
        inf = 10**18
        while self._bfs(s, t):
            self.it = [0] * self.n
            while True:
                f = self._dfs(s, t, inf)
                if f == 0:
                    break
                flow += f
        return flow

    def strongly_connected(self) -> list[int]:
        """Tarjan 求残量图（只看仍有残量的边）的强连通分量。"""
        index = 0
        stack: list[int] = []
        on_stack = [False] * self.n
        indices = [-1] * self.n
        low = [0] * self.n
        comp = [-1] * self.n
        comp_count = 0
        g = self.g

        def walk(start: int) -> None:
            nonlocal index, comp_count
            # 显式栈模拟递归，避免依赖解释器递归上限。
            call_stack: list[tuple[int, int]] = [(start, 0)]
            indices[start] = low[start] = index
            index += 1
            stack.append(start)
            on_stack[start] = True
            while call_stack:
                v, next_i = call_stack[-1]
                if next_i < len(g[v]):
                    e = g[v][next_i]
                    call_stack[-1] = (v, next_i + 1)
                    if e.cap <= 0:
                        continue
                    w = e.to
                    if indices[w] == -1:
                        indices[w] = low[w] = index
                        index += 1
                        stack.append(w)
                        on_stack[w] = True
                        call_stack.append((w, 0))
                    elif on_stack[w]:
                        low[v] = min(low[v], indices[w])
                else:
                    if low[v] == indices[v]:
                        while True:
                            w = stack.pop()
                            on_stack[w] = False
                            comp[w] = comp_count
                            if w == v:
                                break
                        comp_count += 1
                    call_stack.pop()
                    if call_stack:
                        parent = call_stack[-1][0]
                        low[parent] = min(low[parent], low[v])

        for v in range(self.n):
            if indices[v] == -1:
                walk(v)
        return comp


def plan_recovery(
    grid_size: int,
    stage_cells: dict[tuple[int, int], bool],
    row_failed: list[int],
    col_failed: list[int],
) -> dict[tuple[int, int], bool]:
    """计算所有相容填法中判定一致的缺失格。

    参数：
        grid_size: 台面每边格数（<= ``MAX_GRID_SIZE``）。
        stage_cells: 本批当前在台面格子上的判定，``True`` 合格、``False`` 坏片；
            缺失格即不在此映射中的台面格。
        row_failed / col_failed: 小票上每行、每列的坏片合计（长度均为 grid_size）。

    返回：
        可确定补回的缺失格 -> 判定（``True`` 合格 / ``False`` 坏片）。
        其余缺失格为待复测，由调用方用全部缺失格减去返回值得到。

    无解（含合计与已有记录矛盾）时抛 :class:`RecoveryError`。
    """
    n = grid_size
    if n > MAX_GRID_SIZE:
        raise RecoveryError(
            f"恢复仅支持每边至多 {MAX_GRID_SIZE} 格，当前为 {n} 格"
        )
    if len(row_failed) != n or len(col_failed) != n:
        raise RecoveryError(
            f"两组合计都必须给出 {n} 个值（台面为 {n}×{n}）"
        )
    if any(v < 0 for v in row_failed) or any(v < 0 for v in col_failed):
        raise RecoveryError("行/列坏片合计不能为负数")
    if sum(row_failed) != sum(col_failed):
        raise RecoveryError(
            f"两组合计不一致：行合计 {sum(row_failed)} ≠ 列合计 {sum(col_failed)}"
        )

    known: set[tuple[int, int]] = set(stage_cells)
    missing = [
        (x, y)
        for y in range(n)
        for x in range(n)
        if (x, y) not in known
    ]
    if not missing:
        raise RecoveryError("本批台面格已全部有记录，没有可恢复的缺失格")

    row_need = list(row_failed)
    col_need = list(col_failed)
    for (x, y), passed in stage_cells.items():
        if not passed:  # 已有坏片参与合计，且原判定保留。
            row_need[y] -= 1
            col_need[x] -= 1

    if any(v < 0 for v in row_need):
        y = next(i for i, v in enumerate(row_need) if v < 0)
        raise RecoveryError(
            f"第 {y} 行已有坏片数超过小票合计 {row_failed[y]}，无解"
        )
    if any(v < 0 for v in col_need):
        x = next(i for i, v in enumerate(col_need) if v < 0)
        raise RecoveryError(
            f"第 {x} 列已有坏片数超过小票合计 {col_failed[x]}，无解"
        )

    total_need = sum(row_need)
    if total_need != sum(col_need):  # 扣除已有坏片后仍应相等。
        raise RecoveryError("扣除已有坏片后两组合计仍不一致，无解")
    if total_need > len(missing):
        raise RecoveryError(
            f"还缺 {total_need} 颗坏片，但只剩 {len(missing)} 个缺失格，无解"
        )

    # 节点：0..n-1 行，n..2n-1 列，2n 源点，2n+1 汇点。
    source = 2 * n
    sink = source + 1
    dinic = _Dinic(2 * n + 2)
    for y in range(n):
        if row_need[y] > 0:
            dinic.add_edge(source, y, row_need[y])
    for x in range(n):
        if col_need[x] > 0:
            dinic.add_edge(n + x, sink, col_need[x])

    cell_edges: list[tuple[_Edge, int, int]] = []
    for x, y in missing:
        edge = dinic.add_edge(y, n + x, 1)
        cell_edges.append((edge, x, y))

    if dinic.max_flow(source, sink) != total_need:
        raise RecoveryError(
            "行/列坏片合计在当前缺失格上没有相容填法，无解，本次恢复已退回"
        )

    comp = dinic.strongly_connected()
    determined: dict[tuple[int, int], bool] = {}
    for edge, x, y in cell_edges:
        used = edge.init_cap - edge.cap  # 1 表示该格在某个相容填法里为坏片。
        if comp[y] == comp[n + x]:
            continue  # 残量环可翻转：存在判定相反的填法 -> 待复测。
        if used:
            determined[(x, y)] = False  # 所有相容填法都是坏片。
        else:
            determined[(x, y)] = True  # 所有相容填法都是好片。
    return determined
