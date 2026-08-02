import requests

def save_pypi_packages_txt(filename="pypi_packages.txt"):
    url = "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages-30-days.min.json"
    
    print("⏳ מוריד את הרשימה המלאה מ-PyPI...")
    response = requests.get(url)
    response.raise_for_status()
    
    data = response.json()
    rows = data.get("rows", [])
    
    print(f"✅ התקבלו {len(rows):,} חבילות.")
    
    # כתיבת שמות הספריות בלבד - שם אחד בכל שורה
    with open(filename, "w", encoding="utf-8") as f:
        for item in rows:
            f.write(f"{item['project']}\n")
            
    print(f"💾 כל השמות נשמרו בהצלחה בקובץ: {filename}")

if __name__ == "__main__":
    save_pypi_packages_txt()