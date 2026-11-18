#!/usr/bin/env bash
# The server updates itself from GitHub: started by the dashboard's "Update" button (Profile › Server version).
#
# The dashboard (not root) writes runtime/self-update.request with "check" or "update"; a systemd path unit sees it
# and runs this script as root. It keeps a read-only copy of the repo in /opt/algo-trading/src, pulled with a
# read-only GitHub deploy key, and runs that copy's deploy/update_engine.sh (backup, checks, rollback) exactly as an
# update from the Mac would. It writes what it did to runtime/self-update.json and runtime/self-update.log.
#
#   sudo bash /opt/algo-trading/self_update.sh check|update|run      (run: do what the request file asks)
#   peek: a quiet check every 30 minutes (timer): records whether an update is waiting, keeps the last message,
#         and says nothing if GitHub cannot be reached
set -uo pipefail
ENGINE="${ENGINE_DIR:-/opt/algolab}"
HOME_DIR="${SELF_UPDATE_HOME:-/opt/algo-trading}"
# which GitHub repo: written by update_engine.sh into $HOME_DIR/repo (from the Mac's git remote or the repo copy)
REPO_URL="${SELF_UPDATE_REPO:-$(cat "${SELF_UPDATE_HOME:-/opt/algo-trading}/repo" 2>/dev/null || true)}"
[ -n "$REPO_URL" ] || REPO_URL="$(git -C "${SELF_UPDATE_HOME:-/opt/algo-trading}/src" remote get-url origin 2>/dev/null || true)"
BRANCH="${SELF_UPDATE_BRANCH:-main}"
SRC="$HOME_DIR/src"
KEY="$HOME_DIR/secrets/deploy_key"
RT="$ENGINE/runtime"
STATE="$RT/self-update.json"
LOG="$RT/self-update.log"
REQ="$RT/self-update.request"
UPDATE_CMD="${SELF_UPDATE_CMD:-bash deploy/update_engine.sh}"     # tests replace this
mkdir -p "$RT" "$HOME_DIR/secrets"
chmod 700 "$HOME_DIR/secrets"

ACTION="${1:-run}"
if [ "$ACTION" = run ]; then
  ACTION="$(tr -dc 'a-z' < "$REQ" 2>/dev/null | head -c 10)"
  rm -f "$REQ"
fi
case "$ACTION" in check|update|peek) ;; *) exit 0 ;; esac

exec 9>"$RT/self-update.lock"
flock -n 9 || exit 0                          # one at a time

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
state() {   # state <status> <message>: what the dashboard shows
  python3 - "$STATE" "$1" "$2" "$(now)" "$SRC" "$ENGINE/.deployed_commit" "$KEY.pub" "$BRANCH" <<'PY'
import json, subprocess, sys
from pathlib import Path
path, status, msg, when, src, deployed, pub, branch = sys.argv[1:]
def git(*a):
    try:
        return subprocess.run(["git", "-C", src, *a], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""
old = {}
try:
    old = json.loads(Path(path).read_text())
except Exception:
    pass
if status == "keep":                 # the quiet check: only what is waiting changes, not what the last action said
    status, msg = old.get("status") or "checked", old.get("message") or "Checked GitHub"
cur = Path(deployed).read_text().strip() if Path(deployed).exists() else ""
latest = git("rev-parse", f"origin/{branch}") if Path(src, ".git").exists() else ""
out = {"status": status, "message": msg, "at": when, "deployed": cur, "latest": latest,
       "latest_subject": git("log", "-1", "--format=%s", f"origin/{branch}") if latest else "",
       "latest_time": git("log", "-1", "--format=%cI", f"origin/{branch}") if latest else "",
       "behind": int(git("rev-list", "--count", f"{cur}..origin/{branch}") or 0) if cur and latest else None,
       "pending": [l for l in git("log", "--format=%h %s", f"{cur}..origin/{branch}").splitlines()][:20] if cur and latest else [],
       "public_key": Path(pub).read_text().strip() if Path(pub).exists() else "",
       "last_update": old.get("last_update")}
if status in ("updated", "failed"):
    out["last_update"] = {"at": when, "status": status, "commit": latest if status == "updated" else old.get("deployed", "")}
Path(path).write_text(json.dumps(out, indent=1))
PY
  chmod 644 "$STATE" 2>/dev/null || true
}

# a read-only key for GitHub, made once; its public half is shown on the dashboard to add as a deploy key
if [ ! -f "$KEY" ]; then
  ssh-keygen -q -t ed25519 -N "" -C "algolab-server-read-only" -f "$KEY"
fi
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20"
git config --global --add safe.directory "$SRC" 2>/dev/null || true

[ "$ACTION" = peek ] || state working "Checking GitHub"
if [ -z "$REPO_URL" ]; then
  [ "$ACTION" = peek ] && exit 0
  state failed "No GitHub repo is set for this server. Run one update from the Mac (deploy/update-server.command), or put git@github.com:<owner>/<repo>.git in $HOME_DIR/repo."
  exit 1
fi
if [ ! -d "$SRC/.git" ]; then
  [ "$ACTION" = peek ] && exit 0               # the first check is a person's (it may need the key added)
  if ! out="$(git clone -q --branch "$BRANCH" "$REPO_URL" "$SRC" 2>&1)"; then
    rm -rf "$SRC"
    if echo "$out" | grep -qiE "permission denied|could not read from remote|access rights|publickey"; then
      state needs_key "GitHub refused the server's key. Add it to the repo as a read-only deploy key (shown below), then check again."
    else
      state failed "Could not reach GitHub: $(echo "$out" | tail -1)"
    fi
    exit 1
  fi
fi
if ! out="$(git -C "$SRC" fetch -q origin "$BRANCH" 2>&1)"; then
  [ "$ACTION" = peek ] && exit 0               # GitHub unreachable for a moment: try again next time
  if echo "$out" | grep -qiE "permission denied|could not read from remote|access rights|publickey"; then
    state needs_key "GitHub refused the server's key. Add it to the repo as a read-only deploy key (shown below), then check again."
  else
    state failed "Could not reach GitHub: $(echo "$out" | tail -1)"
  fi
  exit 1
fi

if [ "$ACTION" = check ]; then
  state checked "Checked GitHub"
  exit 0
fi
if [ "$ACTION" = peek ]; then
  state keep ""
  exit 0
fi

# update: the merged version, exactly; then the same update as from the Mac (backup, checks, rollback)
LATEST="$(git -C "$SRC" rev-parse "origin/$BRANCH")"
git -C "$SRC" checkout -q -f --detach "$LATEST" && git -C "$SRC" clean -qfdx
state working "Updating to $(git -C "$SRC" log -1 --format='%h %s' "$LATEST")"
{
  echo "=== $(now) update to $(git -C "$SRC" log -1 --format='%h %s' "$LATEST")"
  (cd "$SRC" && bash -c "$UPDATE_CMD")
} >"$LOG.new" 2>&1
RC=$?
mv -f "$LOG.new" "$LOG"; chmod 644 "$LOG"
if [ $RC = 0 ]; then
  echo "$LATEST" > "$ENGINE/.deployed_commit"
  state updated "Updated to $(git -C "$SRC" log -1 --format='%h %s' "$LATEST")"
else
  state failed "The update stopped (exit $RC) and put the previous version back. The log below says why."
fi
exit $RC
