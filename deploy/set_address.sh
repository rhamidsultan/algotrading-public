#!/usr/bin/env bash
# Keeps the dashboard's web address right after a move to another server or region (run by every update).
#
#   sudo bash deploy/set_address.sh                    sslip.io name of this server's current IP, if it changed
#   sudo bash deploy/set_address.sh dash.example.com   your own domain (its DNS "A" record must point here first)
#
# The web server (Caddy) serves the dashboard under one name, e.g. 203-0-113-10.sslip.io: sslip.io turns the name
# back into the IP, so a new IP needs a new name. A domain of your own keeps the same name through any move; set
# it once with the second form and later moves only need its DNS record changed. The address in use is kept in
# /opt/algo-trading/address. Caddy gets the new HTTPS certificate by itself. Nothing changes if all is right.
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
F="${CADDYFILE:-/etc/caddy/Caddyfile}"
KEEP="${ADDRESS_FILE:-/opt/algo-trading/address}"
[ -f "$F" ] || { echo "    (no web server settings at $F; address not checked)"; exit 0; }
OLD="$(awk '!/^[[:space:]]*#/ && $1 ~ /^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:[0-9]+)?$/ && $2=="{" {sub(/:[0-9]+$/, "", $1); print $1; exit}' "$F")"
[ -n "$OLD" ] || { echo "    (no web address found in $F; not changed)"; exit 0; }
IP="${PUBLIC_IP:-$(curl -fs --max-time 5 https://checkip.amazonaws.com || curl -fs --max-time 5 https://api.ipify.org || true)}"
IP="$(echo "$IP" | tr -d '[:space:]')"

NEW="${1:-}"
if [ -z "$NEW" ]; then
  case "$OLD" in
    *.sslip.io|*.nip.io)
      echo "$IP" | grep -qE '^[0-9]+(\.[0-9]+){3}$' || { echo "    (could not learn this server's IP; address not checked)"; exit 0; }
      NEW="$(echo "$IP" | tr . -).sslip.io" ;;
    *) NEW="$OLD" ;;                                       # your own domain: it stays; only its DNS record moves
  esac
fi
echo "$NEW" | grep -qE '^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$' || { echo "!! '$NEW' is not a web address"; exit 1; }
if [ "$NEW" = "$OLD" ]; then
  echo "$NEW" > "$KEEP" 2>/dev/null || true
  echo "    Web address: https://$NEW/"
  exit 0
fi
case "$NEW" in
  *.sslip.io|*.nip.io) ;;
  *)
    DNS="$(getent ahostsv4 "$NEW" | awk '{print $1; exit}')"
    if [ -n "$IP" ] && [ "$DNS" != "$IP" ]; then
      echo "!! $NEW points to '${DNS:-nothing}', not this server ($IP). Set its DNS A record to $IP, wait a few minutes,"
      echo "   and run the update again. The dashboard stays at https://$OLD/ meanwhile."
      exit 1
    fi ;;
esac

echo "==> Web address: $OLD -> $NEW"
cp -p "$F" "$F.before-$NEW"
sed -i "s/$(printf '%s' "$OLD" | sed 's/[.[\*^$/]/\\&/g')/$NEW/g" "$F"
if command -v caddy >/dev/null && ! caddy validate --config "$F" --adapter caddyfile >/dev/null 2>&1; then
  cp -p "$F.before-$NEW" "$F"
  echo "!! The web server did not accept the new address; it was left at https://$OLD/"
  exit 1
fi
systemctl reload caddy 2>/dev/null || systemctl restart caddy
echo "$NEW" > "$KEEP" 2>/dev/null || true
for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$NEW/signin" 2>/dev/null)" = 200 ] && break
  sleep 2
done
echo "    Now at https://$NEW/   (the certificate can take a minute the first time)"
echo "    Also change: the Kite app's redirect URL to https://$NEW/callback, and the dashboard app on your phone"
echo "    (add it again from the new address; set up Face ID and notifications again)."
