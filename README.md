# 晶圆复测判定服务

同一片晶圆换台复测后，按首测坐标逐颗查询最新有效判定。FastAPI 提供接口，
SQLite 持久化，Docker Compose 启动，`/docs` 可直接录入与查询。

## 启动

```bash
docker compose up --build -d
```

- 接口与 Swagger UI：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 数据保存在命名卷 `wafer-data`（容器内 `/data/wafer.db`），
  可用环境变量 `DB_PATH` 覆盖。

镜像内已打包 Swagger UI 静态资源，`/docs` 不依赖外网 CDN。

## 数据模型（坐标约定）

- 首测坐标 `(x, y)`：`x` 为列、`y` 为行，均从 0 起；
  网格为 `grid_size × grid_size` 的正方形。
- 复测批次里的坐标是**台面坐标**；归并时绕网格中心旋转
  `0°/90°/180°/270°` 映射到首测坐标：

  | 方向 | 映射 |
  | --- | --- |
  | 0° | `(x, y)` |
  | 90° | `(n-1-y, x)` |
  | 180° | `(n-1-x, n-1-y)` |
  | 270° | `(y, n-1-x)` |

- 定位点对是「台面坐标 → 首测坐标」的对应关系，每台一组，可整组替换或清空；
  首测一侧必须是已录入的首测芯片，同一组内台面点与首测点都不可重复。
- 批次序号 `batch_seq` 全片唯一，越大越新；批次可乱序补录。

## 归并规则

对每台，候选方向必须同时满足：

1. 每个定位点对都在该方向下吻合；
2. 该台**历次复测的全部台面坐标**旋转后都落在首测芯片上。

- 恰有一个候选方向：该台复测归并（写入 `merged_cells`），
  若已有旧归并则按新方向重建。
- 零个候选方向：本次录入 / 更正 / 定位点替换被退回（HTTP 422），
  原始数据、定位点与查询结果保持不变。
- 多个候选方向：保留原始批次，撤下该台旧归并，查询该台回退首测。

提交复测、更正复测、替换/清空定位点都会在同一个事务中重新计算归并；
更正后的坐标若导致无可行方向，同样整体退回。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/first-test` | 录入首测网格格数、芯片坐标与合格标记（仅一次） |
| GET | `/first-test` | 读取首测数据 |
| GET | `/stations` | 列出各台定位点、原始批次、候选方向、归并状态 |
| GET | `/stations/{station_id}` | 读取单台状态 |
| PUT | `/stations/{station_id}/anchors` | 整组替换定位点对（`anchors: []` 等同清空） |
| DELETE | `/stations/{station_id}/anchors` | 清空定位点 |
| POST | `/stations/{station_id}/batches` | 提交复测批次 |
| POST | `/batches` | 等价入口，台号放在请求体 |
| GET | `/stations/{station_id}/batches` | 重读本台全部原始批次 |
| GET | `/stations/{station_id}/batches/{batch_seq}` | 重读本台单个原始批次 |
| PUT | `/stations/{station_id}/batches/{batch_seq}` | 更正本台批次，与归并一起生效 |
| GET | `/query` | 逐颗查询最新有效判定 |

`/query` 每颗芯片返回 `x`、`y`、`passed`、`source`（`first` / `retest`），
复测来源另有 `station_id`、`batch_seq`。取值规则：在所有已归并数据中取
该坐标**批次序号最大**的标记；没有复测则用首测标记。

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest            # 45 项，含旋转几何与端到端接口测试
DB_PATH=/tmp/wafer.db uvicorn app.main:app --reload
```

## 目录

```
app/
  main.py        FastAPI 路由、/docs、健康检查
  service.py     业务规则：首测、批次、定位点、归并、查询
  geometry.py    旋转与可行方向判定
  db.py          SQLite 连接、建表、事务/只读快照
  schemas.py     请求/响应模型
  entrypoint.py  容器入口：修正数据卷属主后降权运行
  healthcheck.py 容器健康检查
  static/        离线 Swagger UI 资源
tests/           几何单测 + 端到端接口测试
```
