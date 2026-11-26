"""
丸久小山園 補貨監測 → LINE 推播（登入版）
小山園從 2026/11 起只讓登入會員看到各規格庫存與可寄送國家，所以程式用「空殼帳號」登入檢查。

需要的 GitHub Secrets：
  LINE_TOKEN          LINE Messaging API 的 Channel access token
  LINE_USER_ID        你的 LINE user ID（U 開頭）
  KOYAMAEN_EMAIL      空殼帳號的 email（不要用主帳號）
  KOYAMAEN_PASSWORD   空殼帳號的密碼

通知規則：某個規格「有貨」而且「可以寄台灣」時才通知；同一個規格持續有貨不重複通知。
讀不到網站（被擋、登入失敗）超過 ALERT_AFTER_HOURS 小時，會用 LINE 警告一次。
"""
import gzip
import html as htmllib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PRODUCTS_FILE = "products.json"
STATE_FILE = "state.json"
SHOP = "https://www.marukyu-koyamaen.co.jp/english/shop"
BASE = SHOP + "/products/"
LOGIN_URL = SHOP + "/account"
ALERT_AFTER_HOURS = 3
TPE = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "Upgrade-Insecure-Requests": "1",
}

SESSION_DIR = "session"                      # 由 workflow 加密後存在 GitHub cache，不會進 repo
COOKIE_FILE = os.path.join(SESSION_DIR, "cookies.txt")
PENDING_FILE = os.path.join(SESSION_DIR, "pending.json")
OTP_RETRY_HOURS = 12                         # 等驗證碼期間，最快多久重寄一次
OTP_REQUEST_HOURS = range(8, 24)             # 只在台灣時間 08:00–23:59 要求驗證碼，半夜不寄信
ACTIONS_URL = "https://github.com/yiruhihi-dev/matcha-alert/actions/workflows/check.yml"

_jar = http.cookiejar.LWPCookieJar(COOKIE_FILE)
if os.path.exists(COOKIE_FILE):
    try:
        _jar.load(ignore_discard=True)
    except Exception:
        pass
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def save_session():
    os.makedirs(SESSION_DIR, exist_ok=True)
    _jar.save(ignore_discard=True)


def http(url, data=None, referer=None):
    """回傳 (status, html)。data 給 dict 就用 POST。"""
    headers = dict(HEADERS)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = "https://www.marukyu-koyamaen.co.jp"
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with _opener.open(req, timeout=30) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # 網路錯誤
        print(f"  連線錯誤: {type(e).__name__}")
        return 0, ""


def is_logged_in(page):
    # 帳號頁登入後有登出連結；商品頁未登入時會出現「register and login ... to shop」提示
    if "customer-logout" in page:
        return True
    return "variations_form" in page and "to shop.</strong>" not in page and "register and login" not in page.lower()


def describe_forms(page):
    """登入沒成功時，把頁面上的表單結構印出來（不含任何輸入值），用來研究驗證碼流程。"""
    for f in re.findall(r"<form\b.*?</form>", page, re.S)[:5]:
        head = re.search(r"<form\b[^>]*>", f).group(0)
        if "search" in head.lower():
            continue
        print("  [form]", re.sub(r'value="[^"]*"', 'value=…', head)[:200])
        for tag in re.findall(r"<(?:input|button|select|textarea)\b[^>]*>", f):
            name = re.search(r'name="([^"]*)"', tag)
            typ = re.search(r'type="([^"]*)"', tag)
            print("     ", (name.group(1) if name else "-"), (typ.group(1) if typ else ""))
        txt = re.sub(r"<[^>]+>", " ", f)
        print("     text:", re.sub(r"\s+", " ", txt).strip()[:300])


def login_form_fields(page):
    """取出登入表單裡所有 hidden 欄位（nonce、onetime_uid、onetime_token…）"""
    f = re.search(r'<form[^>]*woocommerce-form-login.*?</form>', page, re.S)
    out = {}
    for tag in re.findall(r"<input\b[^>]*>", f.group(0) if f else ""):
        if 'type="hidden"' not in tag:
            continue
        n = re.search(r'name="([^"]*)"', tag)
        v = re.search(r'value="([^"]*)"', tag)
        if n:
            out[n.group(1)] = htmllib.unescape(v.group(1)) if v else ""
    return out


def login_error(page, code):
    err = re.search(r'woocommerce-error[^>]*>(.*?)</ul>', page, re.S)
    msg = re.sub(r"<[^>]+>", " ", err.group(1)).strip() if err else f"HTTP {code}"
    return re.sub(r"\s+", " ", msg)[:120]


def login(email, password, otp, now):
    """回傳 (狀態, 說明)。狀態：ok / need_otp_sent / need_otp_waiting / fail"""
    code, page = http(LOGIN_URL)
    if code != 200:
        return "fail", f"打不開登入頁 (HTTP {code})"
    if is_logged_in(page):
        return "ok", "沿用已儲存的登入狀態"

    pending = load(PENDING_FILE, None)
    base = {"username": email, "password": password, "rememberme": "forever", "login": "Log in"}

    if otp:  # 使用者手動帶驗證碼來執行
        if not pending:
            return "fail", "有填驗證碼，但沒有等待中的登入。請先『不填驗證碼』執行一次，收到新驗證碼後再填"
        data = dict(pending["fields"]); data.update(base); data["onetime_pass"] = otp.strip()
        code, page = http(LOGIN_URL, data, referer=LOGIN_URL)
        os.remove(PENDING_FILE)
        if is_logged_in(page):
            return "ok", "驗證碼正確，登入成功（之後會沿用登入狀態）"
        return "fail", "驗證碼登入失敗：" + login_error(page, code)

    if pending:
        age = now - datetime.fromisoformat(pending["at"])
        if age < timedelta(hours=OTP_RETRY_HOURS):
            return "need_otp_waiting", f"等待驗證碼中（{pending['at'][5:16]} 已寄出）"
    if now.hour not in OTP_REQUEST_HOURS:
        return "need_otp_waiting", "需要重新登入，但現在是半夜，早上 8 點後再寄驗證碼"

    fields = login_form_fields(page)
    if "woocommerce-login-nonce" not in fields:
        return "fail", "登入頁格式改變或被擋（找不到 login nonce）"
    data = dict(fields); data.update(base)
    code, page = http(LOGIN_URL, data, referer=LOGIN_URL)
    if is_logged_in(page):
        return "ok", "登入成功"
    if 'name="onetime_pass"' in page:
        os.makedirs(SESSION_DIR, exist_ok=True)
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump({"at": now.isoformat(), "fields": login_form_fields(page)}, f)
        return "need_otp_sent", "小山園已寄出驗證碼"
    return "fail", "登入失敗：" + login_error(page, code)

def parse(page):
    """回傳 {"status": "ok"/"blocked"/"logged_out", "in": [...], "out": [...], "taiwan": True/False/None}"""
    low = page.lower()
    if "variations_form" not in low or "best before" not in low:
        return {"status": "blocked"}
    if not is_logged_in(page):
        return {"status": "logged_out"}

    start = low.index("variations_form")
    end = low.find("product-labelings", start)
    form = page[start:end if end > 0 else start + 40000]

    sizes_in, sizes_out = [], []
    for c in re.split(r'(?=<div class="product-form-row\b)', form)[1:]:
        m = re.search(r'pa-size">\s*<dt>[^<]*</dt>\s*<dd>([^<]+)</dd>', c)
        size = htmllib.unescape(m.group(1).strip()) if m else "?"
        cl = c.lower()
        if "out-of-stock" in cl or "out of stock" in cl:
            sizes_out.append(size)
        elif "in-stock" in cl or "in stock" in cl or "available-on-backorder" in cl:
            sizes_in.append(size)
        else:
            sizes_out.append(size)  # 看不懂就當缺貨，寧可漏報不要誤報

    taiwan = None
    sel = re.search(r'name="calc_shipping_country".*?</select>', page, re.S)
    if sel:
        taiwan = 'value="TW"' in sel.group(0)
    return {"status": "ok", "in": sizes_in, "out": sizes_out, "taiwan": taiwan}


def push_line(text):
    token, user = os.environ.get("LINE_TOKEN"), os.environ.get("LINE_USER_ID")
    if not token or not user:
        print("!! 沒設定 LINE_TOKEN / LINE_USER_ID，略過推播")
        print(text)
        return False
    data = json.dumps({"to": user, "messages": [{"type": "text", "text": text[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=data, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"LINE: {r.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"LINE 失敗: {e.code} {e.read().decode(errors='replace')}")
        return False


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main():
    now = datetime.now(TPE)
    now_s = now.strftime("%Y-%m-%d %H:%M")

    if os.environ.get("TEST_LINE") == "1":
        ok = push_line("✅ 小山園補貨監測：LINE 推播測試成功")
        sys.exit(0 if ok else 1)

    products = load(PRODUCTS_FILE, [])
    old = load(STATE_FILE, {})
    # v2 起改成登入判斷；舊版（未登入）的狀態不可信，直接捨棄
    state = old if old.get("_v") == 2 else {"_v": 2, "items": {}, "health": {}}
    health = state.setdefault("health", {})
    items = state.setdefault("items", {})
    before = json.dumps(state, sort_keys=True)

    email, password = os.environ.get("KOYAMAEN_EMAIL"), os.environ.get("KOYAMAEN_PASSWORD")
    otp = os.environ.get("OTP", "").strip()
    problem = None
    results = {}
    if not email or not password:
        problem = "沒設定 KOYAMAEN_EMAIL / KOYAMAEN_PASSWORD"
    else:
        status, msg = login(email, password, otp, now)
        print(msg)
        if status == "need_otp_sent":
            push_line("🔑【小山園補貨監測】需要驗證碼\n"
                      "小山園剛寄了驗證碼到空殼帳號的信箱，請在幾分鐘內：\n"
                      "GitHub → Run workflow → 在「otp」欄位填入驗證碼 → 按綠色 Run workflow\n"
                      f"{ACTIONS_URL}")
        if status.startswith("need_otp"):
            save_session()
            if json.dumps(state, sort_keys=True) != before:
                save(state)
            return
        if status == "fail":
            problem = msg

    if not problem:
        for i, p in enumerate(products):
            if i:
                time.sleep(3)
            code, page = http(BASE + p["id"])
            r = parse(page) if code == 200 else {"status": "blocked"}
            if r["status"] != "ok":
                print(f"{p['name']}: 讀取失敗（{r['status']}, HTTP {code}）")
                continue
            results[p["id"]] = r
            tw = {True: "可寄", False: "不可寄", None: "未知"}[r["taiwan"]]
            print(f"{p['name']}: 有貨{r['in']} 缺貨{r['out']} 台灣:{tw}")
        if not results:
            problem = "商品頁全部讀取失敗（可能被擋或被登出）"

    # ---------- 健康狀態：讀不到就累計，太久就警告 ----------
    if problem:
        print("!!", problem)
        if not health.get("fail_since"):
            health["fail_since"] = now.isoformat()
            health["reason"] = problem
        since = datetime.fromisoformat(health["fail_since"])
        if not health.get("warned") and now - since >= timedelta(hours=ALERT_AFTER_HOURS):
            if push_line("⚠️【小山園補貨監測】已超過 "
                         f"{ALERT_AFTER_HOURS} 小時讀不到官網\n"
                         f"原因：{health.get('reason')}\n"
                         f"開始時間：{since.strftime('%m-%d %H:%M')}\n"
                         "這段期間的補貨可能不會通知，請自己留意官網。"):
                health["warned"] = True
        save_session()
        if json.dumps(state, sort_keys=True) != before:
            save(state)
        return

    save_session()
    recovered_after_warning = bool(health.get("warned"))
    if health.get("fail_since"):
        if recovered_after_warning:
            push_line("✅【小山園補貨監測】已恢復正常，繼續監控中")
        state["health"] = health = {}

    # ---------- 判斷要不要通知 ----------
    blocks = []
    for p in products:
        r = results.get(p["id"])
        if not r:
            continue
        url = BASE + p["id"]
        alertable = r["in"] if r["taiwan"] else []      # 有貨＋可寄台灣才算
        prev = [] if recovered_after_warning else items.get(p["id"], {}).get("alerted", [])
        newly = [s for s in alertable if s not in prev]
        items[p["id"]] = {"in": r["in"], "taiwan": r["taiwan"], "alerted": alertable}
        if newly:
            lines = [f"🟢【{p['name']}】",
                     f"📦 有貨：{', '.join(r['in'])}（🆕 {', '.join(newly)}）"]
            if r["out"]:
                lines.append(f"❌ 缺貨：{', '.join(r['out'])}")
            lines.append("✈️ 台灣寄送：可寄 ✅")
            lines.append(f"👉 {url}")
            blocks.append("\n".join(lines))
        elif r["in"] and r["taiwan"] is False:
            print(f"  {p['name']} 有貨但目前不寄台灣，不通知")

    if blocks:
        msg = "\n".join(["🎉【丸久小山園 補貨＋可寄台灣！】", f"時間：{now_s}",
                         "------------------------", "\n\n".join(blocks),
                         "", "（用你的主帳號登入下單，每單抹茶限 5 罐）"])
        if not push_line(msg):
            # 推播失敗：這次的新項目不記成已通知，下次再試
            for p in products:
                if p["id"] in items and p["id"] in results:
                    r = results[p["id"]]
                    old_alerted = old.get("items", {}).get(p["id"], {}).get("alerted", []) if old.get("_v") == 2 else []
                    items[p["id"]]["alerted"] = [s for s in items[p["id"]]["alerted"] if s in old_alerted]

    if json.dumps(state, sort_keys=True) != before:
        save(state)
        print("state.json 已更新")


if __name__ == "__main__":
    main()
