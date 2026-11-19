"""DebitMyData's mining pools, as DebitMyData-PoW/mining-pools lists them (pools-v2.json): a block's
pool by the text its coinbase writes (a pool's tags) or the address it pays (a pool's addresses).

The list is the file dmd-pools.timer keeps up to date (DMDW_POOLS_FILE); without it, none: blocks
are named as before (chain.miner_name). When the list changes, `chainwatch relabel` names every
block again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Where dmd-pools.timer keeps the list (DMDW_POOLS_FILE).
DEFAULT_FILE = Path("/var/lib/debitmydata-monitor/pools/pools-v2.json")


@dataclass(frozen=True)
class Pool:
    id: int
    name: str
    slug: str
    tags: tuple[str, ...]
    addresses: frozenset[str]
    link: str


def slug_of(name: str) -> str:
    """A pool's slug: its name's lowercase letters and digits ("Coin-Miners": coinminers), as the
    analytics site writes it and its logo is named."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


@dataclass(frozen=True)
class Pools:
    pools: tuple[Pool, ...] = ()
    digest: str = ""

    @classmethod
    def load(cls, path: Path | None) -> Pools:
        """The list in `path`; none if it's missing or doesn't read as one."""
        if path is None:
            return cls()
        try:
            raw = path.read_bytes()
            entries = json.loads(raw)
        except (OSError, ValueError):
            return cls()
        if not isinstance(entries, list):
            return cls()
        pools: list[Pool] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            # (As the analytics site reads it: a number from 1 to 255, a slug, each once.)
            number, slug = entry.get("id"), slug_of(entry["name"])
            if (
                not isinstance(number, int)
                or not 1 <= number <= 255
                or not slug
                or slug == "unknown"
                or any(p.slug == slug or p.id == number for p in pools)
            ):
                continue
            pools.append(
                Pool(
                    id=number,
                    name=entry["name"],
                    slug=slug,
                    tags=tuple(
                        t.lower() for t in entry.get("tags", []) if isinstance(t, str) and t
                    ),
                    addresses=frozenset(
                        a for a in entry.get("addresses", []) if isinstance(a, str)
                    ),
                    link=entry.get("link", "") if isinstance(entry.get("link"), str) else "",
                )
            )
        pools.sort(key=lambda p: p.id)
        return cls(tuple(pools), hashlib.sha256(raw).hexdigest()[:16])

    def identify(self, tag: str, paid: Mapping[str, int]) -> Pool | None:
        """The pool a block is, as the analytics site says: the first by number whose tag its
        coinbase text holds or whose address it pays."""
        text = tag.lower()
        for pool in self.pools:
            if any(t in text for t in pool.tags) or pool.addresses & paid.keys():
                return pool
        return None

    def by_name(self) -> dict[str, Pool]:
        return {pool.name: pool for pool in self.pools}


_current: tuple[tuple[str, float], Pools] | None = None


def current() -> Pools:
    """The list now: DMDW_POOLS_FILE's (else the default file's), read again when it changes."""
    global _current
    path = Path(os.environ.get("DMDW_POOLS_FILE", "").strip() or DEFAULT_FILE)
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        key = (str(path), 0.0)
    if _current is None or _current[0] != key:
        _current = (key, Pools.load(path) if key[1] else Pools())
    return _current[1]
