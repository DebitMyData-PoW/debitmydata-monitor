"""DebitMyData's pool list: a block's pool by its coinbase text or the address it pays."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from chainwatch import chain, cli, pools, publish
from chainwatch.config import Settings
from chainwatch.sync import sync

from .conftest import PERIODS, FakeNode

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)

LIST = [
    {
        "id": 1,
        "name": "Official",
        "addresses": ["DHa5vRtz46ENeVdLFjjDSwvWirEejuZrkZ"],
        "tags": ["Official"],
        "link": "https://pool.debitmydata.io/",
    },
    {
        "id": 2,
        "name": "Coin-Miners",
        "addresses": ["DGabFztP4EYnynR7Xe1g2RDDjzDDM6822a"],
        "tags": [],
        "link": "",
    },
    {
        "id": 7,
        "name": "Pep",
        "addresses": [],
        "tags": ["Mined by pep"],
        "link": "",
        "country": "us",
    },
]


def test_slugs_and_matching(tmp_path: Path) -> None:
    path = tmp_path / "pools-v2.json"
    path.write_text(json.dumps(LIST))
    listed = pools.Pools.load(path)
    assert [p.slug for p in listed.pools] == ["official", "coinminers", "pep"]
    assert listed.identify("/Mined by PEP/", {}).name == "Pep"
    assert listed.identify("", {"DGabFztP4EYnynR7Xe1g2RDDjzDDM6822a": 5}).name == "Coin-Miners"
    assert listed.identify("someone else", {"Dxyz": 1}) is None
    # the first by number: Coin-Miners' address beats Pep's tag
    assert listed.identify("Mined by pep", {"DGabFztP4EYnynR7Xe1g2RDDjzDDM6822a": 5}).name == (
        "Coin-Miners"
    )
    # a pool without a number from 1 to 255, or with another's, is left out
    path.write_text(json.dumps([*LIST, {"id": 2, "name": "Twice"}, {"name": "None"}]))
    assert [p.slug for p in pools.Pools.load(path).pools] == ["official", "coinminers", "pep"]
    assert pools.Pools.load(tmp_path / "missing.json").pools == ()


def test_miner_names_from_the_list(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "pools-v2.json"
    path.write_text(json.dumps(LIST))
    monkeypatch.setenv("DMDW_POOLS_FILE", str(path))
    assert chain.miner_name(True, "Official", {}) == chain.PUBLIC_POOL
    assert chain.miner_name(False, "x | Mined by pep", {}) == "Pep"
    assert chain.miner_name(False, "a | b", {}) == "b"
    monkeypatch.setenv("DMDW_POOLS_FILE", str(tmp_path / "missing.json"))
    assert chain.miner_name(False, "x | Mined by pep", {}) == "Mined by pep"


def test_blocks_named_again_when_the_list_changes(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "pools-v2.json"
    monkeypatch.setenv("DMDW_POOLS_FILE", str(path))
    node.add(3)  # the DebitMyData pool's
    node.add(2, tag="x | Mined by pep", payouts={"dm1qpep": Decimal(35)})
    sync(conn, node, PERIODS, "Official", NOW)
    names = lambda: [r["miner"] for r in conn.execute("SELECT miner FROM blocks ORDER BY height")]  # noqa: E731
    assert names()[-2:] == ["Mined by pep", "Mined by pep"]

    # the list now names Pep: its blocks are Pep's, and the stats give its logo's slug
    path.write_text(json.dumps(LIST))
    assert cli.cmd_relabel(replace(settings)) == 0
    assert names() == [chain.PUBLIC_POOL] * 3 + ["Pep"] * 2
    last = datetime.fromtimestamp(node.blocks[-1]["time"], UTC)
    week = publish.miners(conn, last)["7d"]["miners"]
    assert {m["name"]: m.get("slug") for m in week} == {chain.PUBLIC_POOL: "official", "Pep": "pep"}
