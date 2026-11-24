"""请求 / 响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CellIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0, description="坐标 x（列），从 0 起")
    y: int = Field(ge=0, description="坐标 y（行），从 0 起")
    passed: bool = Field(description="是否合格")


class FirstTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grid_size: int = Field(ge=1, le=1000, description="正方网格每边的格数")
    chips: list[CellIn] = Field(min_length=1, description="首测芯片坐标与合格标记")


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    station_id: str | None = Field(
        default=None,
        max_length=64,
        description="复测台号，与路径一致时也可省略",
    )
    batch_seq: int = Field(ge=1, description="全片唯一的批次序号，越大越新")
    cells: list[CellIn] = Field(
        min_length=1,
        description="本批各格坐标与标记；坐标为台面坐标，批内每格只报一次",
    )

    @field_validator("station_id")
    @classmethod
    def _station_id_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or "/" in value or any(ch.isspace() for ch in value):
            raise ValueError("台号不能为空、不能含空白或 '/'")
        return value


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cells: list[CellIn] = Field(min_length=1, description="更正后的整批各格坐标与标记")


class AnchorPairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_x: int = Field(ge=0, description="台面坐标 x")
    stage_y: int = Field(ge=0, description="台面坐标 y")
    first_x: int = Field(ge=0, description="首测坐标 x")
    first_y: int = Field(ge=0, description="首测坐标 y")


class AnchorsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchors: list[AnchorPairIn] = Field(
        default_factory=list,
        description="整组定位点对；传空数组等同清空（也可用 DELETE）",
    )


class ChipOut(BaseModel):
    x: int
    y: int
    passed: bool
    source: Literal["first", "retest"]
    station_id: str | None = None
    batch_seq: int | None = None


class FirstTestOut(BaseModel):
    grid_size: int
    chip_count: int
    created_at: str
    updated_at: str
    chips: list[ChipOut]


class AnchorOut(BaseModel):
    stage_x: int
    stage_y: int
    first_x: int
    first_y: int


class BatchSummaryOut(BaseModel):
    batch_seq: int
    cell_count: int
    created_at: str
    updated_at: str


class BatchOut(BatchSummaryOut):
    station_id: str
    cells: list[CellIn]


class StationOut(BaseModel):
    station_id: str
    anchors: list[AnchorOut]
    batch_count: int
    batches: list[BatchSummaryOut]
    candidates: list[int]
    orientation: int | None
    merge_status: Literal["merged", "ambiguous"]
    merged_cell_count: int


class MergeResultOut(BaseModel):
    station_id: str
    candidates: list[int]
    orientation: int | None
    merge_status: Literal["merged", "ambiguous"]
    message: str


class BatchResultOut(MergeResultOut):
    batch_seq: int
    cell_count: int


class QueryOut(BaseModel):
    grid_size: int
    chip_count: int
    total_passed: int
    total_failed: int
    from_first: int
    from_retest: int
    chips: list[ChipOut]


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: str
