import requests


def save_pypi_packages_txt(filename="pypi_packages.txt"):
    url = "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages-30-days.min.json"

    print("⏳ Downloading the full package list from PyPI...")
    response = requests.get(url)
    response.raise_for_status()

    data = response.json()
    rows = data.get("rows", [])

    print(f"✅ Received {len(rows):,} packages.")

    # Write package names only - one package per line
    with open(filename, "w", encoding="utf-8") as f:
        for item in rows:
            f.write(f"{item['project']}\n")

    print(f"💾 All names were successfully saved to {filename}")


if __name__ == "__main__":
    save_pypi_packages_txt()
