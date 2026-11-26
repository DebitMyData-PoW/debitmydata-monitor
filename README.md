# DebitMyData monitor

Monitoring and public statistics for the DebitMyData Proof-of-Work chain. It watches the chain
around the clock, alerts the team the moment something looks wrong, and publishes the chain's
figures, charts and API on a public page, **DMD Stats**
([stats.debitmydata.io](https://stats.debitmydata.io)).

<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/stats-devices-dark.webp">
  <img src="docs/images/stats-devices-light.webp" alt="DMD Stats on a laptop and as the app on a phone" width="820">
</picture>
</p>

## What it does

- **Keeps the whole chain.** The chain watch (`chainwatch/`) stores every block since block 1,
  read from the monitor's own DebitMyData node, and works out the hashrate, the difficulty, the
  on-chain volume, every address's balance (its own rich list) and the mining cost of DMD, the
  DebitMyData coin.
- **Compares independent views of the chain**: its own node, the public block explorer, the
  mempool explorer and the mining pool's node. When they disagree, it is a fork, and the team
  knows within minutes.
- **Alerts** the team by email and on a private Discord channel, and the community on its public
  channel, for the events below.
- **DMD Stats** (`web/`), the public page: CoinMarketCap-style charts since block 1 (lines or
  candles, from 15 minutes to everything), the reference price and market cap, the rich list,
  large transfers, who found each block, and an open API. It installs as an app on phones and
  computers.
- **Notifications that each visitor chooses**, on any device, without an account.
- **The team's dashboards** (Grafana, behind a sign-in) and a year of figures for the monitoring
  server and the mining pool's server (Prometheus), sent by the pool server through a private
  WireGuard tunnel (`pool-agent/`).

The monitor holds no keys and no coins. Its node is a third view of the chain, independent of the
pool's and the payment gateway's, and its IP address stays hidden behind Tor.

## What it watches

| Alert | Level | Who gets it |
| :-- | :-- | :-- |
| No new block for 30 minutes (warning), an hour (critical) | warning / critical | team |
| The monitor's node and another view of the chain (the public explorer, the mempool explorer, the pool's node) have **different blocks** at the same height, 2 checks in a row: a fork | critical | team |
| A view is more than 3 blocks behind or ahead | warning | team |
| The chain was **reorganized**: 2 blocks or more replaced (critical from 6, the confirmations DMD payments need) | warning / critical, sent once | team (and the community, if chosen) |
| A competing branch, invalid blocks | warning, sent once | team |
| A miner outside the pool found a quarter of the last 144 blocks | warning | team (and the community, if chosen) |
| One address got half of the pool's payouts in the last 144 blocks | warning | team |
| The hashrate jumped (3 times the day's usual) or dropped (a third), within an hour | sent once per 6 hours | team and community |
| The difficulty moved 50% in a day | sent once a day | team and community |
| One of the rich list's **top 10 addresses** sent or received 1,000 DMD or more in a transaction | sent once | team (and the community, if chosen) |
| The reference price moved 5% or more, or stopped updating | sent once / warning | team and community / team |
| The monitor's node: not on the main chain, still syncing, no peers, not answering | warning / critical | team |
| The servers: disk, memory, restarts; the pool server unreachable, its stratum, node, database or jobs down | warning / critical | team |
| The chain watch or the notifications service stopped; Tor stopped or out of date | critical / warning | team |
| The day's summary, after midnight UTC | | community |

Ongoing alerts are repeated while they last (critical every 2 hours, warning every 12 hours), and
the team is told when they end. Every level and threshold is a setting.

## The stats page's notifications

Anyone can turn them on from the stats page, with the bell in the top bar or the Notifications
card: the browser asks for permission, and the device gets a notice even when the page is closed
(on an iPhone, from the app on the Home Screen). Each device chooses, each with its own figure:

| Group | Choices |
| :-- | :-- |
| The network | every new block; the hashrate, the difficulty or the reference price moving by a chosen percentage; the price reaching levels of one's own; transfers of a chosen size; no new block for a chosen time; the halving; the daily summary |
| The chain's safety | the chain needs attention (blocks stopped, a fork between views) and when it is normal again; reorganizations of a chosen depth; competing branches and invalid blocks; a new miner; one miner finding a chosen share of the blocks |
| Pools and miners | up to 10, by name: each block it finds; its hashrate moving; no block from it for a chosen time |
| Wallet addresses | the rich list's top 10 moving a chosen amount; up to 10 addresses of one's own: every transaction to or from them, mining rewards included |

The monitor keeps each device's choices with its browser's push subscription, and nothing else:
no account, no email, no IP address. Turning notifications off on the device removes both. Every
notice is encrypted for its device.

The same service sends the **DMD Web Wallet's** notifications ([mydmd.io](https://mydmd.io)): when
a wallet's user turns them on, the wallet gives it its wallets' addresses (the ones it used and the
next ones), and each transaction to or from them comes as a notice once it is in a block, even
when the wallet is closed. The wallet writes the notice in its user's language; the monitor keeps
none of them. The sites allowed to do this are in `DMDW_PUSH_ORIGINS`.

## The figures

- **Reference price**: the DebitMyData Agent Gateway's, recorded every 5 minutes since October
  2026: the higher of DebitMyData's minimum rate and the mining cost. **Not a market price**, and
  the page says so. Once DMD trades on an exchange that CoinGecko follows, the page adds the market
  price and the trading volume.
- **Mining cost**: for every block since block 1, the work of a block (averaged over the last week
  of blocks) at the approved machine efficiency and electricity price, divided by the DMD the
  block creates. The approved figures are in `config/mining-cost.toml`, the same as the gateway's.
- **Hashrate**: the work of the blocks found, divided by the time they took. **Difficulty**: the
  chain's own. Their candles show how they moved during each period.
- **Market cap**: the reference price × the DMD mined so far.
- **On-chain volume**: the DMD that transactions sent to other addresses (not change, not mining
  rewards).
- **Rich list**: the chain watch counts every address's DMD itself, from its own node's blocks. It
  needs no explorer, and its balances match the node's own coins.
- **Who found the blocks**: from the text in each block's coinbase, or the address a block paid
  when it has none, named by [DebitMyData's pool list](https://github.com/DebitMyData-PoW/mining-pools).

The page's files are its API, explained at
[stats.debitmydata.io/api/](https://stats.debitmydata.io/api/): the summary, the charts for every
range (points and candles), the miners, the last 1,000 blocks, the transfers, the rich list, daily
and per-block CSV files, and, for exchanges and listing sites, `coin.json` with the circulating and
total supply as plain numbers. Any website may read them.
[DebitMyData Docs](https://docs.debitmydata.io/api/coin-data/) explains them for third parties.

## Security

- **No keys, no coins**: the node has no wallet, and its RPC answers the monitoring server only.
  The chain watch only reads (`chainwatch/src/chainwatch/rpc.py` lists the calls).
- **The node's IP address stays hidden**: it accepts no connections and reaches its peers through
  Tor, which is kept up to date and checked every 30 minutes.
- **Only Cloudflare reaches the web ports.** Prometheus, Grafana and PostgreSQL listen on the
  server only; Grafana reads the chain's history with a read-only role.
- **The pool server opens the tunnel**: nothing opens on it, and its database, keys and settings
  never leave it.
- **Secrets** (email password, webhooks, Grafana's password) stay on the server, never in this
  repository.

## Built with

Python and PostgreSQL (the chain watch), a DebitMyData Core node, nginx, Prometheus, Grafana,
WireGuard and Tor. The page is plain HTML, CSS and JavaScript, with ApexCharts for the charts, in
DebitMyData's Aurora design.

## Installation

To run the monitor on a server of your own, see **[INSTALL.md](INSTALL.md)**: what it needs, the
install, the settings, the commands, updates and development.

## License

MIT. Fonts: SIL Open Font License (`web/fonts/OFL-Sora.txt`, `OFL-Inter.txt`,
`OFL-JetBrainsMono.txt`, `OFL-Mada.txt`). ApexCharts: MIT (`web/vendor/LICENSE-apexcharts.txt`).
