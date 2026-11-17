"""The chain watch's database (PostgreSQL on the monitoring server).

  blocks        every block of the node's chain, from block 0: its time, difficulty, total work,
                the coinbase's text (the pool's tag), payouts and miner, the DMD it moved between
                addresses (volume) and its fees, and the mining cost of DMD at that block
  transfers     the large transactions (DMDW_LARGE_TRANSFER_DMD and more)
  address_moves what each block's transactions added to or took from each address
  balances      every address's DMD: the coinbases' payouts and the transactions' moves of the
                blocks counted so far (the rich list, once they all are)
  reorgs        every time blocks left the chain while the chain watch was watching
  prices        the DebitMyData Gateway's reference price, as the gateway recorded it
  market        DMD's trading volume and market price from the exchanges (CoinGecko), once it
                trades on one
  alerts        the alerts sent and still open
  events        what happened on the chain, for the notifications (kept a week)
  push_devices  the devices that turned notifications on, with what each one wants
  state         small values kept between runs

Grafana reads blocks, transfers, reorgs, prices and market with its own read-only role (grafana_ro).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from chainwatch import chain

SCHEMA_VERSION = 2

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

-- Version 2 (monitor 1.1): volume, fees and the miner of each block, the large transfers, the
-- notifications. volume_data stays empty for the blocks stored before, until the chain watch has
-- read them again (a few thousand per minute, newest first).
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS miner text;
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS volume_data bigint;
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS fees_data bigint;
CREATE INDEX IF NOT EXISTS blocks_volume_missing ON blocks (height) WHERE volume_data IS NULL;
CREATE INDEX IF NOT EXISTS blocks_miner ON blocks (miner, height);

CREATE TABLE IF NOT EXISTS market (
    observed_at timestamptz PRIMARY KEY,
    source text NOT NULL,
    price_usd numeric,
    volume_24h_usd numeric
);

CREATE TABLE IF NOT EXISTS transfers (
    txid text PRIMARY KEY,
    height integer NOT NULL REFERENCES blocks (height) ON DELETE CASCADE,
    time timestamptz NOT NULL,
    value_data bigint NOT NULL,
    fee_data bigint
);
CREATE INDEX IF NOT EXISTS transfers_height ON transfers (height);

CREATE TABLE IF NOT EXISTS address_moves (
    height integer NOT NULL REFERENCES blocks (height) ON DELETE CASCADE,
    address text NOT NULL,
    delta_data bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS address_moves_height ON address_moves (height);

CREATE TABLE IF NOT EXISTS balances (
    address text PRIMARY KEY,
    balance_data bigint NOT NULL,
    last_height integer NOT NULL
);
CREATE INDEX IF NOT EXISTS balances_top ON balances (balance_data DESC);

CREATE TABLE IF NOT EXISTS events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    time timestamptz NOT NULL,
    kind text NOT NULL,
    data jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time ON events (time);

CREATE TABLE IF NOT EXISTS push_devices (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    endpoint text NOT NULL,
    endpoint_hash text NOT NULL UNIQUE,
    p256dh text NOT NULL,
    auth text NOT NULL,
    prefs jsonb NOT NULL,
    memory jsonb NOT NULL DEFAULT '{}',
    sent jsonb NOT NULL DEFAULT '[]',
    event_cursor bigint NOT NULL DEFAULT 0,
    failures integer NOT NULL DEFAULT 0,
    test_at timestamptz,
    created_at timestamptz NOT NULL,
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
    fill_miners(conn)


def fill_miners(conn: psycopg.Connection[Any], chunk: int = 20_000) -> int:
    """The miner of the blocks stored before version 2 (once, a few seconds)."""
    done = 0
    while True:
        rows = conn.execute(
            "SELECT height, pool, coinbase_tag, payouts FROM blocks WHERE miner IS NULL "
            "ORDER BY height LIMIT %s",
            (chunk,),
        ).fetchall()
        if not rows:
            return done
        with conn.transaction():
            conn.cursor().executemany(
                "UPDATE blocks SET miner = %s WHERE height = %s",
                [
                    (
                        chain.miner_name(r["pool"], r["coinbase_tag"], r["payouts"] or {}),
                        r["height"],
                    )
                    for r in rows
                ],
            )
        done += len(rows)


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
