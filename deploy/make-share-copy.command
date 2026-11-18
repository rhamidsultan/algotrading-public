#!/bin/bash
# Run on the Mac (bash deploy/make-share-copy.command): makes a clean copy of this repo for someone setting up their
# own server (docs/SERVER_SETUP.md). They get today's merged code as ONE fresh commit:
#   - no history (older versions held a server address and notes about this setup);
#   - no notes about this setup (docs/STATUS.md, docs/DECISIONS.md, docs/handoff/); CLAUDE.md is a general one;
#   - never any data, logins or keys (none are in the repo; it checks once more).
# Two ways to hand it over:
#   1. Give their empty GitHub repo's address: it is pushed there. (They create the repo, empty, with no README,
#      and add you under Settings > Collaborators so you may push; they can remove you afterwards.)
#        bash deploy/make-share-copy.command https://github.com/<their-user>/algotrading.git
#   2. No address: a single file on your Desktop (AlgoLab-code-<date>.bundle) to send them; they unpack it with git.
cd "$(dirname "$0")/.." || exit 1
REPO="$(pwd)"
TARGET="${1:-}"
BRANCH="${SHARE_BRANCH:-origin/main}"
[ -n "${SHARE_NO_FETCH:-}" ] || git fetch -q origin main 2>/dev/null || echo "(Could not reach GitHub; using the copy of main already on this Mac.)"
git rev-parse --verify -q "$BRANCH" >/dev/null || BRANCH=HEAD
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
OUT="$WORK/algotrading"
mkdir -p "$OUT"
echo "==> Taking $(git log -1 --format='%h %s (%cd)' --date=short "$BRANCH")"
git archive --format=tar "$BRANCH" | tar -x -C "$OUT"
rm -rf "$OUT/docs/handoff" "$OUT/docs/STATUS.md" "$OUT/docs/DECISIONS.md" "$OUT/deploy/COMMIT" "$OUT/deploy/REPO"
cat > "$OUT/CLAUDE.md" <<'MD'
# Algo Trading — guide for every Claude session

NIFTY / Bank Nifty / Sensex options platform: backtests on the computer (Algo Lab), paper and guarded live trading
on your own server, shown in one dashboard that also tracks your India (and optionally US) portfolio.
Setting up the server: `docs/SERVER_SETUP.md`.

## House rules

1. Work on a branch and open a pull request; merge to `main` only after the tests pass.
2. Never commit secrets or data: no `secrets.yaml`, `config.json`, `token.json`, `.secret_key`, `.env`, `*.pem`,
   databases, `data/`, `runs/`, `runtime/`.
3. Tests: `python -m pytest -q`. Add a test for new behaviour.
4. Code updates may reach the server in market hours (the dashboard's Update button); copying data waits for the
   close (weekdays 9:00–15:40 IST).
5. Never write a server address or repo name into the code: the launchers read `~/.algo-trading/server.env`.

## What is where

| Path | What it is |
|---|---|
| `algolab/` | Engine: strategies, backtest, real option prices, paper/live trading, Zerodha, TrueData, margins |
| `run.py` | Command line: `backtest`, `sweep`, `paper`, `collect`, `sync-data`, `truedata …` |
| `dashboard.py` | Algo Lab, the backtest dashboard (`start_dashboard.sh`) |
| `deploy/` | Server install and update scripts, computer launchers (`*.command`) |
| `deploy/portfolio_page/` | Pages added to the portfolio dashboard (Paper, Live, Summary, Users, phone app) |
| `server/portfolio/` | Portfolio dashboard (Flask), runs in `/opt/trader` |
| `config.yaml` | Market settings: lot sizes by date, expiry weekdays, holidays, charges |

Backtests use real option prices only by default (`--allow-model` allows the Black-Scholes model).
Before trusting new data, run `python -m algolab.datacheck` (or the `data-reviewer` agent in `.claude/agents/`).
MD

echo "==> Checking nothing private is inside"
SRV="$( (. "${ALGO_SERVER_CONF:-$HOME/.algo-trading/server.env}" 2>/dev/null; echo "${ALGO_HOST#*@}") )"
OWNER_REPO="$(git -C "$REPO" remote get-url origin 2>/dev/null | sed -E 's#^(https://([^@/]+@)?github\.com/|git@github\.com:)##; s#\.git$##')"
EMAIL="$(git -C "$REPO" config user.email 2>/dev/null || true)"
BAD="$(cd "$OUT" && find . \( -name 'secrets.yaml' -o -name 'config.json' -o -name 'token.json' -o -name '.secret_key' -o -name '.env' \
        -o -name '*.pem' -o -name '*.db' -o -name '*.sqlite' -o -name 'access_token*.json' \) -print)"
HITS=""
for s in "$SRV" "$OWNER_REPO" "$EMAIL"; do
  [ -n "$s" ] && HITS="$HITS$(grep -rIlF -- "$s" "$OUT" 2>/dev/null)"$'\n'
done
HITS="$(echo "$HITS" | sed '/^$/d' | sed "s#^$OUT/##" | sort -u)"
if [ -n "$BAD$HITS" ]; then
  echo "!! Stopped: these look private, so nothing was shared:"; echo "$BAD"; echo "$HITS"; exit 1
fi
echo "    none found ($(cd "$OUT" && find . -type f | wc -l | tr -d ' ') files)"

cd "$OUT" || exit 1
git init -q -b main
git add -A
STAMP="$(date +%Y-%m-%d)"
GIT_AUTHOR_NAME="${SHARE_AUTHOR:-Algo Lab}" GIT_AUTHOR_EMAIL="algolab@users.noreply.github.com" \
GIT_COMMITTER_NAME="${SHARE_AUTHOR:-Algo Lab}" GIT_COMMITTER_EMAIL="algolab@users.noreply.github.com" \
  git commit -q -m "Algo Lab, shared copy of $STAMP" -m "Start here: docs/SERVER_SETUP.md"

if [ -n "$TARGET" ]; then
  echo "==> Pushing to $TARGET"
  if [ -n "$(git ls-remote "$TARGET" 2>/dev/null | head -1)" ]; then
    echo "!! $TARGET already has commits. This only fills an EMPTY repo, so nothing of theirs is overwritten."
    exit 1
  fi
  if git push -q "$TARGET" main 2>"$WORK/push.err"; then
    echo ""
    echo "Done: $TARGET now has the code (one commit, on main)."
    echo "Tell them to start with docs/SERVER_SETUP.md. They can remove you as a collaborator now."
  else
    echo "!! The push did not go through:"; tail -3 "$WORK/push.err"
    echo "   Check: the repo exists and is EMPTY (no README), and they added you under Settings > Collaborators."
    echo "   Or run without an address to make a file to send instead."
    exit 1
  fi
else
  F="${SHARE_OUT:-$HOME/Desktop}/AlgoLab-code-$(date +%Y%m%d).bundle"
  mkdir -p "$(dirname "$F")"
  git bundle create -q "$F" HEAD main
  echo ""
  echo "Made: $F  ($(du -h "$F" | cut -f1))"
  echo "Send it (AirDrop, email, Google Drive). On their computer, after creating an empty private repo on GitHub:"
  echo "    git clone AlgoLab-code-$(date +%Y%m%d).bundle algotrading"
  echo "    cd algotrading && git remote set-url origin https://github.com/<their-user>/algotrading.git && git push -u origin main"
  echo "Then they follow docs/SERVER_SETUP.md."
fi
