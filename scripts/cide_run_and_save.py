# import requests


# def get_top_pypi_packages(limit=15000):
#     url = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.min.json"

#     response = requests.get(url)
#     response.raise_for_status()

#     data = response.json()

#     packages = []

#     for item in data["rows"][:limit]:
#         packages.append(item["project"])

#     return packages

# def save_top_pypi_packages(filename="top_pypi_packages.txt", packages=None):
#     with open(filename, "w", encoding="utf-8") as f:
#         for package in packages:
#             f.write(package + "\n")
import requests


def get_top_pypi_packages(limit: int = 15000) -> list[str]:
    url = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.min.json"

    response = requests.get(url)
    response.raise_for_status()

    data = response.json()

    packages = []

    for item in data["rows"][:limit]:
        packages.append(item["project"])

    return packages


def get_existing_packages(filename: str = "import_mapping.txt") -> set[str]:
    packages = set()

    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if ":" not in line:
                continue

            _, package_name = line.split(":", 1)

            packages.add(package_name)

    return packages


def save_missing_packages(filename: str = "missing_popular_packages.txt", packages: list[str] | None = None) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        if packages is None:
            packages = []

        for package in packages:
            f.write(package + "\n")


top_packages = get_top_pypi_packages(15000)

existing_packages = get_existing_packages("import_mapping.txt")

missing = []

for package in top_packages:
    if package not in existing_packages:
        missing.append(package)


save_missing_packages("missing_popular_packages.txt", missing)

print(f"Top packages checked: {len(top_packages)}")
print(f"Missing packages: {len(missing)}")
