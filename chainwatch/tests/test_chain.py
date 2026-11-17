from decimal import Decimal

from chainwatch import chain


def test_subsidy_follows_the_chain_rules() -> None:
    assert chain.subsidy_data(1) == 35 * 100_000_000
    assert chain.subsidy_data(299_999) == 35 * 100_000_000
    assert chain.subsidy_data(300_000) == 1_750_000_000  # 17.5 DMD
    assert chain.subsidy_data(600_000) == 875_000_000
    # the cycle starts again after 9,600,000 blocks
    assert chain.subsidy_data(9_600_000) == 35 * 100_000_000


def test_coins_mined() -> None:
    assert chain.coins_mined_data(0) == 0
    assert chain.coins_mined_data(10) == 10 * 35 * 100_000_000
    assert chain.coins_mined_data(300_000) == (299_999 * 35 + 17.5) * 100_000_000


def test_mining_cost_is_the_gateways_formula() -> None:
    # About 21 TH/s: 21e12 hashes a second for 150 seconds a block.
    hashes = Decimal(21_000_000_000_000) * 150
    cost = chain.mining_cost_usd(hashes, Decimal(35), Decimal(20), Decimal("0.06"))
    # 3,150 TH × 20 J/TH = 63,000 J = 0.0175 kWh = $0.00105 a block, $0.00003 a DMD
    assert cost == Decimal("0.000030000000000")
    assert chain.mining_cost_usd(hashes, Decimal(0), Decimal(20), Decimal("0.06")) == 0


def test_hashrate_from_work() -> None:
    assert chain.hashrate(3000, 1000, 100) == 20
    assert chain.hashrate(3000, 1000, 0) == 0
    assert chain.hashes_per_block(5000, 1000, 4) == 1000
