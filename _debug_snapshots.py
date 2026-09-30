import requests, json
from app.config.settings import settings

s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.moneyweb.co.za/tools-and-data/click-a-company/",
    "Origin": "https://www.moneyweb.co.za",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
})

s.get("https://www.moneyweb.co.za/wp-login.php", timeout=20)
r = s.post(
    "https://www.moneyweb.co.za/wp-login.php",
    data={
        "log": settings.moneyweb_username,
        "pwd": settings.moneyweb_password,
        "wp-submit": "Log In",
        "redirect_to": "https://www.moneyweb.co.za/wp-admin/",
        "testcookie": "1",
        "rememberme": "forever",
    },
    timeout=20,
    allow_redirects=False,
)
print("login:", r.status_code)

codes = "NPN,CFR,AGL,SOL,SBK"
url = "https://cache.moneyweb.co.za/mny-snapshots.php"
for t in ["clickacompany", "share"]:
    r = s.post(url, data={"action": "load_snapshots", "codes": codes, "type": t}, timeout=30)
    print(f"type={t} status:", r.status_code, "content-type:", r.headers.get("Content-Type"))
    try:
        data = r.json()
        print("first:", data[0] if isinstance(data, list) else data)
    except Exception as e:
        print("error:", e)
        print(r.text[:500])
