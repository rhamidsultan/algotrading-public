"""Morning login page for Kite Connect.

Open  http://<SERVER_IP>:8080/  on your phone each trading morning, tap
"Login to Kite", complete the Zerodha login + 2FA, and the server stores
today's access token for your trading script to use.

If Zerodha's redirect does not come back to this page automatically, copy the
address from the browser's address bar after login and paste it in the box.
"""
import os
from urllib.parse import parse_qs, urlparse

from flask import Flask, request
from kiteconnect import KiteConnect
from markupsafe import escape
from waitress import serve

from kite_session import API_KEY, read_token, save_token

API_SECRET = os.environ["KITE_API_SECRET"]
USER_ID = os.environ["KITE_USER_ID"].strip().upper()
PORT = int(os.environ.get("AUTH_PORT", "8080"))

app = Flask(__name__)

PAGE = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kite login</title>
<style>
 body{{font-family:-apple-system,sans-serif;max-width:420px;margin:40px auto;padding:0 16px}}
 .ok{{color:#0a7d33}} .bad{{color:#b3261e}}
 a.btn,button{{display:block;width:100%;box-sizing:border-box;padding:14px;margin:14px 0;
   font-size:17px;text-align:center;border-radius:10px;border:0;background:#387ed1;
   color:#fff;text-decoration:none}}
 input{{width:100%;box-sizing:border-box;padding:12px;font-size:15px}}
 small{{color:#666}}
</style></head><body>
<h2>Kite session</h2>
<p class="{cls}"><b>{status}</b></p>
{message}
<a class="btn" href="{login_url}">Login to Kite</a>
<form method="post" action="/paste">
 <small>Fallback: paste the address shown after login (or just the request_token)</small>
 <input name="value" autocomplete="off" autocapitalize="off">
 <button type="submit">Submit</button>
</form>
</body></html>"""


def render(message=""):
    ok = read_token() is not None
    return PAGE.format(
        cls="ok" if ok else "bad",
        status="Logged in for today" if ok else "Login needed for today",
        message=message,
        login_url=KiteConnect(api_key=API_KEY).login_url(),
    )


def complete_login(request_token):
    if not request_token:
        return render('<p class="bad">No request_token found.</p>')
    try:
        session = KiteConnect(api_key=API_KEY).generate_session(
            request_token, api_secret=API_SECRET)
    except Exception as exc:  # show the broker's reason on the page
        return render(f'<p class="bad">Login failed: {escape(str(exc))}</p>')
    if str(session.get("user_id", "")).upper() != USER_ID:
        return render('<p class="bad">Wrong Zerodha account - token not saved.</p>')
    save_token(session["access_token"])
    return render('<p class="ok">Token saved. You can close this page.</p>')


@app.get("/")
def home():
    return render()


@app.get("/callback")
def callback():
    return complete_login(request.args.get("request_token", ""))


@app.post("/paste")
def paste():
    value = request.form.get("value", "").strip()
    if "request_token" in value:
        value = parse_qs(urlparse(value).query).get("request_token", [""])[0]
    return complete_login(value)


if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=PORT)
