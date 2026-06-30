import os
import requests

def probe_arian_pdf():
    url = "https://arian-motor.com/wp-content/uploads/2016/04/Arian-Motor-list-ameli.pdf"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    pdf_path = "data/arian_motor_list_ameli.pdf"
    os.makedirs("data", exist_ok=True)
    
    print(head := f"🚀 Downloading Arian Motor PDF List...")
    print("-" * len(head))
    
    try:
        # ۱. دانلود فایل PDF
        response = requests.get(url, headers=headers, timeout=25)
        response.raise_for_status()
        
        with open(pdf_path, "wb") as f:
            f.write(response.content)
            
        file_size_kb = os.path.getsize(pdf_path) / 1024
        print(f"💾 PDF successfully saved to: {pdf_path} ({file_size_kb:.2f} KB)")
        
        # ۲. بررسی کتابخانه‌های در دسترس برای پارس کردن PDF
        print("\n🔍 Checking available PDF parsing libraries in container...")
        
        extracted_text = ""
        library_used = None
        
        # تست pdfplumber (بهترین گزینه برای دیتای جدولی)
        try:
            import pdfplumber
            print("  [✓] pdfplumber is available! Analyzing pages...")
            library_used = "pdfplumber"
            with pdfplumber.open(pdf_path) as pdf:
                print(f"  [i] Total Pages: {len(pdf.pages)}")
                # تست استخراج متن از صفحه اول
                extracted_text = pdf.pages[0].extract_text() or ""
        except ImportError:
            print("  [x] pdfplumber NOT available.")
            
            # تست pypdf به عنوان فال‌بک رایج
            if not library_used:
                try:
                    import pypdf
                    print("  [✓] pypdf is available! Analyzing pages...")
                    library_used = "pypdf"
                    reader = pypdf.PdfReader(pdf_path)
                    print(f"  [i] Total Pages: {len(reader.pages)}")
                    extracted_text = reader.pages[0].extract_text() or ""
                except ImportError:
                    print("  [x] pypdf NOT available.")

        # ۳. ارزیابی متن استخراج شده
        if library_used and extracted_text.strip():
            print("\n📝 Sample Text Extracted from Page 1:")
            print("-" * 40)
            # نمایش ۲۰۰ کاراکتر اول برای بررسی ساختار کلمات فارسی
            print(extracted_text[:400])
            print("-" * 40)
            print(f"💡 Success: The PDF is digitized and text can be extracted via {library_used}!")
        elif library_used:
            print("\n⚠️ Warning: Library opened the PDF but extracted ZERO text. The PDF might be scanned images (requires OCR).")
        else:
            print("\n❌ Missing dependency: Please install pypdf or pdfplumber to parse this file.")
            print("👉 Run: pip install pdfplumber pypdf")
            
    except Exception as e:
        print(f"❌ Probe failed: {e}")

if __name__ == "__main__":
    probe_arian_pdf()