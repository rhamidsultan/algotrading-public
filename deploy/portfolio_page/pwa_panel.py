"""The dashboard as a phone app: installable web app, Face ID / fingerprint sign-in, push notifications.

  Install      a web app manifest, icons and a service worker, so "Add to Home Screen" (iPhone, Safari)
               or "Install app" (Android, Chrome) gives a full-screen app with its own icon. Without a
               connection it shows a small offline page instead of a browser error.
  Passkeys     Face ID, Touch ID or an Android fingerprint as sign-in (WebAuthn). Set up per device on
               the Profile page; the password keeps working. Works on the HTTPS address only.
  Push         notifications through the phone's own push service (Web Push, VAPID): sessions that
               stop by themselves, live fills and rejections, protective stops, the end-of-day P&L, the
               morning Zerodha login, the IBKR login, and the portfolio's price alerts. Each device
               chooses which. On iPhone they need the app opened from the home screen (iOS 16.4+).

Anything on the server can ask for a notification by writing a JSON file
{"title", "body", "url", "category"} into <engine>/runtime/push_outbox/ (the Algo Lab engine does);
this module sends them within a few seconds. Categories: trading, daily, logins, alerts.

Loaded by algolab_panel.py (attach); uses only the standard library and `cryptography`, which the
dashboard already has through kiteconnect.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Response, jsonify, request, session

IST = timezone(timedelta(hours=5, minutes=30))
CATEGORIES = {"trading": "Trading: sessions stopping, live fills and rejections, protective stops",
              "daily": "End-of-day P&L summary (3:35 pm IST)",
              "logins": "Login reminders: Zerodha in the morning, IBKR when it needs you",
              "alerts": "Portfolio price alerts"}
PUBLIC_PATHS = ("/manifest.webmanifest", "/sw.js", "/offline", "/passkey/")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    s = str(s)
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---- a minimal CBOR reader (enough for WebAuthn attestation objects and COSE keys) --------------
def cbor_decode(data: bytes):
    def item(i):
        ib = data[i]
        major, info = ib >> 5, ib & 0x1F
        i += 1
        if info < 24:
            val = info
        elif info == 24:
            val, i = data[i], i + 1
        elif info == 25:
            val, i = struct.unpack(">H", data[i:i + 2])[0], i + 2
        elif info == 26:
            val, i = struct.unpack(">I", data[i:i + 4])[0], i + 4
        elif info == 27:
            val, i = struct.unpack(">Q", data[i:i + 8])[0], i + 8
        else:
            raise ValueError("indefinite-length CBOR is not supported")
        if major == 0:
            return val, i
        if major == 1:
            return -1 - val, i
        if major == 2:
            return bytes(data[i:i + val]), i + val
        if major == 3:
            return data[i:i + val].decode(), i + val
        if major == 4:
            out = []
            for _ in range(val):
                v, i = item(i)
                out.append(v)
            return out, i
        if major == 5:
            out = {}
            for _ in range(val):
                k, i = item(i)
                v, i = item(i)
                out[k] = v
            return out, i
        if major == 7:
            return {20: False, 21: True, 22: None}.get(info, None), i
        raise ValueError("unsupported CBOR item")
    value, end = item(0)
    return value, end


# ---- WebAuthn (passkeys) ---------------------------------------------------------------------------
def parse_auth_data(ad: bytes) -> dict:
    out = {"rp_id_hash": ad[:32], "flags": ad[32], "sign_count": struct.unpack(">I", ad[33:37])[0]}
    if out["flags"] & 0x40:              # attested credential data present
        cred_len = struct.unpack(">H", ad[53:55])[0]
        out["credential_id"] = ad[55:55 + cred_len]
        key, used = cbor_decode(ad[55 + cred_len:])
        out["public_key"] = ad[55 + cred_len:55 + cred_len + used]
        out["cose"] = key
    return out


def verify_signature(cose_bytes: bytes, signature: bytes, data: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    key, _ = cbor_decode(cose_bytes)
    try:
        if key.get(3) == -7:     # ES256
            pub = ec.EllipticCurvePublicNumbers(int.from_bytes(key[-2], "big"), int.from_bytes(key[-3], "big"),
                                                ec.SECP256R1()).public_key()
            pub.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            return True
        if key.get(3) == -257:   # RS256
            pub = rsa.RSAPublicNumbers(int.from_bytes(key[-2], "big"), int.from_bytes(key[-1], "big")).public_key()
            pub.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
            return True
    except InvalidSignature:
        return False
    return False


def check_client_data(raw: bytes, kind: str, challenge: str, origin: str) -> str | None:
    try:
        cd = json.loads(raw)
    except ValueError:
        return "Unreadable response from the device."
    if cd.get("type") != kind:
        return "Unexpected response type."
    if not challenge or not hmac.compare_digest(str(cd.get("challenge", "")), challenge):
        return "This request expired: try again."
    if cd.get("origin") != origin:
        return f"Use the dashboard's HTTPS address ({origin})."
    return None


# ---- Web Push (RFC 8291 message encryption, RFC 8292 VAPID) ---------------------------------------
def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt_push(payload: bytes, ua_public: bytes, auth_secret: bytes, salt: bytes | None = None,
                 sender_private=None) -> bytes:
    """aes128gcm content coding for one Web Push message (single record)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = salt or os.urandom(16)
    priv = sender_private or ec.generate_private_key(ec.SECP256R1())
    as_public = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = priv.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">I", 4096) + bytes([len(as_public)]) + as_public + body


def vapid_header(endpoint: str, private_pem: bytes, public_b64: str, subject: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    u = urllib.parse.urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": subject}, separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(private_pem, password=None)
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{claims}.{sig}, k={public_b64}"


def attach(P) -> None:
    """Add the app pieces to the dashboard panel module P (algolab_panel)."""
    panel = P.panel
    ICONS = {k: base64.b64decode(v) for k, v in ICONS_B64.items()}

    def store(name):
        return P._profile_dir() / name

    def read(name, default):
        return P._json(store(name), default)

    def write(name, data):
        d = P._profile_dir()
        d.mkdir(parents=True, exist_ok=True)
        P._write(d / name, data)

    def https_origin():
        """(rp_id, origin) when the page was opened over HTTPS (through Caddy), else (None, None)."""
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
        rp = host.split(":")[0]
        if proto != "https" or not rp or re.fullmatch(r"[\d.]+", rp):
            return None, None
        return rp, f"https://{host}"

    def me():
        return P.current_user() or P.owner_name()

    def owner_of(rec):
        """Records made before several logins existed belong to the owner."""
        return rec.get("user") or P.owner_name()

    # ---- install as an app ---------------------------------------------------------------------
    @panel.route("/manifest.webmanifest")
    def manifest():
        prof = P.load_profile()
        start = prof.get("start_page", "/") if prof.get("start_page") in P.START_PAGES else "/"
        m = {"name": "Portfolio", "short_name": "Portfolio", "id": "/", "start_url": start, "scope": "/",
             "display": "standalone", "background_color": "#0f1115", "theme_color": "#2f6fed",
             "description": "Portfolio, paper and live trading, backtests",
             "icons": [{"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                       {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                       {"src": "/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
             "shortcuts": [{"name": "Paper trading", "url": "/paper"}, {"name": "Live trading", "url": "/live"},
                           {"name": "Market data", "url": "/market-data"}]}
        return Response(json.dumps(m), mimetype="application/manifest+json")

    @panel.route("/icons/<name>.png")
    def icon(name):
        data = ICONS.get(name)
        if data is None:
            return Response(status=404)
        return Response(data, mimetype="image/png", headers={"Cache-Control": "public, max-age=604800"})

    @panel.route("/sw.js")
    def service_worker():
        return Response(SW_JS, mimetype="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @panel.route("/offline")
    def offline():
        return Response(OFFLINE_HTML, mimetype="text/html")

    HEAD = ('<link rel="manifest" href="/manifest.webmanifest">'
            '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">'
            '<link rel="icon" type="image/png" sizes="192x192" href="/icons/icon-192.png">'
            '<meta name="apple-mobile-web-app-capable" content="yes">'
            '<meta name="mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-title" content="Portfolio">'
            '<meta name="apple-mobile-web-app-status-bar-style" content="default">'
            '<meta name="theme-color" content="#2f6fed">'
            '<script>if("serviceWorker" in navigator&&location.protocol==="https:")'
            'navigator.serviceWorker.register("/sw.js",{scope:"/"}).catch(function(){});</script>')

    def add_head(html, path):
        if 'rel="manifest"' in html or "</head>" not in html:
            return html
        html = html.replace("</head>", HEAD + "\n</head>", 1)
        if path.startswith("/signin"):
            html = html.replace("</body>", PASSKEY_SIGNIN + "\n</body>", 1)
        return html

    P.HTML_FILTERS.append(add_head)

    # ---- passkeys -------------------------------------------------------------------------------
    def passkeys():
        return read("passkeys.json", []) or []

    def my_passkeys():
        return [p for p in passkeys() if owner_of(p) == me()]

    @panel.route("/api/passkeys")
    def api_passkeys():
        rp, _ = https_origin()
        return jsonify(https=bool(rp), passkeys=[{k: p.get(k) for k in ("id", "name", "created", "last_used")}
                                                 for p in my_passkeys()])

    @panel.route("/api/passkeys/<pid>", methods=["DELETE"])
    def api_passkey_delete(pid):
        keep = [p for p in passkeys() if not (p["id"] == pid and owner_of(p) == me())]
        write("passkeys.json", keep)
        return jsonify(ok=True)

    def remove_user_keys(user):
        """Used by the Users page: remove every Face ID / fingerprint key of one login."""
        keep = [p for p in passkeys() if owner_of(p) != user]
        removed = len(passkeys()) - len(keep)
        write("passkeys.json", keep)
        write("push_subs.json", [s for s in subs() if owner_of(s) != user])
        return removed

    P.remove_user_keys = remove_user_keys
    P.passkey_count = lambda user: sum(1 for p in passkeys() if owner_of(p) == user)

    @panel.route("/api/passkey/register/options", methods=["POST"])
    def passkey_register_options():
        rp, _ = https_origin()
        if not rp:
            return jsonify(error="Face ID / fingerprint sign-in works on the HTTPS address only."), 400
        ch = b64u(os.urandom(32))
        session["pk_reg"] = ch
        user = me()
        name = P.load_profile().get("name") or user
        return jsonify(publicKey={
            "challenge": ch, "rp": {"name": "Portfolio", "id": rp},
            "user": {"id": b64u(hashlib.sha256(user.encode()).digest()[:16]), "name": user, "displayName": name},
            "pubKeyCredParams": [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
            "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True,
                                       "userVerification": "required"},
            "excludeCredentials": [{"type": "public-key", "id": p["id"]} for p in my_passkeys()],
            "timeout": 60000, "attestation": "none"})

    @panel.route("/api/passkey/register", methods=["POST"])
    def passkey_register():
        rp, origin = https_origin()
        body = request.get_json(silent=True) or {}
        ch = session.pop("pk_reg", None)
        if not rp:
            return jsonify(error="Use the HTTPS address."), 400
        try:
            err = check_client_data(unb64u(body["clientDataJSON"]), "webauthn.create", ch, origin)
            if err:
                return jsonify(error=err), 400
            att, _ = cbor_decode(unb64u(body["attestationObject"]))
            ad = parse_auth_data(att["authData"])
        except (KeyError, ValueError, TypeError, IndexError):
            return jsonify(error="The device's answer could not be read."), 400
        if ad["rp_id_hash"] != hashlib.sha256(rp.encode()).digest():
            return jsonify(error="The passkey was made for another address."), 400
        if not (ad["flags"] & 0x01 and ad["flags"] & 0x04):
            return jsonify(error="Face ID / fingerprint was not confirmed."), 400
        if "credential_id" not in ad or ad["cose"].get(3) not in (-7, -257):
            return jsonify(error="This kind of passkey is not supported."), 400
        name = re.sub(r"\s+", " ", str(body.get("name") or "This device")).strip()[:40]
        keys = [p for p in passkeys() if p["id"] != b64u(ad["credential_id"])]
        keys.append({"id": b64u(ad["credential_id"]), "public_key": b64u(ad["public_key"]),
                     "sign_count": ad["sign_count"], "name": name, "rp_id": rp, "user": me(),
                     "created": datetime.now(IST).strftime("%Y-%m-%d %H:%M"), "last_used": ""})
        write("passkeys.json", keys)
        return jsonify(ok=True)

    def passkey_login_options():
        rp, _ = https_origin()
        if not rp or not passkeys():
            return jsonify(error="No Face ID / fingerprint sign-in is set up for this address."), 400
        ch = b64u(os.urandom(32))
        session["pk_login"] = ch
        return jsonify(publicKey={"challenge": ch, "rpId": rp, "userVerification": "required", "timeout": 60000,
                                  "allowCredentials": []})

    def passkey_login():
        rp, origin = https_origin()
        body = request.get_json(silent=True) or {}
        ch = session.pop("pk_login", None)
        if not rp:
            return jsonify(error="Use the HTTPS address."), 400
        keys = passkeys()
        cred = next((p for p in keys if p["id"] == body.get("id")), None)
        if cred is None:
            return jsonify(error="This passkey is not registered here (it may have been removed)."), 400
        user = owner_of(cred)
        if not P.user_active(user):
            return jsonify(error="This login is switched off. Ask the owner."), 403
        try:
            cdj = unb64u(body["clientDataJSON"])
            err = check_client_data(cdj, "webauthn.get", ch, origin)
            if err:
                return jsonify(error=err), 400
            ad_raw = unb64u(body["authenticatorData"])
            ad = parse_auth_data(ad_raw)
            sig = unb64u(body["signature"])
        except (KeyError, ValueError, TypeError, IndexError):
            return jsonify(error="The device's answer could not be read."), 400
        if ad["rp_id_hash"] != hashlib.sha256(rp.encode()).digest() or not (ad["flags"] & 0x01 and ad["flags"] & 0x04):
            return jsonify(error="Face ID / fingerprint was not confirmed."), 400
        if not verify_signature(unb64u(cred["public_key"]), sig, ad_raw + hashlib.sha256(cdj).digest()):
            return jsonify(error="Sign-in check failed."), 400
        if ad["sign_count"] and cred.get("sign_count") and ad["sign_count"] <= cred["sign_count"]:
            return jsonify(error="This passkey looks copied; sign in with the password."), 400
        cred["sign_count"] = ad["sign_count"]
        cred["last_used"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
        write("passkeys.json", keys)
        session.clear()
        session["user"] = user
        session["uv"] = P.user_version(user)
        session.permanent = True
        try:
            P.log_activity("signin (Face ID / fingerprint)", user=user)
        except Exception:  # noqa: BLE001
            pass
        nxt = str(body.get("next") or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        return jsonify(ok=True, next=nxt)

    def passkey_available():
        rp, _ = https_origin()
        return jsonify(available=bool(rp and passkeys()))

    ROOT_ICONS = {"/apple-touch-icon.png": "apple-180", "/apple-touch-icon-precomposed.png": "apple-180",
                  "/apple-touch-icon-180x180.png": "apple-180", "/favicon.ico": "icon-192"}

    PUBLIC = {"/passkey/login/options": passkey_login_options, "/passkey/login": passkey_login,
              "/passkey/available": passkey_available, "/manifest.webmanifest": manifest, "/sw.js": service_worker,
              "/offline": offline}

    @panel.record_once
    def _public_first(state):
        """These answer before sign-in (the dashboard's own check would send them to the sign-in page)."""
        def public():
            fn = PUBLIC.get(request.path)
            if fn is not None:
                return fn()
            m = re.fullmatch(r"/icons/([a-z0-9-]+)\.png", request.path)
            if m:
                return icon(m.group(1))
            if request.path in ROOT_ICONS:        # where iPhones and browsers look by default
                return icon(ROOT_ICONS[request.path])
            return None
        state.app.before_request_funcs.setdefault(None, []).insert(0, public)
        start_sender(state.app)

    # ---- push notifications ----------------------------------------------------------------------
    def vapid():
        v = read("vapid.json", None)
        if v:
            return v
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        v = {"private_pem": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()).decode(),
             "public": b64u(key.public_key().public_bytes(serialization.Encoding.X962,
                                                          serialization.PublicFormat.UncompressedPoint))}
        write("vapid.json", v)
        try:
            os.chmod(store("vapid.json"), 0o600)
        except OSError:
            pass
        return v

    def subs():
        return read("push_subs.json", []) or []

    def my_subs():
        return [s for s in subs() if owner_of(s) == me()]

    def my_categories():
        return {k: v for k, v in CATEGORIES.items() if P.push_allowed(me(), {"category": k})}

    def device_id(endpoint):
        return hashlib.sha256(endpoint.encode()).hexdigest()[:12]

    @panel.route("/api/push")
    def api_push():
        try:
            pub = vapid()["public"]
        except Exception as e:  # noqa: BLE001
            return jsonify(error=f"Notifications are not available on this server: {e}"), 500
        return jsonify(public_key=pub, categories=my_categories(),
                       devices=[{"id": device_id(s["endpoint"]), "name": s.get("name"), "created": s.get("created"),
                                 "prefs": s.get("prefs"), "last_sent": s.get("last_sent", "")} for s in my_subs()])

    @panel.route("/api/push/subscribe", methods=["POST"])
    def api_push_subscribe():
        body = request.get_json(silent=True) or {}
        sub = body.get("subscription") or {}
        ep, keys = sub.get("endpoint", ""), sub.get("keys") or {}
        if not ep.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            return jsonify(error="The browser gave an incomplete subscription."), 400
        prefs = {k: bool((body.get("prefs") or {}).get(k, True)) for k in CATEGORIES}
        lst = [s for s in subs() if s["endpoint"] != ep]
        lst.append({"endpoint": ep, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}, "prefs": prefs, "user": me(),
                    "name": re.sub(r"\s+", " ", str(body.get("name") or "Device")).strip()[:40],
                    "created": datetime.now(IST).strftime("%Y-%m-%d %H:%M")})
        write("push_subs.json", lst)
        return jsonify(ok=True, id=device_id(ep))

    @panel.route("/api/push/prefs", methods=["POST"])
    def api_push_prefs():
        body = request.get_json(silent=True) or {}
        lst = subs()
        for s in lst:
            if device_id(s["endpoint"]) == body.get("id") and owner_of(s) == me():
                s["prefs"] = {k: bool((body.get("prefs") or {}).get(k, s["prefs"].get(k, True))) for k in CATEGORIES}
        write("push_subs.json", lst)
        return jsonify(ok=True)

    @panel.route("/api/push/remove", methods=["POST"])
    def api_push_remove():
        body = request.get_json(silent=True) or {}
        write("push_subs.json", [s for s in subs() if not (device_id(s["endpoint"]) == body.get("id") and owner_of(s) == me())])
        return jsonify(ok=True)

    @panel.route("/api/push/test", methods=["POST"])
    def api_push_test():
        body = request.get_json(silent=True) or {}
        target = [s for s in my_subs() if not body.get("id") or device_id(s["endpoint"]) == body.get("id")]
        if not target:
            return jsonify(error="No device has notifications turned on yet."), 400
        results = [send(s, {"title": "Portfolio", "body": "Test notification: notifications work on this device.",
                            "url": "/profile", "category": "test"}) for s in target]
        if not any(results):
            return jsonify(error="The push service refused the message (see the server log)."), 502
        return jsonify(ok=True, sent=sum(1 for r in results if r))

    def send(sub, msg) -> bool:
        """Encrypt and send one notification; drops subscriptions the push service says are gone."""
        try:
            v = vapid()
            payload = json.dumps(msg).encode()[:3000]
            body = encrypt_push(payload, unb64u(sub["keys"]["p256dh"]), unb64u(sub["keys"]["auth"]))
            email = P.load_profile(P.owner_name()).get("email") or ""
            subject = f"mailto:{email}" if email else "mailto:portfolio-dashboard@example.com"
            req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
                "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "86400",
                "Urgency": "high" if msg.get("category") in ("trading", "test") else "normal",
                "Authorization": vapid_header(sub["endpoint"], v["private_pem"].encode(), v["public"], subject)})
            with urllib.request.urlopen(req, timeout=15) as r:
                ok = 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                write("push_subs.json", [s for s in subs() if s["endpoint"] != sub["endpoint"]])
            print(f"push: {e.code} {e.read()[:200]!r}")
            return False
        except Exception as e:  # noqa: BLE001
            print(f"push failed: {e}")
            return False
        lst = subs()
        for s in lst:
            if s["endpoint"] == sub["endpoint"]:
                s["last_sent"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
        write("push_subs.json", lst)
        return ok

    def push(title, body, url="/", category="trading"):
        """Queue a notification (sent by the background sender within seconds)."""
        out = P.RT / "push_outbox"
        out.mkdir(parents=True, exist_ok=True)
        f = out / f"{time.time():.6f}-{os.getpid()}.json"
        f.write_text(json.dumps({"title": title, "body": body, "url": url, "category": category}), encoding="utf-8")

    P.push = push

    def deliver_outbox():
        out = P.RT / "push_outbox"
        if not out.is_dir():
            return
        for f in sorted(out.glob("*.json"))[:50]:
            msg = P._json(f, None)
            f.unlink(missing_ok=True)
            if not msg:
                continue
            cat = msg.get("category", "trading")
            for s in subs():
                if (s.get("prefs") or {}).get(cat, True) and P.push_allowed(owner_of(s), msg):
                    send(s, msg)

    # ---- scheduled checks: end-of-day P&L, Zerodha login, IBKR login ------------------------------
    state = {"daily": "", "zerodha": "", "ibkr_at": 0.0}

    def daily_summary(now):
        today = now.date().isoformat()
        lines, total = [], 0.0
        for x in P._slots():
            s = P._json(x["state"], None)
            if not s or not str(s.get("time", "")).startswith(today):
                continue
            an = P._pnl_analysis(x["equity"], s["broker"]["capital"], s["broker"]["trades"], limit=5)
            day = next((d for d in an["daily"] if d["date"] == today), None)
            if day is None:
                continue
            total += day["day"]
            lines.append(f"{s.get('label') or x['id']}: {'+' if day['day'] >= 0 else '-'}₹{abs(day['day']):,.0f}")
        if lines:
            push(f"Today: {'+' if total >= 0 else '-'}₹{abs(total):,.0f} across {len(lines)} session(s)",
                 "; ".join(lines)[:300], "/paper", "daily")

    def zerodha_logged_in_today(now):
        base = Path(P.__file__).resolve().parent
        cfg = P._json(base / "config.json", {}) or {}
        tok = base / cfg.get("token_file", "data/access_token.json")
        try:
            return datetime.fromtimestamp(tok.stat().st_mtime, IST).date() == now.date()
        except OSError:
            return True        # unknown setup: stay quiet

    def ibkr_stale(now):
        base = Path(P.__file__).resolve().parent
        snap = base / "data" / "ibkr" / "ibkr_snapshot.json"
        if not snap.exists():
            return False
        try:
            from zoneinfo import ZoneInfo
            ny = now.astimezone(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001
            return False
        open_ = ny.weekday() < 5 and (ny.hour, ny.minute) >= (9, 45) and ny.hour < 16
        return open_ and time.time() - snap.stat().st_mtime > 25 * 60

    def scheduled(now):
        hm = now.strftime("%H:%M")
        main = sys.modules.get("__main__")
        try:   # trading days only: no login reminder on weekends or NSE holidays
            weekday = bool(main.trading_day(now.date())) if hasattr(main, "trading_day") else now.weekday() < 5
        except Exception:  # noqa: BLE001
            weekday = now.weekday() < 5
        if weekday and "15:35" <= hm < "15:50" and state["daily"] != now.date().isoformat():
            state["daily"] = now.date().isoformat()
            daily_summary(now)
        for slot in ("08:50", "09:05"):
            key = f"{now.date()} {slot}"
            if weekday and slot <= hm < slot[:3] + str(int(slot[3:]) + 5).zfill(2) and state["zerodha"] != key:
                state["zerodha"] = key
                if not zerodha_logged_in_today(now):
                    push("Zerodha login needed", "Log in to Zerodha for today's prices and sessions.", "/login", "logins")
        if time.time() - state["ibkr_at"] > 4 * 3600 and ibkr_stale(now):
            state["ibkr_at"] = time.time()
            push("IBKR needs a login", "US holdings have not updated: run ibkr-login on the Mac and approve in IBKR Mobile.",
                 "/us", "logins")

    def wrap_portfolio_alerts():
        main = sys.modules.get("__main__")
        orig = getattr(main, "notify", None)
        if not callable(orig) or getattr(orig, "_pushes", False):
            return

        def notify(subject, body, *a, **k):
            try:
                push(str(subject)[:120], str(body)[:300], "/", "alerts")
            except Exception:  # noqa: BLE001
                pass
            return orig(subject, body, *a, **k)
        notify._pushes = True
        main.notify = notify

    def start_sender(app):
        if os.environ.get("ALGOLAB_PUSH_SENDER", "1") == "0" or "pytest" in sys.modules:
            return

        def loop():
            wrap_portfolio_alerts()
            while True:
                try:
                    deliver_outbox()
                    scheduled(datetime.now(IST))
                except Exception as e:  # noqa: BLE001
                    print(f"push sender: {e}")
                time.sleep(5)
        threading.Thread(target=loop, name="push-sender", daemon=True).start()

    P.push_deliver, P.push_scheduled, P.push_send = deliver_outbox, scheduled, send


# ---- pieces of the pages ------------------------------------------------------------------------------
SW_JS = r"""// Portfolio dashboard service worker: offline page, push notifications.
const CACHE = "portfolio-v1";
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(["/offline", "/icons/icon-192.png", "/icons/badge-96.png"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;             // pages only; data always comes from the server
  e.respondWith(fetch(e.request).catch(() => caches.match("/offline")));
});
self.addEventListener("push", e => {
  let m = {};
  try { m = e.data ? e.data.json() : {}; } catch (_) { m = {title: "Portfolio", body: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(m.title || "Portfolio", {
    body: m.body || "", icon: "/icons/icon-192.png", badge: "/icons/badge-96.png",
    tag: m.tag || m.category || undefined, renotify: true, data: {url: m.url || "/"}}));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(ws => {
    for (const w of ws) { if ("focus" in w) { w.navigate(url); return w.focus(); } }
    return clients.openWindow(url);
  }));
});
"""

OFFLINE_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Offline · Portfolio</title>
<style>:root{--bg:#f5f6f8;--text:#16181d;--muted:#646b78;--accent:#2f6fed}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--text:#e8eaee;--muted:#8b93a1;--accent:#6c9bff}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,sans-serif;text-align:center;padding:24px}
img{width:72px;height:72px;border-radius:16px}button{margin-top:16px;padding:10px 18px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-size:15px}
p{color:var(--muted)}</style></head><body><div><img src="/icons/icon-192.png" alt=""><h2>No connection</h2>
<p>The dashboard needs the internet. Your sessions keep running on the server.</p>
<button onclick="location.reload()">Try again</button></div></body></html>"""

PASSKEY_SIGNIN = r"""<div id="pkBox" style="display:none;width:100%;max-width:340px;margin:12px auto 0">
<button type="button" id="pkBtn" style="width:100%;padding:11px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text);font-size:15px;cursor:pointer">Sign in with Face ID / fingerprint</button>
<div id="pkErr" style="color:var(--down);font-size:14px;margin-top:8px"></div></div>
<script>
(function(){
  const u = s => Uint8Array.from(atob(s.replace(/-/g,"+").replace(/_/g,"/") + "===".slice((s.length + 3) % 4)), c => c.charCodeAt(0));
  const e = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
  const box = document.getElementById("pkBox"), btn = document.getElementById("pkBtn"), err = document.getElementById("pkErr");
  const form = document.querySelector("form");
  if (form && box) { box.style.display = "none"; form.parentNode.insertBefore(box, form.nextSibling); }
  if (!window.PublicKeyCredential || location.protocol !== "https:") return;
  fetch("/passkey/available", {cache: "no-store"}).then(r => r.json()).then(j => { if (j.available) box.style.display = "block"; }).catch(() => {});
  async function go() {
    err.textContent = "";
    try {
      const o = await (await fetch("/passkey/login/options", {method: "POST"})).json();
      if (o.error) throw new Error(o.error);
      const pk = o.publicKey; pk.challenge = u(pk.challenge);
      const c = await navigator.credentials.get({publicKey: pk});
      const r = await fetch("/passkey/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        id: c.id, clientDataJSON: e(c.response.clientDataJSON), authenticatorData: e(c.response.authenticatorData),
        signature: e(c.response.signature), next: new URLSearchParams(location.search).get("next") || "/"})});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Sign-in failed");
      location.href = j.next || "/";
    } catch (x) { if (x && x.name !== "NotAllowedError") err.textContent = x.message || String(x); }
  }
  btn.onclick = go;
})();
</script>"""


ICONS_B64 = {   # app icons (PNG)
    "icon-192": "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",
    "icon-512": "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qB8yiYyBIuSgERERNUIRAKFJZIwL9UNDY74BgElPVdf71Uyvbv9j8x8REVFDFAEgqg5QM31ozJfaloADv/5GNtfnPeMlkpu6sMTtf0RERI1RAlBjkxJUSq+3NAfbzf/FlDygYjo7o9l+sWi38xKpTUIGfyIiokYqAUgYltRLpDYpFu12ANDZCTF3VZcBgorZxXhpC337hQFERERUz0UAOOOlbVAxuwBRH4Dpf+MhifID2bU68ef+fyIiokZLAUSiWA+g/42HxDtsyo0hjnswoaIfcmEJWM0VwURERFTXjAtLUNEP7XHcg4nDptwYCgBMP7W7rVysvGys36Kuwh4AIiKixioAqJiEuLCc81OJzRec394Trf8XdXPjpTPqePkPERFR4xFRF6jx0pmgqJsD1XK/wu1mvYynqmwAJCIiasgagDrrZTyF220wAYDotiLVXICIiIgaMgcQMYDotkMqAGZH5wKeAEhERNSoBOJcAIXZEQDM9h1P+gAmqYaAMgEgIiJqzPk/RDUEgEnbdzzpe+n2dBrqttKgBAGEiwBERESNWACAaFAC1G2Vbk+nTQLZ94jYjGrI0E9ERNTIRQANVcRmEsi+xzPwplibyLhKUSHcAkhERNSgNQCBC9UkUhk4N8WISBu49E9ERDReEgGISJsR6N5ifKjwEiAiIqJGpgInxodA9zaADETb/9kCQERE1OApQPVLBjynbjvREKIqECYBREREjUpURTWEU7edAbAd1IF9AERERA2fAiCK+djOiGiZA0JERDSO0gDRslHl4j8REdF4ogr1RPB+DUsQ3gNARETU4FN/SDXmv98Y8ZqrPQBMAIiIiBo7AxCogxGv2agGXAIgIiIaR1QDNZz5ExERjb9KgMcWQCIiovHHcAiIiIiYABARERETACIiImICQERERA3B4xYAIiIiVgCIiIiICQARERExASAiIiImAERERMQEgIiIiJgAEBERERMAIiIiig2eA0BERDQeEwDeBkhERDT+cAmAiIiICQARERExASAiIiImAERERMQEgIiIiJgAEBERUb3gOQBERESsABARERETACIiImICQEREREwAiIiIiAkAERERMQEgIiKi2OJtgEREROMxAeA5AEREROMPlwCIiIiYABARERETACIiImICQEREREwAiIiIiAkAERERMQEgIiIiJgBEREQUHzwIiIiI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",
    "maskable-512": "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",
    "apple-180": "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",
    "badge-96": "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",
}
