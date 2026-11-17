"""Settings, from the environment (systemd reads them from /etc/debitmydata-monitor/monitor.env),
and the approved figures of the mining cost (config/mining-cost.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(name: str) -> list[str]:
    return [part.strip() for part in _env(name).replace(";", ",").split(",") if part.strip()]


def _share(name: str, default: str) -> Decimal:
    """A share between 0 and 1 (0.5 = half), or a factor, from the environment."""
    try:
        value = Decimal(_env(name) or default)
    except ArithmeticError:
        value = Decimal(default)
    return value if value > 0 else Decimal(default)


def _yes(name: str, default: bool) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in ("1", "yes", "true", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str
    rpc_url: str
    rpc_cookie: str
    rpc_user: str
    rpc_password: str
    explorer_url: str
    mempool_url: str
    price_url: str
    prometheus_url: str
    public_dir: Path
    metrics_file: Path
    mining_cost_file: Path
    pool_tag: str
    site_url: str
    grafana_url: str
    server_name: str
    # alerts: email
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    email_to: list[str]
    email_critical_to: list[str]
    # alerts: Discord
    discord_webhook: str
    discord_mention: str
    public_discord_webhook: str
    public_reorgs: bool
    public_concentration: bool
    # thresholds
    stall_warning_minutes: int = 30
    stall_critical_minutes: int = 60
    outside_miner_share: Decimal = Decimal("0.25")
    pool_payee_share: Decimal = Decimal("0.5")
    hashrate_jump_factor: Decimal = Decimal(3)
    difficulty_change_share: Decimal = Decimal("0.25")
    price_change_share: Decimal = Decimal("0.05")
    pool_addresses: list[str] = field(default_factory=list)
    pool_view: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=_env(
                "DMDW_DATABASE_URL", "postgresql:///dmdwatch?host=/var/run/postgresql"
            ),
            rpc_url=_env("DMDW_RPC_URL", "http://127.0.0.1:18557"),
            rpc_cookie=_env("DMDW_RPC_COOKIE", "/var/lib/debitmydata/.cookie"),
            rpc_user=_env("DMDW_RPC_USER"),
            rpc_password=_env("DMDW_RPC_PASSWORD"),
            explorer_url=_env("DMDW_EXPLORER_URL", "https://explorer.debitmydata.io").rstrip("/"),
            mempool_url=_env("DMDW_MEMPOOL_URL", "https://mempool.debitmydata.io").rstrip("/"),
            price_url=_env("DMDW_PRICE_URL", "https://api.debitmydata.io/v1/prices/dmd-usd"),
            prometheus_url=_env("DMDW_PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/"),
            public_dir=Path(_env("DMDW_PUBLIC_DIR", "/var/lib/debitmydata-monitor/public")),
            metrics_file=Path(
                _env("DMDW_METRICS_FILE", "/var/lib/debitmydata-monitor/metrics/chainwatch.prom")
            ),
            mining_cost_file=Path(
                _env("DMDW_MINING_COST_FILE", "/opt/debitmydata-monitor/config/mining-cost.toml")
            ),
            pool_tag=_env("DMDW_POOL_TAG", "Official"),
            site_url=_env("DMDW_SITE_URL", "https://stats.debitmydata.io").rstrip("/"),
            grafana_url=_env("DMDW_GRAFANA_URL", "https://monitor.debitmydata.io").rstrip("/"),
            server_name=_env("DMDW_SERVER_NAME", "DebitMyData monitor"),
            smtp_host=_env("DMDW_SMTP_HOST"),
            smtp_port=int(_env("DMDW_SMTP_PORT", "587") or 587),
            smtp_user=_env("DMDW_SMTP_USER"),
            smtp_password=_env("DMDW_SMTP_PASSWORD"),
            smtp_from=_env("DMDW_SMTP_FROM"),
            email_to=_list("DMDW_EMAIL_TO"),
            email_critical_to=_list("DMDW_EMAIL_CRITICAL_TO"),
            discord_webhook=_env("DMDW_DISCORD_WEBHOOK"),
            discord_mention=_env("DMDW_DISCORD_MENTION"),
            public_discord_webhook=_env("DMDW_PUBLIC_DISCORD_WEBHOOK"),
            public_reorgs=_yes("DMDW_PUBLIC_REORGS", False),
            public_concentration=_yes("DMDW_PUBLIC_CONCENTRATION", False),
            pool_addresses=_list("DMDW_POOL_ADDRESSES"),
            pool_view=_yes("DMDW_POOL_VIEW", True),
            stall_warning_minutes=int(_share("DMDW_STALL_WARNING_MINUTES", "30")),
            stall_critical_minutes=int(_share("DMDW_STALL_CRITICAL_MINUTES", "60")),
            outside_miner_share=_share("DMDW_OUTSIDE_MINER_SHARE", "0.25"),
            pool_payee_share=_share("DMDW_POOL_PAYEE_SHARE", "0.5"),
            hashrate_jump_factor=_share("DMDW_HASHRATE_JUMP_FACTOR", "3"),
            difficulty_change_share=_share("DMDW_DIFFICULTY_CHANGE_SHARE", "0.25"),
            price_change_share=_share("DMDW_PRICE_CHANGE_SHARE", "0.05"),
        )


# --------------------------------------------------------------------------- mining cost figures


@dataclass(frozen=True)
class CostPeriod:
    """The approved figures from a date on (the gateway's dmd_mining_cost, at a review)."""

    starts: date
    electricity_usd_per_kwh: Decimal
    efficiency_joules_per_terahash: Decimal


def load_cost_periods(path: Path) -> list[CostPeriod]:
    """The periods, oldest first. Blocks before the first one use its figures."""
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    periods = [
        CostPeriod(
            starts=_as_date(item["from"]),
            electricity_usd_per_kwh=Decimal(str(item["electricity_usd_per_kwh"])),
            efficiency_joules_per_terahash=Decimal(str(item["efficiency_joules_per_terahash"])),
        )
        for item in data.get("period", [])
    ]
    if not periods:
        raise ValueError(f"{path}: no [[period]] of mining-cost figures")
    for period in periods:
        if period.electricity_usd_per_kwh <= 0 or period.efficiency_joules_per_terahash <= 0:
            raise ValueError(f"{path}: the figures from {period.starts} must be positive")
    periods.sort(key=lambda p: p.starts)
    return periods


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def period_for(periods: list[CostPeriod], day: date) -> CostPeriod:
    chosen = periods[0]
    for period in periods:
        if period.starts <= day:
            chosen = period
    return chosen
