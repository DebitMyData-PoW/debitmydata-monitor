from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from chainwatch import api, chain, checks, events, notify, push
from chainwatch.alerts import Finding, Message
from chainwatch.checks import Context
from chainwatch.config import Settings
from chainwatch.sync import rich_list, sync

from .conftest import PERIODS, POOL_ADDRESS, FakeNode, spend, utc

DMD = chain.DATA_PER_DMD
ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-one"
KEYS = {"p256dh": "B" + "A" * 86, "auth": "c" * 22}


@pytest.fixture
def keys(tmp_path: Path) -> push.Keys:
    return push.make_keys(tmp_path / "vapid.pem")[0]


@pytest.fixture
def push_api(settings: Settings, keys: push.Keys, node: FakeNode) -> api.PushApi:
    return api.PushApi(settings, keys, node)  # type: ignore[arg-type]


def prefs(**changes: Any) -> dict[str, Any]:
    return {**notify.DEFAULT_PREFS, **changes}


def live(conn: psycopg.Connection[Any], node: FakeNode, count: int = 1, **kwargs: Any) -> int:
    """New blocks found now, synced, with their events."""
    first = len(node.blocks)
    node.add(count, **kwargs)
    now = utc(node.blocks[-1]["time"] + 5)
    ranks = {r["address"]: n for n, r in enumerate(rich_list(conn, 10), start=1)}
    result = sync(
        conn, node, PERIODS, "Official", now, large=10 * DMD, watched=frozenset(ranks) | WATCHED
    )
    events.record_sync(conn, result, now, ranks)
    return first


WATCHED = frozenset({"dm1qfriend"})


# --------------------------------------------------------------------------- the choices


def test_choices_are_checked() -> None:
    def valid(a: str) -> bool:
        return a.startswith("dm1q")

    clean = notify.clean_prefs({"hashrate": "12.5", "difficulty": None, "blocks": 1}, 1000, valid)
    assert (clean["hashrate"], clean["difficulty"], clean["blocks"]) == (12.5, None, True)
    assert clean["price"] == 5  # the default
    for bad in ({"hashrate": 0}, {"hashrate": "x"}, {"slow": 5}, {"transfers": 10}):
        with pytest.raises(ValueError):
            notify.clean_prefs(bad, 1000, valid)
    with pytest.raises(ValueError, match="not a DMD address"):
        notify.clean_prefs({"addresses": ["dm1zzzzzzzzzz"]}, 1000, valid)
    with pytest.raises(ValueError, match="10 at most"):
        notify.clean_prefs(
            {"addresses": [f"dm1q{'p' * 10}{i:02d}" for i in range(11)]}, 1000, valid
        )
    pools = notify.clean_prefs(
        {"pools": {"the DebitMyData pool": {"blocks": True}, "nobody": {}}}, 1000, valid
    )["pools"]
    assert pools == {"the DebitMyData pool": {"blocks": True, "hashrate": None, "silence": None}}


# --------------------------------------------------------------------------- what devices are told


def device(**changes: Any) -> dict[str, Any]:
    return {"prefs": prefs(**changes), "memory": {}, "event_cursor": 0}


def test_blocks_transfers_and_addresses(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings
) -> None:
    node.add(24 * 40)  # 40 hours of the pool's blocks
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    live(conn, node, 2)
    live(conn, node, 1, tag="Lucky Miner", payouts={"dm1qlucky": Decimal(35)})
    live(
        conn,
        node,
        1,
        txs=[
            spend("big", [(POOL_ADDRESS, "35")], [("dm1qwhale", "34")]),
            spend("gift", [("dm1qaunt", "5")], [("dm1qfriend", "2"), ("dm1qaunt", "2.9")]),
        ],
    )
    rows = conn.execute("SELECT id, kind, data FROM events ORDER BY id").fetchall()
    kinds = [r["kind"] for r in rows]
    assert kinds.count("block") == 4 and "new_miner" not in kinds  # a 40-hour-old chain
    figs = notify.figures(conn, utc(node.blocks[-1]["time"] + 10), ["Lucky Miner"])

    out, _, cursor = notify.collect(device(blocks=True, slow=None), rows, figs, settings)
    assert cursor == rows[-1]["id"]
    titles = [m["title"] for m in out]
    assert "4 new blocks" in titles
    body = next(m["body"] for m in out if m["title"] == "4 new blocks")
    assert body.startswith(f"Blocks {len(node.blocks) - 4:,}–{len(node.blocks) - 1:,}: ")
    assert "1 by Lucky Miner" in body and "3 by the DebitMyData pool" in body

    # a followed pool: its blocks only; a large transfer; a followed address
    one = device(
        pools={"Lucky Miner": {"blocks": True, "hashrate": None, "silence": None}},
        transfers=30,
        addresses=["dm1qfriend"],
    )
    out, _, _ = notify.collect(one, rows, figs, replace(settings, explorer_tx_url="https://x/tx/"))
    by_title = {m["title"]: m for m in out}
    assert "Lucky Miner found a block" in by_title
    assert by_title["Large transfer: 34 DMD"]["url"] == "https://x/tx/big"
    assert (
        by_title["Received 2 DMD"]["body"] == f"To dm1qfriend, in block {len(node.blocks) - 1:,}."
    )

    # nothing new since the cursor: nothing again
    again, _, _ = notify.collect({**one, "event_cursor": cursor}, rows, figs, settings)
    assert again == []


def test_figures_that_move(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings
) -> None:
    node.add(24 * 26, difficulty=1000)
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    now = utc(node.blocks[-1]["time"] + 10)
    one = device(hashrate=20, difficulty=20, price=None, slow=30)
    out, memory, _ = notify.collect(one, [], notify.figures(conn, now), settings)
    assert out == [] and set(memory["base"]) == {"hashrate", "difficulty"}  # the starting values

    node.add(24, difficulty=1500)  # the hour's average difficulty 50% higher
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    now = utc(node.blocks[-1]["time"] + 10)
    out, memory, _ = notify.collect(
        {**one, "memory": memory}, [], notify.figures(conn, now), settings
    )
    titles = [m["title"] for m in out]
    assert titles == ["Difficulty up 50%: 1.50K"]  # the 24-hour hashrate moved less than 20%

    # no block for 40 minutes, then blocks again
    later = now + timedelta(minutes=40)
    out, memory, _ = notify.collect(
        {**one, "memory": memory}, [], notify.figures(conn, later), settings
    )
    assert [m["title"] for m in out] == ["No new block for 40 minutes"]
    out, memory, _ = notify.collect(
        {**one, "memory": memory}, [], notify.figures(conn, later), settings
    )
    assert out == []  # once
    node.add(1, start_time=int(later.timestamp()))
    sync(conn, node, PERIODS, "Official", later)
    out, memory, _ = notify.collect(
        {**one, "memory": memory}, [], notify.figures(conn, later), settings
    )
    assert [m["title"] for m in out] == ["New blocks again"]


def test_a_pool_that_stops_and_a_miner_that_grows(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings
) -> None:
    node.add(200)
    node.add(150, tag="Big Miner", payouts={"dm1qbig": Decimal(35)})  # 6 h 15 min of its blocks
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    now = utc(node.blocks[-1]["time"] + 10)
    one = device(
        concentration=51,
        pools={chain.PUBLIC_POOL: {"blocks": False, "hashrate": None, "silence": 3}},
        slow=None,
    )
    figs = notify.figures(conn, now, [chain.PUBLIC_POOL])
    out, memory, _ = notify.collect(one, [], figs, settings)
    titles = [m["title"] for m in out]
    assert "Big Miner: 100% of the blocks" in titles
    assert "No block from the DebitMyData pool for 6 h 15 min" in titles
    node.add(144)  # the pool is back, the big miner gone
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    figs = notify.figures(conn, utc(node.blocks[-1]["time"] + 10), [chain.PUBLIC_POOL])
    out, memory, _ = notify.collect({**one, "memory": memory}, [], figs, settings)
    titles = [m["title"] for m in out]
    assert "Big Miner: back under 51%" in titles
    assert "The DebitMyData pool found a block again" in titles


def test_safety_events(conn: psycopg.Connection[Any], node: FakeNode, settings: Settings) -> None:
    node.add(50)
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    now = utc(node.blocks[-1]["time"] + 5)
    node.replace_from(47, 4, tag="Other", payouts={"dm1qother": Decimal(35)})
    result = sync(conn, node, PERIODS, "Official", now)
    events.record_sync(conn, result, now)
    branch = Finding("forks.competing.40", "warning", "A competing branch", "Detail", kind="event")
    stall = Finding("stall.blocks", "warning", "No new DebitMyData block for 31 minutes", "x")
    daily = Finding(
        "daily.2026-11-16", "info", "DebitMyData network on November 16", "a\nb", kind="event"
    )
    messages = [Message(branch, "event"), Message(stall, "open"), Message(daily, "event")]
    assert events.record_messages(conn, messages, now) == 3
    assert events.record_messages(conn, [Message(branch, "event")], now) == 0  # once
    events.record_messages(conn, [Message(stall, "resolved")], now)
    rows = conn.execute("SELECT id, kind, data FROM events ORDER BY id").fetchall()
    figs = notify.figures(conn, now)
    out, _, _ = notify.collect(
        device(daily=True, slow=None, hashrate=None, price=None), rows, figs, settings
    )
    titles = [m["title"] for m in out]
    assert titles[:3] == [
        "The DebitMyData chain is back to normal",
        "Chain reorganization: 3 blocks",
        "A competing branch",
    ]
    assert "DebitMyData network on November 16" in titles
    quiet, _, _ = notify.collect(
        device(status=False, reorg=5, branches=False, slow=None, hashrate=None, price=None),
        rows,
        figs,
        settings,
    )
    assert quiet == []


def test_the_rich_list(conn: psycopg.Connection[Any], node: FakeNode, settings: Settings) -> None:
    node.add(5)
    node.add(1, txs=[spend("w1", [(POOL_ADDRESS, "35")], [("dm1qwhale", "34.9")])])
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    assert [r["address"] for r in rich_list(conn, 10)] == [POOL_ADDRESS, "dm1qwhale"]
    state_now = utc(node.blocks[-1]["time"])
    ctx = Context(conn, node, replace(settings, richlist_min_dmd=Decimal(10)), state_now)  # type: ignore[arg-type]
    assert checks.check_richlist(ctx) == []  # the first run starts from now
    live(
        conn,
        node,
        1,
        txs=[spend("w2", [("dm1qwhale", "34.9")], [("dm1qsomeone", "20"), ("dm1qwhale", "14.8")])],
    )
    found = checks.check_richlist(ctx)
    assert [f.title for f in found] == ["Top 10 address #2 sent 20.10 DMD"]
    assert found[0].audience == "private" and found[0].key == "richlist.w2.dm1qwhale"
    rows = conn.execute("SELECT id, kind, data FROM events ORDER BY id").fetchall()
    figs = notify.figures(conn, state_now)
    out, _, _ = notify.collect(device(richlist=20, slow=None), rows, figs, settings)
    assert [m["title"] for m in out] == ["Top 10 address #2 sent 20.1 DMD"]
    out, _, _ = notify.collect(device(richlist=50, slow=None), rows, figs, settings)
    assert out == []


# ------------------------------------------------------------------------ the API and the service


def test_the_api(conn: psycopg.Connection[Any], push_api: api.PushApi, keys: push.Keys) -> None:
    assert push_api.config()["key"] == keys.public
    sub = {"endpoint": ENDPOINT, "keys": KEYS}
    with pytest.raises(api.Refused) as refused:
        push_api.handle(
            {"action": "subscribe", "subscription": {**sub, "endpoint": "https://evil.example/x"}}
        )
    assert refused.value.status == 400
    with pytest.raises(api.Refused, match="not a DMD address"):
        push_api.handle(
            {"action": "subscribe", "subscription": sub, "prefs": {"addresses": ["dm1zzzzzzzz"]}}
        )
    answer = push_api.handle(
        {"action": "subscribe", "subscription": sub, "prefs": {"addresses": ["dm1qfrendaddress"]}}
    )
    assert answer["on"] and answer["prefs"]["addresses"] == ["dm1qfrendaddress"]
    assert notify.watched_addresses(conn) == {"dm1qfrendaddress"}
    answer = push_api.handle({"action": "update", "endpoint": ENDPOINT, "prefs": {"blocks": True}})
    assert answer["prefs"]["blocks"] and answer["prefs"]["addresses"] == []
    assert push_api.handle({"action": "get", "endpoint": ENDPOINT})["on"]
    assert push_api.handle({"action": "test", "endpoint": ENDPOINT}) == {"ok": True}
    with pytest.raises(api.Refused) as refused:
        push_api.handle({"action": "test", "endpoint": ENDPOINT})
    assert refused.value.status == 429
    new = "https://fcm.googleapis.com/fcm/send/device-two"
    push_api.handle(
        {"action": "renew", "old": ENDPOINT, "subscription": {"endpoint": new, "keys": KEYS}}
    )
    assert not push_api.handle({"action": "get", "endpoint": ENDPOINT})["on"]
    assert push_api.handle({"action": "get", "endpoint": new})["on"]
    push_api.handle({"action": "unsubscribe", "endpoint": new})
    assert conn.execute("SELECT count(*) AS n FROM push_devices").fetchone()["n"] == 0


def test_the_service_sends_and_drops(
    conn: psycopg.Connection[Any],
    node: FakeNode,
    settings: Settings,
    push_api: api.PushApi,
    keys: push.Keys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node.add(30)
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    push_api.handle(
        {
            "action": "subscribe",
            "subscription": {"endpoint": ENDPOINT, "keys": KEYS},
            "prefs": {"blocks": True},
        }
    )
    gone = "https://fcm.googleapis.com/fcm/send/gone"
    push_api.handle(
        {
            "action": "subscribe",
            "subscription": {"endpoint": gone, "keys": KEYS},
            "prefs": {"blocks": True},
        }
    )
    sent: list[tuple[str, str]] = []
    answers = {ENDPOINT: 201, gone: 410}

    def deliver(
        keys_: Any, subject: str, dev: dict[str, Any], message: dict[str, Any]
    ) -> tuple[int, str]:
        sent.append((dev["endpoint"], message["title"]))
        return answers[dev["endpoint"]], ""

    monkeypatch.setattr(push, "deliver", deliver)
    notifier = notify.Notifier(settings, conn, keys)
    live(conn, node, 1)
    assert notifier.run_once(utc(node.blocks[-1]["time"] + 5)) == 1
    assert (ENDPOINT, "New block") in sent and (gone, "New block") in sent
    assert conn.execute("SELECT count(*) AS n FROM push_devices").fetchone()["n"] == 1  # gone
    items = push_api.handle(
        {"action": "get", "endpoint": ENDPOINT}, now=utc(node.blocks[-1]["time"] + 60)
    )["items"]
    assert [i["title"] for i in items] == ["New block"]
    # refused: tried again at the next run, from the same place
    answers[ENDPOINT] = 500
    live(conn, node, 1)
    notifier.run_once(utc(node.blocks[-1]["time"] + 5))
    row = conn.execute("SELECT failures, event_cursor FROM push_devices").fetchone()
    assert row["failures"] == 1
    answers[ENDPOINT] = 201
    sent.clear()
    notifier.run_once(utc(node.blocks[-1]["time"] + 6))
    assert sent == [(ENDPOINT, "New block")]
    assert conn.execute("SELECT failures FROM push_devices").fetchone()["failures"] == 0


def test_price_direction_and_targets(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings
) -> None:
    from chainwatch import price as gateway_price

    node.add(10)
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    start = node.blocks[-1]["time"]
    one = device(
        price=10,
        price_direction="up",
        price_targets=[0.2, 0.25],
        hashrate=None,
        slow=None,
    )
    told: list[list[str]] = []
    for step, usd in enumerate(
        ["0.18", "0.15", "0.162", "0.17", "0.2", "0.21", "0.2", "0.3", "0.22"]
    ):
        now = utc(start + 300 * (step + 1))
        gateway_price.record(conn, now, Decimal(usd), {}, "gateway")
        out, memory, _ = notify.collect(one, [], notify.figures(conn, now), settings)
        one = {**one, "memory": memory}
        told.append([m["title"] for m in out])
    assert told[0] == []  # the starting price
    assert told[1] == []  # down 17%: not wanted (up only)
    assert told[2] == []  # up 8% from the bottom ($0.15)
    assert told[3] == ["Reference price up 13%: $0.17"]  # 13% from $0.15
    assert told[4] == ["Reference price up 18%: $0.2", "Reference price reached $0.2"]
    assert told[5] == []  # moving away from $0.2: nothing yet
    assert told[6] == ["Reference price reached $0.2"]  # back on it
    assert told[7] == [
        "Reference price up 50%: $0.3",
        "Reference price rose above $0.25: $0.3",
    ]
    assert told[8] == ["Reference price fell below $0.25: $0.22"]
    # a removed target is forgotten
    one = {**one, "prefs": prefs(price=None, price_targets=[0.2], hashrate=None, slow=None)}
    out, memory, _ = notify.collect(one, [], notify.figures(conn, utc(start + 3000)), settings)
    assert out == [] and set(memory["targets"]) == {"0.2"} and "price" not in memory["base"]


def test_price_choices_are_checked() -> None:
    clean = notify.clean_prefs(
        {"price_direction": "down", "price_targets": ["0.2", 0.2, 0.100000001, 5]}, 1000, bool
    )
    assert clean["price_direction"] == "down"
    assert clean["price_targets"] == [0.1, 0.2, 5.0]
    for bad in (
        {"price_direction": "sideways"},
        {"price_targets": [0]},
        {"price_targets": ["x"]},
        {"price_targets": [1, 2, 3, 4, 5, 6]},
    ):
        with pytest.raises(ValueError):
            notify.clean_prefs(bad, 1000, bool)
