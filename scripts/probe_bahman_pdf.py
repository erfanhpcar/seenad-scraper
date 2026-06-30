import os
import requests

def probe_bahman_pdf():
    url = "https://bamco.ir/files/dealers.pdf"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    pdf_path = "data/bahman_dealers.pdf"
    os.makedirs("data", exist_ok=True)
    
    print(head := f"🚀 Analyzing Bahman Motor PDF List...")
    print("-" * len(head))
    
    try:
        # ۱. بررسی وجود فایل محلی یا دانلود آن
        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            print(f"📦 Local PDF found at {pdf_path}. Skipping download!")
        else:
            print(f"🌐 Local PDF not found. Attempting network download...")
            response = requests.get(url, headers=headers, timeout=25, verify=False)
            response.raise_for_status()
            with open(pdf_path, "wb") as f:
                f.write(response.content)
            print(f"💾 PDF successfully downloaded and saved to: {pdf_path}")
            
        file_size_kb = os.path.getsize(pdf_path) / 1024
        print(f"📊 PDF Size: {file_size_kb:.2f} KB")
        
        # ۲. بررسی محتوای متنی با pdfplumber
        print("\n🔍 Checking text readability with pdfplumber...")
        import pdfplumber
        
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            print(f"  [i] Total Pages: {total_pages}")
            
            # استخراج نمونه از صفحه اول برای تست دیجیتالی بودن و جهت متن
            first_page_text = pdf.pages[0].extract_text() or ""
            
            if first_page_text.strip():
                print("\n📝 Sample Text Extracted from Page 1:")
                print("-" * 40)
                print(first_page_text[:400])
                print("-" * 40)
                print("💡 Success: The PDF text is extractable!")
            else:
                print("\n⚠️ Warning: Zero text extracted. Pages might be scanned images.")
                
    except ImportError:
        print("\n❌ Missing dependency: pdfplumber is not installed in this environment.")
    except Exception as e:
        print(f"\n❌ Probe failed: {e}")
        print("💡 Hint: If it's a DNS or Network issue, try downloading the file manually onto the host machine.")

if __name__ == "__main__":
    probe_bahman_pdf()