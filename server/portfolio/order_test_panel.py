"""Order test page for the portfolio dashboard: send a small NSE equity order through Kite Connect
to prove that API orders work from this server (static IP whitelisted, login valid).

Routes (behind the dashboard's sign-in):
  GET  /order-test                  the page
  GET  /api/order-test/ltp?symbol=  last price
  POST /api/order-test/place        {symbol, qty, side, product, price}
  GET  /api/order-test/orders       today's orders
  POST /api/order-test/cancel       {order_id}
Only LIMIT orders, NSE equity, at most MAX_QTY shares and MAX_VALUE rupees per order.
"""
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, jsonify, request

BASE = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
MAX_QTY = 100
MAX_VALUE = 20000.0
SYM_OK = re.compile(r"^[A-Z0-9&_-]{1,30}$")
TAG = "ordertest"

panel = Blueprint("order_test", __name__)


def _kite():
    try:
        cfg = json.loads((BASE / "config.json").read_text())
    except (OSError, ValueError):
        return None, "config.json not readable"
    key = os.environ.get("KITE_API_KEY") or cfg.get("api_key")
    tok_path = BASE / cfg.get("token_file", "token.json")
    try:
        token = json.loads(tok_path.read_text())["access_token"]
        when = datetime.fromtimestamp(tok_path.stat().st_mtime, IST)
    except (OSError, ValueError, KeyError):
        return None, "The server has no Zerodha login yet. Log in on the dashboard first."
    now = datetime.now(IST)
    cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    if when < cutoff:
        return None, "The server's Zerodha login is from before this morning. Log in on the dashboard first."
    if not key:
        return None, "Kite API key not found on the server"
    from kiteconnect import KiteConnect
    k = KiteConnect(api_key=key)
    k.set_access_token(token)
    return k, None


def _err(msg, code=400):
    return jsonify(error=str(msg)), code


@panel.route("/api/order-test/ltp")
def ot_ltp():
    sym = (request.args.get("symbol") or "").strip().upper()
    if not SYM_OK.match(sym):
        return _err("Enter an NSE symbol, e.g. ITC")
    k, why = _kite()
    if k is None:
        return _err(why)
    try:
        q = k.ltp([f"NSE:{sym}"]).get(f"NSE:{sym}")
    except Exception as e:  # noqa: BLE001
        return _err(f"Zerodha: {e}")
    if not q:
        return _err(f"{sym} not found on NSE")
    return jsonify(symbol=sym, ltp=q["last_price"])


@panel.route("/api/order-test/place", methods=["POST"])
def ot_place():
    d = request.get_json(silent=True) or {}
    sym = str(d.get("symbol", "")).strip().upper()
    side = str(d.get("side", "")).upper()
    product = str(d.get("product", "CNC")).upper()
    try:
        qty = int(d.get("qty", 0))
        price = float(d.get("price", 0))
    except (TypeError, ValueError):
        return _err("Quantity and price must be numbers")
    if not SYM_OK.match(sym):
        return _err("Enter an NSE symbol")
    if side not in ("BUY", "SELL"):
        return _err("Choose Buy or Sell")
    if product not in ("CNC", "MIS"):
        return _err("Product must be CNC or MIS")
    if not 1 <= qty <= MAX_QTY:
        return _err(f"Quantity must be 1 to {MAX_QTY}")
    if price <= 0:
        return _err("Enter a limit price")
    price = round(round(price / 0.05) * 0.05, 2)
    if qty * price > MAX_VALUE:
        return _err(f"Order value Rs {qty * price:,.0f} is above this page's Rs {MAX_VALUE:,.0f} limit")
    k, why = _kite()
    if k is None:
        return _err(why)
    try:
        oid = k.place_order(variety=k.VARIETY_REGULAR, exchange=k.EXCHANGE_NSE, tradingsymbol=sym,
                            transaction_type=side, quantity=qty, product=product,
                            order_type=k.ORDER_TYPE_LIMIT, price=price, validity=k.VALIDITY_DAY, tag=TAG)
    except Exception as e:  # noqa: BLE001
        return _err(f"Zerodha refused the order: {e}")
    return jsonify(order_id=oid, price=price)


@panel.route("/api/order-test/orders")
def ot_orders():
    k, why = _kite()
    if k is None:
        return _err(why)
    try:
        rows = k.orders()
    except Exception as e:  # noqa: BLE001
        return _err(f"Zerodha: {e}")
    out = [{"id": o["order_id"], "time": str(o.get("order_timestamp") or "")[11:19], "symbol": o["tradingsymbol"],
            "side": o["transaction_type"], "qty": o["quantity"], "filled": o.get("filled_quantity", 0),
            "price": o.get("price"), "avg": o.get("average_price"), "product": o.get("product"),
            "status": o["status"], "msg": o.get("status_message") or "", "variety": o.get("variety", "regular"),
            "api": o.get("tag") == TAG}
           for o in rows if o.get("exchange") in ("NSE", "BSE")]
    out.sort(key=lambda r: r["time"], reverse=True)
    return jsonify(orders=out[:30])


@panel.route("/api/order-test/cancel", methods=["POST"])
def ot_cancel():
    d = request.get_json(silent=True) or {}
    oid = str(d.get("order_id", ""))
    if not oid.isdigit():
        return _err("Unknown order")
    k, why = _kite()
    if k is None:
        return _err(why)
    try:
        k.cancel_order(variety=str(d.get("variety") or "regular"), order_id=oid)
    except Exception as e:  # noqa: BLE001
        return _err(f"Zerodha: {e}")
    return jsonify(ok=True)


@panel.route("/order-test")
def ot_page():
    return Response(PAGE.replace("__MAXQ__", str(MAX_QTY)).replace("__MAXV__", f"{MAX_VALUE:,.0f}"),
                    mimetype="text/html")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Order test</title>
<style>
:root{--bg:#f4f6f5;--card:#fff;--fg:#1c2421;--muted:#5f6c67;--line:#d6ddda;--buy:#1565c0;--sell:#c62828;--ok:#1b7a4a;--warn:#a15c00}
@media (prefers-color-scheme:dark){:root{--bg:#121715;--card:#1b2220;--fg:#e3eae7;--muted:#9aa7a2;--line:#2d3835;--buy:#64a8f0;--sell:#ef7a7a;--ok:#4cc38a;--warn:#e0a24a;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:560px;margin:0 auto;padding:16px;display:grid;gap:14px}
a{color:inherit}
.top{display:flex;justify-content:space-between;align-items:baseline}
h1{font-size:1.3rem;margin:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;display:grid;gap:12px}
label{display:grid;gap:4px;font-size:.85rem;color:var(--muted)}
input,select{font:16px system-ui;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);width:100%;box-sizing:border-box}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.seg{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{font:600 16px system-ui;padding:11px;border:0;background:var(--bg);color:var(--muted)}
.seg button.on.buy{background:var(--buy);color:#fff}.seg button.on.sell{background:var(--sell);color:#fff}
.seg button.on.plain{background:var(--fg);color:var(--bg)}
.btn{font:600 16px system-ui;padding:12px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.go{border:0;color:#fff}.go.buy{background:var(--buy)}.go.sell{background:var(--sell)}
.ltp{font-variant-numeric:tabular-nums;color:var(--muted);font-size:.9rem}
.msg{padding:10px;border-radius:8px;font-size:.92rem}
.msg.err{background:color-mix(in srgb,var(--sell) 15%,transparent)}.msg.ok{background:color-mix(in srgb,var(--ok) 15%,transparent)}
.confirm{border:2px solid var(--warn)}
table{width:100%;border-collapse:collapse;font-size:.85rem;font-variant-numeric:tabular-nums}
td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.st{font-weight:600}.COMPLETE{color:var(--ok)}.REJECTED,.CANCELLED{color:var(--sell)}.OPEN{color:var(--warn)}
.small{font-size:.8rem;color:var(--muted)}
.x{font:600 13px system-ui;padding:4px 8px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
</style></head><body><main>
<div class="top"><h1>Order test</h1><a href="/" class="small">Portfolio</a></div>
<p class="small" style="margin:0">Real orders on your Zerodha account: NSE equity, limit orders only, up to __MAXQ__ shares and Rs __MAXV__ per order.</p>
<div class="card">
  <div class="row">
    <label>NSE symbol<input id="sym" list="syms" autocapitalize="characters" autocomplete="off" placeholder="e.g. ITC"></label>
    <label>Quantity<input id="qty" type="number" inputmode="numeric" min="1" max="__MAXQ__" value="1"></label>
  </div>
  <datalist id="syms"><option>IDEA</option><option>YESBANK</option><option>SUZLON</option><option>NHPC</option><option>IRFC</option><option>ITC</option><option>SBIN</option></datalist>
  <div class="ltp" id="ltp">Enter a symbol to see its last price.</div>
  <div class="seg" id="side"><button type="button" class="buy on" data-v="BUY">Buy</button><button type="button" class="sell" data-v="SELL">Sell</button></div>
  <div class="row">
    <label>Product<div class="seg" id="prod"><button type="button" class="plain on" data-v="CNC">CNC</button><button type="button" class="plain" data-v="MIS">MIS</button></div></label>
    <label>Limit price<input id="price" type="number" inputmode="decimal" step="0.05" min="0"></label>
  </div>
  <button type="button" class="btn" id="review">Review order</button>
  <div id="out"></div>
</div>
<div class="card confirm" id="conf" hidden>
  <div id="conftext"></div>
  <div class="row"><button type="button" class="btn" id="back">Change</button><button type="button" class="btn go buy" id="send">Send order</button></div>
</div>
<div class="card">
  <div class="top"><b>Today's equity orders</b><button type="button" class="x" id="refresh">Refresh</button></div>
  <div id="orders" class="small">Loading...</div>
</div>
<p class="small">CNC is delivery: a bought share stays in your demat. Selling with CNC needs the shares in your holdings. MIS is intraday and Zerodha squares it off near 3:20 pm, and blocks new MIS orders after that.</p>
</main><script>
const $=id=>document.getElementById(id);let side="BUY",prod="CNC",ltp=null,timer=null;
function seg(id,cb){$(id).querySelectorAll("button").forEach(b=>b.onclick=()=>{$(id).querySelectorAll("button").forEach(x=>x.classList.remove("on"));b.classList.add("on");cb(b.dataset.v)})}
seg("side",v=>{side=v;suggest()});seg("prod",v=>prod=v);
function msg(t,ok){$("out").innerHTML=t?`<div class="msg ${ok?"ok":"err"}">${t}</div>`:""}
async function api(path,body){const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});let j={};try{j=await r.json()}catch(e){j={error:"HTTP "+r.status}}if(!r.ok||j.error)throw new Error(j.error||("HTTP "+r.status));return j}
function suggest(){if(ltp==null)return;const p=side==="BUY"?ltp*1.002:ltp*0.998;$("price").value=(Math.round(p/0.05)*0.05).toFixed(2)}
let lt=null;$("sym").addEventListener("input",()=>{clearTimeout(lt);lt=setTimeout(getLtp,500)});
async function getLtp(){const s=$("sym").value.trim().toUpperCase();if(!s){return}$("ltp").textContent="Getting price...";
 try{const j=await api("/api/order-test/ltp?symbol="+encodeURIComponent(s));ltp=j.ltp;$("ltp").textContent=`${j.symbol} last price Rs ${j.ltp.toFixed(2)} · limit price set slightly through it so it fills`;suggest()}
 catch(e){ltp=null;$("ltp").textContent=e.message}}
function order(){return{symbol:$("sym").value.trim().toUpperCase(),qty:parseInt($("qty").value||"0"),side,product:prod,price:parseFloat($("price").value||"0")}}
$("review").onclick=()=>{const o=order();msg("");if(!o.symbol||!o.qty||!o.price){msg("Fill in symbol, quantity and limit price.");return}
 $("conftext").innerHTML=`<b style="font-size:1.1rem">${o.side} ${o.qty} ${o.symbol}</b><br>Limit Rs ${o.price.toFixed(2)} · ${o.product} · NSE<br>Value about Rs ${(o.qty*o.price).toLocaleString("en-IN",{maximumFractionDigits:0})}`;
 $("send").className="btn go "+(o.side==="BUY"?"buy":"sell");$("send").textContent="Send "+o.side.toLowerCase()+" order";$("send").disabled=false;$("conf").hidden=false;$("conf").scrollIntoView({behavior:"smooth"})};
$("back").onclick=()=>{$("conf").hidden=true};
$("send").onclick=async()=>{$("send").disabled=true;$("send").textContent="Sending...";
 try{const j=await api("/api/order-test/place",order());$("conf").hidden=true;msg(`Sent to Zerodha. Order ${j.order_id} at Rs ${j.price.toFixed(2)}. Watch its status below.`,true);poll()}
 catch(e){$("conf").hidden=true;msg(e.message)}};
async function loadOrders(){try{const j=await api("/api/order-test/orders");
 if(!j.orders.length){$("orders").textContent="No equity orders today.";return}
 $("orders").innerHTML="<table><tr><th>Time</th><th>Order</th><th>Status</th><th></th></tr>"+j.orders.map(o=>`<tr><td>${o.time}</td><td>${o.side} ${o.filled}/${o.qty} ${o.symbol}<br><span class="small">${o.product} @ ${o.avg?o.avg:o.price}${o.api?" · this page":""}</span></td><td><span class="st ${o.status.split(" ")[0]}">${o.status}</span>${o.msg?`<br><span class="small">${o.msg}</span>`:""}</td><td>${["OPEN","TRIGGER PENDING","AMO REQ RECEIVED"].includes(o.status)?`<button class="x" data-id="${o.id}" data-v="${o.variety}">Cancel</button>`:""}</td></tr>`).join("")+"</table>";
 $("orders").querySelectorAll("button[data-id]").forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api("/api/order-test/cancel",{order_id:b.dataset.id,variety:b.dataset.v});msg("Cancel sent.",true)}catch(e){msg(e.message)}loadOrders()})}
 catch(e){$("orders").textContent=e.message}}
function poll(){let n=0;clearInterval(timer);loadOrders();timer=setInterval(()=>{loadOrders();if(++n>=10)clearInterval(timer)},3000)}
$("refresh").onclick=loadOrders;loadOrders();
</script></body></html>"""
