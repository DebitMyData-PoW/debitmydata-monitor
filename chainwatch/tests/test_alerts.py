from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from chainwatch import alerts
from chainwatch.alerts import CRITICAL, WARNING, Finding, Message
from chainwatch.config import Settings

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


def states(messages: list[Message]) -> list[tuple[str, str]]:
    return [(m.finding.key, m.state) for m in messages]


def test_a_condition_opens_repeats_and_ends(conn: psycopg.Connection[Any]) -> None:
    stall = Finding("stall.blocks", WARNING, "No new block", "detail")
    sent = alerts.apply(conn, [stall], {"stall"}, NOW)
    assert states(sent) == [("stall.blocks", "open")]
    alerts.mark_sent(conn, sent, NOW)
    # still there a minute later: nothing new
    assert alerts.apply(conn, [stall], {"stall"}, NOW + timedelta(minutes=1)) == []
    # worse: sent again at once
    worse = replace(stall, severity=CRITICAL)
    assert states(alerts.apply(conn, [worse], {"stall"}, NOW + timedelta(minutes=2))) == [
        ("stall.blocks", "open")
    ]
    alerts.mark_sent(conn, [Message(worse, "open")], NOW + timedelta(minutes=2))
    # critical: again after 2 hours
    later = NOW + timedelta(hours=2, minutes=3)
    assert states(alerts.apply(conn, [worse], {"stall"}, later)) == [("stall.blocks", "reminder")]
    # its check ran and found nothing: it ended
    assert states(alerts.apply(conn, [], {"stall"}, later)) == [("stall.blocks", "resolved")]
    assert alerts.apply(conn, [], {"stall"}, later) == []


def test_an_alert_stays_while_its_check_cannot_run(conn: psycopg.Connection[Any]) -> None:
    fork = Finding("views.explorer.block", CRITICAL, "Different chains", "detail")
    alerts.apply(conn, [fork], {"views"}, NOW)
    assert alerts.apply(conn, [], {"node"}, NOW + timedelta(minutes=1)) == []
    row = conn.execute("SELECT resolved_at FROM alerts WHERE key = %s", (fork.key,)).fetchone()
    assert row["resolved_at"] is None


def test_an_event_is_sent_once(conn: psycopg.Connection[Any]) -> None:
    reorg = Finding("forks.reorg.100", WARNING, "Reorganized", "detail", kind="event")
    assert states(alerts.apply(conn, [reorg], {"forks"}, NOW)) == [("forks.reorg.100", "event")]
    # not sent (no channel answered): tried again within the hour, then never
    assert states(alerts.apply(conn, [reorg], {"forks"}, NOW + timedelta(minutes=1))) == [
        ("forks.reorg.100", "event")
    ]
    alerts.mark_sent(conn, [Message(reorg, "event")], NOW + timedelta(minutes=1))
    assert alerts.apply(conn, [reorg], {"forks"}, NOW + timedelta(minutes=2)) == []
    assert alerts.apply(conn, [], {"forks"}, NOW + timedelta(days=2)) == []


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(alerts, "_post_json", lambda url, payload: calls.append((url, payload)))
    return calls


def test_audiences(settings: Settings, posted: list[tuple[str, dict[str, Any]]]) -> None:
    s = replace(
        settings,
        discord_webhook="https://private",
        public_discord_webhook="https://pub",
        discord_mention="<@123>",
    )
    critical = Finding("forks.reorg.5.deep", CRITICAL, "Deep reorg", "d", "event", "both")
    team = Finding("node.peers", WARNING, "No peers", "d")
    news = Finding("daily.2026-11-16", "info", "The day", "d", "event", "public")
    sent = alerts.send(
        s,
        [
            Message(critical, "event"),
            Message(team, "open"),
            Message(news, "event"),
            Message(team, "resolved"),
        ],
    )
    assert len(sent) == 4
    urls = [url for url, _ in posted]
    assert urls == [
        "https://private",
        "https://pub",
        "https://private",
        "https://pub",
        "https://private",
    ]
    private_critical = posted[0][1]
    assert private_critical["content"] == "<@123>"
    assert private_critical["allowed_mentions"] == {"users": ["123"]}
    assert "content" not in posted[1][1]  # the public channel: never a mention
    assert posted[4][1]["embeds"][0]["title"] == "Resolved: No peers"


def test_without_channels_alerts_are_only_logged(settings: Settings) -> None:
    team = Finding("node.peers", WARNING, "No peers", "d")
    assert alerts.send(settings, [Message(team, "open")]) == [Message(team, "open")]


def test_a_failed_channel_is_tried_again(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(url: str, payload: dict[str, Any]) -> None:
        raise OSError("refused")

    monkeypatch.setattr(alerts, "_post_json", refuse)
    s = replace(settings, discord_webhook="https://private")
    assert alerts.send(s, [Message(Finding("node.peers", WARNING, "t", "d"), "open")]) == []
