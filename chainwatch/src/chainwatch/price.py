"""The DebitMyData Gateway's reference price for DMD, recorded here as the gateway recorded it.

The gateway computes the price every 5 minutes and publishes the latest one at
https://api.debitmydata.io/v1/prices/dmd-usd. The chain watch reads it every 5 minutes and keeps
each new one (by the time the gateway computed it). The gateway's earlier records come in once,
from a CSV export of its price_records table (import_csv), so the history starts when the gateway
started, not when the monitor did.

It is not a market price: until DMD trades on an exchange, it is the higher of DebitMyData's
minimum rate and the mining cost.
"""

from __future__ import annotations

import csv
import json
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TextIO

import psycopg
from psycopg.types.json import Jsonb

from chainwatch import __version__

USER_AGENT = f"DebitMyData-Monitor/{__version__} (+https://stats.debitmydata.io)"


class PriceUnavailable(RuntimeError):
    pass


def fetch_json(url: str, timeout: float = 20) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(), parse_float=Decimal)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise PriceUnavailable(f"{url}: {exc}") from exc


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def record(
    conn: psycopg.Connection[Any],
    observed_at: datetime,
    usd_per_dmd: Decimal,
    inputs: dict[str, Any],
    source: str,
) -> bool:
    """Keeps one price; False when that moment is already recorded."""
    cost = _decimal(inputs.get("mining_cost_usd_per_dmd"))
    minimum = _decimal(inputs.get("minimum_usd_per_dmd"))
    added = conn.execute(
        "INSERT INTO prices (observed_at, usd_per_dmd, mining_cost_usd, minimum_usd, price_from, "
        "source, inputs) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (observed_at) DO NOTHING",
        (
            observed_at,
            usd_per_dmd,
            cost,
            minimum,
            inputs.get("price_from")
            or ("minimum" if minimum and cost and minimum > cost else None),
            source,
            Jsonb(json.loads(json.dumps(inputs, default=str))),
        ),
    ).rowcount
    return bool(added)


def poll(conn: psycopg.Connection[Any], url: str) -> bool:
    """Reads the gateway's latest price and keeps it if it is new."""
    data = fetch_json(url)
    try:
        price = Decimal(str(data["usd_per_unit"]))
        observed = datetime.fromisoformat(str(data["observed_at"]).replace("Z", "+00:00"))
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise PriceUnavailable(f"{url}: unexpected answer ({exc})") from exc
    if price <= 0 or observed.tzinfo is None:
        raise PriceUnavailable(f"{url}: unexpected answer")
    inputs = dict(data.get("inputs") or {})
    with conn.transaction():
        return record(conn, observed, price, inputs, "gateway")


def import_csv(conn: psycopg.Connection[Any], source: Path | TextIO) -> tuple[int, int]:
    """The gateway's records from a CSV with the columns observed_at, usd_per_unit and inputs
    (the export command is in the README), from a file or standard input. Returns (added,
    already there)."""
    if isinstance(source, Path):
        with source.open(newline="") as handle:
            return _import_rows(conn, handle, str(source))
    return _import_rows(conn, source, "standard input")


def _import_rows(conn: psycopg.Connection[Any], handle: TextIO, name: str) -> tuple[int, int]:
    added = skipped = 0
    with conn.transaction():
        for row in csv.DictReader(handle):
            try:
                observed = datetime.fromisoformat(row["observed_at"].strip().replace(" ", "T", 1))
                price = Decimal(row["usd_per_unit"].strip())
                inputs = json.loads(row.get("inputs") or "{}")
            except (KeyError, InvalidOperation, ValueError) as exc:
                problem = f"{name}: a row could not be read ({exc}): {str(row)[:80]}"
                raise ValueError(problem) from exc
            if observed.tzinfo is None:
                raise ValueError(f"{name}: {row['observed_at']} has no time zone")
            if record(conn, observed, price, inputs, "gateway-import"):
                added += 1
            else:
                skipped += 1
    return added, skipped
