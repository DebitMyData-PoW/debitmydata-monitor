#!/usr/bin/env bash
# The monitoring server's websites (DMD Stats and Grafana) behind Cloudflare (proxied, the orange
# cloud): nginx takes each visitor's real IP from Cloudflare (CF-Connecting-IP, only on requests
# from Cloudflare's addresses), and the firewall lets only Cloudflare's addresses reach ports 80
# and 443, so that nobody can go around Cloudflare to the server's own IP. install.sh runs it.
# Cloudflare rarely changes its addresses (https://www.cloudflare.com/ips/); when it does:
#
#   sudo /opt/debitmydata-monitor/cloudflare.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

# Cloudflare's addresses as published in November 2026, for when the list cannot be downloaded.
CF_PUBLISHED="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18
108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15
104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32
2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32"

echo "Getting Cloudflare's addresses..."
CF_RANGES=$({
    curl -fsS --max-time 20 https://www.cloudflare.com/ips-v4
    echo
    curl -fsS --max-time 20 https://www.cloudflare.com/ips-v6
} 2>/dev/null | grep -E '^[0-9A-Fa-f:.]+/[0-9]+$' || true)
if [ "$(echo "$CF_RANGES" | grep -c /)" -lt 10 ]; then
    echo "Cloudflare's list could not be downloaded: using the one published in November 2026."
    # shellcheck disable=SC2086  # one range per word
    CF_RANGES=$(printf '%s\n' $CF_PUBLISHED)
fi

# nginx: the visitor's real IP, only from Cloudflare's addresses
{
    echo "# Visitors' real IPs from Cloudflare (https://www.cloudflare.com/ips/), $(date -u +%Y-%m-%d)."
    echo "# Written by /opt/debitmydata-monitor/cloudflare.sh: run it again to update it."
    echo "$CF_RANGES" | sed 's/^/set_real_ip_from /; s/$/;/'
    echo "real_ip_header CF-Connecting-IP;"
} >/etc/nginx/conf.d/cloudflare-real-ip.conf
nginx -t -q
systemctl reload nginx 2>/dev/null || true

# The firewall: ports 80 and 443 for Cloudflare's addresses only. The new rules first, then the
# old ones go (addresses Cloudflare no longer lists, and 80 and 443 open to everyone), so the
# websites are never cut off.
if command -v ufw >/dev/null; then
    echo "Letting only Cloudflare reach ports 80 and 443..."
    FW_IPV6=no
    if [ -e /proc/net/if_inet6 ] && grep -q '^IPV6=yes' /etc/default/ufw 2>/dev/null; then
        FW_IPV6=yes
    fi
    for range in $CF_RANGES; do
        [[ "$range" == *:* && "$FW_IPV6" == "no" ]] && continue
        ufw allow proto tcp from "$range" to any port 80,443 comment 'Cloudflare' >/dev/null
    done
    # The numbers of the rules to remove, highest first (each removal renumbers the ones after it)
    ufw status numbered | sed -n \
        -e 's/^\[ *\([0-9]*\)\] 80,443\/tcp  *ALLOW IN  *\([^ ]*\) .*# Cloudflare$/\1 \2/p' \
        -e 's/^\[ *\([0-9]*\)\] \(80\|443\|80,443\)\(\/tcp\)\?\( (v6)\)\?  *ALLOW IN  *Anywhere\( (v6)\)\?.*$/\1 open/p' |
        while read -r number range; do
            if [ "$range" == "open" ] || ! echo "$CF_RANGES" | grep -qxF "$range"; then
                echo "$number"
            fi
        done | sort -rn | while read -r number; do
            ufw --force delete "$number" >/dev/null
        done
    echo "Only Cloudflare reaches ports 80 and 443 ($(echo "$CF_RANGES" | grep -c /) address ranges)."
fi
