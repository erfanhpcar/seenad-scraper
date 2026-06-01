from bs4 import BeautifulSoup
import requests

url = "https://www.mvmco.ir/dealersmvm"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

print("Fetching full page...")
response = requests.get(url, headers=headers, timeout=15)
html = response.text
soup = BeautifulSoup(html, 'html.parser')

print(f"Total Content Length: {len(html)} characters")

# ۱. بررسی تگ‌های Select (استان و شهر)
print("\n--- 1. Dropdown Select Elements ---")
selects = soup.find_all('select')
for s in selects:
    print(f"Select ID: {s.get('id')}, Class: {s.get('class')}, Name: {s.get('name')}")

# ۲. جستجوی کلمات کلیدی در تگ‌های اسکریپت (کشف دیتای اینلاین یا آدرس API)
print("\n--- 2. Script Keywords Scan ---")
keywords = ['dealers', 'dealerList', 'mapData', 'provinces', 'نمایندگی', 'getDealers']
for i, script in enumerate(soup.find_all('script')):
    if script.string:
        found = [kw for kw in keywords if kw in script.string]
        if found:
            print(f"🔹 Script #{i} contains keywords {found}. Preview (first 300 chars):")
            print(script.string.strip()[:300])
            print("-" * 50)

# ۳. بررسی وجود کانتینرهای کارت‌های نمایندگی در HTML
print("\n--- 3. Potential Dealer Container Elements ---")
potential_classes = ['dealer', 'agency', 'card', 'item', 'list']
for p_class in potential_classes:
    elements = soup.find_all(class_=lambda x: x and p_class in x.lower())
    if elements:
        print(f"Found {len(elements)} elements matching class keyword: '{p_class}' (e.g., {elements[0].get('class')})")
