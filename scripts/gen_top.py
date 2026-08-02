"""
שולף את top_level.txt מתוך wheel files ב-PyPI בלי להוריד את כל ה-wheel
ובלי לעשות unzip - שולף רק את הבייטים הרלוונטיים דרך HTTP Range requests.

איך זה עובד:
1. PyPI JSON API -> מוצא את ה-URL של ה-wheel האחרון של החבילה
2. Range request על הסוף של הקובץ -> מוצא את ה-EOCD (End Of Central Directory)
3. Range request על ה-Central Directory -> מוצא את המיקום של top_level.txt
4. Range request רק על הבייטים של top_level.txt -> מפענח (deflate/store)

תלוי בזה שהשרת (Fastly CDN של PyPI) תומך ב-Range requests - והוא כן תומך.

קלט: pypi_packages.txt (שם חבילה בכל שורה)
פלט:
  - top_level_results.jsonl -> תוצאות מלאות, שורה אחת לכל חבילה (resume-able)
  - import_map.txt          -> מיפוי שטוח, שורה לכל import name: "import_name:package_name"
                               לדוגמה: cv2:opencv-python
"""

import json
import struct
import time
import zlib
from pathlib import Path
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

INPUT_FILE = "pypi_packages.txt"
OUTPUT_FILE = "top_level_results.jsonl"
MAP_FILE = "import_map.txt"
MAX_WORKERS = 20
TAIL_SIZE = 65536          # 64KB בדרך כלל מספיק כדי לתפוס את ה-EOCD + comment
REQUEST_TIMEOUT = 30

write_lock = Lock()
session = requests.Session()


def get_wheel_url(package_name):
    """מחזיר (url, size) של ה-wheel העדכני ביותר של החבילה, או (None, None)"""
    r = session.get(f"https://pypi.org/pypi/{package_name}/json", timeout=15)
    r.raise_for_status()
    data = r.json()

    version = data["info"]["version"]
    candidates = data["releases"].get(version, []) or data.get("urls", [])

    for f in candidates:
        if f["packagetype"] == "bdist_wheel" and f["filename"].endswith(".whl"):
            return f["url"], f["size"]
    return None, None


def fetch_range(url, start, end):
    headers = {"Range": f"bytes={start}-{end}"}
    r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    if r.status_code != 206:
        # השרת לא תמך ב-Range וחזר 200 עם כל הקובץ - תרחיש נדיר
        raise RuntimeError("Server did not honor Range request (no 206)")
    return r.content


def get_central_directory(url, total_size):
    """מביא רק את הזנב של הקובץ, מוצא EOCD, ואז מביא את ה-Central Directory"""
    tail_size = min(TAIL_SIZE, total_size)
    tail = fetch_range(url, total_size - tail_size, total_size - 1)

    idx = tail.rfind(b"PK\x05\x06")
    if idx == -1:
        raise ValueError("EOCD signature not found - comment section too large?")

    eocd = tail[idx:idx + 22]
    (_, _, _, _, _, cd_size, cd_offset, _) = struct.unpack("<IHHHHIIH", eocd)

    cd_data = fetch_range(url, cd_offset, cd_offset + cd_size - 1)
    return cd_data


def find_entries(cd_data, suffix="top_level.txt"):
    """פרסור ידני של ה-Central Directory, מחזיר רשומות שמסתיימות ב-suffix"""
    offset = 0
    results = []
    while offset + 46 <= len(cd_data):
        if cd_data[offset:offset + 4] != b"PK\x01\x02":
            break
        fields = struct.unpack("<HHHHHHIIIHHHHHII", cd_data[offset + 4:offset + 46])
        (_, _, _, method, _, _, _,
         comp_size, _, name_len, extra_len, comment_len,
         _, _, _, local_offset) = fields

        name = cd_data[offset + 46:offset + 46 + name_len].decode("utf-8", "replace")
        if name.endswith(suffix):
            results.append((name, local_offset, comp_size, method))

        offset += 46 + name_len + extra_len + comment_len
    return results


def read_entry_data(url, local_offset, comp_size, method):
    """מביא local file header כדי לדעת את גודל השם/extra, ואז מביא רק את הדאטה"""
    header = fetch_range(url, local_offset, local_offset + 29)
    (_, _, _, _, _, _, _, _, _, name_len, extra_len) = struct.unpack("<IHHHHHIIIHH", header)

    data_start = local_offset + 30 + name_len + extra_len
    data = fetch_range(url, data_start, data_start + comp_size - 1)

    if method == 0:  # STORED
        return data.decode("utf-8", "replace")
    elif method == 8:  # DEFLATE
        return zlib.decompress(data, -15).decode("utf-8", "replace")
    else:
        raise ValueError(f"unsupported compression method: {method}")


def process_package(name):
    try:
        url, size = get_wheel_url(name)
        if not url:
            return {"package": name, "error": "no wheel found"}

        cd_data = get_central_directory(url, size)
        entries = find_entries(cd_data)
        if not entries:
            return {"package": name, "error": "no top_level.txt in wheel"}

        top_level = []
        for entry_name, local_offset, comp_size, method in entries:
            content = read_entry_data(url, local_offset, comp_size, method)
            top_level.extend(line.strip() for line in content.splitlines() if line.strip())

        return {"package": name, "top_level": top_level}
    except Exception as e:
        return {"package": name, "error": str(e)}


def load_done(output_path):
    done = set()
    if Path(output_path).exists():
        with open(output_path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["package"])
                except Exception:
                    continue
    return done


def main():
    packages = [l.strip() for l in open(INPUT_FILE, encoding="utf-8") if l.strip()]
    done = load_done(OUTPUT_FILE)
    todo = [p for p in packages if p not in done]

    print(f"total: {len(packages)} | already done: {len(done)} | remaining: {len(todo)}")

    success_count = 0
    fail_count = 0
    start_time = time.monotonic()

    with open(OUTPUT_FILE, "a", encoding="utf-8") as out, \
         open(MAP_FILE, "a", encoding="utf-8") as map_out:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(process_package, p): p for p in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                result = fut.result()

                if "error" in result:
                    fail_count += 1
                else:
                    success_count += 1

                with write_lock:
                    out.write(json.dumps(result, ensure_ascii=False) + "\n")
                    out.flush()

                    for import_name in result.get("top_level", []):
                        map_out.write(f"{import_name}:{result['package']}\n")
                    map_out.flush()

                if i % 100 == 0 or i == len(todo):
                    elapsed = time.monotonic() - start_time
                    rate = i / elapsed if elapsed > 0 else 0
                    remaining = len(todo) - i
                    eta_sec = remaining / rate if rate > 0 else 0
                    print(
                        f"{i}/{len(todo)} | success: {success_count} | fail: {fail_count} "
                        f"| rate: {rate:.1f}/s | eta: {eta_sec/60:.1f} min"
                    )

    print(f"\ndone. success: {success_count} | fail: {fail_count}")
    print(f"note: {MAP_FILE} may contain duplicate import names across packages "
          f"(e.g. two packages both exporting the same top-level name). "
          f"dedupe manually if you need a strict 1:1 mapping.")


if __name__ == "__main__":
    main()