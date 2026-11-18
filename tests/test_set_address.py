"""deploy/set_address.sh: after a move the dashboard's sslip.io name follows the new IP; your own domain stays."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CADDY = """198-51-100-7.sslip.io {
    reverse_proxy 127.0.0.1:5010
}
198-51-100-7.sslip.io:8443 {
    reverse_proxy 127.0.0.1:8601
}
"""


def _run(tmp_path, *args, caddyfile=CADDY, ip="65.2.10.20", dns="", caddy_ok=True):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    stubs = {"systemctl": "exit 0", "curl": "echo 200", "id": "echo 0",
             "caddy": "exit 0" if caddy_ok else "exit 1",
             "getent": f'[ -n "{dns}" ] && echo "{dns} STREAM x"'}
    for name, body in stubs.items():
        (b / name).write_text(f"#!/bin/bash\n{body}\n")
        (b / name).chmod(0o755)
    f = tmp_path / "Caddyfile"
    f.write_text(caddyfile)
    env = dict(os.environ, PATH=f"{b}:/usr/bin:/bin", CADDYFILE=str(f), ADDRESS_FILE=str(tmp_path / "address"),
               PUBLIC_IP=ip)
    r = subprocess.run(["bash", str(ROOT / "deploy" / "set_address.sh"), *args], env=env, capture_output=True, text=True)
    return r, f.read_text()


def test_sslip_name_follows_the_new_ip(tmp_path):
    r, text = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "198-51-100-7" not in text and text.count("65-2-10-20.sslip.io") == 2 and "65-2-10-20.sslip.io:8443 {" in text
    assert (tmp_path / "address").read_text().strip() == "65-2-10-20.sslip.io" and "/callback" in r.stdout


def test_nothing_changes_when_the_ip_is_the_same(tmp_path):
    r, text = _run(tmp_path, ip="198.51.100.7")
    assert r.returncode == 0 and text == CADDY and "Web address: https://198-51-100-7.sslip.io/" in r.stdout


def test_own_domain_needs_its_dns_first_and_then_stays(tmp_path):
    r, text = _run(tmp_path, "dash.example.com", dns="198.51.100.7")
    assert r.returncode == 1 and text == CADDY and "points to '198.51.100.7'" in r.stdout
    r, text = _run(tmp_path, "dash.example.com", dns="65.2.10.20")
    assert r.returncode == 0 and "dash.example.com {" in text and "sslip" not in text
    r, text2 = _run(tmp_path, caddyfile=text, ip="99.1.1.1")     # a later move: the domain stays
    assert text2 == text


def test_rejected_settings_are_put_back(tmp_path):
    r, text = _run(tmp_path, caddy_ok=False)
    assert r.returncode == 1 and text == CADDY


def test_updates_run_it_and_the_mac_route_ships_it():
    upd = (ROOT / "deploy" / "update_engine.sh").read_text()
    assert 'bash "$SRC/deploy/set_address.sh" "${DASH_HOST:-}" || echo' in upd
    pack = (ROOT / "deploy" / "make_server_update.sh").read_text()
    assert "deploy/set_address.sh" in pack and 'DASH_HOST=\\"\\${DASH_HOST:-}\\"' in pack
