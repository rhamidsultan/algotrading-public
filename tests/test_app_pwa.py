"""The dashboard as a phone app: install files, passkey (Face ID / fingerprint) sign-in, push notifications."""
import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")
pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

HOST = "203-0-113-10.sslip.io"
HTTPS = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": HOST}


def cbor(v):
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        if n < 256:
            return bytes([major << 5 | 24, n])
        return bytes([major << 5 | 25]) + struct.pack(">H", n)
    if isinstance(v, bool):
        return bytes([0xF5 if v else 0xF4])
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        return head(3, len(v.encode())) + v.encode()
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(v)


class Authenticator:
    """A software passkey: what Face ID / a fingerprint sensor does, minus the face."""
    def __init__(self, rp=HOST):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = b"cred-" + bytes(range(11))
        self.rp, self.count = rp, 0

    def cose(self):
        n = self.key.public_key().public_numbers()
        return cbor({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def auth_data(self, attested, flags=0x05):
        self.count += 0
        ad = hashlib.sha256(self.rp.encode()).digest() + bytes([flags | (0x40 if attested else 0)]) + struct.pack(">I", self.count)
        if attested:
            ad += bytes(16) + struct.pack(">H", len(self.cred_id)) + self.cred_id + self.cose()
        return ad

    def create(self, options, origin=f"https://{HOST}"):
        cd = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": origin}).encode()
        att = cbor({"fmt": "none", "attStmt": {}, "authData": self.auth_data(True)})
        return {"id": b64u(self.cred_id), "clientDataJSON": b64u(cd), "attestationObject": b64u(att), "name": "iPhone"}

    def get(self, options, origin=f"https://{HOST}", flags=0x05):
        cd = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin}).encode()
        ad = self.auth_data(False, flags)
        sig = self.key.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "clientDataJSON": b64u(cd), "authenticatorData": b64u(ad), "signature": b64u(sig)}


def b64u(b):
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_app(tmp_path, monkeypatch, guarded=True):
    import importlib.util
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    (tmp_path / "runtime").mkdir(exist_ok=True)
    here = Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page"
    sys.modules.pop("pwa_panel", None)
    sys.modules.pop("users_panel", None)
    spec = importlib.util.spec_from_file_location("algolab_panel_app", here / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_profile_dir", lambda: tmp_path / "runtime")
    app = flask.Flask("t")
    app.secret_key = "s"

    if guarded:   # the dashboard's own sign-in check
        @app.before_request
        def require_signin():
            if flask.request.endpoint in ("signin", "static") or flask.session.get("user"):
                return None
            return ("not signed in", 401)
    app.add_url_rule("/signin", "signin", lambda: "<html><head></head><body><form method=post></form></body></html>")
    app.add_url_rule("/", "home", lambda: "<html><head></head><body>home</body></html>")
    app.register_blueprint(mod.panel)
    return mod, app, app.test_client()


def test_install_files_and_sign_in_page_are_public(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.get_json()["display"] == "standalone"
    assert c.get("/sw.js").status_code == 200 and b"showNotification" in c.get("/sw.js").data
    icon = c.get("/icons/icon-192.png")
    assert icon.status_code == 200 and icon.data[:4] == b"\x89PNG"
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/favicon.ico"):
        r = c.get(path)                                               # iPhone's default places, signed out
        assert r.status_code == 200 and r.data[:4] == b"\x89PNG", path
    assert c.get("/offline").status_code == 200
    assert c.get("/passkey/available", headers=HTTPS).get_json() == {"available": False}
    assert c.get("/api/push").status_code == 401                     # everything else still needs sign-in
    page = c.get("/signin").get_data(as_text=True)
    assert 'rel="manifest"' in page and 'id="pkBtn"' in page and "apple-touch-icon" in page


def test_passkey_register_and_sign_in(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    with c.session_transaction() as s:
        s["user"] = "admin"
    assert c.post("/api/passkey/register/options").status_code == 400              # plain HTTP: refused
    a = Authenticator()
    r = c.post("/api/passkey/register/options", headers=HTTPS)
    assert r.status_code == 200, (r.status_code, r.data[:200])
    opts = r.get_json()["publicKey"]
    assert opts["rp"]["id"] == HOST and opts["authenticatorSelection"]["userVerification"] == "required"
    bad = a.create(opts, origin="https://evil.example")
    assert c.post("/api/passkey/register", json=bad, headers=HTTPS).status_code == 400
    opts = c.post("/api/passkey/register/options", headers=HTTPS).get_json()["publicKey"]
    assert c.post("/api/passkey/register", json=a.create(opts), headers=HTTPS).get_json() == {"ok": True}
    keys = c.get("/api/passkeys", headers=HTTPS).get_json()
    assert keys["https"] and keys["passkeys"][0]["name"] == "iPhone"
    # sign out, then sign in with the passkey
    with c.session_transaction() as s:
        s.clear()
    assert c.get("/").status_code == 401
    assert c.get("/passkey/available", headers=HTTPS).get_json() == {"available": True}
    lo = c.post("/passkey/login/options", headers=HTTPS).get_json()["publicKey"]
    assert lo["rpId"] == HOST and lo["allowCredentials"] == []
    r = c.post("/passkey/login", json={**a.get(lo), "next": "/paper"}, headers=HTTPS)
    assert r.get_json() == {"ok": True, "next": "/paper"}
    assert c.get("/").status_code == 200
    # replay, wrong user verification, forged signature, foreign next address
    with c.session_transaction() as s:
        s.clear()
    assert c.post("/passkey/login", json=a.get(lo), headers=HTTPS).status_code == 400          # challenge used up
    lo = c.post("/passkey/login/options", headers=HTTPS).get_json()["publicKey"]
    assert c.post("/passkey/login", json=a.get(lo, flags=0x01), headers=HTTPS).status_code == 400   # no Face ID
    lo = c.post("/passkey/login/options", headers=HTTPS).get_json()["publicKey"]
    forged = Authenticator().get(lo)
    assert c.post("/passkey/login", json=forged, headers=HTTPS).status_code == 400
    lo = c.post("/passkey/login/options", headers=HTTPS).get_json()["publicKey"]
    r = c.post("/passkey/login", json={**a.get(lo), "next": "//evil.example"}, headers=HTTPS)
    assert r.get_json()["next"] == "/"
    # removing it
    with c.session_transaction() as s:
        s["user"] = "admin"
    pid = c.get("/api/passkeys", headers=HTTPS).get_json()["passkeys"][0]["id"]
    c.delete(f"/api/passkeys/{pid}")
    assert c.get("/passkey/available", headers=HTTPS).get_json() == {"available": False}


def test_push_encryption_matches_rfc8291():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page"))
    import pwa_panel as w
    priv = ec.derive_private_key(int.from_bytes(w.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                                 ec.SECP256R1())
    out = w.encrypt_push(b"When I grow up, I want to be a watermelon",
                         w.unb64u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
                         w.unb64u("BTBZMqHH6r4Tts7J_aSIgg"), salt=w.unb64u("DGv6ra1nlYgDCS1FRnbzlw"), sender_private=priv)
    assert w.b64u(out) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
                           "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_push_subscribe_queue_deliver_and_vapid(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    with c.session_transaction() as s:
        s["user"] = "admin"
    info = c.get("/api/push").get_json()
    assert len(info["public_key"]) > 80 and set(info["categories"]) == {"trading", "daily", "logins", "alerts"}
    ua = ec.generate_private_key(ec.SECP256R1())
    p256 = b64u(ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    sub = {"endpoint": "https://web.push.apple.com/QWERTY", "keys": {"p256dh": p256, "auth": b64u(b"0123456789abcdef")}}
    assert c.post("/api/push/subscribe", json={"subscription": sub, "name": "iPhone"}).get_json()["ok"]
    dev = c.get("/api/push").get_json()["devices"][0]
    c.post("/api/push/prefs", json={"id": dev["id"], "prefs": {"daily": False}})
    sent = []

    class Resp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=15):
        sent.append(req)
        return Resp()
    monkeypatch.setattr(mod._pwa.urllib.request, "urlopen", urlopen)
    mod.push("Paper session stopped: iron_fly", "Daily loss limit reached", "/paper", "trading")
    mod.push("Today: +₹1,200", "iron_fly: +₹1,200", "/paper", "daily")      # this device turned these off
    from algolab import pushbox                                              # the engine's way of asking
    monkeypatch.setenv("ALGOLAB_PUSH_DIR", str(tmp_path / "runtime" / "push_outbox"))
    pushbox.push("LIVE order filled", "SELL 65 NIFTY 24000 CE at 101.5", "/live", "trading")
    mod.push_deliver()
    assert len(sent) == 2 and all(r.full_url == sub["endpoint"] for r in sent)
    h = dict(sent[0].header_items())
    assert h["Content-encoding"] == "aes128gcm" and h["Authorization"].startswith("vapid t=")
    # the VAPID token is signed with the server's key and names the push service
    import base64
    token = h["Authorization"].split("t=")[1].split(",")[0]
    head, claims, sig = token.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    assert json.loads(pad(claims))["aud"] == "https://web.push.apple.com"
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    raw = pad(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pad(info["public_key"]))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert not list((tmp_path / "runtime" / "push_outbox").glob("*.json"))
    # a subscription the push service says is gone is dropped
    import urllib.error

    def gone(req, timeout=15):
        raise urllib.error.HTTPError(req.full_url, 410, "Gone", {}, None)
    monkeypatch.setattr(mod._pwa.urllib.request, "urlopen", gone)
    assert c.post("/api/push/test", json={}).status_code == 502
    assert c.get("/api/push").get_json()["devices"] == []


def test_profile_page_has_the_app_sections(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch, guarded=False)
    page = c.get("/profile").get_data(as_text=True)
    assert "Face ID / fingerprint sign-in" in page and "Notifications" in page and "Install as an app" in page
    assert 'rel="manifest"' in page
