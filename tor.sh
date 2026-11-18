#!/usr/bin/env bash
# Tor for this server's DebitMyData node. The websites already hide this server's IP address
# behind Cloudflare; the node's own connections to its peers would still show it to each of them
# (and the network's explorers list the peers they see). With Tor, the node reaches its peers
# through the Tor network, and they only see a Tor exit's address.
#
#   sudo /opt/debitmydata-monitor/tor.sh      (install.sh runs it; it can run alone, and again)
#
# What it sets up:
#   Tor                     from the Tor Project's own package repository (its key checked by
#                           fingerprint): Ubuntu's own Tor package lags behind, and the Tor
#                           network refuses a Tor that has become too old
#   unattended-upgrades     installs each new Tor version within a day, and restarts Tor
#   dmd-tor-check.timer     every 30 minutes: Tor running, connected, its version still
#                           recommended (if not: upgraded at once), the node going through it;
#                           what it finds goes to its log, and problems to Discord (dmd-tor-check)
#   the node                proxy=127.0.0.1:9050 in its settings, and it starts after Tor
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK="$HERE/bin/dmd-tor-check"
UNITS="$HERE/config/systemd"
SERVER_NAME="the monitoring server"
DMD_CONF=/etc/debitmydata/debitmydata.conf
KEY_FPR=A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89
KEYRING=/usr/share/keyrings/deb.torproject.org-keyring.gpg
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1

arch="$(dpkg --print-architecture)"
case "$arch" in
    amd64 | arm64) ;;
    *) echo "The Tor Project publishes Tor for amd64 and arm64 servers, not $arch." >&2; exit 1 ;;
esac
codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"

echo "Tor, from the Tor Project's repository..."
apt-get install -y -q --no-install-recommends curl gnupg ca-certificates python3 unattended-upgrades >/dev/null
# The Tor Project's signing key, trusted only if it is the one with the published fingerprint
has_key() { gpg --show-keys --with-colons "$1" 2>/dev/null | grep -q "^fpr:::::::::$KEY_FPR:"; }
if [ ! -s "$KEYRING" ] || ! has_key "$KEYRING"; then
    key="$(mktemp)"
    curl -fsSL "https://deb.torproject.org/torproject.org/$KEY_FPR.asc" -o "$key"
    has_key "$key" || { rm -f "$key"; echo "The Tor Project's key does not have the expected fingerprint: stopped." >&2; exit 1; }
    gpg --dearmor <"$key" >"$KEYRING.new"
    chmod 644 "$KEYRING.new"
    mv "$KEYRING.new" "$KEYRING"
    rm -f "$key"
fi
cat >/etc/apt/sources.list.d/tor.sources <<EOF
# Tor from the Tor Project's own repository (tor.sh)
Types: deb
URIs: https://deb.torproject.org/torproject.org/
Suites: $codename
Components: main
Architectures: $arch
Signed-By: $KEYRING
EOF
# Tor from there only, never Ubuntu's own (older) package
cat >/etc/apt/preferences.d/tor <<'EOF'
Package: tor tor-geoipdb deb.torproject.org-keyring
Pin: origin "deb.torproject.org"
Pin-Priority: 900
EOF
apt-get update -q
apt-get install -y -q tor deb.torproject.org-keyring >/dev/null
systemctl enable --now tor >/dev/null

# Each new version within a day: unattended-upgrades, which Ubuntu already runs every day for
# its security updates, also takes Tor's from the Tor Project
cat >/etc/apt/apt.conf.d/52debitmydata-tor <<'EOF'
// Tor's new versions, from the Tor Project's repository, every day (tor.sh): the Tor network
// refuses a Tor that has become too old, and the DebitMyData node would lose its peers.
Unattended-Upgrade::Origins-Pattern { "origin=TorProject"; };
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true

# The check, every 30 minutes
install -m 755 "$CHECK" /usr/local/sbin/dmd-tor-check
if [ ! -f /etc/default/dmd-tor-check ]; then
    cat >/etc/default/dmd-tor-check <<EOF
# dmd-tor-check's settings (tor.sh). Its problems go to the log (journalctl -u dmd-tor-check),
# and to Discord with a webhook: here, or the alerts' DMDW_DISCORD_WEBHOOK on the monitoring server.
DMD_SERVER_NAME="$SERVER_NAME"
DMD_TOR_WEBHOOK=
EOF
    chmod 600 /etc/default/dmd-tor-check
fi
install -m 644 "$UNITS/dmd-tor-check.service" "$UNITS/dmd-tor-check.timer" /etc/systemd/system/

# The node: through Tor, started after it
changed=0
if [ -f "$DMD_CONF" ] && ! grep -qE '^proxy=' "$DMD_CONF"; then
    printf '%s\n' "" "# Peers are reached through Tor, so that none of them sees this server's IP address (tor.sh)." \
        "proxy=127.0.0.1:9050" >>"$DMD_CONF"
    changed=1
fi
install -d -m 755 /etc/systemd/system/debitmydata.service.d
cat >/etc/systemd/system/debitmydata.service.d/tor.conf <<'EOF'
# The node reaches its peers through Tor (tor.sh): it starts after Tor.
[Unit]
Wants=tor.service
After=tor.service
EOF
systemctl daemon-reload
systemctl enable --now dmd-tor-check.timer >/dev/null
if [ "$changed" = 1 ] && systemctl is-active --quiet debitmydata; then
    systemctl restart debitmydata
    echo "The node now goes through Tor (restarted)."
fi

# Tor needs a few seconds to connect the first time
for _ in $(seq 1 30); do
    /usr/local/sbin/dmd-tor-check --status | grep -q "^Tor: .*, connected" && break
    sleep 2
done
/usr/local/sbin/dmd-tor-check --status
