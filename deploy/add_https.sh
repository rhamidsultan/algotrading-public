#!/usr/bin/env bash
# Optional: serves the dashboard at https://<your.domain> with a free certificate (Caddy, renewed by itself).
# Needed for the phone app (install, Face ID, notifications) and so passwords never cross the internet in clear.
#
#     sudo bash deploy/add_https.sh dashboard.example.com
#
# First point the name at this server: a DNS "A" record to the server's address (Lightsail > Networking >
# static IP), or a free name from duckdns.org. Then allow TCP 80 and 443 in Lightsail > Networking.
# Safe to run again; it only touches /etc/caddy/Caddyfile, between its own marker lines.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo:  sudo bash deploy/add_https.sh <your.domain>"; exit 1; }
DOMAIN="${1:-}"
echo "$DOMAIN" | grep -qE '^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$' || { echo "Give the web address, e.g.:  sudo bash deploy/add_https.sh dashboard.example.com"; exit 1; }
PORT="$(python3 -c 'import json; print(json.load(open("/opt/trader/config.json")).get("port", 5010))' 2>/dev/null || echo 5010)"
IP="$(curl -s --max-time 5 https://api.ipify.org || true)"
DNS="$(getent ahostsv4 "$DOMAIN" | awk '{print $1; exit}' || true)"
if [ -n "$IP" ] && [ "$DNS" != "$IP" ]; then
  echo "!! $DOMAIN points to '${DNS:-nothing}', not this server ($IP). Fix the DNS record first (it can take a few minutes)."
  exit 1
fi
command -v caddy >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null; }
F=/etc/caddy/Caddyfile
touch "$F"; cp -p "$F" "$F.before-algolab-https"
python3 - "$F" "$DOMAIN" "$PORT" <<'PY'
import re, sys
f, domain, port = sys.argv[1:]
s = open(f).read()
s = re.sub(r"\n?# >>> algolab dashboard.*?# <<< algolab dashboard\n", "\n", s, flags=re.S)
if re.search(r"^:80\s*\{", s, re.M) and "file_server" in s:      # the package's sample site: replaced
    s = re.sub(r"^:80\s*\{.*?^\}\n?", "", s, flags=re.S | re.M)
s = s.rstrip() + f"\n\n# >>> algolab dashboard (deploy/add_https.sh)\n{domain} {{\n    encode gzip\n    reverse_proxy 127.0.0.1:{port}\n}}\n# <<< algolab dashboard\n"
open(f, "w").write(s.lstrip())
PY
if ! caddy validate --config "$F" --adapter caddyfile >/dev/null 2>&1; then
  cp -p "$F.before-algolab-https" "$F"; echo "!! The web server settings did not check out; nothing was changed."; exit 1
fi
ufw allow 80/tcp >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true
systemctl enable -q caddy && systemctl reload caddy 2>/dev/null || systemctl restart caddy
echo "Waiting for the certificate (up to a minute)..."
for _ in $(seq 1 30); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/signin" || true)"
  [ "$CODE" = 200 ] && break; sleep 2
done
if [ "${CODE:-000}" = 200 ]; then
  echo "Done: https://$DOMAIN/"
  echo "In the Kite developer console, change the redirect URL to   https://$DOMAIN/callback"
  echo "You can now remove the Lightsail rule for TCP $PORT (the dashboard is reached through 443)."
else
  echo "Caddy is set, but https://$DOMAIN/ did not answer yet (HTTP ${CODE:-000}). Check Lightsail allows TCP 80 and 443,"
  echo "then:  sudo journalctl -u caddy -n 30"
fi
