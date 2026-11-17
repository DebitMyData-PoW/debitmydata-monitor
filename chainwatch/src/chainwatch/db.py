"""The chain watch's database (PostgreSQL on the monitoring server).

  blocks   every block of the node's chain, from block 0: its time, difficulty, total work, the
           coinbase's text (the pool's tag) and payouts, and the mining cost of DMD at that block
  reorgs   every time blocks left the chain while the chain watch was watching
  prices   the DebitMyData Gateway's reference price, as the gateway recorded it
  alerts   the alerts sent and still open
  state    small values kept between runs

Grafana reads blocks, reorgs and prices with its own read-only role (grafana_ro).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    height integer PRIMARY KEY,
    hash text NOT NULL UNIQUE,
    time timestamptz NOT NULL,
    median_time timestamptz NOT NULL,
    difficulty double precision NOT NULL,
    chainwork numeric(80, 0) NOT NULL,
    tx_count integer NOT NULL,
    size integer NOT NULL,
    coinbase_tag text NOT NULL,
    pool boolean NOT NULL,
    reward_data bigint NOT NULL,
    subsidy_data bigint NOT NULL,
    payouts jsonb NOT NULL,
    hashes_per_block numeric NOT NULL,
    mining_cost_usd numeric NOT NULL,
    cost_figures_from date
);
CREATE INDEX IF NOT EXISTS blocks_time ON blocks (time);

CREATE TABLE IF NOT EXISTS reorgs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    detected_at timestamptz NOT NULL,
    fork_height integer NOT NULL,
    depth integer NOT NULL,
    left_hashes jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS prices (
    observed_at timestamptz PRIMARY KEY,
    usd_per_dmd numeric NOT NULL,
    mining_cost_usd numeric,
    minimum_usd numeric,
    price_from text,
    source text NOT NULL,
    inputs jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    key text PRIMARY KEY,
    severity text NOT NULL,
    kind text NOT NULL,
    audience text NOT NULL,
    title text NOT NULL,
    detail text NOT NULL,
    first_seen timestamptz NOT NULL,
    last_seen timestamptz NOT NULL,
    last_sent timestamptz,
    resolved_at timestamptz
);

CREATE TABLE IF NOT EXISTS state (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL
);
"""


def connect(url: str) -> psycopg.Connection[dict[str, Any]]:
    """Autocommit: a statement alone is kept at once; several that belong together run inside
    ``with conn.transaction():`` (kept together, or not at all)."""
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def create_schema(conn: psycopg.Connection[Any]) -> None:
    with conn.transaction():
        conn.execute(SCHEMA)
        conn.execute(
            "INSERT INTO state (key, value, updated_at) VALUES ('schema', %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (Jsonb({"version": SCHEMA_VERSION}),),
        )


def get_state(conn: psycopg.Connection[Any], key: str) -> Any:
    row = conn.execute("SELECT value FROM state WHERE key = %s", (key,)).fetchone()
    return None if row is None else row["value"]


def put_state(conn: psycopg.Connection[Any], key: str, value: Any, now: datetime) -> None:
    conn.execute(
        "INSERT INTO state (key, value, updated_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
        (key, Jsonb(value), now),
    )


def tip(conn: psycopg.Connection[Any]) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT height, hash, time, chainwork FROM blocks ORDER BY height DESC LIMIT 1"
    ).fetchone()
