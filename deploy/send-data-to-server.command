#!/bin/bash
# Double-click to copy this Mac's market data to the Lightsail server (/opt/algolab/data), as a backup and so
# the server has the same prices for backtests and the Data page.
#
#  - Shows the sizes and the server's free space first, and copies only after you type a choice.
#  - Never overwrites a file the server already has (its own nightly recordings stay as they are).
#  - Each part goes to a holding folder on the server first and is moved into place only once it arrived
#    whole, so a dropped connection never leaves half a file; double-click again to carry on.
#  - Refuses to run in market hours (weekdays 9:00-15:40 IST), when the server is trading.
cd "$(dirname "$0")/.." || exit 1
. deploy/server_login.sh                 # which server and key (~/.algo-trading/server.env)
KEY="$ALGO_KEY"
HOST="$ALGO_HOST"
E="${ALGO_REMOTE:-/opt/algolab}"
LOG=deploy/last-data-send.log
if [ -n "$ALGO_SSH" ]; then SSH=("$ALGO_SSH"); else   # ALGO_SSH: a stand-in for ssh, for the tests
  chmod 600 "$KEY" 2>/dev/null
  SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30)
fi
export COPYFILE_DISABLE=1   # no macOS "._" files inside the copies

say() { echo "$@" | tee -a "$LOG"; }
: > "$LOG"
say "Send market data to the server  ($(date '+%d %b %Y %H:%M'))"

# ---- not in market hours -----------------------------------------------------------------------------
read -r DOW HM <<<"$(TZ=Asia/Kolkata date '+%u %H%M')"
if [ -z "$ALGO_IGNORE_HOURS" ] && [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -le 1540 ]; then
  say "It is market hours in India ($(TZ=Asia/Kolkata date '+%H:%M') IST). The server is trading, so nothing is copied now."
  say "Run this again after 15:40 IST or at the weekend."
  echo; echo "You can close this window."; exit 1
fi

# ---- what there is to send -----------------------------------------------------------------------------
# name | path under data/ | in choice 1 (backtest data) or only in choice 2 (everything)
PARTS=()
for u in data/options/*/; do
  [ -d "$u" ] && PARTS+=("Options $(basename "$u")|options/$(basename "$u")|1")
done
[ -d data/spot ] && PARTS+=("Index bars|spot|1")
[ -d data/eod ] && PARTS+=("NSE closing prices|eod|1")
for t in data/truedata/*/; do
  [ -d "$t" ] && PARTS+=("TrueData raw: $(basename "$t")|truedata/$(basename "$t")|2")
done
[ -f data/truedata/progress.db ] && PARTS+=("TrueData download record|truedata/progress.db|2")
if [ ${#PARTS[@]} -eq 0 ]; then say "No data/ folder here: nothing to send."; exit 1; fi

kb() { du -sk "data/$1" 2>/dev/null | cut -f1; }
gbs() { awk -v k="$1" 'BEGIN { printf (k >= 1048576 ? "%.2f GB" : "%.0f MB"), (k >= 1048576 ? k / 1048576 : k / 1024) }'; }
say ""
say "On this Mac:"
T1=0; T2=0
for p in "${PARTS[@]}"; do
  IFS='|' read -r name path choice <<<"$p"
  k=$(kb "$path"); k=${k:-0}
  n=$(find "data/$path" -type f 2>/dev/null | wc -l | tr -d ' ')
  printf '  %-34s %10s  %8s files\n' "$name" "$(gbs "$k")" "$n" | tee -a "$LOG"
  T2=$((T2 + k)); [ "$choice" = 1 ] && T1=$((T1 + k))
done
say "  -------------------------------------------------------------"
say "  1) Backtest data (options, index, NSE closes)   $(gbs $T1)"
say "  2) Everything, with the TrueData raw backup      $(gbs $T2)"

say ""
say "Checking the server..."
REMOTE_INFO=$("${SSH[@]}" "$HOST" "df -Pk '$E' | tail -1 | awk '{print \$4, \$2}'; du -sk '$E/data' 2>/dev/null | cut -f1" 2>&1) || {
  say "Cannot reach the server: $REMOTE_INFO"; echo; echo "You can close this window."; exit 1; }
read -r FREE TOTAL <<<"$(echo "$REMOTE_INFO" | grep -E '^[0-9]+ [0-9]+$' | tail -1)"
HAVE=$(echo "$REMOTE_INFO" | grep -E '^[0-9]+$' | tail -1)
say "  Server disk: $(gbs "$FREE") free of $(gbs "$TOTAL"); its data folder now holds $(gbs "${HAVE:-0}")."
say "  Files the server already has are skipped, so the copy can be smaller than shown."
say "  Text price files are compressed on the way: expect to upload roughly a third of the size above."

if [ -n "$ALGO_CHOICE" ]; then CHOICE="$ALGO_CHOICE"; else
  echo; printf "Type 1 or 2 and press Return to start (anything else cancels): "; read -r CHOICE; fi
case "$CHOICE" in
  1) NEED=$T1 ;;
  2) NEED=$T2 ;;
  *) say "Cancelled. Nothing was copied."; echo; echo "You can close this window."; exit 0 ;;
esac
# the holding folder needs room for the biggest part on top of the data itself, plus 5 GB to spare
BIGGEST=0
for p in "${PARTS[@]}"; do IFS='|' read -r _ path choice <<<"$p"; [ "$choice" -le "$CHOICE" ] || continue
  k=$(kb "$path"); [ "${k:-0}" -gt "$BIGGEST" ] && BIGGEST=$k; done
if [ $((NEED + BIGGEST + 5242880)) -gt "$FREE" ]; then
  say "Not enough free space on the server for this: it needs about $(gbs $((NEED + BIGGEST + 5242880)))."
  exit 1
fi

# ---- copy, part by part --------------------------------------------------------------------------------
# On the server: run as the user that owns the engine; unpack into incoming/<part>, then move into data/
# without replacing anything that is already there, then remove the holding folder.
REMOTE_PREP="E='$E'; U=\$(stat -c %U \"\$E/run.py\"); if [ \"\$U\" = \"\$(id -un)\" ]; then AS=''; else AS=\"sudo -n -u \$U\"; fi"
i=0; n=0; for p in "${PARTS[@]}"; do IFS='|' read -r _ _ c <<<"$p"; [ "$c" -le "$CHOICE" ] && n=$((n + 1)); done
FAILED=0
START=$(date +%s)
for p in "${PARTS[@]}"; do
  IFS='|' read -r name path choice <<<"$p"
  [ "$choice" -le "$CHOICE" ] || continue
  i=$((i + 1))
  tag=$(echo "$path" | tr '/' '_')
  say ""
  say "[$i/$n] $name ($(gbs "$(kb "$path")")) ..."
  t0=$(date +%s)
  # .gz files are already compressed; everything else is compressed on the way
  case "$path" in truedata/*) Z="" ;; *) Z="z" ;; esac
  if (cd data && tar -c${Z}f - --exclude '.DS_Store' --exclude '*.tmp' "$path") |
     "${SSH[@]}" "$HOST" "$REMOTE_PREP; set -e
        \$AS rm -rf \"\$E/incoming/$tag\"; \$AS mkdir -p \"\$E/incoming/$tag\" \"\$E/data\"
        \$AS tar -x${Z}f - --warning=no-unknown-keyword -C \"\$E/incoming/$tag\"
        \$AS rsync -a --ignore-existing \"\$E/incoming/$tag/\" \"\$E/data/\"
        \$AS rm -rf \"\$E/incoming/$tag\"" >>"$LOG" 2>&1; then
    say "      done in $(( $(date +%s) - t0 )) s"
  else
    FAILED=$((FAILED + 1))
    say "      FAILED (details in $LOG). Double-click again later to retry; finished parts are kept."
  fi
done
"${SSH[@]}" "$HOST" "$REMOTE_PREP; \$AS rmdir \"\$E/incoming\" 2>/dev/null; du -sk \"\$E/data\" | cut -f1; df -Pk \"\$E\" | tail -1 | awk '{print \$4}'" >"$LOG.tmp" 2>&1
# only the two numbers count: ssh or sudo may print a warning line first (16 Oct: it showed "0.00 GB")
NUMS=$(grep -E '^[0-9]+$' "$LOG.tmp" | tail -2); cat "$LOG.tmp" >>"$LOG"; rm -f "$LOG.tmp"
NOW=$(echo "$NUMS" | sed -n 1p); FREE2=$(echo "$NUMS" | sed -n 2p)
say ""
say "Finished in $(( ($(date +%s) - START) / 60 )) min: $((n - FAILED)) of $n parts copied."
say "The server's data folder now holds $(gbs "${NOW:-0}"); $(gbs "${FREE2:-0}") free."
[ "$FAILED" = 0 ] || exit 2
echo; echo "You can close this window."
