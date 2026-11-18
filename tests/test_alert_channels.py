"""Email (Gmail) and Telegram alerts: set on Profile, saved in the dashboard's config.json, sent by the engine in the
background so an alert never holds up an order."""
import json
import sys
import threading
import time
import types

import pytest

from test_defined_risk import _panel


@pytest.fixture
def app(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    mod = _panel(tmp_path, monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"dashboard_user": "admin", "port": 5010,
                                    "email": {"enabled": False, "from": "you@gmail.com",
                                              "app_password": "PASTE_GMAIL_APP_PASSWORD", "to": "you@gmail.com"},
                                    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""}}))
    sent = []
    main = types.SimpleNamespace(CONFIG_PATH=cfg_path, CFG=json.loads(cfg_path.read_text()),
                                 notify=lambda subj, body: sent.append((subj, body)) or ["email sent", "telegram sent"])
    monkeypatch.setitem(sys.modules, "__main__", main)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    return a.test_client(), mod, cfg_path, main, sent


TOKEN = "123456789:AAH" + "x" * 30


def test_settings_are_shown_without_secrets_and_saved(app):
    c, mod, path, main, _ = app
    v = c.get("/api/alert-channels").get_json()
    assert v["email"] == {"enabled": False, "from": "you@gmail.com", "to": "you@gmail.com", "has_password": False}
    r = c.post("/api/alert-channels", json={"email": {"enabled": True, "from": "me@gmail.com", "to": "",
                                                      "app_password": "abcd efgh ijkl mnop"},
                                            "telegram": {"enabled": True, "bot_token": TOKEN, "chat_id": "98765"}})
    assert r.status_code == 200, r.get_json()
    out = r.get_json()
    assert out["email"]["has_password"] and out["telegram"]["has_token"] and "app_password" not in json.dumps(out)
    saved = json.loads(path.read_text())
    assert saved["email"]["app_password"] == "abcdefghijklmnop" and saved["email"]["to"] == "me@gmail.com"
    assert saved["telegram"] == {"enabled": True, "bot_token": TOKEN, "chat_id": "98765"}
    assert saved["dashboard_user"] == "admin" and saved["port"] == 5010          # the rest of the settings kept
    assert path.stat().st_mode & 0o077 == 0                                     # readable by its owner only
    assert main.CFG["email"]["enabled"] and main.CFG["telegram"]["chat_id"] == "98765"   # used at once
    # an empty password or token keeps the saved one
    c.post("/api/alert-channels", json={"email": {"enabled": True, "from": "me@gmail.com", "to": "x@y.com"},
                                        "telegram": {"enabled": False, "chat_id": "98765"}})
    saved = json.loads(path.read_text())
    assert saved["email"]["app_password"] == "abcdefghijklmnop" and saved["telegram"]["bot_token"] == TOKEN


def test_bad_settings_are_refused_with_a_reason(app):
    c, *_ = app
    r = c.post("/api/alert-channels", json={"email": {"enabled": True, "from": "me@gmail.com"}, "telegram": {}})
    assert r.status_code == 400 and "app password" in r.get_json()["error"]
    r = c.post("/api/alert-channels", json={"email": {}, "telegram": {"enabled": True, "bot_token": "nope", "chat_id": "1"}})
    assert r.status_code == 400 and "BotFather" in r.get_json()["error"]
    r = c.post("/api/alert-channels", json={"email": {}, "telegram": {"enabled": True, "bot_token": TOKEN, "chat_id": ""}})
    assert r.status_code == 400 and "Find my chat id" in r.get_json()["error"]


def test_send_a_test_goes_through_the_dashboard_sender(app):
    c, mod, path, main, sent = app
    r = c.post("/api/alert-channels/test").get_json()
    assert r["result"] == ["email sent", "telegram sent"] and sent and "test" in sent[0][0].lower()


def test_find_my_chat_id(app, monkeypatch):
    c, mod, *_ = app
    calls = []

    def fake(token, method, data=None, timeout=15):
        calls.append(method)
        if method == "getMe":
            return {"ok": True, "result": {"username": "rosh_algo_bot"}}
        return {"ok": True, "result": [{"message": {"chat": {"id": 111, "first_name": "Old"}}},
                                       {"message": {"chat": {"id": 98765, "first_name": "Hamid"}}}]}
    monkeypatch.setattr(mod, "_telegram", fake)
    r = c.post("/api/alert-channels/telegram-chat", json={"bot_token": TOKEN}).get_json()
    assert r["chat_id"] == "98765" and r["name"] == "Hamid" and r["bot"] == "rosh_algo_bot"   # the newest chat
    monkeypatch.setattr(mod, "_telegram", lambda t, m, d=None, timeout=15: {"result": {"username": "b"}} if m == "getMe" else {"result": []})
    r = c.post("/api/alert-channels/telegram-chat", json={"bot_token": TOKEN})
    assert r.status_code == 404 and "send any message to @b" in r.get_json()["error"]


def test_alert_settings_are_owner_only():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "users_panel_t", Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page" / "users_panel.py")
    up = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(up)
    for p in ("/api/alert-channels", "/api/alert-channels/test", "/api/alert-channels/telegram-chat"):
        assert p in up.OWNER_EXACT


def test_engine_alerts_are_sent_in_the_background(tmp_path, monkeypatch):
    from algolab import live
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"email": {"enabled": True, "from": "me@gmail.com", "to": "me@gmail.com",
                                         "app_password": "abcdefghijklmnop"},
                               "telegram": {"enabled": True, "bot_token": TOKEN, "chat_id": "98765"}}))
    release, got = threading.Event(), []

    class SMTP:
        def __init__(self, host, port, timeout):
            release.wait(5)                            # a slow Gmail login
            got.append(("smtp", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            got.append(("login", user))

        def send_message(self, msg):
            got.append(("mail", msg["Subject"], msg["To"]))

    monkeypatch.setattr(live.smtplib, "SMTP_SSL", SMTP)
    monkeypatch.setattr(live.urllib.request, "urlopen", lambda url, data, timeout: got.append(("tg", url.split("/")[-1], data)))
    logs = []
    notify = live.make_notifier(str(cfg), logs.append)
    t0 = time.monotonic()
    notify("Algo Lab: BUY 65 NIFTY26N1724300CE", "at 10.10 (Exit); order 1")
    assert time.monotonic() - t0 < 0.5                 # the order code is not held up by the slow login
    assert logs and logs[0].startswith("ALERT: Algo Lab: BUY 65")
    release.set()
    for _ in range(100):
        if any(g[0] == "tg" for g in got):
            break
        time.sleep(0.02)
    assert ("mail", "Algo Lab: BUY 65 NIFTY26N1724300CE", "me@gmail.com") in got
    tg = next(g for g in got if g[0] == "tg")
    assert tg[1] == "sendMessage" and b"chat_id=98765" in tg[2]
