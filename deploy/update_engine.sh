#!/usr/bin/env bash
# Updates the Algo Lab engine on the server (/opt/algolab) with newer code, keeping its data,
# settings (config.yaml), login link (secrets.yaml) and paper accounts.
#
#   sudo bash deploy/update_engine.sh          (from the unpacked update folder)
#
# Saves the current engine code first and puts it back by itself if the new code does not start.
# The portfolio dashboard is not touched.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo:  sudo bash deploy/update_engine.sh"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ENGINE_DIR:-/opt/algolab}"
[ -x "$ENGINE/venv/bin/python" ] || { echo "No Algo Lab engine found in $ENGINE. Nothing was changed."; exit 1; }
RUN_USER="$(stat -c %U "$ENGINE/run.py")"
PY="$ENGINE/venv/bin/python"
as_user() { sudo -u "$RUN_USER" "$@"; }

STAMP="$(date +%Y%m%d-%H%M%S)"
BK="$(dirname "$ENGINE")/algolab-code-backup-$STAMP.tgz"
echo "==> Saving the current engine code to $BK"
tar -czf "$BK" -C "$ENGINE" --exclude='./venv' --exclude='./data' --exclude='./runtime' --exclude='./runs' .

restore() {
  echo "!! $1"
  echo "   Putting the previous engine code back."
  tar -xzf "$BK" -C "$ENGINE"
  chown -R "$RUN_USER:$RUN_USER" "$ENGINE"
  systemctl restart algolab-paper || true
  echo "   Done; the engine runs its previous version. Please send me the lines above."
  exit 1
}

echo "==> Copying the new code"
for item in algolab studio run.py requirements-server.txt deploy/update_engine.sh; do
  [ -e "$SRC/$item" ] || continue
  mkdir -p "$ENGINE/$(dirname "$item")"
  if [ -d "$SRC/$item" ]; then
    mkdir -p "$ENGINE/$item" && cp -a "$SRC/$item/." "$ENGINE/$item/"
  else
    cp -p "$SRC/$item" "$ENGINE/$item"
  fi
done
chown -R "$RUN_USER:$RUN_USER" "$ENGINE"

echo "==> Python packages (websocket-client for the live price stream)"
as_user "$PY" -m pip install -q -r "$ENGINE/requirements-server.txt" || restore "Could not install the Python packages."

echo "==> Checking it loads"
(cd "$ENGINE" && as_user "$PY" -c "import algolab.kite, algolab.engine, algolab.eod, algolab.paper, algolab.stream, websocket, studio.app; from algolab.strategy import load_strategies; print('    strategies:', ', '.join(load_strategies()))") \
  || restore "The new code does not load."
(cd "$ENGINE" && as_user "$PY" run.py catalog >/dev/null) || restore "The strategy list could not be written."

echo "==> Paper sessions: remove the four cycles traded on the 20 Oct holiday (once; backup first)"
CYC_PLAN="$(cd "$ENGINE" && as_user "$PY" run.py paper-remove-holiday-cycles --dry-run 2>&1)" || true
echo "$CYC_PLAN" | sed 's/^/    /'
if echo "$CYC_PLAN" | grep -q "^Will remove"; then
  systemctl stop algolab-paper || true
  (cd "$ENGINE" && as_user "$PY" run.py paper-remove-holiday-cycles 2>&1 | sed 's/^/    /') || echo "    (removal failed)"
fi

echo "==> Restarting the paper-trading engine"
systemctl restart algolab-paper
sleep 6
systemctl is-active --quiet algolab-paper || restore "The paper-trading engine did not stay up: $(journalctl -u algolab-paper -n 5 --no-pager | tail -3)"

echo "==> Margin check (Zerodha's margin for each strategy and each session's open legs)"
cat > /etc/systemd/system/algolab-margins.service <<UNIT
[Unit]
Description=Algo Lab: Zerodha margin for each strategy and session

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$ENGINE
ExecStart=$PY -m algolab.margins --auto
TimeoutStartSec=300
OOMScoreAdjust=500
UNIT
cat > /etc/systemd/system/algolab-margins.timer <<UNIT
[Unit]
Description=Algo Lab: look every 5 minutes whether margins are due (every 30 minutes in market hours, or when asked)

[Timer]
OnCalendar=*:0/5
AccuracySec=30s

[Install]
WantedBy=timers.target
UNIT
# Backtests asked for on the dashboard (Paper > Run a backtest): a path unit sees the queue folder is not empty and
# runs them one by one at the lowest priority, with capped CPU and memory, so trading sessions are never slowed.
TRADER_DIR="${TRADER_DIR:-/opt/trader}"
install -d -o "$RUN_USER" -g "$RUN_USER" "$ENGINE/runtime/backtest-queue" "$ENGINE/runtime/backtest-jobs"
cat > /etc/systemd/system/algolab-backtest.service <<UNIT
[Unit]
Description=Algo Lab: run the backtests asked for on the dashboard

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$ENGINE
Environment=BACKTESTS_STORE=$TRADER_DIR/data/backtests
ExecStart=$PY run.py backtest-queue
# after every run, also when it was killed (out of memory, time limit): a job it left 'running' shows why it stopped
ExecStopPost=$PY run.py backtest-queue --stopped
Nice=19
IOSchedulingClass=idle
CPUQuota=60%
MemoryMax=800M
OOMScoreAdjust=900
TimeoutStartSec=3h
UNIT
cat > /etc/systemd/system/algolab-backtest.path <<UNIT
[Unit]
Description=Algo Lab: watch for backtests asked for on the dashboard

[Path]
DirectoryNotEmpty=$ENGINE/runtime/backtest-queue
MakeDirectory=yes

[Install]
WantedBy=multi-user.target
UNIT
# Weekly paper-vs-backtest report (algolab/paper_report.py): Saturday morning, at the backtests' low priority
cat > /etc/systemd/system/algolab-paper-report.service <<UNIT
[Unit]
Description=Algo Lab: backtest each paper session's week and compare with what it did

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$ENGINE
Environment=BACKTESTS_STORE=$TRADER_DIR/data/backtests
ExecStart=$PY run.py paper-report
Nice=19
IOSchedulingClass=idle
CPUQuota=60%
MemoryMax=800M
OOMScoreAdjust=900
TimeoutStartSec=2h
UNIT
cat > /etc/systemd/system/algolab-paper-report.timer <<UNIT
[Unit]
Description=Algo Lab: weekly paper-vs-backtest report (Saturdays 08:05 IST)

[Timer]
OnCalendar=Sat 08:05 Asia/Kolkata
Persistent=true

[Install]
WantedBy=timers.target
UNIT
# Zerodha tick count for the dashboard's "TrueData vs Zerodha" test: its own runner, so it never waits for a backtest
install -d -o "$RUN_USER" -g "$RUN_USER" "$ENGINE/runtime/tick-test-queue"
cat > /etc/systemd/system/algolab-ticktest.service <<UNIT
[Unit]
Description=Algo Lab: count Zerodha live ticks for the TrueData comparison (asked for on the dashboard)

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$ENGINE
ExecStart=$PY run.py tick-test-queue
TimeoutStartSec=15min
UNIT
cat > /etc/systemd/system/algolab-ticktest.path <<UNIT
[Unit]
Description=Algo Lab: watch for the dashboard's Zerodha tick-count requests

[Path]
DirectoryNotEmpty=$ENGINE/runtime/tick-test-queue
MakeDirectory=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q --now algolab-ticktest.path || echo "    (could not start the tick-test watcher)"
systemctl enable -q --now algolab-paper-report.timer || echo "    (could not start the weekly report timer)"
systemctl enable -q --now algolab-margins.timer
systemctl enable -q --now algolab-backtest.path || echo "    (could not start the backtest watcher: $(systemctl is-failed algolab-backtest.path 2>&1))"
(cd "$ENGINE" && as_user "$PY" run.py backtest-queue --stopped >/dev/null) || true   # jobs an earlier killed run left 'running'
(cd "$ENGINE" && as_user timeout 240 "$PY" -m algolab.margins) \
  || echo "    (Not now - usually because there is no Zerodha login today. It retries by itself.)"

# The Blaze Butterfly now runs as an ordinary Algo Lab strategy: retire its original engine (trader-paper).
# Its history stays in the dashboard's data folder; to bring it back: sudo systemctl enable --now trader-paper
if systemctl cat trader-paper >/dev/null 2>&1; then
  WAS_ON=0
  systemctl is-active --quiet trader-paper && WAS_ON=1
  if systemctl is-enabled --quiet trader-paper 2>/dev/null || [ "$WAS_ON" = 1 ]; then
    echo "==> Retiring the original Blaze Butterfly engine (trader-paper); the strategy runs in Algo Lab"
    systemctl disable -q --now trader-paper || true
    if [ "$WAS_ON" = 1 ]; then
      as_user "$PY" - "$ENGINE/runtime/sessions" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
mine = [d for d in root.iterdir() if d.is_dir() and json.loads(((d / "desired.json").read_text() if (d / "desired.json").exists()
        else (d / "state.json").read_text() if (d / "state.json").exists() else "{}")).get("strategy") == "blaze_butterfly"]
if mine:
    print(f"    Algo Lab already has a Blaze Butterfly session ({mine[0].name}); nothing started.")
else:
    sid, n = "blaze_butterfly", 1
    while (root / sid).exists():
        n += 1
        sid = f"blaze_butterfly-{n}"
    (root / sid).mkdir()
    (root / sid / "desired.json").write_text(json.dumps({"strategy": "blaze_butterfly", "params": {}, "feed": "kite",
                                                         "label": "Blaze Butterfly"}))
    (root / sid / "started_by.json").write_text(json.dumps({"user": "update (replaces the original engine)"}))
    print(f"    Started a Blaze Butterfly paper session in Algo Lab ({sid}) in its place.")
PY
    fi
  fi
fi

# The portfolio dashboard's Paper trading page (a separate file in the dashboard's folder)
TRADER_DIR="${TRADER_DIR:-/opt/trader}"
PANEL_NEW="$SRC/deploy/portfolio_page/algolab_panel.py"
PWA_NEW="$SRC/deploy/portfolio_page/pwa_panel.py"     # the dashboard as a phone app (install, Face ID, notifications)
USERS_NEW="$SRC/deploy/portfolio_page/users_panel.py" # several logins, access per page
changed() { [ -f "$1" ] && ! cmp -s "$1" "$TRADER_DIR/$(basename "$1")"; }
# The dashboard's own program files (app.py and its panels), shipped from server/portfolio
APP_CHANGED=()
NEW_OK=" positions_panel.py "   # dashboard files that may be new on the server (installed even if missing)
for f in "$SRC"/server/portfolio/{app,backtests_panel,ibkr_panel,order_test_panel,positions_panel,paper_engine}.py "$SRC"/server/portfolio/server/{auth_server,kite_session}.py; do
  n="$(basename "$f")"
  [ -f "$f" ] || continue
  if [ -f "$TRADER_DIR/$n" ]; then changed "$f" && APP_CHANGED+=("$f")
  elif [[ "$NEW_OK" == *" $n "* ]]; then APP_CHANGED+=("$f"); fi
done
# The dashboard's own pages (Portfolio, Stocks, US, Backtests, ...): templates/*.html, shipped from server/portfolio/templates
TPL_NEW="$SRC/server/portfolio/templates"
TPL_CHANGED=()
if [ -d "$TPL_NEW" ] && [ -d "$TRADER_DIR/templates" ]; then
  for f in "$TPL_NEW"/*.html; do
    [ -f "$f" ] && ! cmp -s "$f" "$TRADER_DIR/templates/$(basename "$f")" && TPL_CHANGED+=("$(basename "$f")")
  done
fi
if [ -f "$PANEL_NEW" ] && [ -f "$TRADER_DIR/algolab_panel.py" ] && { changed "$PANEL_NEW" || changed "$PWA_NEW" || changed "$USERS_NEW" || [ ${#TPL_CHANGED[@]} -gt 0 ] || [ ${#APP_CHANGED[@]} -gt 0 ]; }; then
  echo "==> Updating the dashboard's pages${TPL_CHANGED[*]:+ (and ${TPL_CHANGED[*]})}"
  for f in "${APP_CHANGED[@]}"; do      # files the server has (or listed in NEW_OK); a broken one is put back below
    n="$(basename "$f")"
    echo "    $n"
    [ -f "$TRADER_DIR/$n" ] && cp -p "$TRADER_DIR/$n" "$TRADER_DIR/$n.bak-$STAMP"
    install -o "$RUN_USER" -g "$RUN_USER" -m 644 "$f" "$TRADER_DIR/$n"
  done
  for n in "${TPL_CHANGED[@]}"; do
    [ -f "$TRADER_DIR/templates/$n" ] && cp -p "$TRADER_DIR/templates/$n" "$TRADER_DIR/templates/$n.bak-$STAMP"
    install -o "$RUN_USER" -g "$RUN_USER" -m 444 "$TPL_NEW/$n" "$TRADER_DIR/templates/$n"
  done
  PANEL_BK="$TRADER_DIR/algolab_panel.py.bak-$STAMP"
  cp -p "$TRADER_DIR/algolab_panel.py" "$PANEL_BK"
  [ -f "$TRADER_DIR/pwa_panel.py" ] && cp -p "$TRADER_DIR/pwa_panel.py" "$TRADER_DIR/pwa_panel.py.bak-$STAMP"
  [ -f "$TRADER_DIR/users_panel.py" ] && cp -p "$TRADER_DIR/users_panel.py" "$TRADER_DIR/users_panel.py.bak-$STAMP"
  if [ -x "$TRADER_DIR/venv/bin/python" ] && ! "$TRADER_DIR/venv/bin/python" -c "import cryptography" 2>/dev/null; then
    "$TRADER_DIR/venv/bin/pip" install -q cryptography || echo "   (cryptography not installed: Face ID sign-in and notifications stay off)"
  fi
  [ -f "$PWA_NEW" ] && install -o "$RUN_USER" -g "$RUN_USER" -m 444 "$PWA_NEW" "$TRADER_DIR/pwa_panel.py"
  [ -f "$USERS_NEW" ] && install -o "$RUN_USER" -g "$RUN_USER" -m 444 "$USERS_NEW" "$TRADER_DIR/users_panel.py"
  install -o "$RUN_USER" -g "$RUN_USER" -m 444 "$PANEL_NEW" "$TRADER_DIR/algolab_panel.py"
  APP_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 5000))' "$TRADER_DIR/config.json" 2>/dev/null || echo 5000)"
  systemctl restart "${APP_SERVICE:-trader-app}"
  CHECK="fail"
  for _ in $(seq 1 30); do
    sleep 1
    CHECK="$(python3 - "$TRADER_DIR/config.json" "$APP_PORT" <<'PY' 2>/dev/null || true
import http.cookiejar, json, sys, urllib.parse, urllib.request
cfg = json.load(open(sys.argv[1]))
base = f"http://127.0.0.1:{sys.argv[2]}"
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
form = urllib.parse.urlencode({"username": cfg["dashboard_user"], "password": cfg["dashboard_password"]}).encode()
web.open(base + "/signin", form, timeout=10)
d = json.load(web.open(base + "/api/paper", timeout=10))
home = web.open(base + "/", timeout=10).read().decode("utf-8", "replace")
paper = web.open(base + "/paper", timeout=10).read().decode("utf-8", "replace")
for page in ("/stock", "/us", "/backtests", "/summary", "/paper/overview", "/positions", "/api/history", "/api/data"):   # every page renders
    web.open(base + page, timeout=10).read()
md = json.load(web.open(base + "/api/market-data/summary", timeout=30))
mf = json.load(urllib.request.urlopen(base + "/manifest.webmanifest", timeout=10))
acct = json.load(web.open(base + "/api/account", timeout=10))
users = json.load(web.open(base + "/api/users", timeout=10))
print("ok" if "sessions" in d and "catalog" in d and "sitenav" in home and "sitenav" in paper and "expiries" in md and mf.get("display") == "standalone"
      and acct.get("owner") and "users" in users else "unexpected")
PY
)"
    [ "$CHECK" = ok ] && break
  done
  if [ "$CHECK" != ok ]; then
    echo "!! The Paper trading page did not respond as expected; putting the previous page back."
    cp -p "$PANEL_BK" "$TRADER_DIR/algolab_panel.py"
    [ -f "$TRADER_DIR/pwa_panel.py.bak-$STAMP" ] && cp -p "$TRADER_DIR/pwa_panel.py.bak-$STAMP" "$TRADER_DIR/pwa_panel.py"
    if [ -f "$TRADER_DIR/users_panel.py.bak-$STAMP" ]; then cp -p "$TRADER_DIR/users_panel.py.bak-$STAMP" "$TRADER_DIR/users_panel.py"
    else rm -f "$TRADER_DIR/users_panel.py"; fi
    for n in "${TPL_CHANGED[@]}"; do
      if [ -f "$TRADER_DIR/templates/$n.bak-$STAMP" ]; then cp -p "$TRADER_DIR/templates/$n.bak-$STAMP" "$TRADER_DIR/templates/$n"
      else rm -f "$TRADER_DIR/templates/$n"; fi
    done
    for f in "${APP_CHANGED[@]}"; do n="$(basename "$f")"; if [ -f "$TRADER_DIR/$n.bak-$STAMP" ]; then cp -p "$TRADER_DIR/$n.bak-$STAMP" "$TRADER_DIR/$n"; else rm -f "$TRADER_DIR/$n"; fi; done
    systemctl restart "${APP_SERVICE:-trader-app}" || true
  else
    echo "    Dashboard pages updated."
  fi
fi

# Algo Studio (studio/): the strategy pages at https://<address>/studio/, its own small web app on 127.0.0.1:8020.
# It signs people in through the dashboard (the browser's cookie is checked at the dashboard's /api/studio-auth) and
# reads the engine's runtime folder and the dashboard's backtest store. A Studio that does not start leaves the rest as it was.
if [ -d "$ENGINE/studio" ] && command -v systemctl >/dev/null; then
  echo "==> Algo Studio"
  DASH_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 5000))' "$TRADER_DIR/config.json" 2>/dev/null || echo 5000)"
  STUDIO_PORT="${STUDIO_PORT:-8020}"
  cat > /etc/systemd/system/algolab-studio.service <<UNIT
[Unit]
Description=Algo Studio: strategy pages (https://<address>/studio/)
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$ENGINE
Environment=RUNTIME=$ENGINE/runtime
Environment=BACKTESTS_STORE=$TRADER_DIR/data/backtests
Environment=STUDIO_DASHBOARD=http://127.0.0.1:$DASH_PORT
Environment=STUDIO_PORT=$STUDIO_PORT
ExecStart=$PY -m studio
Restart=on-failure
RestartSec=5
MemoryMax=300M
OOMScoreAdjust=600

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable -q algolab-studio || true
  systemctl restart algolab-studio || true
  CODE=""
  for _ in $(seq 1 20); do
    sleep 1
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$STUDIO_PORT/studio/api/me" 2>/dev/null || true)"
    [ "$CODE" = 401 ] && break
  done
  if [ "$CODE" = 401 ]; then
    [ -f "$SRC/deploy/studio_caddy.sh" ] && { bash "$SRC/deploy/studio_caddy.sh" "$STUDIO_PORT" || echo "    (Studio runs, but its web address was not added; see above)"; }
  else
    echo "    (Algo Studio did not start: $(journalctl -u algolab-studio -n 3 --no-pager 2>/dev/null | tail -1)). The rest is updated."
  fi
fi

# The server updates itself from GitHub when asked from the dashboard (Profile › Server version › Update).
# A path unit watches for the dashboard's request file and runs self_update.sh as root; that script pulls the
# merged version with a read-only deploy key and runs this same update script from it.
SELF="${SELF_UPDATE_HOME:-/opt/algo-trading}"
if [ -f "$SRC/deploy/self_update.sh" ] && command -v systemctl >/dev/null; then
  echo "==> Update from GitHub (the dashboard's Update button)"
  mkdir -p "$SELF/secrets" && chmod 700 "$SELF/secrets"
  install -m 755 "$SRC/deploy/self_update.sh" "$SELF/self_update.sh.new" && mv -f "$SELF/self_update.sh.new" "$SELF/self_update.sh"
  # the GitHub repo to pull from: stamped by the Mac's pack (deploy/REPO) or this copy's own remote, as ssh
  REPO="$(cat "$SRC/deploy/REPO" 2>/dev/null || git -C "$SRC" remote get-url origin 2>/dev/null || true)"
  REPO="$(echo "$REPO" | sed -E 's#^https://([^@/]+@)?github\.com/#git@github.com:#')"
  case "$REPO" in git@github.com:*) case "$REPO" in *.git) ;; *) REPO="$REPO.git" ;; esac
                                    echo "$REPO" > "$SELF/repo"; chmod 644 "$SELF/repo" ;; esac
  cat > /etc/systemd/system/algolab-selfupdate.service <<UNIT
[Unit]
Description=Algo Lab: update the server from GitHub (asked for on the dashboard)

[Service]
Type=oneshot
ExecStart=$SELF/self_update.sh run
TimeoutStartSec=1200
UNIT
  cat > /etc/systemd/system/algolab-selfupdate.path <<UNIT
[Unit]
Description=Algo Lab: watch for the dashboard's update request

[Path]
PathExists=$ENGINE/runtime/self-update.request
MakeDirectory=yes

[Install]
WantedBy=multi-user.target
UNIT
  # a quiet look at GitHub every 30 minutes, so the dashboard shows "Update waiting" without anyone tapping Check
  cat > /etc/systemd/system/algolab-selfupdate-check.service <<UNIT
[Unit]
Description=Algo Lab: see whether merged changes are waiting on GitHub (shown on the dashboard)

[Service]
Type=oneshot
ExecStart=$SELF/self_update.sh peek
TimeoutStartSec=120
Nice=10
UNIT
  cat > /etc/systemd/system/algolab-selfupdate-check.timer <<UNIT
[Unit]
Description=Algo Lab: look for merged changes on GitHub every 30 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=30min
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNIT
  # the IBKR snapshot from the Claude scheduled task (repo branch ibkr-snapshot) for the US page: the task runs 09:35 and
  # 16:05 New York time on weekdays, so look 10 minutes after each run, plus hourly as a fallback
  install -m 755 "$SRC/deploy/ibkr_snapshot_pull.sh" "$SELF/ibkr_snapshot_pull.sh"
  install -m 644 "$SRC/server/portfolio/server/ibkr/snapshot_import.py" "$SELF/ibkr_snapshot_import.py"
  cat > /etc/systemd/system/algolab-ibkr-snapshot.service <<UNIT
[Unit]
Description=Algo Lab: bring in the daily IBKR snapshot from GitHub for the US page

[Service]
Type=oneshot
ExecStart=$SELF/ibkr_snapshot_pull.sh
TimeoutStartSec=120
Nice=10
UNIT
  cat > /etc/systemd/system/algolab-ibkr-snapshot.timer <<UNIT
[Unit]
Description=Algo Lab: look for a new IBKR snapshot after the US open and close, and every hour

[Timer]
OnCalendar=Mon..Fri 09:45 America/New_York
OnCalendar=Mon..Fri 16:15 America/New_York
OnBootSec=5min
OnUnitActiveSec=1h
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable -q --now algolab-selfupdate.path || echo "    (could not start the watcher: $(systemctl is-failed algolab-selfupdate.path 2>&1))"
  systemctl enable -q --now algolab-selfupdate-check.timer || echo "    (could not start the GitHub check timer)"
  systemctl enable -q --now algolab-ibkr-snapshot.timer || echo "    (could not start the IBKR snapshot timer)"
  if [ ! -f "$SELF/secrets/deploy_key.pub" ]; then
    bash "$SELF/self_update.sh" check >/dev/null 2>&1 || true    # makes the key; the dashboard shows it to add on GitHub
  fi
fi
# IBKR start / stop from the dashboard (Summary › Health): a root script, kept where the dashboard cannot change it,
# run when runtime/ibkr.request appears (only does anything when IBKR is installed: deploy/add_us_stocks.sh)
if [ -f "$SRC/deploy/ibkr_control.sh" ] && command -v systemctl >/dev/null; then
  install -d -m 755 "$SELF"
  install -m 755 "$SRC/deploy/ibkr_control.sh" "$SELF/ibkr_control.sh.new" && mv -f "$SELF/ibkr_control.sh.new" "$SELF/ibkr_control.sh"
  cat > /etc/systemd/system/algolab-ibkr.service <<UNIT
[Unit]
Description=Algo Lab: start or stop IBKR when the dashboard asks

[Service]
Type=oneshot
Environment=ENGINE_DIR=$ENGINE
ExecStart=$SELF/ibkr_control.sh
TimeoutStartSec=300
UNIT
  cat > /etc/systemd/system/algolab-ibkr.path <<UNIT
[Unit]
Description=Algo Lab: watch for the dashboard's IBKR start / stop request

[Path]
PathExists=$ENGINE/runtime/ibkr.request
MakeDirectory=yes

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable -q --now algolab-ibkr.path || echo "    (could not start the IBKR control watcher)"
fi
# which version is running: the GitHub commit (from the repo copy, or stamped by the Mac's pack)
COMMIT="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || cat "$SRC/deploy/COMMIT" 2>/dev/null || true)"
[ -n "$COMMIT" ] && echo "$COMMIT" > "$ENGINE/.deployed_commit"

# The Market data page reads data/collect_log.txt, which the recording writes from now on. Fill it once
# with the earlier runs still in the system journal, so the page shows them too.
LOG="$ENGINE/data/collect_log.txt"
if [ ! -s "$LOG" ] && command -v journalctl >/dev/null; then
  echo "==> Copying earlier recording results from the system journal to the Market data page"
  journalctl -u algolab-collect -o json --no-pager 2>/dev/null | python3 -c '
import json, sys
from datetime import datetime, timedelta, timezone
ist = timezone(timedelta(hours=5, minutes=30))
out, last = [], ""
for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    msg = m.get("MESSAGE")
    if isinstance(msg, list):
        msg = bytes(msg).decode("utf-8", "replace")
    msg = str(msg or "").strip()
    when = datetime.fromtimestamp(int(m.get("__REALTIME_TIMESTAMP", 0)) / 1e6, ist).strftime("%Y-%m-%d %H:%M")
    if m.get("SYSLOG_IDENTIFIER") == "systemd":
        if "Failed with result" in msg:
            out.append(f"{when}  FAILED: {last or msg}")
        last = ""
    elif "NIFTY bars stored" in msg:
        out.append(msg)
    elif msg:
        last = msg[-300:]
print("\n".join(out))
' > "$LOG.tmp" || true
  if [ -s "$LOG.tmp" ]; then
    mv "$LOG.tmp" "$LOG" && chown "$RUN_USER:$RUN_USER" "$LOG"
    echo "    $(wc -l < "$LOG") earlier runs found"
  else
    rm -f "$LOG.tmp"
    echo "    none found in the journal; the log starts with the next recording"
  fi
fi

# The dashboard's web address follows the server's IP after a move (sslip.io), or is set to your own domain
# (DASH_HOST=dash.example.com, passed by deploy/update-server.command). Never stops the update.
if [ -f "$SRC/deploy/set_address.sh" ]; then
  bash "$SRC/deploy/set_address.sh" "${DASH_HOST:-}" || echo "    (web address left as it was; see the lines above)"
fi

echo "==> What the daily price recording will store (uses the dashboard's Zerodha login)"
(cd "$ENGINE" && as_user timeout 120 "$PY" run.py collect --plan) \
  || echo "    (Could not check right now - usually because there is no Zerodha login today. It runs at 3:45 pm IST.)"
echo
echo "Done. Next recording: $(systemctl list-timers algolab-collect.timer --no-pager | awk 'NR==2{print $1, $2, $3, $4}')"
echo "The previous code is kept in $BK"
