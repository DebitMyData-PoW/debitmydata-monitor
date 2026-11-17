"""Test helpers: a test database, and a pretend DebitMyData node with a chain built in memory."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from chainwatch import db
from chainwatch.config import CostPeriod, Settings
from chainwatch.rpc import RpcError

DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://dmdwatch:dmdwatch-test-only@localhost:5432/dmdwatch_test"
)
START = 1_790_000_000  # 2026-09-21, a time in the chain's past
POOL_ADDRESS = "dm1qpoolwallet"
PERIODS = [CostPeriod(datetime(2026, 10, 24).date(), Decimal("0.06"), Decimal(20))]


def coinbase_hex(height: int, tag: str = "Official") -> str:
    return height.to_bytes(4, "little").hex() + "0a1b" + tag.encode().hex()


class FakeNode:
    """A chain of blocks, every 150 seconds, at a constant difficulty unless told otherwise."""

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []
        self.tips_extra: list[dict[str, Any]] = []
        self.peers = 8
        self.chain = "main"
        self.ibd = False
        self.down = False

    def _hash(self, height: int, salt: str) -> str:
        return hashlib.sha256(f"{height}:{salt}".encode()).hexdigest()

    def add(
        self,
        count: int,
        *,
        difficulty: float = 1000.0,
        seconds: int = 150,
        tag: str = "Official",
        payouts: dict[str, Decimal] | None = None,
        salt: str = "main",
        start_time: int | None = None,
    ) -> None:
        for _ in range(count):
            height = len(self.blocks)
            previous = self.blocks[-1] if self.blocks else None
            work = (int(previous["chainwork"], 16) if previous else 0) + int(difficulty * 2**32)
            when = (
                (previous["time"] + seconds if previous else START)
                if start_time is None
                else start_time
            )
            start_time = None
            paid = payouts or {POOL_ADDRESS: Decimal(35)}
            vout = [
                {"value": v, "scriptPubKey": {"address": a, "type": "witness_v0_keyhash"}}
                for a, v in paid.items()
            ]
            vout.append({"value": Decimal(0), "scriptPubKey": {"type": "nulldata"}})
            self.blocks.append(
                {
                    "height": height,
                    "hash": self._hash(height, salt),
                    "previousblockhash": previous["hash"] if previous else None,
                    "time": when,
                    "mediantime": when,
                    "difficulty": Decimal(str(difficulty)),
                    "chainwork": format(work, "064x"),
                    "nTx": 1,
                    "size": 250,
                    "tx": [{"vin": [{"coinbase": coinbase_hex(height, tag)}], "vout": vout}],
                }
            )

    def replace_from(self, height: int, count: int, **kwargs: Any) -> list[str]:
        """A reorganization: blocks from ``height`` replaced by ``count`` others."""
        left = [b["hash"] for b in self.blocks[height:]]
        del self.blocks[height:]
        self.add(count, salt=kwargs.pop("salt", "other"), **kwargs)
        return left

    def _index(self) -> dict[str, dict[str, Any]]:
        """The blocks by hash, made again only when the chain changed."""
        key = (len(self.blocks), self.blocks[-1]["hash"])
        if getattr(self, "_key", None) != key:
            self._by_hash = {b["hash"]: b for b in self.blocks}
            self._key = key
        return self._by_hash

    # --- RPC
    def call(self, method: str, *params: Any) -> Any:
        if self.down:
            from chainwatch.rpc import RpcUnavailable

            raise RpcUnavailable("connection refused")
        tip = self.blocks[-1]
        by_hash = self._index()
        if method == "getblockcount":
            return tip["height"]
        if method == "getbestblockhash":
            return tip["hash"]
        if method == "getblockhash":
            height = int(params[0])
            if height > tip["height"]:
                raise RpcError("Block height out of range")
            return self.blocks[height]["hash"]
        if method == "getblock":
            return by_hash[params[0]]
        if method == "getblockheader":
            return {k: v for k, v in by_hash[params[0]].items() if k != "tx"}
        if method == "getblockchaininfo":
            return {
                "chain": self.chain,
                "blocks": tip["height"],
                "headers": tip["height"],
                "bestblockhash": tip["hash"],
                "difficulty": tip["difficulty"],
                "initialblockdownload": self.ibd,
            }
        if method == "getconnectioncount":
            return self.peers
        if method == "getchaintips":
            return [
                {"height": tip["height"], "hash": tip["hash"], "branchlen": 0, "status": "active"},
                *self.tips_extra,
            ]
        raise AssertionError(method)

    def batch(self, calls: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        return [self.call(method, *params) for method, params in calls]


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    try:
        connection = db.connect(DATABASE_URL)
    except psycopg.OperationalError:
        pytest.skip("no test database (TEST_DATABASE_URL)")
    connection.execute("DROP TABLE IF EXISTS blocks, reorgs, prices, alerts, state CASCADE")
    db.create_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings.from_env()
    return replace(
        base,
        database_url=DATABASE_URL,
        public_dir=tmp_path / "public",
        metrics_file=tmp_path / "metrics" / "chainwatch.prom",
        smtp_host="",
        email_to=[],
        discord_webhook="",
        public_discord_webhook="",
        pool_addresses=[],
    )


def utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)
