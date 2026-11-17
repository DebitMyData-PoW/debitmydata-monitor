"""The DebitMyData chain's rules, and the mining cost of DMD.

The rules are DebitMyData PoW Core's (chainparams.cpp, GetBlockSubsidy). The mining cost is the
DebitMyData Gateway's formula (pricing/mining_cost.py), so the history here matches what the
gateway records:

    USD per DMD = hashes per block × joules per hash × USD per joule ÷ DMD per block

with the hashes per block averaged over the last week (4,032 blocks) from the chain's total
work, and the DMD per block of the block's own height.
"""

from __future__ import annotations

from decimal import Decimal

BLOCK_SECONDS = 150
DATA_PER_DMD = 100_000_000  # "data": the smallest unit
INITIAL_SUBSIDY_DATA = 35 * DATA_PER_DMD
HALVING_BLOCKS = 300_000
SUBSIDY_CYCLE_BLOCKS = 9_600_000
WINDOW_BLOCKS = 4032  # one week of 150-second blocks
COINBASE_MATURITY = 250

JOULES_PER_KWH = Decimal(3_600_000)
HASHES_PER_TERAHASH = Decimal(10) ** 12
COST_PLACES = Decimal("1e-15")


def subsidy_data(height: int) -> int:
    """New coins in the block at this height, in data (1 DMD = 100,000,000 data)."""
    halvings = (height % SUBSIDY_CYCLE_BLOCKS) // HALVING_BLOCKS
    if halvings >= 64:
        return 0
    return INITIAL_SUBSIDY_DATA >> halvings


def coins_mined_data(height: int) -> int:
    """Coins created by blocks 1 to ``height`` (the genesis block's cannot be spent)."""
    total, start = 0, 1
    while start <= height:
        era_end = min(height, (start // HALVING_BLOCKS + 1) * HALVING_BLOCKS - 1)
        total += subsidy_data(start) * (era_end - start + 1)
        start = era_end + 1
    return total


def mining_cost_usd(
    hashes_per_block: Decimal,
    dmd_per_block: Decimal,
    efficiency_joules_per_terahash: Decimal,
    electricity_usd_per_kwh: Decimal,
) -> Decimal:
    """USD of electricity it takes to mine one DMD."""
    if dmd_per_block <= 0:
        return Decimal(0)
    joules = hashes_per_block / HASHES_PER_TERAHASH * efficiency_joules_per_terahash
    usd_per_block = joules / JOULES_PER_KWH * electricity_usd_per_kwh
    return (usd_per_block / dmd_per_block).quantize(COST_PLACES)


def hashes_per_block(work_now: int, work_before: int, blocks: int) -> Decimal:
    """Average hashes per block between two points of the chain's total work."""
    if blocks <= 0:
        return Decimal(0)
    return Decimal(work_now - work_before) / blocks


def hashrate(work_now: int, work_before: int, seconds: int) -> float:
    """Hashes per second between two points of the chain (work done ÷ time taken)."""
    if seconds <= 0:
        return 0.0
    return (work_now - work_before) / seconds


def difficulty_to_hashes(difficulty: float) -> float:
    return difficulty * 2**32
