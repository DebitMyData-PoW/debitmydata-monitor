"""Alerts: what the checks find, kept in the alerts table, and sent.

Two kinds:
  condition  lasts (a stalled chain, a fork between two nodes): sent when it starts, again while
             it lasts (critical every 2 hours, warning every 12 hours), and once more when it ends
  event      happened once (a reorganization, a hashrate jump): sent once, ever, for its key

Three audiences:
  private    the team: email and the private Discord channel (critical ones with a mention)
  public     the public Discord channel only (the DMD community: difficulty, hashrate, the price)
  both       the two

A check that fails to run (a node or a site that does not answer) changes nothing: its alerts stay
as they were, and the failure itself is reported as "monitor.<check>".
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

import psycopg

from chainwatch.config import Settings

log = logging.getLogger(__name__)

CRITICAL, WARNING, INFO = "critical", "warning", "info"
REPEAT = {CRITICAL: timedelta(hours=2), WARNING: timedelta(hours=12), INFO: None}
COLOURS = {CRITICAL: 0xC80815, WARNING: 0xE8A317, INFO: 0x098551, "resolved": 0x098551}


@dataclass(frozen=True)
class Finding:
    key: str
    severity: str
    title: str
    detail: str
    kind: str = "condition"
    audience: str = "private"

    @property
    def check(self) -> str:
        return self.key.split(".", 1)[0]


@dataclass(frozen=True)
class Message:
    finding: Finding
    state: str  # "open", "reminder", "resolved", "event"


def apply(
    conn: psycopg.Connection[Any],
    findings: Iterable[Finding],
    completed: set[str],
    now: datetime,
) -> list[Message]:
    """Records what the checks found; returns the messages to send."""
    found = {f.key: f for f in findings}
    messages: list[Message] = []
    rows = {
        r["key"]: r
        for r in conn.execute("SELECT * FROM alerts WHERE resolved_at IS NULL").fetchall()
    }
    for key, f in found.items():
        if f.kind == "event":
            seen = conn.execute("SELECT 1 FROM alerts WHERE key = %s", (key,)).fetchone()
            if seen:
                continue
            conn.execute(
                "INSERT INTO alerts (key, severity, kind, audience, title, detail, first_seen, "
                "last_seen, resolved_at) VALUES (%s, %s, 'event', %s, %s, %s, %s, %s, %s)",
                (key, f.severity, f.audience, f.title, f.detail, now, now, now),
            )
            messages.append(Message(f, "event"))
            continue
        row = rows.get(key)
        if row is None:
            conn.execute(
                "INSERT INTO alerts (key, severity, kind, audience, title, detail, first_seen, "
                "last_seen) VALUES (%s, %s, 'condition', %s, %s, %s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET severity = EXCLUDED.severity, "
                "audience = EXCLUDED.audience, title = EXCLUDED.title, detail = EXCLUDED.detail, "
                "first_seen = EXCLUDED.first_seen, last_seen = EXCLUDED.last_seen, "
                "last_sent = NULL, resolved_at = NULL",
                (key, f.severity, f.audience, f.title, f.detail, now, now),
            )
            messages.append(Message(f, "open"))
            continue
        worse = f.severity == CRITICAL and row["severity"] != CRITICAL
        conn.execute(
            "UPDATE alerts SET severity = %s, title = %s, detail = %s, last_seen = %s "
            "WHERE key = %s",
            (f.severity, f.title, f.detail, now, key),
        )
        repeat = REPEAT.get(f.severity)
        due = row["last_sent"] is None or (repeat is not None and now - row["last_sent"] >= repeat)
        if worse:
            messages.append(Message(f, "open"))
        elif due:
            messages.append(Message(f, "reminder"))
    # events whose sending failed: again, for an hour
    for row in conn.execute(
        "SELECT * FROM alerts WHERE kind = 'event' AND last_sent IS NULL AND first_seen > %s "
        "AND first_seen < %s",
        (now - timedelta(hours=1), now),
    ).fetchall():
        f = Finding(
            row["key"], row["severity"], row["title"], row["detail"], "event", row["audience"]
        )
        messages.append(Message(f, "event"))
    for key, row in rows.items():
        if key in found or row["kind"] == "event":
            continue
        if key.split(".", 1)[0] not in completed:
            continue  # its check did not run: nothing is known
        conn.execute("UPDATE alerts SET resolved_at = %s WHERE key = %s", (now, key))
        f = Finding(key, row["severity"], row["title"], row["detail"], audience=row["audience"])
        messages.append(Message(f, "resolved"))
    return messages


def mark_sent(conn: psycopg.Connection[Any], messages: Iterable[Message], now: datetime) -> None:
    for message in messages:
        conn.execute("UPDATE alerts SET last_sent = %s WHERE key = %s", (now, message.finding.key))


# --------------------------------------------------------------------------- sending


def _subject(message: Message) -> str:
    f = message.finding
    if message.state == "resolved":
        return f"Resolved: {f.title}"
    label = {CRITICAL: "CRITICAL", WARNING: "Warning", INFO: "Info"}[f.severity]
    again = " (still)" if message.state == "reminder" else ""
    return f"[{label}] {f.title}{again}"


def _post_json(url: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "DebitMyData-Monitor"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def discord(url: str, message: Message, mention: str = "", footer: str = "") -> None:
    f = message.finding
    colour = COLOURS["resolved" if message.state == "resolved" else f.severity]
    embed: dict[str, Any] = {
        "title": _subject(message)[:256],
        "description": f.detail[:4000],
        "color": colour,
    }
    if footer:
        embed["footer"] = {"text": footer[:2048]}
    payload: dict[str, Any] = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if mention and f.severity == CRITICAL and message.state in ("open", "event", "reminder"):
        payload["content"] = mention
        user = mention.strip("<@!>")
        if user.isdigit():
            payload["allowed_mentions"] = {"users": [user]}
    _post_json(url, payload)


def email(settings: Settings, message: Message, recipients: list[str]) -> None:
    f = message.finding
    mail = EmailMessage()
    mail["Subject"] = _subject(message)
    mail["From"] = settings.smtp_from
    mail["To"] = ", ".join(recipients)
    when = "ended" if message.state == "resolved" else "is going on"
    body = f"{f.detail}\n\n"
    if message.state != "event":
        body += f"This alert {when}.\n"
    body += f"Alert key: {f.key}\nFrom: {settings.server_name}\nCharts: {settings.grafana_url}\n"
    mail.set_content(body)
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, 465, context=context, timeout=30) as smtp:
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(mail)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=context)
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(mail)


def send(settings: Settings, messages: list[Message]) -> list[Message]:
    """Sends each message to its audience; returns those that went out on at least one channel,
    or that had no channel to go to (then they are only in the log). One that failed everywhere
    is tried again at the next pass."""
    sent = []
    for message in messages:
        f = message.finding
        log.warning("%s: %s — %s", message.state, f.key, f.title)
        ok = tried = False
        if f.audience in ("private", "both"):
            if settings.discord_webhook:
                tried = True
                try:
                    discord(
                        settings.discord_webhook,
                        message,
                        settings.discord_mention,
                        f"{f.key} · {settings.server_name}",
                    )
                    ok = True
                except (urllib.error.URLError, OSError) as exc:
                    log.error("Discord (private): %s", exc)
            recipients = list(settings.email_to)
            if f.severity == CRITICAL:
                recipients += [r for r in settings.email_critical_to if r not in recipients]
            if settings.smtp_host and settings.smtp_from and recipients:
                tried = True
                try:
                    email(settings, message, recipients)
                    ok = True
                except (smtplib.SMTPException, OSError) as exc:
                    log.error("email: %s", exc)
        # The community gets the news, not reminders or the end of every condition.
        public = f.audience in ("public", "both") and message.state in ("open", "event")
        if public and settings.public_discord_webhook:
            tried = True
            try:
                discord(
                    settings.public_discord_webhook,
                    message,
                    "",
                    f"DebitMyData network · {settings.site_url}",
                )
                ok = True
            except (urllib.error.URLError, OSError) as exc:
                log.error("Discord (public): %s", exc)
        if ok or not tried:
            sent.append(message)
    return sent
