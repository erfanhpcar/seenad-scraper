#!/usr/bin/env python3
import requests
import sys
from bs4 import BeautifulSoup

def probe():
    url = "https://kermanmotor.com/dealers/1/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "fa,en-US;q=0.7,en;q=0.3",
    }
    
    print(f"[+] Sending probe request to: {url}")
    try:
        # کرمان موتور معمولاً SSL معتبری دارد ولی برای اطمینان بیشتر verify=False می‌گذاریم
        response = requests.get(url, headers=headers, verify=False, timeout=15)
        print(f"[=] Status Code: {response.status_code}")
        print(f"[=] Content Length: {len(response.content)} bytes")
        
        if response.status_code != 200:
            print("[-] Failed to fetch the page.")
            return

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # بررسی وجود ساختارهای دیتایی مثل جدول یا کارت‌های نمایندگی
        tables = soup.find_all('table')
        print(f"[i] Found {len(tables)} <table> tags.")
        
        # بررسی کلماتی که معمولاً در صفحات نمایندگی کرمان موتور هست (مثل کد نمایندگی، تلفن، آدرس)
        sample_keywords = ["نمایندگی", "تلفن", "آدرس", "کد"]
        for kw in sample_keywords:
            count = response.text.count(kw)
            print(f"[i] Keyword '{kw}' appeared {count} times.")

        # پیدا کردن لینک‌های پیجینیشن یا اعدادی که شبیه صفحات بعدی هستند
        pagination = soup.find_all(class_="pagination") or soup.find_all(class_=["pager", "page-link"])
        print(f"[i] Pagination component detected: {None if not pagination else 'Yes'}")

    except Exception as e:
        print(f"[!] Error during probe: {e}")

if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    probe()