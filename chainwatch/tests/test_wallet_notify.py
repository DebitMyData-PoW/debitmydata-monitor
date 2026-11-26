"""The DMD Web Wallet's notifications: its wallets' transactions, once in a block, to its devices,
from its own site (mydmd.io) only."""

import json
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from chainwatch import api, chain, events, notify, push
from chainwatch.config import Settings
from chainwatch.sync import sync

from .conftest import PERIODS, POOL_ADDRESS, FakeNode, spend, utc

DMD = chain.DATA_PER_DMD
ENDPOINT = "https://fcm.googleapis.com/fcm/send/wallet-phone"
KEYS = {"p256dh": "B" + "A" * 86, "auth": "c" * 22}
MAIN = "a1b2c3d4e5f60718"
SAVINGS = "0f1e2d3c4b5a6978"
RECEIVE = "dm1qwalletrecv0"
CHANGE = "dm1qwalletchange0"
NEXT = "dm1qwalletrecv3"
SAVED = "dm1qsavedaddress"
WALLET_PREFS = {
    "app": "wallet",
    "wallets": [
        {"id": MAIN, "addresses": [RECEIVE, CHANGE, NEXT]},
        {"id": SAVINGS, "addresses": [SAVED]},
    ],
}


@pytest.fixture
def keys(tmp_path: Path) -> push.Keys:
    return push.make_keys(tmp_path / "vapid.pem")[0]


@pytest.fixture
def push_api(settings: Settings, keys: push.Keys, node: FakeNode) -> api.PushApi:
    return api.PushApi(settings, keys, node)  # type: ignore[arg-type]


def wallet_device(prefs: dict[str, Any], cursor: int = 0) -> dict[str, Any]:
    return {"prefs": prefs, "memory": {}, "event_cursor": cursor}


def live(conn: psycopg.Connection[Any], node: FakeNode, **kwargs: Any) -> None:
    node.add(1, **kwargs)
    now = utc(node.blocks[-1]["time"] + 5)
    watched = notify.watched_addresses(conn) | {RECEIVE, CHANGE, NEXT, SAVED}
    result = sync(conn, node, PERIODS, "Official", now, large=10 * DMD, watched=watched)
    events.record_sync(conn, result, now, {})


def test_wallet_choices_are_checked() -> None:
    def never(a: str) -> bool:
        raise AssertionError("a wallet's addresses are not checked one by one with the node")

    clean = notify.clean_prefs(
        {**WALLET_PREFS, "blocks": True, "price": 5}, 1000, never
    )  # the stats page's choices are not taken for a wallet
    assert clean == WALLET_PREFS
    assert "blocks" not in clean and notify.is_wallet({"prefs": clean})
    bad_cases = [
        ({"wallets": [{"id": "XYZ", "addresses": [RECEIVE]}]}, "hexadecimal"),
        ({"wallets": [{"id": MAIN, "addresses": []}]}, "1 to 150"),
        ({"wallets": [{"id": MAIN, "addresses": ["dm1qbobobobo"]}]}, "not a DMD address"),
        ({"wallets": [{"id": MAIN, "addresses": [RECEIVE]}] * 2}, "each id once"),
        (
            {"wallets": [{"id": f"{n:016x}", "addresses": [RECEIVE]} for n in range(11)]},
            "10 at most",
        ),
        (
            {"wallets": [{"id": MAIN, "addresses": [f"dm1qwallet{n:04d}" for n in range(151)]}]},
            "1 to 150",
        ),
    ]
    for raw, message in bad_cases:
        with pytest.raises(ValueError, match=message):
            notify.clean_prefs({"app": "wallet", **raw}, 1000, never)


def test_each_transaction_of_a_wallet(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings
) -> None:
    node.add(24 * 40)
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    live(
        conn,
        node,
        txs=[
            spend("pay", [(POOL_ADDRESS, "35")], [(RECEIVE, "5"), (POOL_ADDRESS, "29.9")]),
            spend("other", [(POOL_ADDRESS, "35")], [("dm1qsomeoneelse", "34.9")]),
        ],
    )
    # the wallet spends: 2 DMD out, its change back, 0.001 DMD fee
    live(
        conn,
        node,
        txs=[spend("spent", [(RECEIVE, "5")], [("dm1qshop", "2"), (CHANGE, "2.999")])],
    )
    # a mining reward to the savings wallet
    live(conn, node, tag="Solo", payouts={SAVED: Decimal("35")})
    rows = conn.execute("SELECT id, kind, data FROM events ORDER BY id").fetchall()

    out, memory, cursor = notify.collect(wallet_device(WALLET_PREFS), rows, None, settings)  # type: ignore[arg-type]
    assert cursor == rows[-1]["id"] and memory == {}
    by_txid = {m["wallet"].get("txid"): m for m in out}
    assert set(by_txid) == {"pay", "spent", node.blocks[-1]["tx"][0]["txid"]}  # nothing else
    received, spent = by_txid["pay"], by_txid["spent"]
    assert received["title"] == "Received 5 DMD"
    assert received["body"] == f"In block {len(node.blocks) - 3:,}."
    assert received["wallet"] == {
        "kind": "tx",
        "wallet": MAIN,
        "txid": "pay",
        "height": len(node.blocks) - 3,
        "net": 5 * DMD,
        "reward": False,
    }
    assert spent["title"] == "Sent 2.001 DMD"  # what left the wallet: the payment and its fee
    assert spent["wallet"]["net"] == -2_001 * DMD // 1000
    assert received["tag"] == "tx-pay" and received["url"] == "/"
    reward = by_txid[node.blocks[-1]["tx"][0]["txid"]]
    assert reward["title"] == "Mining reward: 35 DMD"
    assert reward["wallet"]["wallet"] == SAVINGS and reward["wallet"]["reward"] is True
    # nothing again from the same cursor
    again, _, _ = notify.collect(wallet_device(WALLET_PREFS, cursor), rows, None, settings)  # type: ignore[arg-type]
    assert again == []


def test_many_transactions_at_once_are_one_notice(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings
) -> None:
    node.add(24 * 40)
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    live(
        conn,
        node,
        txs=[
            spend(f"gift{n}", [(POOL_ADDRESS, "35")], [(NEXT, "1"), (POOL_ADDRESS, "33.9")])
            for n in range(5)
        ],
    )
    rows = conn.execute("SELECT id, kind, data FROM events ORDER BY id").fetchall()
    out, _, _ = notify.collect(wallet_device(WALLET_PREFS), rows, None, settings)  # type: ignore[arg-type]
    assert len(out) == 1
    assert out[0]["title"] == "5 transactions: +5 DMD"
    assert out[0]["wallet"] == {
        "kind": "many",
        "wallet": MAIN,
        "count": 5,
        "net": 5 * DMD,
        "height": len(node.blocks) - 1,
    }


def test_wallet_devices_through_the_api_and_the_service(
    conn: psycopg.Connection[Any],
    node: FakeNode,
    settings: Settings,
    push_api: api.PushApi,
    keys: push.Keys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node.add(24 * 40)
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    config = push_api.config()
    assert (config["max_wallets"], config["max_wallet_addresses"]) == (10, 150)
    sub = {"endpoint": ENDPOINT, "keys": KEYS}
    answer = push_api.handle({"action": "subscribe", "subscription": sub, "prefs": WALLET_PREFS})
    assert answer == {"on": True, "prefs": WALLET_PREFS, "items": []}
    assert {RECEIVE, CHANGE, NEXT, SAVED} <= notify.watched_addresses(conn)
    # a stats page device next to it: its own choices only
    push_api.handle(
        {
            "action": "subscribe",
            "subscription": {"endpoint": ENDPOINT + "-stats", "keys": KEYS},
            "prefs": {"blocks": True, "addresses": ["dm1qstatsfrend"]},
        }
    )
    assert notify.watched_addresses(conn) == {RECEIVE, CHANGE, NEXT, SAVED, "dm1qstatsfrend"}

    sent: list[tuple[str, dict[str, Any]]] = []

    def deliver(
        keys_: Any, subject: str, dev: dict[str, Any], message: dict[str, Any]
    ) -> tuple[int, str]:
        sent.append((dev["endpoint"], message))
        return 201, ""

    monkeypatch.setattr(push, "deliver", deliver)
    notifier = notify.Notifier(settings, conn, keys)
    push_api.handle({"action": "test", "endpoint": ENDPOINT})
    live(
        conn,
        node,
        txs=[spend("pay", [(POOL_ADDRESS, "35")], [(NEXT, "3"), (POOL_ADDRESS, "31.9")])],
    )
    notifier.run_once(utc(node.blocks[-1]["time"] + 5))
    wallet = [m for e, m in sent if e == ENDPOINT]
    assert [m["wallet"]["kind"] for m in wallet] == ["test", "tx"]  # no block notices
    assert wallet[1]["title"] == "Received 3 DMD" and wallet[1]["wallet"]["wallet"] == MAIN
    stats = [m["title"] for e, m in sent if e != ENDPOINT]
    assert "New block" in stats and all("wallet" not in m for e, m in sent if e != ENDPOINT)
    # what a wallet was told is not kept on the server
    row = conn.execute(
        "SELECT sent FROM push_devices WHERE endpoint_hash = %s", (notify.endpoint_hash(ENDPOINT),)
    ).fetchone()
    assert row["sent"] == []
    assert push_api.handle({"action": "get", "endpoint": ENDPOINT})["items"] == []
    # new addresses (the wallet used one more), then off
    more = {"app": "wallet", "wallets": [{"id": MAIN, "addresses": [RECEIVE, "dm1qwalletrecv2"]}]}
    assert (
        push_api.handle({"action": "update", "endpoint": ENDPOINT, "prefs": more})["prefs"] == more
    )
    assert SAVED not in notify.watched_addresses(conn)
    push_api.handle({"action": "unsubscribe", "endpoint": ENDPOINT})
    assert RECEIVE not in notify.watched_addresses(conn)


def _call(
    port: int, method: str, origin: str | None, body: Any = None
) -> tuple[int, dict[str, str], Any]:
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/push", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), json.loads(raw) if raw else None


def test_only_the_wallets_site_may_call_from_its_pages(
    conn: psycopg.Connection[Any], push_api: api.PushApi
) -> None:
    server = api.serve(push_api, "127.0.0.1:0")
    port = server.server_address[1]
    try:
        wallet = "https://mydmd.io"
        status, headers, _ = _call(port, "OPTIONS", wallet)
        assert status == 204
        assert headers["Access-Control-Allow-Origin"] == wallet
        assert "POST" in headers["Access-Control-Allow-Methods"]
        assert headers["Access-Control-Allow-Headers"] == "Content-Type"
        assert "Access-Control-Allow-Credentials" not in headers
        status, headers, body = _call(port, "GET", wallet)
        assert status == 200 and body["key"] and headers["Access-Control-Allow-Origin"] == wallet
        status, headers, body = _call(
            port,
            "POST",
            wallet,
            {
                "action": "subscribe",
                "subscription": {"endpoint": ENDPOINT, "keys": KEYS},
                "prefs": WALLET_PREFS,
            },
        )
        assert status == 200 and body["on"] and headers["Access-Control-Allow-Origin"] == wallet
        # the stats page itself: as before
        status, _, body = _call(
            port, "POST", "https://stats.debitmydata.io", {"action": "get", "endpoint": ENDPOINT}
        )
        assert status == 200 and body["on"]
        # any other page: refused, and its browser may not read the answer
        for method in ("OPTIONS", "POST"):
            status, headers, _ = _call(
                port,
                method,
                "https://evil.example",
                {"action": "unsubscribe", "endpoint": ENDPOINT},
            )
            assert status == 403 and "Access-Control-Allow-Origin" not in headers
        assert push_api.handle({"action": "get", "endpoint": ENDPOINT})["on"]
    finally:
        server.shutdown()
        server.server_close()
