#!/usr/bin/env python3
import requests
import re
from bs4 import BeautifulSoup

def probe_advanced():
    url = "https://kermanmotor.com/dealers/1/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    print(f"[+] Fetching advanced scripts from: {url}")
    try:
        response = requests.get(url, headers=headers, verify=False, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # ۱. بررسی تمام تگ‌های اسکریپت داخلی برای پیدا کردن متغیرهای دیتا یا آدرس API
        scripts = soup.find_all('script')
        print(f"[i] Found {len(scripts)} <script> tags. Analyzing contents...")
        
        for i, script in enumerate(scripts):
            content = script.string or ""
            if any(word in content for word in ["api", "fetch", "ajax", "Dealer", "axios", "json"]):
                print(f"\n[!] Signal spotted in <script> tag #{i}:")
                # چاپ خطوطی که کلمات کلیدی در آن‌ها هستند
                for line in content.split('\n'):
                    if any(w in line for w in ["api", "fetch", "ajax", "Dealer", "json", "url"]):
                        print(f"    -> {line.strip()[:120]}")

        # ۲. بررسی فایل‌های اسکریپت خارجی متصل شده
        print("\n[i] External script files:")
        for script in scripts:
            src = script.get('src')
            if src:
                if any(w in src.lower() for w in ["dealer", "app", "main", "custom", "agency"]):
                    print(f"    -> Target JS file found: {src}")

    except Exception as e:
        print(f"[!] Error: {e}")

if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    probe_advanced()