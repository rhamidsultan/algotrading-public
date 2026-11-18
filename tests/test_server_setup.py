"""Anyone can set up their own server from the repo: no one's server address or GitHub repo in the code,
one command for a new server, and US stocks (IBKR) only when asked for."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
OK_IPS = {"127.0.0.1", "0.0.0.0"}


def _scripts():
    return sorted([*ROOT.glob("deploy/*.command"), *ROOT.glob("deploy/*.sh"), *ROOT.glob("server/portfolio/server/**/*.sh"),
                   *(ROOT / "server/portfolio/server/ibkr/mac").iterdir()])


def test_no_server_address_or_repo_written_into_the_code():
    for f in _scripts():
        text = f.read_text()
        ips = {ip for ip in IP.findall(text) if ip not in OK_IPS and not ip.startswith("203.0.113.")}   # 203.0.113.x: examples
        assert not ips, f"{f.name} has a server address: {ips}"
        assert not re.search(r"github\.com[:/][\w.-]+/[\w.-]+", text), f"{f.name} names a GitHub repo"
    panel = (ROOT / "deploy/portfolio_page/algolab_panel.py").read_text()
    assert not re.search(r"github\.com/(?!settings|\{)[\w.-]+/[\w.-]+", panel)


def test_launchers_ask_once_and_save_the_server(tmp_path):
    conf = tmp_path / "server.env"
    script = f'. "{ROOT}/deploy/server_login.sh"; echo "host=$ALGO_HOST key=$ALGO_KEY addr=$ALGO_SERVER_ADDR"'
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ALGO_SERVER_CONF": str(conf)}
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "No server set" in r.stdout and not conf.exists()     # no terminal: says so, never guesses
    conf.write_text('ALGO_HOST="ubuntu@203.0.113.5"\nALGO_KEY="/k.pem"\n')
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert "host=ubuntu@203.0.113.5 key=/k.pem addr=203.0.113.5" in r.stdout
    r = subprocess.run(["bash", "-c", script], env=dict(env, ALGO_HOST="server"), capture_output=True, text=True)
    assert "host=server key=/k.pem" in r.stdout                                          # the environment wins (tests)


def test_every_launcher_uses_the_saved_server():
    for f in [*ROOT.glob("deploy/*.command")]:
        text = f.read_text()
        if re.search(r"\bssh\b", text) and "make-friend-bundle" not in f.name:
            assert ". deploy/server_login.sh" in text, f.name
    for name in ("ibkr-login", "ibkr-done"):
        assert ".algo-trading/server.env" in (ROOT / "server/portfolio/server/ibkr/mac" / name).read_text()


def test_new_server_runs_the_steps_in_order_and_leaves_us_stocks_out():
    text = (ROOT / "deploy/new_server.sh").read_text()
    steps = ["server/portfolio/server/setup.sh", "server/portfolio/server/deploy.sh", "deploy/install_server.sh", "deploy/update_engine.sh"]
    at = [text.index("bash " + s) for s in steps]
    assert at == sorted(at) and "trader-paper" in text
    assert "bash deploy/add_us_stocks.sh" not in text.replace("sudo bash deploy/add_us_stocks.sh", "")   # only offered
    for f in ("new_server.sh", "add_https.sh", "add_us_stocks.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / f)]).returncode == 0
    assert (ROOT / "docs/SERVER_SETUP.md").exists()


def test_dashboard_deploy_finds_the_app_from_the_repo(tmp_path):
    text = (ROOT / "server/portfolio/server/deploy.sh").read_text()
    assert '[ -f "$SRC_DIR/app.py" ] || SRC_DIR=' in text


def test_update_engine_records_the_repo_for_the_update_button(tmp_path):
    text = (ROOT / "deploy/update_engine.sh").read_text()
    assert 'echo "$REPO" > "$SELF/repo"' in text
    conv = re.search(r"sed -E '([^']+)'", text[text.index('REPO="$(echo'):]).group(1)
    for url, want in (("https://github.com/a/b.git", "git@github.com:a/b.git"), ("git@github.com:a/b.git", "git@github.com:a/b.git")):
        assert subprocess.run(["sed", "-E", conv], input=url, capture_output=True, text=True).stdout.strip() == want
