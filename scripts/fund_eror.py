# import json
# import struct
# import time
# import zlib
# from pathlib import Path
# from threading import Lock
# from concurrent.futures import ThreadPoolExecutor, as_completed

# import requests

# INPUT_FILE = "errors_packages.txt"
# OUTPUT_FILE = "top_level_results.jsonl"
# MAP_FILE = "fix_mapping_eeroes.txt"
# MAX_WORKERS = 20
# TAIL_SIZE = 65536          # 64KB is usually enough to capture the EOCD + comment
# REQUEST_TIMEOUT = 30

# write_lock = Lock()
# session = requests.Session()


# def get_wheel_url(package_name):
#     """Return (url, size) of the latest wheel for the package, or (None, None)"""
#     r = session.get(f"https://pypi.org/pypi/{package_name}/json", timeout=15)
#     r.raise_for_status()
#     data = r.json()

#     version = data["info"]["version"]
#     candidates = data["releases"].get(version, []) or data.get("urls", [])

#     for f in candidates:
#         if f["packagetype"] == "bdist_wheel" and f["filename"].endswith(".whl"):
#             return f["url"], f["size"]
#     return None, None


# def fetch_range(url, start, end):
#     headers = {"Range": f"bytes={start}-{end}"}
#     r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
#     r.raise_for_status()
#     if r.status_code != 206:
#         # The server did not support Range and returned the full file instead - rare case
#         raise RuntimeError("Server did not honor Range request (no 206)")
#     return r.content


# def get_central_directory(url, total_size):
#     """Fetch only the tail of the file, find the EOCD, then fetch the Central Directory"""
#     tail_size = min(TAIL_SIZE, total_size)
#     tail = fetch_range(url, total_size - tail_size, total_size - 1)

#     idx = tail.rfind(b"PK\x05\x06")
#     if idx == -1:
#         raise ValueError("EOCD signature not found - comment section too large?")

#     eocd = tail[idx:idx + 22]
#     (_, _, _, _, _, cd_size, cd_offset, _) = struct.unpack("<IHHHHIIH", eocd)

#     cd_data = fetch_range(url, cd_offset, cd_offset + cd_size - 1)
#     return cd_data


# def find_entries(cd_data, suffix="top_level.txt"):
#     """Manual parse of the Central Directory, returns entries ending with the suffix"""
#     offset = 0
#     results = []
#     while offset + 46 <= len(cd_data):
#         if cd_data[offset:offset + 4] != b"PK\x01\x02":
#             break
#         fields = struct.unpack("<HHHHHHIIIHHHHHII", cd_data[offset + 4:offset + 46])
#         (_, _, _, method, _, _, _,
#          comp_size, _, name_len, extra_len, comment_len,
#          _, _, _, local_offset) = fields

#         name = cd_data[offset + 46:offset + 46 + name_len].decode("utf-8", "replace")
#         if name.endswith(suffix):
#             results.append((name, local_offset, comp_size, method))

#         offset += 46 + name_len + extra_len + comment_len
#     return results


# def infer_top_level_from_wheel_filenames(cd_data):
#     """
#     Fallback for wheels that do not include top_level.txt (very common for newer wheels).
#     No additional network request is required - we already have the Central Directory in memory.
#     Infers import names from the first path segment of each file, ignoring
#     metadata directories (*.dist-info, *.data).
#     """
#     offset = 0
#     top_level = set()
#     SKIP_SUFFIXES = (".dist-info", ".data")

#     while offset + 46 <= len(cd_data):
#         if cd_data[offset:offset + 4] != b"PK\x01\x02":
#             break
#         fields = struct.unpack("<HHHHHHIIIHHHHHII", cd_data[offset + 4:offset + 46])
#         name_len, extra_len, comment_len = fields[9], fields[10], fields[11]
#         name = cd_data[offset + 46:offset + 46 + name_len].decode("utf-8", "replace")

#         first_segment = name.split("/", 1)[0]
#         if not any(first_segment.endswith(suf) for suf in SKIP_SUFFIXES):
#             if "/" in name:
#                 top_level.add(first_segment)          # package folder
#             elif name.endswith(".py") and first_segment != "setup.py":
#                 top_level.add(first_segment[:-3])      # single module at root

#         offset += 46 + name_len + extra_len + comment_len

#     return sorted(top_level)


# def get_sdist_url(package_name, package_json=None):
#     """Return the sdist (tar.gz) URL if available"""
#     data = package_json
#     if data is None:
#         r = session.get(f"https://pypi.org/pypi/{package_name}/json", timeout=15)
#         r.raise_for_status()
#         data = r.json()

#     version = data["info"]["version"]
#     candidates = data["releases"].get(version, []) or data.get("urls", [])
#     for f in candidates:
#         if f["packagetype"] == "sdist":
#             return f["url"]
#     return None


# def infer_top_level_from_sdist(url):
#     """
#     Fallback for packages without any wheel - only sdist is available.
#     tar.gz does not have a Central Directory like zip, so range requests by index are impossible
#     and the file must be streamed fully. Nothing is written to disk yet,
#     everything is read and parsed in memory while streaming.
#     """
#     import tarfile

#     r = session.get(url, stream=True, timeout=90)
#     r.raise_for_status()

#     top_level = set()
#     SKIP_DIRS = {"tests", "test", "docs", "doc", "examples", "build", "dist", "src"}

#     with tarfile.open(fileobj=r.raw, mode="r|gz") as tar:
#         for member in tar:
#             parts = member.name.split("/")
#             if len(parts) < 2:
#                 continue
#             candidate = parts[1]  # parts[0] is the root directory (pkgname-version)

#             if candidate.endswith(".egg-info") or candidate in SKIP_DIRS or not candidate:
#                 continue

#             if len(parts) >= 3 and parts[2] == "__init__.py":
#                 top_level.add(candidate)
#             elif len(parts) == 2 and candidate.endswith(".py") and candidate != "setup.py":
#                 top_level.add(candidate[:-3])

#     return sorted(top_level)


# def read_entry_data(url, local_offset, comp_size, method):
#     """Fetch the local file header to obtain name/extra lengths, then fetch only the file data"""
#     header = fetch_range(url, local_offset, local_offset + 29)
#     (_, _, _, _, _, _, _, _, _, name_len, extra_len) = struct.unpack("<IHHHHHIIIHH", header)

#     data_start = local_offset + 30 + name_len + extra_len
#     data = fetch_range(url, data_start, data_start + comp_size - 1)

#     if method == 0:  # STORED
#         return data.decode("utf-8", "replace")
#     elif method == 8:  # DEFLATE
#         return zlib.decompress(data, -15).decode("utf-8", "replace")
#     else:
#         raise ValueError(f"unsupported compression method: {method}")


# def process_package(name):
#     try:
#         url, size = get_wheel_url(name)

#         if url:
#             cd_data = get_central_directory(url, size)
#             entries = find_entries(cd_data)

#             if entries:
#                 top_level = []
#                 for entry_name, local_offset, comp_size, method in entries:
#                     content = read_entry_data(url, local_offset, comp_size, method)
#                     top_level.extend(l.strip() for l in content.splitlines() if l.strip())
#                 if top_level:
#                     return {"package": name, "top_level": top_level, "source": "top_level.txt"}

#             # fallback 1: no top_level.txt - infer from the wheel file listing
#             # (no extra network request - cd_data is already in memory)
#             top_level = infer_top_level_from_wheel_filenames(cd_data)
#             if top_level:
#                 return {"package": name, "top_level": top_level, "source": "wheel_filenames"}

#         # fallback 2: no wheel available (or the wheel yielded nothing) - try sdist
#         sdist_url = get_sdist_url(name)
#         if sdist_url:
#             top_level = infer_top_level_from_sdist(sdist_url)
#             if top_level:
#                 return {"package": name, "top_level": top_level, "source": "sdist"}

#         return {"package": name, "error": "no top_level info found (wheel or sdist)"}
#     except Exception as e:
#         return {"package": name, "error": str(e)}


# def load_done(output_path):
#     """
#     A package is considered 'complete' only if it succeeded. packages with errors remain in todo
#     and may retry later (relevant because we added new fallbacks that can succeed on retry).
#     """
#     done = set()
#     if Path(output_path).exists():
#         with open(output_path, encoding="utf-8") as f:
#             for line in f:
#                 try:
#                     obj = json.loads(line)
#                     if "error" not in obj:
#                         done.add(obj["package"])
#                 except Exception:
#                     continue
#     return done


# def dedupe_output_file(path):
#     """
#     After retry there may be multiple rows for the same package (for example an old error row +
#     a new success row). Keep only the last occurrence for each package.
#     """
#     if not Path(path).exists():
#         return
#     latest = {}
#     with open(path, encoding="utf-8") as f:
#         for line in f:
#             line = line.strip()
#             if not line:
#                 continue
#             try:
#                 obj = json.loads(line)
#                 latest[obj["package"]] = obj
#             except Exception:
#                 continue
#     with open(path, "w", encoding="utf-8") as f:
#         for obj in latest.values():
#             f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# def main():
#     packages = [l.strip() for l in open(INPUT_FILE, encoding="utf-8") if l.strip()]
#     done = load_done(OUTPUT_FILE)
#     todo = [p for p in packages if p not in done]

#     print(f"total: {len(packages)} | already done: {len(done)} | remaining: {len(todo)}")

#     success_count = 0
#     fail_count = 0
#     start_time = time.monotonic()

#     with open(OUTPUT_FILE, "a", encoding="utf-8") as out, \
#          open(MAP_FILE, "a", encoding="utf-8") as map_out:
#         with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
#             futures = {ex.submit(process_package, p): p for p in todo}
#             for i, fut in enumerate(as_completed(futures), 1):
#                 result = fut.result()

#                 if "error" in result:
#                     fail_count += 1
#                 else:
#                     success_count += 1

#                 with write_lock:
#                     out.write(json.dumps(result, ensure_ascii=False) + "\n")
#                     out.flush()

#                     for import_name in result.get("top_level", []):
#                         map_out.write(f"{import_name}:{result['package']}\n")
#                     map_out.flush()

#                 if i % 100 == 0 or i == len(todo):
#                     elapsed = time.monotonic() - start_time
#                     rate = i / elapsed if elapsed > 0 else 0
#                     remaining = len(todo) - i
#                     eta_sec = remaining / rate if rate > 0 else 0
#                     print(
#                         f"{i}/{len(todo)} | success: {success_count} | fail: {fail_count} "
#                         f"| rate: {rate:.1f}/s | eta: {eta_sec/60:.1f} min"
#                     )

#     print(f"\ndone. success: {success_count} | fail: {fail_count}")

#     dedupe_output_file(OUTPUT_FILE)
#     print(f"note: {MAP_FILE} may contain duplicate import names across packages "
#           f"(e.g. two packages both exporting the same top-level name). "
#           f"dedupe manually if you need a strict 1:1 mapping.")


# if __name__ == "__main__":
#     main()
