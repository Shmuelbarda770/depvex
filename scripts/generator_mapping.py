import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

TOP_PACKAGES_URL = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.min.json"
PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"

MAX_PACKAGES = 25000

# Request settings
REQUEST_TIMEOUT = 10
MAX_WORKERS = 30
RETRY_DELAY = 0.5
MAX_RETRIES = 2

# Packages to skip (meta-packages, namespace packages, etc.)
SKIP_PACKAGES = {
    "setuptools", "pip", "wheel", "distribute", "pkg-resources",
    "pkg_resources", "easy-install", "easy_install",
}

# Manual overrides for packages where metadata is wrong or missing
MANUAL_OVERRIDES = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "attr": "attrs",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "gi": "PyGObject",
    "git": "GitPython",
    "google.cloud": "google-cloud-core",
    "jose": "python-jose",
    "jwt": "PyJWT",
    "magic": "python-magic",
    "nacl": "PyNaCl",
    "serial": "pyserial",
    "usb": "pyusb",
    "wx": "wxPython",
    "Crypto": "pycryptodome",
    "Cryptodome": "pycryptodome",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "PyMuPDF",
    "zmq": "pyzmq",
    "dns": "dnspython",
    "websocket": "websocket-client",
    "socketio": "python-socketio",
    "Bio": "biopython",
    "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary",
    "win32com": "pywin32",
    "win32api": "pywin32",
    "win32gui": "pywin32",
    "pythoncom": "pywin32",
    "pywintypes": "pywin32",
    "speech_recognition": "SpeechRecognition",
    "skimage": "scikit-image",
    "hdbscan": "hdbscan",
    "paddle": "paddlepaddle",
    "decouple": "python-decouple",
    "telegram": "python-telegram-bot",
    "ldap": "python-ldap",
    "can": "python-can",
    "barcode": "python-barcode",
    "rapidjson": "python-rapidjson",
}


def fetch_top_packages() -> list[str]:
    """Fetch the list of top PyPI packages by download count."""
    print(f"[*] Fetching top packages list from {TOP_PACKAGES_URL}...")
    response = requests.get(TOP_PACKAGES_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    packages = [row["project"] for row in data["rows"]]
    print(f"[+] Got {len(packages)} packages from top-packages list")
    return packages[:MAX_PACKAGES]


def fetch_additional_packages_from_simple() -> list[str]:
    """
    Fetch additional package names from PyPI simple index to reach target count.
    This is slow but comprehensive.
    """
    print("[*] Fetching additional packages from PyPI simple index...")
    response = requests.get(
        "https://pypi.org/simple/",
        headers={"Accept": "application/vnd.pypi.simple.v1+json"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    packages = [p["name"] for p in data["projects"]]
    print(f"[+] Got {len(packages)} total packages from simple index")
    return packages


def normalize_package_name(name: str) -> str:
    """Normalize a PyPI package name for comparison."""
    return re.sub(r"[-_]+", "-", name).lower()


def get_package_import_names(package_name: str) -> list[str] | None:
    """
    Query PyPI JSON API to get a package's top-level import names.
    Returns None if the request fails.
    """
    url = PYPI_JSON_URL.format(package=package_name)

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            info = data.get("info", {})

            # Try to find top-level import names from various metadata fields
            import_names = set()

            # Method 1: Check "top_level.txt" equivalent from package info
            # PyPI doesn't directly expose this, but we can infer from:

            # Method 2: Check the provides_extra or packages fields
            packages_field = info.get("packages")
            if packages_field and isinstance(packages_field, list):
                for pkg in packages_field:
                    if "-" not in pkg and pkg.isidentifier():
                        import_names.add(pkg)

            # Method 3: Infer from the project name itself
            # Many packages use underscores in import but hyphens in package name
            project_name = info.get("name", package_name)
            inferred_import = project_name.replace("-", "_").lower()

            # Method 4: Check keywords/classifiers for actual module name
            # This is heuristic

            # Method 5: Look at the project URLs for source to find top_level.txt
            # Too slow for bulk processing

            # Best heuristic: the import name is usually the package name with
            # hyphens replaced by underscores
            if not import_names:
                import_names.add(inferred_import)

            # Also check for common patterns
            # e.g., python-dateutil -> dateutil
            if project_name.startswith("python-"):
                import_names.add(project_name[7:].replace("-", "_").lower())
            if project_name.startswith("py"):
                import_names.add(project_name[2:].replace("-", "_").lower())

            return list(import_names)

        except requests.RequestException:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                return None

    return None


def infer_import_from_package_name(package_name: str) -> set[str]:
    """
    Infer possible import names from a package name using common patterns.
    Returns a set of possible import names that differ from the package name.
    """
    results = set()
    normalized = normalize_package_name(package_name)
    lower_name = package_name.lower()

    # Hyphens become underscores in imports
    underscore_name = lower_name.replace("-", "_")
    if underscore_name != lower_name:
        results.add(underscore_name)

    # python-X -> X
    if lower_name.startswith("python-"):
        results.add(lower_name[7:].replace("-", "_"))

    # py-X or pyX -> X (sometimes)
    if lower_name.startswith("py-") and len(lower_name) > 3:
        results.add(lower_name[3:].replace("-", "_"))

    # X-python -> X
    if lower_name.endswith("-python"):
        results.add(lower_name[:-7].replace("-", "_"))

    # X-py -> X
    if lower_name.endswith("-py"):
        results.add(lower_name[:-3].replace("-", "_"))

    return results


def build_mapping_from_packages(packages: list[str]) -> dict[str, str]:
    """
    Build import_name -> package_name mapping for packages where they differ.
    Uses concurrent requests to speed up the process.
    """
    mapping: dict[str, str] = {}
    processed = 0
    total = len(packages)

    print(f"[*] Processing {total} packages with {MAX_WORKERS} workers...")

    def process_package(pkg_name: str) -> tuple[str, set[tuple[str, str]]]:
        """Process a single package and return (package_name, set of (import, package) pairs)."""
        pairs = set()
        normalized_pkg = normalize_package_name(pkg_name)

        # Infer import names from the package name pattern
        inferred = infer_import_from_package_name(pkg_name)
        for import_name in inferred:
            # Only add if import name is actually different from the normalized package name
            if import_name and import_name != normalized_pkg and import_name.isidentifier():
                pairs.add((import_name, pkg_name))

        return pkg_name, pairs

    # Process in batches
    batch_size = 500
    for batch_start in range(0, total, batch_size):
        batch = packages[batch_start:batch_start + batch_size]

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process_package, pkg): pkg for pkg in batch}

            for future in as_completed(futures):
                processed += 1
                try:
                    pkg_name, pairs = future.result()
                    for import_name, package_name in pairs:
                        if import_name not in mapping:
                            mapping[import_name] = package_name
                except Exception as e:
                    pass

        if processed % 1000 == 0:
            print(f"    [{processed}/{total}] processed, {len(mapping)} mappings so far...")

    return mapping


def fetch_all_pypi_packages_with_different_imports() -> dict[str, str]:
    """
    Main function: fetch packages and build the complete mapping.
    Uses the simple index to get ALL package names, then infers import names.
    """
    # Start with top packages for quality
    try:
        top_packages = fetch_top_packages()
    except Exception as e:
        print(f"[!] Could not fetch top packages: {e}")
        top_packages = []

    # Get ALL packages from simple index
    try:
        all_packages = fetch_additional_packages_from_simple()
    except Exception as e:
        print(f"[!] Could not fetch simple index: {e}")
        all_packages = []

    # Combine: top packages first (higher priority), then rest
    seen = set()
    combined = []
    for pkg in top_packages + all_packages:
        norm = normalize_package_name(pkg)
        if norm not in seen and norm not in SKIP_PACKAGES:
            seen.add(norm)
            combined.append(pkg)

    print(f"[*] Total unique packages to process: {len(combined)}")

    # Build mapping from package name patterns (no network needed per-package)
    mapping = build_mapping_from_packages(combined)

    # Add manual overrides (highest priority)
    for import_name, package_name in MANUAL_OVERRIDES.items():
        mapping[import_name] = package_name

    return mapping

def write_mappings_file(mapping: dict[str, str], output_path: Path) -> None:
    """Write the mapping dictionary to a Python file."""
    # Sort by import name for readability
    sorted_items = sorted(mapping.items(), key=lambda x: x[0].lower())

    lines = [
        '"""',
        "Static mapping of Python import names to their PyPI package names.",
        "",
        "This covers cases where the import name differs from the pip install name.",
        'For example: `import cv2` requires `pip install opencv-python`.',
        "",
        f"Auto-generated with {len(sorted_items)} entries.",
        "To regenerate: python scripts/generate_mappings.py",
        '"""',
        "",
        "KNOWN_IMPORT_TO_PACKAGE: dict[str, str] = {",
    ]

    for import_name, package_name in sorted_items:
        # Escape any quotes in names
        safe_import = import_name.replace('"', '\\"')
        safe_package = package_name.replace('"', '\\"')
        lines.append(f'    "{safe_import}": "{safe_package}",')

    lines.append("}")
    lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[+] Written {len(sorted_items)} mappings to {output_path}")


def main() -> None:
    # Determine output path
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    output_path = project_root / "depvex" / "known_mappings.py"

    print("=" * 60)
    print("  depvex - Import-to-Package Mapping Generator")
    print("=" * 60)
    print()

    mapping = fetch_all_pypi_packages_with_different_imports()

    print(f"\n[*] Total mappings found: {len(mapping)}")
    print(f"[*] Writing to: {output_path}")

    write_mappings_file(mapping, output_path)

    print("\n[+] Done!")
    print(f"    Generated {len(mapping)} import -> package mappings.")


if __name__ == "__main__":
    main()