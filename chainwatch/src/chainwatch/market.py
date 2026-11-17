"""DMD's figures from the exchanges, once it trades on one: its trading volume over 24 hours and
its market price, as CoinGecko gives them (CoinGecko follows the exchanges that list a coin).

Off until DMDW_COINGECKO_ID is set: the coin's id on CoinGecko (the "API ID" on its page there).
Until then the stats page shows the trading volume chart ready but empty, with a note, and the
API's fields are null. From then on, every 5 minutes:

  GET <DMDW_COINGECKO_URL>/simple/price?ids=<id>&vs_currencies=usd&include_24hr_vol=true
      &include_last_updated_at=true

CoinGecko's free (Demo) key goes in DMDW_COINGECKO_KEY; a paid key works with
DMDW_COINGECKO_URL=https://pro-api.coingecko.com/api/v3. Each answer is kept in the market table,
by the time CoinGecko updated it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from chainwatch.config import Settings
from chainwatch.price import USER_AGENT


class MarketUnavailable(RuntimeError):
    pass


def _url(settings: Settings) -> str:
    query = urllib.parse.urlencode(
        {
            "ids": settings.coingecko_id,
            "vs_currencies": "usd",
            "include_24hr_vol": "true",
            "include_last_updated_at": "true",
        }
    )
    return f"{settings.coingecko_url}/simple/price?{query}"


def fetch(settings: Settings, timeout: float = 20) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if settings.coingecko_key:
        pro = "pro-api." in settings.coingecko_url
        headers["x-cg-pro-api-key" if pro else "x-cg-demo-api-key"] = settings.coingecko_key
    request = urllib.request.Request(_url(settings), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(), parse_float=Decimal)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise MarketUnavailable(f"CoinGecko: {exc}") from exc


def _number(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def poll(conn: psycopg.Connection[Any], settings: Settings, now: datetime) -> bool:
    """Reads CoinGecko once; True when a new figure was kept. Nothing when it is off."""
    if not settings.coingecko_id:
        return False
    answer = fetch(settings)
    coin = answer.get(settings.coingecko_id) if isinstance(answer, dict) else None
    if not isinstance(coin, dict) or "usd" not in coin:
        raise MarketUnavailable(f"CoinGecko has no USD price for {settings.coingecko_id!r}")
    updated = coin.get("last_updated_at")
    observed = datetime.fromtimestamp(int(updated), UTC) if updated else now
    row = conn.execute(
        "INSERT INTO market (observed_at, source, price_usd, volume_24h_usd) "
        "VALUES (%s, 'coingecko', %s, %s) ON CONFLICT (observed_at) DO NOTHING RETURNING 1",
        (observed, _number(coin.get("usd")), _number(coin.get("usd_24h_vol"))),
    ).fetchone()
    return row is not None
