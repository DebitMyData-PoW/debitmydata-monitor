#!/usr/bin/env bash
# Lets the pool server's agent in: adds its WireGuard key to this server's tunnel (wg0), and
# turns on the comparison with the pool's node. Run it after pool-agent/install.sh on the pool
# server, with the key that script printed:
#
#   sudo /opt/debitmydata-monitor/add-pool.sh <the pool server's WireGuard public key>
#
# Run again with a new key if the pool agent is installed again (the old key is replaced).
set -euo pipefail
WG=/etc/wireguard/wg0.conf
ENV_FILE=/etc/debitmydata-monitor/monitor.env
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
KEY="${1:-}"
[[ "$KEY" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]] || {
    echo "usage: sudo $0 <the pool server's WireGuard public key (44 characters, ends with =)>" >&2
    exit 1
}
[ -s "$WG" ] || { echo "$WG is missing: run install.sh first" >&2; exit 1; }
# the pool's peer: everything from its marker to the end of the file is replaced
sed -i '/^# The pool server (add-pool.sh)/,$d' "$WG"
cat >>"$WG" <<EOT
# The pool server (add-pool.sh)
[Peer]
PublicKey = $KEY
AllowedIPs = 10.88.0.2/32
EOT
chmod 600 "$WG"
systemctl restart wg-quick@wg0
# Prometheus reads the pool server's figures from now on (it notices this file by itself)
echo '[{"targets": ["10.88.0.2:9100"], "labels": {"server": "pool"}}]' >/etc/prometheus/debitmydata-pool.json
chmod 644 /etc/prometheus/debitmydata-pool.json
sed -i 's/^DMDW_POOL_VIEW=.*/DMDW_POOL_VIEW=yes/' "$ENV_FILE"
grep -q '^DMDW_POOL_VIEW=' "$ENV_FILE" || echo 'DMDW_POOL_VIEW=yes' >>"$ENV_FILE"
systemctl restart dmd-chainwatch
echo "The pool server can connect. Within a minute: sudo wg show (a 'latest handshake'), then"
echo "curl -s 10.88.0.2:9100/metrics | grep ^dmd_pool  (the pool's figures)."
