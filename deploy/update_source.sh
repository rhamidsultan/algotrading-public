#!/usr/bin/env bash
# Where the server takes its updates and the IBKR snapshot from. Run by update_engine.sh at every update; writes
# into $SELF (/opt/algo-trading):
#   repo         git@github.com:<owner>/<repo>.git, read with the server's read-only deploy key: the IBKR snapshot
#                branch (ibkr_snapshot_pull.sh), and the updates when no update_repo is set
#   update_repo  https://github.com/<owner>/<repo>: a public repo the Update button pulls from (no key needed)
# The code's own GitHub repo (deploy/REPO from the Mac's pack, else the copy's git remote) is used: as update_repo if
# anyone can read it, else as repo. A repo can move the updates elsewhere with one line in deploy/UPDATE_FROM
# (an https://github.com/<owner>/<repo> address); `repo` then stays as it was, for the IBKR snapshot.
#
#   bash deploy/update_source.sh <code copy> <self dir>
set -uo pipefail
SRC="$1"
SELF="$2"
mkdir -p "$SELF"

ORIGIN="$(cat "$SRC/deploy/REPO" 2>/dev/null || git -C "$SRC" remote get-url origin 2>/dev/null || true)"
ORIGIN="$(echo "$ORIGIN" | tr -d ' \r\n')"
HTTPS="$(echo "$ORIGIN" | sed -E 's#^https://[^@/]+@github\.com/#https://github.com/#; s#^git@github\.com:#https://github.com/#; s#\.git$##')"
UP="$(tr -d ' \r\n' < "$SRC/deploy/UPDATE_FROM" 2>/dev/null || true)"
UP="${UP%.git}"

public() {   # can this GitHub repo be read without any key or password?
  case "$1" in https://github.com/*/*) ;; *) return 1 ;; esac
  if [ -n "${UPDATE_SOURCE_PUBLIC:-}" ]; then [ "$UPDATE_SOURCE_PUBLIC" = yes ]; return; fi   # tests: no network
  GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false GIT_SSH_COMMAND=/bin/false \
    timeout "${UPDATE_SOURCE_TIMEOUT:-30}" git -c credential.helper= ls-remote -q "$1" HEAD >/dev/null 2>&1
}

if [ -z "$UP" ] && public "$HTTPS"; then
  UP="$HTTPS"
fi
case "$UP" in
  https://github.com/*/*) echo "$UP" > "$SELF/update_repo"; chmod 644 "$SELF/update_repo"
                          echo "    updates from $UP" ;;
esac

# the deploy-key address, for the IBKR snapshot (and updates from a private repo); never the public update repo
CUR_UP="$(cat "$SELF/update_repo" 2>/dev/null | tr -d ' \r\n')"
if [ -n "$HTTPS" ] && [ "$HTTPS" != "$CUR_UP" ]; then
  REPO="$(echo "$ORIGIN" | sed -E 's#^https://([^@/]+@)?github\.com/#git@github.com:#')"
  case "$REPO" in
    git@github.com:*) case "$REPO" in *.git) ;; *) REPO="$REPO.git" ;; esac
                      echo "$REPO" > "$SELF/repo"; chmod 644 "$SELF/repo" ;;
  esac
fi
exit 0
