#!/usr/bin/env bash
# Read-only look at what already runs on this server. It changes nothing and prints no passwords
# or tokens (long codes are hidden). Run:  bash deploy/check_server.sh
hide() { sed -E 's/[A-Za-z0-9]{24,}/<hidden>/g'; }
echo "== System"
. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME | $(python3 --version 2>&1) | user $(whoami)"
free -m | awk 'NR==2{print "memory MB: total " $2 ", free " $7} NR==3{print "swap MB: " $2}'
df -h / | awk 'NR==2{print "disk: " $4 " free of " $2}'
echo; echo "== Programs listening on network ports"
(sudo -n ss -ltnpH 2>/dev/null || ss -ltnH 2>/dev/null) | awk '{print $4, $6}' | sort -u
echo; echo "== Web servers"
for s in nginx apache2 caddy; do systemctl is-active -q "$s" 2>/dev/null && echo "$s: running"; done
grep -HnE "^\s*(listen|server_name|proxy_pass|root|auth_basic|ssl_certificate)\b" /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf 2>/dev/null | hide
[ -f /etc/caddy/Caddyfile ] && hide < /etc/caddy/Caddyfile
echo; echo "== Your own background services"
for f in /etc/systemd/system/*.service; do
  [ -f "$f" ] || continue
  echo "$(basename "$f"): $(systemctl is-active "$(basename "$f")" 2>/dev/null)"
  grep -E "^(User|WorkingDirectory|ExecStart|EnvironmentFile)=" "$f" | hide | sed 's/^/    /'
done
command -v docker >/dev/null && { echo; echo "== Docker"; (sudo -n docker ps --format '{{.Names}}  {{.Image}}  {{.Ports}}' 2>/dev/null || echo "(needs sudo)"); }
echo; echo "== Scheduled jobs"; crontab -l 2>/dev/null | grep -v '^#' | hide
echo; echo "== Folders in your home"; ls -d "$HOME"/*/ 2>/dev/null
echo; echo "== Where the Zerodha login may be saved (file names, then code lines)"
SKIP="--exclude-dir=.venv --exclude-dir=venv --exclude-dir=env --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=.cache --exclude-dir=.local --exclude-dir=algo-trading-platform --exclude-dir=site-packages"
grep -rIlE "access_token" "$HOME" $SKIP --include='*.json' --include='*.env' --include='.env' --include='*.txt' --include='*.yaml' --include='*.yml' --include='*.toml' --include='*.ini' 2>/dev/null | head -20
ls "$HOME"/*/*.db "$HOME"/*/*.sqlite* "$HOME"/*/*/*.db 2>/dev/null | head
grep -rnIE "access_token|generate_session|login_url" "$HOME" $SKIP --include='*.py' --include='*.js' --include='*.ts' 2>/dev/null | hide | cut -c1-220 | head -40
