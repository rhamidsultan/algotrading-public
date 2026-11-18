#!/bin/bash
# Double-click (or: bash deploy/make-friend-bundle.command) on this Mac: makes one file with Algo Lab and your stored
# market data, to give to a friend for his own Mac. It never includes your logins, keys, server address, backtest
# results, paper accounts or portfolio: only the backtest program and the price data.
#
#   Code: this repo's committed version (git archive), without the server, deploy scripts and notes.
#   Data: data/options, data/spot, data/eod (+ the TrueData raw backup if you choose it), from this folder's data/,
#         else ~/Trading/data, else ~/AI - VS/algo-lab/data.
# The file lands on your Desktop: AlgoLab-<date>.tar.gz, with a .sha256 to check it arrived whole.
cd "$(dirname "$0")/.." || exit 1
REPO="$(pwd)"
say() { echo "$@"; }
command -v git >/dev/null && git -C "$REPO" rev-parse HEAD >/dev/null 2>&1 || { say "This must run inside the algotrading repo (git clone)."; exit 1; }

DATA=""
for d in "$REPO/data" "$HOME/Trading/data" "$HOME/AI - VS/algo-lab/data"; do
  if [ -d "$d/options" ] && [ -n "$(ls -A "$d/options" 2>/dev/null | grep -v '^\.keep$')" ]; then DATA="$d"; break; fi
done
[ -n "${ALGO_DATA:-}" ] && DATA="$ALGO_DATA"
[ -n "$DATA" ] || { say "No stored option prices found (looked in ./data, ~/Trading/data, ~/AI - VS/algo-lab/data)."; exit 1; }

kb() { du -sk "$DATA/$1" 2>/dev/null | cut -f1; }
gbs() { awk -v k="${1:-0}" 'BEGIN { printf (k >= 1048576 ? "%.2f GB" : "%.0f MB"), (k >= 1048576 ? k / 1048576 : k / 1024) }'; }
BASE=0; for p in options spot eod; do BASE=$((BASE + $(kb $p || echo 0))); done
RAW=$(kb truedata || echo 0)
say "Algo Lab for a friend"
say "  Code:  $(git -C "$REPO" log -1 --format='%h %s (%cd)' --date=short)"
say "  Data:  $DATA"
say "    1) Backtest data (option prices, index bars, NSE closes)   $(gbs $BASE)"
say "    2) Also the TrueData raw backup (OHLC, volume, OI, stocks)  + $(gbs $RAW)"
say "  CSV files shrink to about a third in the file."
say ""
say "  Please check first: data you downloaded under your own TrueData or Zerodha account may be"
say "  licensed for your personal use only. Sharing it is your call."
say ""
CHOICE="${ALGO_CHOICE:-}"
[ -n "$CHOICE" ] || read -r -p "Type 1 or 2 (anything else cancels): " CHOICE
case "$CHOICE" in 1|2) ;; *) say "Nothing was made."; exit 0 ;; esac

STAMP="$(date +%Y%m%d)"
OUT="${ALGO_OUT:-$HOME/Desktop}/AlgoLab-$STAMP.tar.gz"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
DIR="$WORK/AlgoLab"
mkdir -p "$DIR"

say "==> Code"
git -C "$REPO" archive --format=tar HEAD | tar -x -C "$DIR"
# not for a friend: the server and its dashboard, server deploy scripts, notes about this setup, CI, Claude settings
rm -rf "$DIR/server" "$DIR/deploy" "$DIR/docs" "$DIR/.claude" "$DIR/.github" "$DIR/CLAUDE.md" "$DIR/requirements-server.txt"
# the tests also cover the server pages, which are not included; this README describes this setup, not his
rm -rf "$DIR/tests" "$DIR/README.md"
rm -rf "$DIR/data" "$DIR/runtime" "$DIR/runs"
mkdir -p "$DIR/data" "$DIR/runtime" "$DIR/runs"
# the sample data set stays, so the dashboard opens even before anything else
git -C "$REPO" show HEAD:data/spot/SAMPLE_NIFTY_5minute.csv > "$WORK/sample.csv" 2>/dev/null || true
cat > "$DIR/READ ME FIRST.txt" <<TXT
Algo Lab: backtest NIFTY option strategies on real prices (version $(git -C "$REPO" log -1 --format=%h), $(date +%d\ %b\ %Y))

On your Mac
  1. Put this AlgoLab folder where you want it, e.g. in your home folder.
  2. Open Terminal (Applications > Utilities > Terminal) and run, one line at a time:
         cd ~/AlgoLab                (or drag the folder onto the Terminal window after typing "cd ")
         bash setup_mac.sh           (once: installs what it needs, a few minutes)
         bash start_dashboard.sh     (every time: opens the dashboard in your browser)
     If Python is missing, setup says so: install Python from https://www.python.org/downloads/ and run it again.
  3. Leave the Terminal window open while you use the dashboard. Ctrl+C stops it.

On Windows
  1. Install Python 3.12 from https://www.python.org/downloads/ (tick "Add python.exe to PATH").
  2. Unpack this file (7-Zip, or Windows 11's Extract all), then in the AlgoLab folder double-click
     setup.bat once, then start_dashboard.bat each time.

What is in it
  - The backtest program with all strategies, the parameter sweep and the replay.
  - Stored market data in data/: real NIFTY (and BANK NIFTY / SENSEX) option prices and index bars,
    so backtests run straight away (Backtest tab > pick a strategy > Run backtest).
  - No logins, keys, accounts or anyone's results. Settings are in config.yaml.

New data
  To add newer prices you need your own data source (a Zerodha Kite Connect app or a TrueData
  subscription); the Data tab explains each. secrets.example.yaml shows what to fill in.

Checking the download
  The .sha256 file next to the download holds its checksum:  shasum -a 256 -c AlgoLab-$STAMP.tar.gz.sha256
TXT

sed -n '1,/^Checking the download/p' "$DIR/READ ME FIRST.txt" | sed '$d' > "$DIR/README.md"

say "==> Data ($([ "$CHOICE" = 2 ] && echo "with the TrueData raw backup" || echo "backtest data"))"
PARTS=(options spot eod)
[ "$CHOICE" = 2 ] && PARTS+=(truedata)
for p in "${PARTS[@]}"; do
  [ -d "$DATA/$p" ] || continue
  say "    $p ($(gbs "$(kb "$p")"))"
  (cd "$DATA" && COPYFILE_DISABLE=1 tar -c --exclude '.DS_Store' --exclude '*.tmp' --exclude 'progress.db-*' "$p") | tar -x -C "$DIR/data"
done
[ -f "$DIR/data/spot/SAMPLE_NIFTY_5minute.csv" ] || { mkdir -p "$DIR/data/spot"; [ -s "$WORK/sample.csv" ] && cp "$WORK/sample.csv" "$DIR/data/spot/SAMPLE_NIFTY_5minute.csv"; }

say "==> Checking nothing private is inside"
BAD="$(cd "$DIR" && find . \( -name 'secrets.yaml' -o -name 'config.json' -o -name 'token.json' -o -name '.secret_key' -o -name '.env' \
        -o -name '*.pem' -o -name 'session.json' -o -name 'kite_session*' -o -name 'profile.json' -o -name '*.sqlite' \) -print)"
# your server's address (from ~/.algo-trading/server.env) anywhere in the code; real-looking keys outside the tests
SRV="$( (. "${ALGO_SERVER_CONF:-$HOME/.algo-trading/server.env}" 2>/dev/null; echo "${ALGO_HOST#*@}") )"
HITS="$( [ -n "$SRV" ] && grep -rIlF --exclude-dir=data "$SRV" "$DIR" 2>/dev/null)
$(grep -rIl --exclude-dir=data --exclude-dir=tests -E 'api_secret:\s*["'"'"']?[A-Za-z0-9]{10,}|access_token["'"'"']?\s*[:=]\s*["'"'"']?[A-Za-z0-9]{16,}' "$DIR" 2>/dev/null)"
HITS="$(echo "$HITS" | sed '/^$/d')"
if [ -n "$BAD$HITS" ]; then
  say "!! Stopped: these look private, so nothing was made:"; say "$BAD"; say "$HITS"; exit 1
fi
say "    none found"

say "==> Packing (a few minutes for GBs of prices)"
mkdir -p "$(dirname "$OUT")"
(cd "$WORK" && COPYFILE_DISABLE=1 tar -czf "$OUT" AlgoLab) || { say "Packing failed."; exit 1; }
(cd "$(dirname "$OUT")" && shasum -a 256 "$(basename "$OUT")" > "$(basename "$OUT").sha256") 2>/dev/null \
  || (cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
say ""
say "Made: $OUT  ($(du -h "$OUT" | cut -f1))"
say "Send it with AirDrop, a USB drive, or Google Drive / iCloud Drive (too big for email)."
say "Your friend double-clicks it to unpack, then follows 'READ ME FIRST.txt' in the AlgoLab folder."
