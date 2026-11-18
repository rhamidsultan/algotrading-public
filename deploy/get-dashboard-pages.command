#!/bin/bash
# Double-click to copy the portfolio dashboard's page code from the server to this Mac (read-only: nothing on the
# server changes). The server's copy (/opt/trader) was edited there over time, so this is how GitHub learns what
# is really live before those pages are restyled and shipped by the normal server update.
#
# Copies: the app's .py files, templates/ and static/. Never copies settings or secrets (config.json, token.json,
# .secret_key, databases, backups, the venv). Lands in ~/AI - VS/server-copy/<date-time>/ (Claude can read it there).
cd "$(dirname "$0")/.." || exit 1
. deploy/server_login.sh                 # which server and key (~/.algo-trading/server.env)
KEY="$ALGO_KEY"
HOST="$ALGO_HOST"
REMOTE="${ALGO_TRADER:-/opt/trader}"
OUT="${ALGO_COPY_TO:-$HOME/AI - VS/server-copy}/$(date +%Y%m%d-%H%M%S)"
if [ -n "$ALGO_SSH" ]; then SSH=("$ALGO_SSH"); else   # ALGO_SSH: a stand-in for ssh, for the tests
  chmod 600 "$KEY" 2>/dev/null
  SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20)
fi
mkdir -p "$OUT" || exit 1
echo "Copying the dashboard's page code from the server (read-only)..."
# on the server: list everything (names, sizes, dates, checksums of code), then pack only code and pages
"${SSH[@]}" "$HOST" "cd '$REMOTE' && ls -la . templates static 2>/dev/null; echo; sha256sum *.py templates/* static/* 2>/dev/null" > "$OUT/listing.txt" 2>&1
if ! "${SSH[@]}" "$HOST" "cd '$REMOTE' && files=\$(ls -d *.py templates static requirements.txt 2>/dev/null); [ -n \"\$files\" ] && \
      (tar -czf - --exclude='*.bak*' --exclude='*.before-*' --exclude='__pycache__' --exclude='*.db' --exclude='*.json' \
         --exclude='.secret_key' --exclude='*.pem' --exclude='.env' \$files 2>/dev/null || sudo -n tar -czf - --exclude='*.bak*' \
         --exclude='*.before-*' --exclude='__pycache__' --exclude='*.db' --exclude='*.json' --exclude='.secret_key' --exclude='*.pem' \
         --exclude='.env' \$files)" | tar -xzf - -C "$OUT"; then
  echo "Could not copy from the server (see above). Nothing was changed there."; exit 1
fi
# belt and braces: nothing secret leaves this Mac either
find "$OUT" \( -name '*.json' -o -name '*.db' -o -name '.secret_key' -o -name '*.pem' -o -name '.env' -o -name '*.bak*' \) -delete
n=$(find "$OUT" -type f | wc -l | tr -d ' ')
echo "Done: $n files in $OUT"
echo "Tell Claude \"dashboard pages copied\"; it will read them from there."
echo; echo "You can close this window."
