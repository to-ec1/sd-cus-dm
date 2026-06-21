import sys, time
sys.path.append(r"C:\data\dev\.313p")
from chrome_utils import start_chrome
from DrissionPage import ChromiumPage, ChromiumOptions

start_chrome()
co = ChromiumOptions()
co.set_local_port(9222)
page = ChromiumPage(co)

url = "https://www.superdelivery.com/l/management/customer/detail.do?code=574050"
page.get(url)
time.sleep(5)

with open(r"C:\data\dev\sd_cus\live_dom3.html", "w", encoding="utf-8") as f:
    f.write(page.html)
print("保存完了: live_dom3.html")