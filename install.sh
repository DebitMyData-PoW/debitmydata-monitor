#!/usr/bin/env bash
# The DebitMyData monitoring server: installs everything, or updates it when run again.
#
#   sudo /opt/debitmydata-monitor/install.sh
#
# Before the first run (README, "Install"): a fresh Ubuntu 24.04 server; this repository at
# /opt/debitmydata-monitor; the DebitMyData Core binaries (debitmydatad, debitmydata-cli) in
# /usr/local/bin; the Cloudflare origin certificate in /etc/ssl/cloudflare/.
#
# What it sets up:
#   debitmydata.service        this server's own DebitMyData node (main chain, no wallet,
#                              outgoing connections only, through Tor; RPC on 127.0.0.1)
#   Tor                        tor.sh: from the Tor Project's repository, kept up to date by
#                              unattended-upgrades, checked every 30 minutes (dmd-tor-check.timer)
#   PostgreSQL                 the chain watch's database (dmdwatch), Grafana's read-only role
#   dmd-chainwatch.service     the chain watch (chainwatch/): blocks, alerts, the stats files
#   dmd-notify.service         the stats page's notifications (/push, and the notices)
#   dmd-watchdog.timer         checks the chain watch every 5 minutes
#   Prometheus, node_exporter  this server's and the pool server's figures, a year of them
#   Grafana                    the team's dashboards, on the Grafana address (its own sign-in)
#   nginx                      DMD Stats (web/) and Grafana, behind Cloudflare
#   WireGuard (wg0)            the private tunnel the pool server's agent comes in through
#   ufw                        SSH, WireGuard, and ports 80 and 443 for Cloudflare only
#
# It keeps the settings, the passwords, the database and the node's chain. Settings that need
# you (alerts: email and Discord) are in /etc/debitmydata-monitor/monitor.env.
set -euo pipefail
# A command that fails stops the install: say where, rather than stopping without a word.
trap 'printf "\nERROR: the install stopped at line %s: %s\nRun it again once that is fixed: it carries on with what is already done.\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

APP=/opt/debitmydata-monitor
CONF=/etc/debitmydata-monitor
ENV_FILE=$CONF/monitor.env
INSTALL_CONF=$CONF/install.conf
DATA=/var/lib/debitmydata-monitor
VENV=$APP/.venv
DMD_DATA=/var/lib/debitmydata
DMD_CONF=/etc/debitmydata/debitmydata.conf
WEB=/var/www/debitmydata-stats
WG=/etc/wireguard/wg0.conf

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
setting() { grep -E "^$1=" "$INSTALL_CONF" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
ask() {  # ask NAME "question" default: the answer kept in install.conf, asked only once
    local name="$1" question="$2" default="$3" value
    value="$(setting "$name")"
    if [ -z "$value" ]; then
        if [ -t 0 ]; then
            read -r -p "$question [$default]: " value
        fi
        value="${value:-$default}"
        echo "$name=$value" >>"$INSTALL_CONF"
    fi
    printf -v "$name" '%s' "$value"
}

# ------------------------------------------------------------------ checks
step "Checking"
[ "$(id -u)" -eq 0 ] || fail "run as root: sudo $0"
grep -q 'VERSION_ID="24.04"' /etc/os-release || fail "this script is for Ubuntu 24.04"
[ "$(cd "$(dirname "$0")" && pwd)" = "$APP" ] || fail "the repository must be at $APP (git clone ... $APP)"
for bin in debitmydatad debitmydata-cli; do
    [ -x /usr/local/bin/$bin ] || fail "/usr/local/bin/$bin is missing: copy the DebitMyData Core binaries there first (README, step 2)"
done
install -d -m 750 "$CONF"
touch "$INSTALL_CONF"
chmod 600 "$INSTALL_CONF"

ask STATS_HOST "The public stats page's address" "stats.debitmydata.io"
ask GRAFANA_HOST "Grafana's address (the team's dashboards)" "monitor.debitmydata.io"
ask CERT_NAME "The Cloudflare origin certificate's name in /etc/ssl/cloudflare/" "debitmydata.io"
CERT=/etc/ssl/cloudflare/$CERT_NAME.pem
CERT_KEY=/etc/ssl/cloudflare/$CERT_NAME.key
[ -s "$CERT" ] && [ -s "$CERT_KEY" ] ||
    fail "the Cloudflare origin certificate is missing: save it as $CERT and its private key as $CERT_KEY (README, step 3)"
openssl x509 -in "$CERT" -noout -checkhost "$STATS_HOST" | grep -q "does match" ||
    fail "$CERT does not cover $STATS_HOST: make one for *.$(echo "$STATS_HOST" | cut -d. -f2-) in Cloudflare"
openssl x509 -in "$CERT" -noout -checkhost "$GRAFANA_HOST" | grep -q "does match" ||
    fail "$CERT does not cover $GRAFANA_HOST"
chmod 644 "$CERT"
chmod 600 "$CERT_KEY"

# ------------------------------------------------------------------ packages
step "Installing packages"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
apt-get update -q
apt-get install -y -q --no-install-recommends nginx postgresql python3-venv ufw jq curl librsvg2-bin webp \
    gnupg ca-certificates wireguard-tools prometheus prometheus-node-exporter \
    libevent-core-2.1-7t64 libevent-extra-2.1-7t64 libevent-pthreads-2.1-7t64 libsqlite3-0 libzmq5 >/dev/null
missing="$(ldd /usr/local/bin/debitmydatad | grep 'not found' || true)"
[ -z "$missing" ] || fail "debitmydatad needs libraries that are not installed: $missing"
# Grafana, from Grafana's own package repository (signed)
if [ ! -s /etc/apt/keyrings/grafana.gpg ]; then
    install -d -m 755 /etc/apt/keyrings
    curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
    chmod 644 /etc/apt/keyrings/grafana.gpg
fi
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
    >/etc/apt/sources.list.d/grafana.list
apt-get update -q
apt-get install -y -q grafana >/dev/null

# ------------------------------------------------------------------ users and folders
step "Users and folders"
id dmd >/dev/null 2>&1 || useradd --system --home-dir "$DMD_DATA" --shell /usr/sbin/nologin dmd
id dmdwatch >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin dmdwatch
usermod -aG dmd dmdwatch  # reads the node's cookie (the node makes it readable by its group)
install -d -m 750 -o dmd -g dmd "$DMD_DATA"
install -d -m 750 -o root -g dmd /etc/debitmydata
install -d -m 755 -o dmdwatch -g dmdwatch "$DATA" "$DATA/public" "$DATA/public/api" "$DATA/metrics"

# ------------------------------------------------------------------ Tor, for the node
step "Tor (the node reaches its peers through it, hiding this server's IP address)"
"$APP/tor.sh"

# ------------------------------------------------------------------ the DebitMyData node
step "The DebitMyData node (main chain)"
if [ ! -s "$DMD_CONF" ]; then
    cat >"$DMD_CONF" <<EOF
# The monitoring server's own DebitMyData node: the main chain, no wallet, RPC on this server
# only, outgoing connections only. The chain watch signs in with the cookie (group-readable).
# Peers are reached through Tor, so that none of them sees this server's IP address (tor.sh).
proxy=127.0.0.1:9050
server=1
disablewallet=1
listen=0
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=18557
rpccookieperms=group
dbcache=300
# The network's seed nodes, as in DebitMyData Core's own default settings.
seednode=dnsseed.debitmydata.network
seednode=dnsseed2.debitmydata.network
$(for n in $(seq 1 10); do printf 'addnode=seed%s.debitmydata.network\n' "$n"; done)
EOF
fi
chown root:dmd "$DMD_CONF"
chmod 640 "$DMD_CONF"
grep -qE '^(regtest|testnet|signet)=1' "$DMD_CONF" && fail "$DMD_CONF is not for the main chain"
cat >/usr/local/bin/dmd-cli <<'EOF'
#!/bin/sh
# debitmydata-cli for this server's node, as its service user (cookie sign-in).
exec runuser -u dmd -- /usr/local/bin/debitmydata-cli -conf=/etc/debitmydata/debitmydata.conf -datadir=/var/lib/debitmydata "$@"
EOF
chmod 755 /usr/local/bin/dmd-cli
install -m 644 "$APP/config/systemd/debitmydata.service" /etc/systemd/system/debitmydata.service
systemctl daemon-reload
systemctl enable --now debitmydata >/dev/null
systemctl restart debitmydata

# ------------------------------------------------------------------ the database
step "The database"
systemctl enable --now postgresql >/dev/null
GRAFANA_DB_PASSWORD="$(setting GRAFANA_DB_PASSWORD)"
if [ -z "$GRAFANA_DB_PASSWORD" ]; then
    GRAFANA_DB_PASSWORD="$(secret)"
    echo "GRAFANA_DB_PASSWORD=$GRAFANA_DB_PASSWORD" >>"$INSTALL_CONF"
fi
runuser -u postgres -- psql -q -v ON_ERROR_STOP=1 -v pw="$GRAFANA_DB_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE dmdwatch LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dmdwatch') \gexec
SELECT 'CREATE DATABASE dmdwatch OWNER dmdwatch' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'dmdwatch') \gexec
SELECT 'CREATE ROLE grafana_ro LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') \gexec
ALTER ROLE grafana_ro PASSWORD :'pw';
SQL

# ------------------------------------------------------------------ the chain watch
step "The chain watch"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip >/dev/null
"$VENV/bin/pip" install -q --upgrade "$APP/chainwatch" >/dev/null
# the repository's code even when its version number did not change
"$VENV/bin/pip" install -q --force-reinstall --no-deps "$APP/chainwatch" >/dev/null
if [ ! -s "$ENV_FILE" ]; then
    cat >"$ENV_FILE" <<EOF
# The DebitMyData chain watch's settings (systemd reads them: NAME=value, no quotes).
# After a change: sudo systemctl restart dmd-chainwatch, then sudo dmd-chainwatch-admin test-alerts

# Alerts by email (SMTP with STARTTLS on 587, or TLS on 465). Several addresses: commas.
DMDW_SMTP_HOST=
DMDW_SMTP_PORT=587
DMDW_SMTP_USER=
DMDW_SMTP_PASSWORD=
DMDW_SMTP_FROM=
DMDW_EMAIL_TO=
# Critical alerts also go to these
DMDW_EMAIL_CRITICAL_TO=

# Alerts in the team's private Discord channel (a webhook URL), and who to mention on critical
# ones: <@your Discord user ID>
DMDW_DISCORD_WEBHOOK=
DMDW_DISCORD_MENTION=

# The public Discord channel for the DMD community: the daily summary, hashrate jumps,
# difficulty changes, reference price changes. Empty: nothing public is posted.
DMDW_PUBLIC_DISCORD_WEBHOOK=
# Also post reorganizations, and miners with too large a share, publicly (yes/no)
DMDW_PUBLIC_REORGS=no
DMDW_PUBLIC_CONCENTRATION=no

# The pool's own addresses (its wallet, its fee address): not counted as miners. Commas.
DMDW_POOL_ADDRESSES=
# The text the pool writes in its blocks' coinbase
DMDW_POOL_TAG=Official
# Compare with the pool's node through the pool agent (no until the agent is set up)
DMDW_POOL_VIEW=no

# Where things are (normally left as they are)
DMDW_DATABASE_URL=postgresql:///dmdwatch?host=/var/run/postgresql
DMDW_RPC_URL=http://127.0.0.1:18557
DMDW_RPC_COOKIE=$DMD_DATA/.cookie
DMDW_EXPLORER_URL=https://explorer.debitmydata.io
DMDW_MEMPOOL_URL=https://mempool.debitmydata.io
DMDW_PRICE_URL=https://api.debitmydata.io/v1/prices/dmd-usd
DMDW_PROMETHEUS_URL=http://127.0.0.1:9090
DMDW_PUBLIC_DIR=$DATA/public
DMDW_METRICS_FILE=$DATA/metrics/chainwatch.prom
DMDW_MINING_COST_FILE=$APP/config/mining-cost.toml
DMDW_SITE_URL=https://$STATS_HOST
DMDW_GRAFANA_URL=https://$GRAFANA_HOST
DMDW_SERVER_NAME=DebitMyData monitor
EOF
fi
# Settings added by later versions, for a server installed before them: added once, at the end
add_setting() {  # add_setting NAME value "comment"
    grep -qE "^#? ?$1=" "$ENV_FILE" || printf '\n# %s\n%s=%s\n' "$3" "$1" "$2" >>"$ENV_FILE"
}
add_setting DMDW_WATCH_RICHLIST 10 \
    "Alerts when one of the rich list's top addresses sends or receives a lot (how many of the top: 0 = off)"
add_setting DMDW_RICHLIST_MIN_DMD 1000 "From how many DMD in one transaction"
add_setting DMDW_PUBLIC_RICHLIST no "Also post those in the public Discord channel (yes/no)"
add_setting DMDW_LARGE_TRANSFER_DMD 1000 \
    "Transactions of this many DMD or more are large transfers (the stats page and its notifications)"
add_setting DMDW_COINGECKO_ID "" \
    "Once DMD trades on an exchange: its id on CoinGecko (the trading volume and market price)"
add_setting DMDW_COINGECKO_KEY "" "CoinGecko's free Demo API key, if you have one"
add_setting DMDW_PUSH_ORIGINS https://mydmd.io \
    "Other sites whose pages may turn notifications on here (the DMD Web Wallet). Commas; empty: none"
chown root:dmdwatch "$ENV_FILE"
chmod 640 "$ENV_FILE"
# The chain watch's tables, and Grafana's read access to the history (not to the alerts)
(cd / && runuser -u dmdwatch -- "$VENV/bin/python" -I -c \
    'from chainwatch import db; db.create_schema(db.connect("postgresql:///dmdwatch?host=/var/run/postgresql"))')
runuser -u postgres -- psql -q -v ON_ERROR_STOP=1 dmdwatch <<'SQL'
GRANT CONNECT ON DATABASE dmdwatch TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON blocks, prices, reorgs, transfers, market TO grafana_ro;
SQL
# The notifications' key (made once: a new key would make every device turn them on again)
install -d -m 700 -o dmdwatch -g dmdwatch "$DATA/push"
(cd / && runuser -u dmdwatch -- "$VENV/bin/dmd-chainwatch" push-keys >/dev/null)
# Commands: sudo dmd-chainwatch-admin status|sync|check|publish|import-prices|recompute-costs|test-alerts|push-keys
cat >/usr/local/sbin/dmd-chainwatch-admin <<'EOF'
#!/bin/sh
# The chain watch's commands, with the service's settings and user. A file to import is read
# from standard input: sudo dmd-chainwatch-admin import-prices - < dmd-price-records.csv
exec systemd-run --quiet --pipe --wait --collect --uid=dmdwatch --gid=dmdwatch \
    -p EnvironmentFile=/etc/debitmydata-monitor/monitor.env \
    /opt/debitmydata-monitor/.venv/bin/dmd-chainwatch "$@"
EOF
chmod 755 /usr/local/sbin/dmd-chainwatch-admin
chmod 755 "$APP/bin/dmd-watchdog"
for unit in dmd-chainwatch.service dmd-notify.service dmd-watchdog.service dmd-watchdog.timer dmd-pools.service dmd-pools.timer; do
    install -m 644 "$APP/config/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable dmd-chainwatch dmd-notify dmd-watchdog.timer dmd-pools.timer >/dev/null
systemctl restart dmd-chainwatch dmd-notify
systemctl start dmd-watchdog.timer dmd-pools.timer
# DebitMyData's pools and their logos from GitHub now, then every day (bin/dmd-pools)
"$APP/bin/dmd-pools" || echo "The pools couldn't be read from GitHub now: dmd-pools.timer tries again."

# ------------------------------------------------------------------ Prometheus
step "Prometheus and node_exporter"
install -m 644 "$APP/config/prometheus/prometheus.yml" /etc/prometheus/prometheus.yml
install -m 644 "$APP/config/prometheus/debitmydata-rules.yml" /etc/prometheus/debitmydata-rules.yml
# The pool server's address, once add-pool.sh has let it in (until then, nothing to reach)
[ -s /etc/prometheus/debitmydata-pool.json ] || echo '[]' >/etc/prometheus/debitmydata-pool.json
chmod 644 /etc/prometheus/debitmydata-pool.json
promtool check config /etc/prometheus/prometheus.yml >/dev/null
echo 'ARGS="--web.listen-address=127.0.0.1:9090 --storage.tsdb.retention.time=400d"' >/etc/default/prometheus
echo "ARGS=\"--web.listen-address=127.0.0.1:9100 --collector.textfile.directory=$DATA/metrics\"" \
    >/etc/default/prometheus-node-exporter
systemctl enable prometheus prometheus-node-exporter >/dev/null
systemctl restart prometheus prometheus-node-exporter

# ------------------------------------------------------------------ Grafana
step "Grafana"
GRAFANA_ADMIN_PASSWORD="$(setting GRAFANA_ADMIN_PASSWORD)"
if [ -z "$GRAFANA_ADMIN_PASSWORD" ]; then
    GRAFANA_ADMIN_PASSWORD="$(secret)"
    echo "GRAFANA_ADMIN_PASSWORD=$GRAFANA_ADMIN_PASSWORD" >>"$INSTALL_CONF"
fi
cat >/etc/default/grafana-server <<EOF
GRAFANA_USER=grafana
GRAFANA_GROUP=grafana
GRAFANA_HOME=/usr/share/grafana
LOG_DIR=/var/log/grafana
DATA_DIR=/var/lib/grafana
MAX_OPEN_FILES=10000
CONF_DIR=/etc/grafana
CONF_FILE=/etc/grafana/grafana.ini
RESTART_ON_UPGRADE=true
PLUGINS_DIR=/var/lib/grafana/plugins
PROVISIONING_CFG_DIR=/etc/grafana/provisioning
PID_FILE_DIR=/run/grafana
# Behind nginx, on $GRAFANA_HOST only; no sign-up, no anonymous access, nothing sent out
GF_SERVER_HTTP_ADDR=127.0.0.1
GF_SERVER_DOMAIN=$GRAFANA_HOST
GF_SERVER_ROOT_URL=https://$GRAFANA_HOST/
GF_SECURITY_ADMIN_USER=admin
GF_SECURITY_ADMIN_PASSWORD=$GRAFANA_ADMIN_PASSWORD
GF_SECURITY_COOKIE_SECURE=true
GF_SECURITY_DISABLE_GRAVATAR=true
GF_USERS_ALLOW_SIGN_UP=false
GF_AUTH_ANONYMOUS_ENABLED=false
GF_ANALYTICS_REPORTING_ENABLED=false
GF_ANALYTICS_CHECK_FOR_UPDATES=false
GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false
GF_NEWS_NEWS_FEED_ENABLED=false
GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/debitmydata/chain.json
EOF
chmod 640 /etc/default/grafana-server
chown root:grafana /etc/default/grafana-server
install -d -m 755 -o grafana -g grafana /var/lib/grafana/dashboards/debitmydata
install -m 644 -o grafana -g grafana "$APP"/config/grafana/dashboards/*.json /var/lib/grafana/dashboards/debitmydata/
install -m 644 "$APP/config/grafana/provisioning/dashboards/debitmydata.yaml" \
    /etc/grafana/provisioning/dashboards/debitmydata.yaml
sed "s#@GRAFANA_DB_PASSWORD@#$GRAFANA_DB_PASSWORD#" \
    "$APP/config/grafana/provisioning/datasources/debitmydata.yaml.template" \
    >/etc/grafana/provisioning/datasources/debitmydata.yaml
chown root:grafana /etc/grafana/provisioning/datasources/debitmydata.yaml
chmod 640 /etc/grafana/provisioning/datasources/debitmydata.yaml
systemctl enable grafana-server >/dev/null
systemctl restart grafana-server

# ------------------------------------------------------------------ the stats page and nginx
step "DMD Stats and nginx"
install -d -m 755 "$WEB"
cp -r "$APP/web/." "$WEB/"
find "$WEB" -type d -exec chmod 755 {} + && find "$WEB" -type f -exec chmod 644 {} +
install -m 644 "$APP/config/nginx/headers.conf" /etc/nginx/snippets/debitmydata-headers.conf
install -m 644 "$APP/config/nginx/stats-csp.conf" /etc/nginx/snippets/debitmydata-stats-csp.conf
sed -e "s#@STATS_HOST@#$STATS_HOST#g" -e "s#@GRAFANA_HOST@#$GRAFANA_HOST#g" \
    -e "s#@CERT@#$CERT#g" -e "s#@KEY@#$CERT_KEY#g" \
    "$APP/config/nginx/sites.conf.template" >/etc/nginx/sites-available/debitmydata-monitor
if [ ! -e /proc/net/if_inet6 ]; then
    sed -i '/listen \[::\]/d' /etc/nginx/sites-available/debitmydata-monitor
fi
ln -sf /etc/nginx/sites-available/debitmydata-monitor /etc/nginx/sites-enabled/debitmydata-monitor
rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl enable nginx >/dev/null
systemctl reload nginx || systemctl restart nginx

# ------------------------------------------------------------------ WireGuard (the pool's tunnel)
step "WireGuard (the pool server's private tunnel)"
if [ ! -s "$WG" ]; then
    umask 077
    key="$(wg genkey)"
    cat >"$WG" <<EOF
# The private tunnel the pool server's agent connects through (pool-agent/). This server is
# 10.88.0.1; the pool server is 10.88.0.2 (added by add-pool.sh). Only Prometheus uses it.
[Interface]
Address = 10.88.0.1/24
ListenPort = 51820
PrivateKey = $key
EOF
    umask 022
fi
chmod 600 "$WG"
systemctl enable wg-quick@wg0 >/dev/null
systemctl restart wg-quick@wg0

# ------------------------------------------------------------------ the firewall
step "The firewall"
# The SSH port to keep open: the one this session came in on (Ubuntu 24.04 can listen through
# ssh.socket, which sshd -T does not show, and sshd -T can fail), else sshd's setting, else 22.
SSH_PORT="${SSH_CONNECTION##* }"
if ! [[ "$SSH_PORT" =~ ^[0-9]+$ ]]; then
    SSH_PORT="$( (sshd -T 2>/dev/null || true) | awk '/^port /{print $2; exit}')"
fi
ufw allow "${SSH_PORT:-22}/tcp" comment 'SSH' >/dev/null
ufw allow 51820/udp comment 'WireGuard (pool agent)' >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw --force enable >/dev/null
"$APP/cloudflare.sh"

# ------------------------------------------------------------------ done
step "Checking"
for unit in tor@default debitmydata postgresql dmd-chainwatch dmd-notify prometheus prometheus-node-exporter grafana-server nginx wg-quick@wg0 dmd-tor-check.timer; do
    printf '  %-26s %s\n' "$unit" "$(systemctl is-active "$unit" || true)"
done
SYNC="$(dmd-cli getblockchaininfo 2>/dev/null | jq -r '"block \(.blocks) of \(.headers)"' || echo "starting")"
cat <<EOF

The monitoring server is installed.

  DebitMyData node:  $SYNC (the first sync takes a while; the chain watch follows it)
  DMD Stats:         https://$STATS_HOST
  Grafana:           https://$GRAFANA_HOST  (user admin; its password: sudo grep GRAFANA_ADMIN $INSTALL_CONF)
  This server's WireGuard key, for the pool agent:  $(wg show wg0 public-key)

Next (README):
  1. The alerts: sudo nano $ENV_FILE, then sudo systemctl restart dmd-chainwatch
     and sudo dmd-chainwatch-admin test-alerts
  2. The gateway's earlier prices: sudo dmd-chainwatch-admin import-prices - < dmd-price-records.csv
  3. The pool agent, on the pool server (pool-agent/README part), then here:
     sudo $APP/add-pool.sh <the pool server's key>
EOF
