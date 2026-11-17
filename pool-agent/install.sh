#!/usr/bin/env bash
# The pool agent: lets the DebitMyData monitoring server watch the pool server, through a private
# WireGuard tunnel that this server opens (no port opens here). Run it on the pool server:
#
#   sudo bash install.sh
#
# It asks for the monitoring server's IP and WireGuard key (install.sh printed the key there),
# finds the pool's DebitMyData node, then prints this server's key: give it to the monitoring
# server (sudo /opt/debitmydata-monitor/add-pool.sh <key>). Safe to run again.
#
# What it sets up:
#   wg-quick@dmdmon            the tunnel: this server is 10.88.0.2, the monitor 10.88.0.1
#   prometheus-node-exporter   CPU, memory, disks, network, listening on the tunnel only
#   dmd-pool-metrics.timer     every 30 seconds: the pool's node (height, peers, a block's hash
#                              to compare chains), the stratum's port, MariaDB, the jobs
set -euo pipefail
CONF=/etc/dmd-pool-agent.conf
WG=/etc/wireguard/dmdmon.conf
FIGURES=/var/lib/dmd-pool-agent
HERE="$(cd "$(dirname "$0")" && pwd)"
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
value() { grep -E "^$1=" "$CONF" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true; }
[ "$(id -u)" -eq 0 ] || fail "run as root: sudo bash $0"

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends wireguard-tools prometheus-node-exporter jq >/dev/null

echo "==> The pool's DebitMyData node"
NODE_USER="$(value NODE_USER)"; NODE_DATADIR="$(value NODE_DATADIR)"; NODE_CLI="$(value NODE_CLI)"
NODE_ARGS="$(value NODE_ARGS)"
if [ -z "$NODE_DATADIR" ]; then
    # the node's folder: the one with debitmydata.conf, under the pool's home folders
    NODE_DATADIR="$(find /home /root /var/lib -maxdepth 4 -name debitmydata.conf -printf '%h\n' 2>/dev/null | head -1)"
    read -r -p "The pool node's data folder [$NODE_DATADIR]: " answer
    NODE_DATADIR="${answer:-$NODE_DATADIR}"
fi
[ -d "$NODE_DATADIR" ] || fail "no such folder: $NODE_DATADIR"
NODE_USER="${NODE_USER:-$(stat -c %U "$NODE_DATADIR")}"
NODE_CLI="${NODE_CLI:-$(command -v debitmydata-cli || true)}"
[ -n "$NODE_CLI" ] || read -r -p "Where is debitmydata-cli (full path)? " NODE_CLI
[ -x "$NODE_CLI" ] || fail "debitmydata-cli not found at '$NODE_CLI'"
# shellcheck disable=SC2086  # NODE_ARGS is split into its options on purpose
runuser -u "$NODE_USER" -- "$NODE_CLI" -datadir="$NODE_DATADIR" $NODE_ARGS getblockcount >/dev/null ||
    fail "the node does not answer as $NODE_USER with $NODE_CLI -datadir=$NODE_DATADIR $NODE_ARGS (set NODE_ARGS in $CONF if it needs more options, then run this again)"

STRATUM_PORT="$(value STRATUM_PORT)"
if [ -z "$STRATUM_PORT" ]; then
    read -r -p "The DMD stratum port [8700]: " STRATUM_PORT
    STRATUM_PORT="${STRATUM_PORT:-8700}"
fi
MONITOR_IP="$(value MONITOR_IP)"; MONITOR_KEY="$(value MONITOR_KEY)"
[ -n "$MONITOR_IP" ] || read -r -p "The monitoring server's public IP: " MONITOR_IP
[ -n "$MONITOR_KEY" ] || read -r -p "The monitoring server's WireGuard key (install.sh printed it): " MONITOR_KEY
[[ "$MONITOR_KEY" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]] || fail "that is not a WireGuard key"
cat >"$CONF" <<EOT
# The pool agent's settings (pool-agent/install.sh; run it again after a change).
NODE_USER="$NODE_USER"
NODE_DATADIR="$NODE_DATADIR"
NODE_CLI="$NODE_CLI"
# more options for debitmydata-cli, if the node needs them (for example -conf=/path/to/debitmydata.conf)
NODE_ARGS="$NODE_ARGS"
STRATUM_PORT="$STRATUM_PORT"
MONITOR_IP="$MONITOR_IP"
MONITOR_KEY="$MONITOR_KEY"
EOT
chmod 600 "$CONF"

echo "==> The tunnel to the monitoring server"
if [ ! -s "$WG" ] || ! grep -q '^PrivateKey' "$WG"; then
    umask 077
    printf 'PrivateKey = %s\n' "$(wg genkey)" >"$WG.key"
    umask 022
else
    grep '^PrivateKey' "$WG" >"$WG.key"
fi
{
    echo "# The DebitMyData monitoring server's tunnel (pool-agent/install.sh). This server opens it:"
    echo "# no port is open here for it. Only the monitor's Prometheus uses it (port 9100)."
    echo "[Interface]"
    echo "Address = 10.88.0.2/24"
    cat "$WG.key"
    echo
    echo "[Peer]"
    echo "PublicKey = $MONITOR_KEY"
    echo "Endpoint = $MONITOR_IP:51820"
    echo "AllowedIPs = 10.88.0.1/32"
    echo "PersistentKeepalive = 25"
} >"$WG.new"
rm -f "$WG.key"
chmod 600 "$WG.new"
mv "$WG.new" "$WG"
systemctl enable wg-quick@dmdmon >/dev/null
systemctl restart wg-quick@dmdmon

echo "==> node_exporter, on the tunnel only"
install -d -m 755 "$FIGURES"
echo "ARGS=\"--web.listen-address=10.88.0.2:9100 --collector.textfile.directory=$FIGURES\"" \
    >/etc/default/prometheus-node-exporter
install -d /etc/systemd/system/prometheus-node-exporter.service.d
cat >/etc/systemd/system/prometheus-node-exporter.service.d/dmdmon.conf <<'EOT'
# It listens on the tunnel's address: start after the tunnel, and try again until it is up.
[Unit]
After=wg-quick@dmdmon.service
Wants=wg-quick@dmdmon.service
[Service]
Restart=always
RestartSec=10
EOT
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow in on dmdmon from 10.88.0.1 to any port 9100 proto tcp comment 'DebitMyData monitor' >/dev/null
fi

echo "==> The pool's figures, every 30 seconds"
install -m 755 "$HERE/dmd-pool-metrics" /usr/local/sbin/dmd-pool-metrics
install -m 644 "$HERE/dmd-pool-metrics.service" /etc/systemd/system/dmd-pool-metrics.service
install -m 644 "$HERE/dmd-pool-metrics.timer" /etc/systemd/system/dmd-pool-metrics.timer
systemctl daemon-reload
systemctl enable --now dmd-pool-metrics.timer >/dev/null
systemctl restart prometheus-node-exporter
/usr/local/sbin/dmd-pool-metrics
grep -E '^dmd_pool_(node_up|node_blocks|stratum_listening)' "$FIGURES/pool.prom" | sed 's/^/  /'

cat <<EOT

The pool agent is installed. On the monitoring server, let this server in with its key:

  sudo /opt/debitmydata-monitor/add-pool.sh $(wg show dmdmon public-key)

Then, here, within a minute: sudo wg show dmdmon (a "latest handshake" line).
EOT
