#!/usr/bin/env bash
# Daily IBKR snapshot, the server side: fetch ibkr_snapshot.json from the repo's `ibkr-snapshot` branch
# (written by the Claude scheduled task 5 minutes after the US open and close) and hand it to the US page.
# Uses the same read-only deploy key and repo as self_update.sh. Run by algolab-ibkr-snapshot.timer
# 10 minutes after each task run (09:45 and 16:15 New York time, weekdays) and hourly.
#
#   sudo bash /opt/algo-trading/ibkr_snapshot_pull.sh
set -uo pipefail
HOME_DIR="${SELF_UPDATE_HOME:-/opt/algo-trading}"
REPO_URL="${SNAPSHOT_REPO:-$(cat "$HOME_DIR/repo" 2>/dev/null || true)}"
BRANCH="${SNAPSHOT_BRANCH:-ibkr-snapshot}"
CACHE="$HOME_DIR/ibkr-snapshot.git"
KEY="$HOME_DIR/secrets/deploy_key"
IMPORTER="${SNAPSHOT_IMPORTER:-$HOME_DIR/ibkr_snapshot_import.py}"
export IBKR_DATA_DIR="${IBKR_DATA_DIR:-/opt/trader/data/ibkr}"

[ -n "$REPO_URL" ] || { echo "No GitHub repo set ($HOME_DIR/repo); nothing to do."; exit 0; }
[ -f "$KEY" ] && export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20"
[ -d "$CACHE" ] || git init -q --bare "$CACHE"
if ! git -C "$CACHE" fetch -q --depth 1 "$REPO_URL" "$BRANCH" 2>/dev/null; then
  echo "Could not fetch the $BRANCH branch (none yet, or GitHub unreachable); trying again next hour."
  exit 0
fi
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
git -C "$CACHE" show FETCH_HEAD:ibkr_snapshot.json > "$TMP" 2>/dev/null || { echo "No ibkr_snapshot.json on $BRANCH."; exit 0; }
python3 "$IMPORTER" "$TMP"
