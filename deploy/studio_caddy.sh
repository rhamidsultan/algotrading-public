#!/usr/bin/env bash
# Sends https://<address>/studio/ to Algo Studio (service algolab-studio) in the web server's settings (run by every update).
#
#   sudo bash deploy/studio_caddy.sh [port]        default port 8020
#
# Adds one block to the dashboard's site in /etc/caddy/Caddyfile, before its reverse_proxy line:
#     handle /studio* { reverse_proxy 127.0.0.1:8020 }
# Checks the settings with Caddy and puts the old file back if they are not accepted. Nothing changes if it is there.
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
F="${CADDYFILE:-/etc/caddy/Caddyfile}"
PORT="${1:-8020}"
[ -f "$F" ] || { echo "    (no web server settings at $F; Studio not added)"; exit 0; }
if grep -qE '^[[:space:]]*handle[[:space:]]+/studio\*' "$F"; then
  echo "    Studio already in the web server settings."
  exit 0
fi
STAMP="$(date +%Y%m%d-%H%M%S)"
cp -p "$F" "$F.before-studio-$STAMP"
python3 - "$F" "$PORT" <<'PY' || { echo "!! Could not add Studio to the web server settings"; exit 1; }
import re, sys
path, port = sys.argv[1], sys.argv[2]
lines = open(path).read().split("\n")
depth, site, out, done = 0, False, [], False
for ln in lines:
    s = ln.strip()
    if not done and depth == 0 and not s.startswith("#") and re.match(r"^[A-Za-z0-9.:-]+\s*\{$", s):
        site = True
    if not done and site and depth == 1 and re.match(r"^reverse_proxy\b", s):
        ind = ln[:len(ln) - len(ln.lstrip())]
        out.append(f"{ind}handle /studio* {{")
        out.append(f"{ind}  reverse_proxy 127.0.0.1:{port}")
        out.append(f"{ind}}}")
        done = True
    out.append(ln)
    depth += ln.count("{") - ln.count("}")
if not done:
    sys.exit(1)
open(path, "w").write("\n".join(out))
PY
if command -v caddy >/dev/null && ! caddy validate --config "$F" --adapter caddyfile >/dev/null 2>&1; then
  cp -p "$F.before-studio-$STAMP" "$F"
  echo "!! The web server did not accept the Studio settings; left as they were"
  exit 1
fi
systemctl reload caddy 2>/dev/null || systemctl restart caddy
echo "    Studio added: https://$(cat /opt/algo-trading/address 2>/dev/null || echo '<address>')/studio/"
