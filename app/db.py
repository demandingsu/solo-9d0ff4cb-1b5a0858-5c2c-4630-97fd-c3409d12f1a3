"""SQLite 持久化层。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS wafer (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    grid_size   INTEGER NOT NULL CHECK (grid_size >= 1),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS first_chips (
    x       INTEGER NOT NULL,
    y       INTEGER NOT NULL,
    passed  INTEGER NOT NULL CHECK (passed IN (0, 1)),
    PRIMARY KEY (x, y)
);

CREATE TABLE IF NOT EXISTS stations (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_anchors (
    station_id  TEXT NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    stage_x     INTEGER NOT NULL,
    stage_y     INTEGER NOT NULL,
    first_x     INTEGER NOT NULL,
    first_y     INTEGER NOT NULL,
    PRIMARY KEY (station_id, stage_x, stage_y),
    UNIQUE (station_id, first_x, first_y)
);

CREATE TABLE IF NOT EXISTS batches (
    station_id  TEXT NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    batch_seq   INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (station_id, batch_seq),
    UNIQUE (batch_seq)
);

CREATE TABLE IF NOT EXISTS batch_cells (
    station_id  TEXT NOT NULL,
    batch_seq   INTEGER NOT NULL,
    x           INTEGER NOT NULL,
    y           INTEGER NOT NULL,
    passed      INTEGER NOT NULL CHECK (passed IN (0, 1)),
    PRIMARY KEY (station_id, batch_seq, x, y),
    FOREIGN KEY (station_id, batch_seq)
        REFERENCES batches(station_id, batch_seq) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS station_merge (
    station_id   TEXT PRIMARY KEY REFERENCES stations(id) ON DELETE CASCADE,
    orientation  INTEGER NOT NULL CHECK (orientation IN (0, 90, 180, 270)),
    merged_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merged_cells (
    station_id  TEXT NOT NULL,
    batch_seq   INTEGER NOT NULL,
    x           INTEGER NOT NULL,
    y           INTEGER NOT NULL,
    passed      INTEGER NOT NULL CHECK (passed IN (0, 1)),
    PRIMARY KEY (station_id, batch_seq, x, y),
    FOREIGN KEY (station_id, batch_seq)
        REFERENCES batches(station_id, batch_seq) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_batch_cells_station
    ON batch_cells (station_id, batch_seq);
CREATE INDEX IF NOT EXISTS idx_merged_cells_pos
    ON merged_cells (x, y, batch_seq);
"""


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(
        db_path, timeout=15, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """写事务：BEGIN IMMEDIATE 让读-校验-写整体串行化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """只读快照：多条 SELECT 看到同一份已提交数据。"""
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()
