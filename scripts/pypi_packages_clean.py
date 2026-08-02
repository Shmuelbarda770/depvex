import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

INPUT_FILE = "pypi_packages.txt"
OUTPUT_FILE = "pypi_packages_clean.txt"
MAX_WORKERS = 50
TIMEOUT = 5

write_lock = threading.Lock()


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503],
        allowed_methods=["HEAD", "GET"],
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_WORKERS,
        pool_maxsize=MAX_WORKERS,
    )
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "depvex-indexer/1.0"})
    return session


def load_done(path: str) -> set[str]:
    """טוען חבילות שכבר נבדקו בהצלחה, לצורך המשך ריצה."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def check_package(session: requests.Session, name: str) -> str | None:
    url = f"https://pypi.org/simple/{name}/"
    try:
        r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        return name if r.status_code == 200 else None
    except requests.RequestException:
        return None


def main():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        packages = [line.strip() for line in f if line.strip()]

    already_done = load_done(OUTPUT_FILE)
    to_check = [p for p in packages if p not in already_done]

    print(f"סה\"כ חבילות בקובץ: {len(packages)}")
    print(f"כבר נבדקו בעבר: {len(already_done)}")
    print(f"נותרו לבדיקה: {len(to_check)}")

    if not to_check:
        print("אין מה לבדוק, הכל כבר קיים בקובץ הפלט.")
        return

    session = build_session()
    found_count = 0

    # פתיחת קובץ הפלט במצב append כדי לא לאבד תוצאות אם הריצה נקטעת
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor, \
         open(OUTPUT_FILE, "a", encoding="utf-8") as out:

        futures = {
            executor.submit(check_package, session, pkg): pkg
            for pkg in to_check
        }

        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()

            if result:
                found_count += 1
                with write_lock:
                    out.write(result + "\n")
                    out.flush()

            if i % 1000 == 0:
                print(f"נבדקו {i}/{len(to_check)} (נמצאו עד כה: {found_count})")

    print()
    print(f"נמצאו {found_count} חבילות תקינות בריצה הזו")
    print(f"נשמר לקובץ: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()