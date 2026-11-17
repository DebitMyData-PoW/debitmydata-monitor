"""With a real DebitMyData node on regtest (a private test chain): set DMDW_TEST_NODE_BIN to the
folder with debitmydatad and debitmydata-cli to run these."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from chainwatch.checks import Context, check_forks
from chainwatch.config import Settings
from chainwatch.rpc import Rpc, RpcError
from chainwatch.sync import sync

from .conftest import PERIODS

BIN = os.environ.get("DMDW_TEST_NODE_BIN", "")
pytestmark = pytest.mark.skipif(not BIN, reason="no node binaries (DMDW_TEST_NODE_BIN)")
KEYS = [
    "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
    "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5",
]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Regtest:
    def __init__(self, folder: Path) -> None:
        self.folder, self.port = folder, _port()
        self.base = ["-regtest", f"-datadir={folder}", f"-rpcport={self.port}"]

    def start(self) -> None:
        subprocess.run(
            [f"{BIN}/debitmydatad", *self.base, "-daemon", "-listen=0", "-rpccookieperms=group"],
            check=True,
            capture_output=True,
        )
        for _ in range(100):
            if (self.folder / "regtest" / ".cookie").exists():
                try:
                    self.cli("getblockcount")
                    return
                except subprocess.CalledProcessError:
                    pass
            time.sleep(0.1)
        raise RuntimeError("the regtest node did not start")

    def cli(self, *args: str) -> Any:
        out = subprocess.run(
            [f"{BIN}/debitmydata-cli", *self.base, *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        try:
            return json.loads(out)
        except ValueError:
            return out

    def stop(self) -> None:
        subprocess.run([f"{BIN}/debitmydata-cli", *self.base, "stop"], capture_output=True)

    def address(self, which: int = 0) -> str:
        descriptor = self.cli("getdescriptorinfo", f"wpkh({KEYS[which]})")["descriptor"]
        return str(self.cli("deriveaddresses", descriptor)[0])


@pytest.fixture
def regtest(tmp_path: Path) -> Iterator[Regtest]:
    node = Regtest(tmp_path)
    node.start()
    yield node
    node.stop()


def test_sync_and_a_reorganization_on_a_real_node(
    conn: psycopg.Connection[Any], regtest: Regtest, settings: Settings
) -> None:
    address = regtest.address()
    regtest.cli("generatetoaddress", "30", address)
    rpc = Rpc(f"http://127.0.0.1:{regtest.port}", str(regtest.folder / "regtest" / ".cookie"))
    with pytest.raises(RpcError, match="not a call"):
        rpc.call("stop")
    now = datetime.now(UTC)
    assert sync(conn, rpc, PERIODS, "Official", now).added == 31
    block = conn.execute("SELECT * FROM blocks WHERE height = 30").fetchone()
    assert block["payouts"] == {address: 3_500_000_000}
    assert block["pool"] is False

    # Blocks 28 to 30 replaced by 4 others, then the old ones known again: a valid fork 3 long
    old = regtest.cli("getblockhash", "28")
    regtest.cli("invalidateblock", old)
    regtest.cli("generatetoaddress", "4", regtest.address(1))  # another miner
    regtest.cli("reconsiderblock", old)
    result = sync(conn, rpc, PERIODS, "Official", now)
    assert (result.removed, result.added, result.fork_height) == (3, 4, 27)
    found = {f.key: f for f in check_forks(Context(conn, rpc, settings, now))}
    assert list(found) == ["forks.reorg.27"]
    assert found["forks.reorg.27"].title == (
        "The DebitMyData chain was reorganized: 3 blocks after block 27 were replaced"
    )
