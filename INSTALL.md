# Installing the DebitMyData monitor

These steps install the monitor on a server of your own: the chain watch with its own
DebitMyData node, the public stats page and its notifications, Prometheus and Grafana. Below,
`example.com`, `stats.example.com` and `monitor.example.com` stand for your own domain and names.

## What you need

- **A fresh Ubuntu 24.04 server** (x86_64): 2 vCPU, 4 GB of memory, 80 GB of disk, at any
  provider. Best in another data centre than the services it watches (a mining pool, a payment
  gateway), so that one outage can't stop both. Root access with an SSH key.
- **Two names** for the server, one for the public page and one for Grafana (for example
  `stats.example.com` and `monitor.example.com`), as DNS records in **Cloudflare**, **proxied**
  (the orange cloud): the install lets only Cloudflare reach the web ports.
- **A Cloudflare origin certificate** for those names: in Cloudflare, your domain, **SSL/TLS**,
  **Origin Server**, **Create Certificate** (RSA, 15 years). The names Cloudflare suggests,
  `example.com` and `*.example.com`, cover both. In **SSL/TLS**, **Overview**, set the encryption
  mode to **Full (strict)**.
- **DebitMyData Core** 1.3.5 or later, for Linux x86_64 (step 2).

## Install

1. **The server.** Log in as root with your key, then turn off password logins and update it:

   ```
   printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' > /etc/ssh/sshd_config.d/00-keys-only.conf
   sshd -t && systemctl reload ssh
   apt-get update && apt-get -y upgrade && apt-get -y install git
   ```

2. **DebitMyData Core.** Download the Linux release from
   [GitHub](https://github.com/DebitMyData-PoW/DebitMyData-PoW-Core/releases) and check it: the
   SHA-256 that `sha256sum` prints must be the one published for that release. For a newer
   version, change `1.3.5` everywhere below:

   ```
   cd /tmp
   curl -LO https://github.com/DebitMyData-PoW/DebitMyData-PoW-Core/releases/download/v1.3.5/debitmydata-1.3.5-x86_64-linux-gnu.tar.gz
   sha256sum debitmydata-1.3.5-x86_64-linux-gnu.tar.gz
   tar -xzf debitmydata-1.3.5-x86_64-linux-gnu.tar.gz
   install -m 755 debitmydata-1.3.5-x86_64-linux-gnu/debitmydatad debitmydata-1.3.5-x86_64-linux-gnu/debitmydata-cli /usr/local/bin/
   rm -rf debitmydata-1.3.5-x86_64-linux-gnu*
   ```

   Binaries built from [the source](https://github.com/DebitMyData-PoW/DebitMyData-PoW-Core) work
   too: copy `debitmydatad` and `debitmydata-cli` to `/usr/local/bin/`.

3. **The certificate.** Paste the origin certificate, then its private key (in nano: Ctrl+O,
   Enter, Ctrl+X to save). Name both files after your domain:

   ```
   mkdir -p /etc/ssl/cloudflare
   nano /etc/ssl/cloudflare/example.com.pem
   nano /etc/ssl/cloudflare/example.com.key
   chmod 600 /etc/ssl/cloudflare/example.com.key
   ```

4. **This repository, and the install** (about 10 minutes):

   ```
   git clone https://github.com/DebitMyData-PoW/debitmydata-monitor.git /opt/debitmydata-monitor
   /opt/debitmydata-monitor/install.sh
   ```

   It asks three questions: the public page's name (`stats.example.com`), Grafana's name
   (`monitor.example.com`) and the certificate's name from step 3 (`example.com`). Type your own:
   Enter keeps the answer in brackets. At the end, it prints the command that shows Grafana's
   password, and **this server's WireGuard key** (for step 7).

5. **The settings.** Open the settings file, fill in the alerts and the sources, then send a test:

   ```
   nano /etc/debitmydata-monitor/monitor.env
   systemctl restart dmd-chainwatch dmd-notify
   dmd-chainwatch-admin test-alerts
   ```

   - **Email**: your mail server's `DMDW_SMTP_*` settings, and who gets the alerts
     (`DMDW_EMAIL_TO`, and `DMDW_EMAIL_CRITICAL_TO` for critical ones only).
   - **Discord webhooks**: in the channel's settings, **Integrations**, **Webhooks**,
     **New Webhook**, **Copy Webhook URL**. One for the team's private channel
     (`DMDW_DISCORD_WEBHOOK`), one for the community's public channel
     (`DMDW_PUBLIC_DISCORD_WEBHOOK`, optional).
   - `DMDW_DISCORD_MENTION=<@your user ID>`, for critical alerts (Discord: Settings, Advanced,
     Developer Mode, then right-click your name, Copy User ID).
   - **The other views of the chain** it compares its node with, and the price:
     `DMDW_EXPLORER_URL` (an eIquidus explorer), `DMDW_MEMPOOL_URL` (a mempool explorer),
     `DMDW_EXPLORER_TX_URL` (where a transaction's link points), `DMDW_PRICE_URL` (the reference
     price, from a DebitMyData Agent Gateway's `/v1/prices/dmd-usd`). They start on the DebitMyData
     network's public ones; change them to use your own.
   - `DMDW_POOL_ADDRESSES`: your pool's own wallet address (and its fee address if it has one),
     so that they are not counted as miners. `DMDW_POOL_TAG`: the text your pool writes in its
     blocks.

6. **Earlier reference prices** (optional, once). If your reference price comes from a DebitMyData
   Agent Gateway that recorded prices before the monitor existed, export them on the gateway's
   server:

   ```
   sudo -u postgres psql agent_gateway -c "\copy (SELECT observed_at, usd_per_unit, inputs FROM price_records WHERE source = 'dmd_mining_cost' AND asset_symbol = 'DMD' ORDER BY observed_at) TO '/tmp/dmd-price-records.csv' CSV HEADER"
   ```

   Copy `/tmp/dmd-price-records.csv` to the monitoring server (with `scp` or an SFTP client), then:

   ```
   dmd-chainwatch-admin import-prices - < dmd-price-records.csv
   ```

   It prints how many it added; running it again adds nothing twice. Delete the file from the
   gateway's `/tmp` afterwards.

7. **The pool agent** (optional, for a YiiMP mining pool server). On the pool server, as root:

   ```
   git clone https://github.com/DebitMyData-PoW/debitmydata-monitor.git /root/debitmydata-monitor
   bash /root/debitmydata-monitor/pool-agent/install.sh
   ```

   It asks for the monitoring server's IP address and WireGuard key (step 4), finds the pool's
   DebitMyData node, and checks it answers. It prints the pool server's key; on the **monitoring**
   server:

   ```
   /opt/debitmydata-monitor/add-pool.sh <the pool server's key>
   ```

   Within a minute, `wg show` shows a "latest handshake", and Grafana's servers dashboard shows the
   pool server.

8. **Check.** `https://stats.example.com` shows the figures once the node has synced (a few
   minutes to an hour). `https://monitor.example.com` asks for Grafana's sign-in (user `admin`,
   password: `grep GRAFANA_ADMIN /etc/debitmydata-monitor/install.conf`; change it in Grafana:
   your profile, Change password). `dmd-chainwatch-admin status` lists the open alerts.

## Your own page

The page's name, text and images (`web/`), its app manifest (`web/manifest.json`) and the mining
cost's figures (`config/mining-cost.toml`) are DebitMyData's: change them for a page of your own,
then update the server (below). `tools/make-app-icons.py` makes the app's icons again from a new
image.

## Commands

| Command | |
| :-- | :-- |
| `dmd-chainwatch-admin status` | The database's last block, the prices recorded, the open alerts |
| `dmd-chainwatch-admin check` | Runs every check once and prints what it finds (sends nothing) |
| `dmd-chainwatch-admin test-alerts` | A test message to each alert channel |
| `dmd-chainwatch-admin publish` | Writes the stats page's files now |
| `dmd-chainwatch-admin import-prices - < file.csv` | Adds a gateway's prices from its CSV export |
| `dmd-chainwatch-admin recompute-costs` | Works out every block's mining cost again (after new approved figures) |
| `dmd-chainwatch-admin push-keys` | Shows the notifications' public key (makes the key if there is none) |
| `dmd-chainwatch-admin relabel` | Names every block again by the pool list |
| `dmd-cli getblockchaininfo` | The monitor's node (any `debitmydata-cli` command) |
| `journalctl -u dmd-chainwatch -f` | The chain watch's log |
| `journalctl -u dmd-notify -f` | The notifications service's log (devices, notices, refusals) |
| `systemctl status debitmydata dmd-chainwatch dmd-notify prometheus grafana-server nginx` | The services |
| `wg show` | The tunnel to the pool server |
| `sudo dmd-tor-check --status` | Tor: running, its version, connected, the node going through it |
| `journalctl -u dmd-tor-check` | What the Tor check found and repaired |
| `sudo systemctl start dmd-pools` | Reads the pool list and logos from GitHub now (else every day) |

## Settings

`/etc/debitmydata-monitor/monitor.env` (after a change: `systemctl restart dmd-chainwatch dmd-notify`):

| Setting | Default | |
| :-- | :-- | :-- |
| `DMDW_SMTP_*`, `DMDW_EMAIL_TO`, `DMDW_EMAIL_CRITICAL_TO` | | Email alerts |
| `DMDW_DISCORD_WEBHOOK`, `DMDW_DISCORD_MENTION` | | The team's Discord channel |
| `DMDW_PUBLIC_DISCORD_WEBHOOK` | | The community's channel (empty: nothing public) |
| `DMDW_PUBLIC_REORGS`, `DMDW_PUBLIC_CONCENTRATION` | `no` | Also tell the community about reorganizations, and about one miner with too large a share |
| `DMDW_EXPLORER_URL`, `DMDW_MEMPOOL_URL` | the DebitMyData network's | The explorers the node is compared with |
| `DMDW_EXPLORER_TX_URL` | the DebitMyData network's | Where the page's transaction links point |
| `DMDW_PRICE_URL` | the DebitMyData network's | The reference price (a DebitMyData Agent Gateway) |
| `DMDW_SERVER_NAME` | `DebitMyData monitor` | The name the alerts are sent from |
| `DMDW_POOL_ADDRESSES` | | The pool's own addresses |
| `DMDW_POOL_TAG` | `Official` | The text the pool writes in its blocks |
| `DMDW_POOLS_FILE` | `/var/lib/debitmydata-monitor/pools/pools-v2.json` | The pool list, as `dmd-pools.timer` keeps it from GitHub |
| `DMDW_POOL_VIEW` | `no` | Compare with the pool's node (`add-pool.sh` sets it to `yes`) |
| `DMDW_STALL_WARNING_MINUTES`, `DMDW_STALL_CRITICAL_MINUTES` | `30`, `60` | No new block |
| `DMDW_OUTSIDE_MINER_SHARE` | `0.25` | A miner outside the pool, share of the last 144 blocks |
| `DMDW_POOL_PAYEE_SHARE` | `0.5` | One address, share of the pool's payouts (`1` turns it off) |
| `DMDW_HASHRATE_JUMP_FACTOR` | `3` | Hashrate jump or drop |
| `DMDW_DIFFICULTY_CHANGE_SHARE` | `0.5` | Difficulty change in a day (the hour's average against the same hour a day before) |
| `DMDW_PRICE_CHANGE_SHARE` | `0.05` | Reference price change |
| `DMDW_WATCH_RICHLIST` | `10` | How many of the rich list's top addresses the team's alerts watch (`0`: none) |
| `DMDW_RICHLIST_MIN_DMD` | `1000` | From how many DMD in a transaction |
| `DMDW_PUBLIC_RICHLIST` | `no` | Also tell the community |
| `DMDW_LARGE_TRANSFER_DMD` | `1000` | A large transfer, for the page, its API and its notifications |
| `DMDW_COINGECKO_ID`, `DMDW_COINGECKO_KEY` | | The exchanges' figures (below) |
| `DMDW_PUSH_KEY_FILE` | `/var/lib/debitmydata-monitor/push/vapid.pem` | The notifications' key |
| `DMDW_PUSH_MAX_DEVICES` | `20000` | Devices with notifications on, at most |
| `DMDW_PUSH_ORIGINS` | `https://mydmd.io` | Other sites whose pages may turn notifications on here: the DMD Web Wallet, for its wallets' transactions. Commas; empty: none |

**Exchanges.** Once the coin trades on an exchange that CoinGecko follows, put its CoinGecko id
(the "API ID" on its CoinGecko page) in `DMDW_COINGECKO_ID`, and CoinGecko's free Demo key in
`DMDW_COINGECKO_KEY`, then restart the chain watch: the trading volume and the market price come
in every 5 minutes, on the page and in the API.

**The mining cost's approved figures** are in `config/mining-cost.toml`. When they change, add a
period there with the date, commit it, update the server (below), then
`dmd-chainwatch-admin recompute-costs`.

**The notifications' key** (`/var/lib/debitmydata-monitor/push/vapid.pem`, made by the install)
belongs in your backups: a new key means every device has to turn notifications on again.

## The app on Google Play (optional)

The page installs as an app from the browser. If you also publish it on Google Play (a Bubblewrap
app, with a package name like `com.example.stats`):

1. In `web/manifest.json`, add to `related_applications`:
   `{ "platform": "play", "id": "com.example.stats", "url": "https://play.google.com/store/apps/details?id=com.example.stats" }`.
   Chrome then tells the page when the Play app is installed, and the page does not offer the
   browser version.
2. Add `web/.well-known/assetlinks.json` with the package name and the SHA-256 fingerprint of the
   Play app signing key (Play Console, App integrity), as Bubblewrap explains.
3. Update the server.

## Updating

```
cd /opt/debitmydata-monitor && git pull && ./install.sh
```

It keeps the settings, the passwords, the database, the notifications' key and the node's chain.
Settings added by a new version are added at the end of `monitor.env`, with their defaults.

After an update from version 1.0, the chain watch reads the blocks it already had again, newest
first, to count the volume and the balances, between new blocks (`dmd-chainwatch-admin status`
shows how far it is): the volume fills from today backwards, and the rich list starts once every
block is counted.

## Development

```
cd chainwatch
python3 -m venv .venv && .venv/bin/pip install -e '.[test]' ruff
createdb dmdwatch_test   # a PostgreSQL database the tests may empty
TEST_DATABASE_URL=postgresql:///dmdwatch_test .venv/bin/pytest
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
```

With `DMDW_TEST_NODE_BIN=<folder with debitmydatad and debitmydata-cli>`, the tests also run a real
DebitMyData node on regtest (a private test chain). The Grafana dashboards are written by
`config/grafana/make-dashboards.py`.
