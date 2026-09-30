import requests
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
s.post(
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

url = "https://cache.moneyweb.co.za/mny-snapshots.php"

codes_list = ["NPN", "CFR"]

# Try codes as array form
r = s.post(url, data={"action": "load_snapshots", "codes[]": codes_list, "type": "clickacompany"}, timeout=30)
print("codes[] list status:", r.status_code)
print("text:", r.text[:500])

# Try single code string
r2 = s.post(url, data={"action": "load_snapshots", "codes": "NPN", "type": "clickacompany"}, timeout=30)
print("single string status:", r2.status_code)
print("text:", r2.text[:500])
