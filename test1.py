import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

source_file = "/Users/shmuelbarda/Desktop/depvex/all_packages.txt"
output_file = "/Users/shmuelbarda/Desktop/depvex/packages_downloads.txt"

MAX_WORKERS = 20  # concurrency level - raise/lower depending on rate-limit behavior

write_lock = Lock()


def get_stats(package):
    url = f"https://pypistats.org/api/packages/{package}/recent"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None
        data = response.json()
        downloads = data.get("data")
        return downloads
    except Exception:
        return None


def load_already_done():
    """Read output file (if exists) and return set of packages already processed."""
    done = set()
    try:
        with open(output_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # line format: "package | day=... | week=... | month=..."
                pkg = line.split("|", 1)[0].strip()
                if pkg:
                    done.add(pkg)
    except FileNotFoundError:
        pass
    return done


def process_package(package, out_f):
    stats = get_stats(package)
    if stats:
        line = (
            f"{package} | "
            f"day={stats['last_day']} | "
            f"week={stats['last_week']} | "
            f"month={stats['last_month']}\n"
        )
        with write_lock:
            out_f.write(line)
            out_f.flush()
        return True
    else:
        print(f"NO DATA: {package}")
        return False


def main():
    with open(source_file, "r") as f:
        all_packages = [line.strip() for line in f if line.strip()]

    already_done = load_already_done()
    packages = [p for p in all_packages if p not in already_done]

    total_all = len(all_packages)
    skipped = total_all - len(packages)
    total = len(packages)

    print(f"Total packages: {total_all} | already done: {skipped} | remaining: {total}")

    if total == 0:
        print("Nothing left to do. Finished")
        return

    completed = 0
    # append mode - keeps previous results, resumes naturally
    with open(output_file, "a") as out_f:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_pkg = {
                executor.submit(process_package, pkg, out_f): pkg
                for pkg in packages
            }

            for future in as_completed(future_to_pkg):
                pkg = future_to_pkg[future]
                completed += 1
                try:
                    future.result()
                except Exception as e:
                    print(f"ERROR on {pkg}: {e}")
                print(f"{completed}/{total} done ({pkg})")

    print("Finished")


if __name__ == "__main__":
    main()