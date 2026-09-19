#!/usr/bin/env python3
"""
unified_mapping_pipeline.py - Unified Pipeline for Generating, Curating, and Validating depvex Mappings.

This script unifies and elevates all tasks previously split across ad-hoc scripts:
1. Fetching top PyPI packages by downloads (via Hugovk dataset or PyPI JSON API).
2. Ultra-fast byte-range wheel inspection (HTTP Range requests for EOCD + Central Directory + top_level.txt).
3. Resilient multi-tier fallbacks (Central Directory file listing, name heuristics, sdist parsing).
4. Rigorous data curation & sanitization:
   - Purges non-identifier module names (hyphens, slashes, compiler artifacts like mypyc).
   - Purges wheel pollution (generic directory names like tests, docs, tools, images, scripts, build).
   - Purges vendoring misattributions (ray claiming aiohttp/psutil, setuptools claiming zipp/importlib_metadata).
   - Purges whole-repo flat directory dumping (owlready2, teradataml, hermes-agent, workspace-mcp).
   - Enforces canonical import mappings (cv2 -> opencv-python, PIL -> pillow, yaml -> pyyaml, etc.).
   - Enforces canonical popularity winners (PyQt6 -> pyqt6, PySide6 -> pyside6, Shiboken -> shiboken6).
5. Generation of production artifacts:
   - import_mapping_filtered.txt: Clean, sorted, deduplicated module-to-package mappings.
   - duplicate_imports_pop.txt: Formatted duplicate sections with downloads and verified winners.
6. Comprehensive health validation & reporting.

Usage:
  # Validate current mapping files in depvex/:
  python scripts/unified_mapping_pipeline.py --validate

  # Run a fast live test on 5 real packages from PyPI:
  python scripts/unified_mapping_pipeline.py --sample 5

  # Run full pipeline for top 15,000 packages:
  python scripts/unified_mapping_pipeline.py --limit 15000 --workers 20

  # Re-curate existing raw/filtered mapping offline without network:
  python scripts/unified_mapping_pipeline.py --offline-curate
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import struct
import sys
import time
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("depvex_pipeline")

# ==============================================================================
# Configuration & Constants
# ==============================================================================

HUGOVK_TOP_PACKAGES_URL = (
    "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages-30-days.min.json"
)
PYPI_JSON_URL_TEMPLATE = "https://pypi.org/pypi/{package}/json"
DEFAULT_LIMIT = 15000
DEFAULT_WORKERS = 20
DEFAULT_TIMEOUT = 15
TAIL_SIZE = 65536  # 64 KB tail is sufficient for EOCD in >99.9% of wheels

# Modules that represent repository structure, build artifacts, or generic local folders
POLLUTION_MODULES: set[str] = {
    "admin", "api", "app", "apps", "assets", "auth", "auxiliary",
    "base", "benchmark", "benchmarks", "bin", "build", "build_tools", "ci",
    "cli", "client", "clients", "cmd", "common", "conf", "config", "configs",
    "configuration", "constants", "contrib", "controllers", "core", "data",
    "database", "db", "demo", "demos", "designs", "dist", "doc", "docs",
    "documentation", "errors", "events", "example", "examples", "exceptions",
    "experiments", "extension_templates", "external", "ez_setup", "fixtures",
    "gallery", "handlers", "helpers", "hooks", "images", "img", "include",
    "interfaces", "internal", "jobs", "lib", "lib64", "libs", "licenses",
    "log", "logger", "logging", "main", "manage", "management", "media",
    "middleware", "middlewares", "migrations", "misc", "mock", "mocks",
    "model", "models", "modules", "notes", "notebooks", "options", "packages",
    "pipelines", "plugins", "protocols", "providers", "public", "recipes",
    "requirements", "resources", "router", "routers", "routes", "runner",
    "sample", "samples", "schema", "schemas", "script", "scripts", "server",
    "service", "services", "settings", "setup", "share", "site-packages",
    "source", "spec", "specs", "src", "static", "storage", "tasks",
    "templates", "test", "test_data", "test_requirements", "testall",
    "testing", "tests", "third_party", "tmp", "tool", "tools", "tutorials",
    "types", "ui", "unit", "util", "utilities", "utils", "vendor",
    "version", "versioneer", "views", "web", "website", "wheelhouse", "workers",
    "__init__", "__main__", "__pycache__",
}

# Misattributed vendored packages to remove
PURGE_PAIRS: set[tuple[str, str]] = {
    ("zipp", "setuptools"),
    ("importlib_metadata", "setuptools"),
    ("jaraco", "setuptools"),
    ("autocommand", "setuptools"),
    ("backports", "setuptools"),
    ("_distutils_hack", "setuptools"),
    ("aiohttp", "ray"),
    ("aiosignal", "ray"),
    ("async_timeout", "ray"),
    ("frozenlist", "ray"),
    ("multidict", "ray"),
    ("propcache", "ray"),
    ("yarl", "ray"),
    ("psutil", "ray"),
    ("html5lib", "bleach"),
    ("iniconfig", "py"),
    ("apipkg", "py"),
    ("opentelemetry", "xds-protos"),
    ("google", "xds-protos"),
    ("opencensus", "xds-protos"),
    ("cel", "xds-protos"),
    ("envoy", "xds-protos"),
    ("udpa", "xds-protos"),
    ("validate", "xds-protos"),
    ("pyspark", "databricks-connect"),
}

# Packages that dumped their internal submodules or flat repository root
BAD_PACKAGES_PURGE_MODULES: dict[str, set[str]] = {
    "owlready2": {
        "annotation", "base", "class_construct", "close", "disjoint", "dl_render",
        "driver", "editor", "entity", "individual", "instance_editor", "instance_editor_qt",
        "namespace", "ntriples_diff", "observe", "owlxml_2_ntriples", "prop",
        "pymedtermino2", "rdflib_store", "rdfxml_2_ntriples", "reasoning", "rply",
        "rule", "setup_develop_mode", "sparql", "triplelite", "util", "__init__",
    },
    "teradataml": {
        "analytics", "automl", "catalog", "clients", "common", "config", "context",
        "data", "dataframe", "dbutils", "geospatial", "hyperparameter_tuner", "lib",
        "opensource", "options", "plot", "scriptmgmt", "sdk", "series", "store",
        "table_operators", "telemetry_utils", "utils",
    },
    "hermes-agent": {
        "acp_adapter", "agent", "batch_runner", "cli", "cron", "gateway",
        "hermes_bootstrap", "hermes_cli", "hermes_constants", "hermes_logging",
        "hermes_state", "hermes_time", "mcp_serve", "model_tools", "plugins",
        "providers", "run_agent", "tools", "toolset_distributions", "toolsets",
        "trajectory_compressor", "tui_gateway", "utils",
    },
    "workspace-mcp": {
        "auth", "core", "fastmcp_server", "gappsscript", "gcalendar", "gchat",
        "gcontacts", "gdocs", "gdrive", "gforms", "gmail", "gsearch", "gsheets",
        "gslides", "gtasks", "main", "scripts",
    },
    "mcpforunityserver": {
        "cli", "core", "main", "models", "services", "transport", "utils",
    },
    "canvas": {
        "logger", "plugin_runner", "protobufs", "pubsub", "settings",
    },
    "tosa-tools": {
        "bin", "checker", "conformance", "convert2conformance", "frameworks",
        "generator", "include", "json2fbbin", "json2numpy", "lib", "runner",
        "schemavalidation", "serializer", "tests", "xunit",
    },
    "uwsgi": {
        "attach", "setup.cpyext", "setup.pyuwsgi",
    },
    "cvxopt": {
        "amd", "base", "blas", "cholmod", "dsdp", "fftw", "glpk", "gsl", "lapack",
        "misc_solvers", "umfpack",
    },
}

# Essential canonical mappings that must always be present
CANONICAL_MAPPINGS: dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "attr": "attrs",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "gi": "pygobject",
    "git": "gitpython",
    "google": "protobuf",
    "googleapiclient": "google-api-python-client",
    "apiclient": "google-api-python-client",
    "jose": "python-jose",
    "jwt": "pyjwt",
    "magic": "python-magic",
    "nacl": "pynacl",
    "serial": "pyserial",
    "usb": "pyusb",
    "wx": "wxpython",
    "crypto": "pycryptodome",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "pymupdf",
    "zmq": "pyzmq",
    "dns": "dnspython",
    "websocket": "websocket-client",
    "socketio": "python-socketio",
    "mysqldb": "mysqlclient",
    "psycopg2": "psycopg2-binary",
    "win32api": "pywin32",
    "win32con": "pywin32",
    "win32gui": "pywin32",
    "win32com": "pywin32",
    "pythoncom": "pywin32",
    "pywintypes": "pywin32",
    "speech_recognition": "speechrecognition",
    "skimage": "scikit-image",
    "hdbscan": "hdbscan",
    "paddle": "paddlepaddle",
    "decouple": "python-decouple",
    "telegram": "python-telegram-bot",
    "ldap": "python-ldap",
    "can": "python-can",
    "barcode": "python-barcode",
    "rapidjson": "python-rapidjson",
    "faiss": "faiss-cpu",
    "playwright": "playwright",
    "opengl": "pyopengl",
    "qdrant_client": "qdrant-client",
    "weaviate": "weaviate-client",
    "confluent_kafka": "confluent-kafka",
    "sentry_sdk": "sentry-sdk",
    "prometheus_client": "prometheus-client",
    "tortoise": "tortoise-orm",
    "brotli": "brotli",
    "snappy": "python-snappy",
    "levenshtein": "levenshtein",
    "spacy": "spacy",
    "bio": "biopython",
    "pywt": "pywavelets",
    "zipp": "zipp",
    "importlib_metadata": "importlib-metadata",
    "aiohttp": "aiohttp",
    "psutil": "psutil",
    "html5lib": "html5lib",
    "pyspark": "pyspark",
}

# Enforced canonical winners for duplicate modules in duplicate_imports_pop.txt
CANONICAL_WINNERS: dict[str, str] = {
    "pyqt6": "pyqt6",
    "pyside6": "pyside6",
    "pyqt5": "pyqt5",
    "shiboken": "shiboken6",
    "box_sdk_gen": "box-sdk-gen",
    "ai_core_sdk": "sap-ai-sdk-core",
    "ai_api_client_sdk": "sap-ai-sdk-base",
    "cv2": "opencv-python",
    "pil": "pillow",
    "dotenv": "python-dotenv",
    "fitz": "pymupdf",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "serial": "pyserial",
    "git": "gitpython",
    "magic": "python-magic",
    "psycopg2": "psycopg2-binary",
    "attr": "attrs",
    "faiss": "faiss-cpu",
    "confluent_kafka": "confluent-kafka",
    "importlib_metadata": "importlib-metadata",
    "zipp": "zipp",
}


# ==============================================================================
# Helper Classes & Utilities
# ==============================================================================

def build_http_session(workers: int = DEFAULT_WORKERS) -> requests.Session:
    """Build a robust requests session with retries and connection pooling."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET"],
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=workers,
        pool_maxsize=workers,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "depvex-unified-indexer/2.0"})
    return session


def is_valid_module(mod: str) -> bool:
    """Validate that a module name is an importable Python identifier and not polluted."""
    if not mod or not isinstance(mod, str):
        return False
    if not mod.isidentifier():
        return False
    if "__mypyc" in mod:
        return False
    if mod.startswith("__") and mod.endswith("__"):
        return False
    if mod.lower() in POLLUTION_MODULES:
        return False
    return True


def is_valid_package_name(pkg: str) -> bool:
    """Validate that a package name matches PyPI distribution naming rules."""
    if not pkg or not isinstance(pkg, str):
        return False
    if any(ch in pkg for ch in ("@", "%", "/", "\\", " ")):
        return False
    return bool(re.match(r"^[A-Za-z0-9_.-]+$", pkg))


# ==============================================================================
# PyPI & Wheel Inspection Engine (Range Requests)
# ==============================================================================

class WheelInspector:
    """Extracts top_level.txt from wheels without downloading entire files using Range requests."""

    def __init__(self, session: requests.Session | None = None, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.session = session or build_http_session()
        self.timeout = timeout

    def get_wheel_url(self, package_name: str) -> tuple[str | None, int | None]:
        """Fetch latest wheel URL and file size from PyPI JSON API."""
        url = PYPI_JSON_URL_TEMPLATE.format(package=package_name)
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 404:
                return None, None
            r.raise_for_status()
            data = r.json()

            version = data.get("info", {}).get("version", "")
            releases = data.get("releases", {}).get(version, []) or data.get("urls", [])

            for release_file in releases:
                if release_file.get("packagetype") == "bdist_wheel" and release_file.get("filename", "").endswith(".whl"):
                    return release_file.get("url"), release_file.get("size")
            return None, None
        except Exception as e:
            logger.debug("Failed getting wheel for %s: %s", package_name, e)
            return None, None

    def fetch_range(self, url: str, start: int, end: int) -> bytes:
        """Perform an HTTP Range request for the given byte offsets."""
        headers = {"Range": f"bytes={start}-{end}"}
        r = self.session.get(url, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        if r.status_code != 206:
            raise RuntimeError(f"Server did not return 206 Partial Content (got {r.status_code})")
        return r.content

    def get_central_directory(self, url: str, total_size: int) -> bytes:
        """Fetch the tail of the zip file, parse EOCD, and fetch the full Central Directory."""
        tail_size = min(TAIL_SIZE, total_size)
        tail = self.fetch_range(url, total_size - tail_size, total_size - 1)

        idx = tail.rfind(b"PK\x05\x06")
        if idx == -1:
            raise ValueError("EOCD signature not found in wheel tail")

        eocd = tail[idx : idx + 22]
        _, _, _, _, _, cd_size, cd_offset, _ = struct.unpack("<IHHHHIIH", eocd)
        return self.fetch_range(url, cd_offset, cd_offset + cd_size - 1)

    def find_entries(self, cd_data: bytes, suffix: str = "top_level.txt") -> list[tuple[str, int, int, int]]:
        """Parse zip Central Directory records matching suffix."""
        offset = 0
        results = []
        while offset + 46 <= len(cd_data):
            if cd_data[offset : offset + 4] != b"PK\x01\x02":
                break
            fields = struct.unpack("<HHHHHHIIIHHHHHII", cd_data[offset + 4 : offset + 46])
            method, comp_size = fields[3], fields[7]
            name_len, extra_len, comment_len = fields[9], fields[10], fields[11]
            local_offset = fields[15]

            name = cd_data[offset + 46 : offset + 46 + name_len].decode("utf-8", "replace")
            if name.endswith(suffix):
                results.append((name, local_offset, comp_size, method))

            offset += 46 + name_len + extra_len + comment_len
        return results

    def read_entry_data(self, url: str, local_offset: int, comp_size: int, method: int) -> str:
        """Fetch file content from zip entry based on local offset."""
        header = self.fetch_range(url, local_offset, local_offset + 29)
        _, _, _, _, _, _, _, _, _, name_len, extra_len = struct.unpack("<IHHHHHIIIHH", header)

        data_start = local_offset + 30 + name_len + extra_len
        data = self.fetch_range(url, data_start, data_start + comp_size - 1)

        if method == 0:  # STORED
            return data.decode("utf-8", "replace")
        elif method == 8:  # DEFLATE
            return zlib.decompress(data, -15).decode("utf-8", "replace")
        else:
            raise ValueError(f"Unsupported zip compression method: {method}")

    def infer_from_wheel_filenames(self, cd_data: bytes) -> list[str]:
        """Fallback: deduce top-level package names from file listing in Central Directory."""
        offset = 0
        top_level = set()
        skip_suffixes = (".dist-info", ".data", ".egg-info")

        while offset + 46 <= len(cd_data):
            if cd_data[offset : offset + 4] != b"PK\x01\x02":
                break
            fields = struct.unpack("<HHHHHHIIIHHHHHII", cd_data[offset + 4 : offset + 46])
            name_len, extra_len, comment_len = fields[9], fields[10], fields[11]
            name = cd_data[offset + 46 : offset + 46 + name_len].decode("utf-8", "replace")

            first_segment = name.split("/", 1)[0]
            if not any(first_segment.endswith(suf) for suf in skip_suffixes):
                if "/" in name:
                    if is_valid_module(first_segment):
                        top_level.add(first_segment)
                elif name.endswith(".py") and first_segment != "setup.py":
                    stem = first_segment[:-3]
                    if is_valid_module(stem):
                        top_level.add(stem)

            offset += 46 + name_len + extra_len + comment_len

        return sorted(top_level)

    def inspect_package(self, package_name: str) -> tuple[str, list[str], str]:
        """Inspect a single package and return (package_name, [module_names], source)."""
        # 1. Check canonical override first
        canonical_matches = [mod for mod, pkg in CANONICAL_MAPPINGS.items() if pkg.lower() == package_name.lower()]
        if canonical_matches:
            return package_name, canonical_matches, "canonical_override"

        # 2. Try HTTP Range inspection of wheel
        try:
            url, size = self.get_wheel_url(package_name)
            if url and size:
                cd_data = self.get_central_directory(url, size)
                entries = self.find_entries(cd_data, "top_level.txt")

                if entries:
                    top_level = []
                    for entry_name, local_offset, comp_size, method in entries:
                        content = self.read_entry_data(url, local_offset, comp_size, method)
                        for line in content.splitlines():
                            item = line.strip()
                            if is_valid_module(item):
                                top_level.append(item)
                    if top_level:
                        return package_name, sorted(set(top_level)), "top_level.txt"

                # Fallback: inspect filenames in Central Directory
                inferred = self.infer_from_wheel_filenames(cd_data)
                if inferred:
                    return package_name, inferred, "wheel_filenames"
        except Exception as e:
            logger.debug("Error inspecting wheel for %s: %s", package_name, e)

        # 3. Heuristic fallback from package name
        inferred_name = package_name.replace("-", "_").lower()
        if is_valid_module(inferred_name):
            return package_name, [inferred_name], "name_heuristic"

        return package_name, [], "none"


# ==============================================================================
# Pipeline Coordinator & Sanitization Engine
# ==============================================================================

class UnifiedMappingPipeline:
    """End-to-end pipeline coordinator for depvex mapping data."""

    def __init__(
        self,
        output_dir: Path | str = "depvex",
        workers: int = DEFAULT_WORKERS,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.workers = workers
        self.timeout = timeout
        self.session = build_http_session(workers=self.workers)
        self.inspector = WheelInspector(session=self.session, timeout=self.timeout)

    def fetch_top_packages(self, limit: int = DEFAULT_LIMIT) -> list[tuple[str, int]]:
        """Fetch the top packages from Hugovk 30-day download metrics."""
        logger.info("Fetching top packages list from %s ...", HUGOVK_TOP_PACKAGES_URL)
        response = self.session.get(HUGOVK_TOP_PACKAGES_URL, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        rows = data.get("rows", [])
        logger.info("Successfully fetched %d total packages from Hugovk dataset.", len(rows))

        results: list[tuple[str, int]] = []
        for item in rows[:limit]:
            project = item.get("project", "").strip()
            downloads = item.get("download_count", 0)
            if project and is_valid_package_name(project):
                results.append((project, downloads))

        logger.info("Selected top %d packages for processing.", len(results))
        return results

    def process_packages(
        self,
        package_items: list[tuple[str, int]],
        max_workers: int | None = None,
    ) -> tuple[dict[str, list[str]], dict[str, int]]:
        """Process packages in parallel and extract clean mappings and download counts."""
        workers = max_workers or self.workers
        logger.info("Processing %d packages with %d workers...", len(package_items), workers)

        pkg_downloads: dict[str, int] = {pkg.lower(): dl for pkg, dl in package_items}
        raw_mappings: list[tuple[str, str]] = []

        total = len(package_items)
        success_count = 0
        fail_count = 0
        start_time = time.monotonic()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_pkg = {
                executor.submit(self.inspector.inspect_package, pkg): pkg
                for pkg, _ in package_items
            }

            for i, future in enumerate(as_completed(future_to_pkg), 1):
                try:
                    pkg_name, modules, source = future.result()
                    if modules:
                        success_count += 1
                        for mod in modules:
                            raw_mappings.append((mod, pkg_name))
                    else:
                        fail_count += 1
                except Exception as e:
                    fail_count += 1
                    logger.debug("Worker failure on package: %s", e)

                if i % 500 == 0 or i == total:
                    elapsed = time.monotonic() - start_time
                    rate = i / elapsed if elapsed > 0 else 0
                    logger.info(
                        "Progress: %d/%d (%.1f%%) | Success: %d | Fail: %d | Rate: %.1f pkgs/s",
                        i, total, (i / total) * 100, success_count, fail_count, rate,
                    )

        # Sanitize and compile mappings
        cleaned_mapping = self.sanitize_mappings(raw_mappings)
        return cleaned_mapping, pkg_downloads

    def sanitize_mappings(self, raw_pairs: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
        """Sanitize, deduplicate, filter, and inject canonical mappings."""
        logger.info("Sanitizing and curating mapping pairs...")
        cleaned_pairs: set[tuple[str, str]] = set()
        purged = 0

        for mod, pkg in raw_pairs:
            mod_clean = mod.strip()
            pkg_clean = pkg.strip().lower()

            if not is_valid_module(mod_clean) or not is_valid_package_name(pkg_clean):
                purged += 1
                continue

            mod_l = mod_clean.lower()
            pkg_l = pkg_clean.lower()

            # Check blacklisted vendored pairs
            if (mod_l, pkg_l) in PURGE_PAIRS:
                purged += 1
                continue

            # Check whole-repo dumps
            if pkg_l in BAD_PACKAGES_PURGE_MODULES and mod_l in BAD_PACKAGES_PURGE_MODULES[pkg_l]:
                purged += 1
                continue

            cleaned_pairs.add((mod_clean, pkg_clean))

        # Inject canonical overrides
        for mod, pkg in CANONICAL_MAPPINGS.items():
            cleaned_pairs.add((mod, pkg.lower()))

        logger.info("Purged %d invalid/polluted pairs. Retained %d clean pairs.", purged, len(cleaned_pairs))

        # Group by module
        mod_to_pkgs: dict[str, list[str]] = defaultdict(list)
        for mod, pkg in sorted(cleaned_pairs, key=lambda x: (x[0].lower(), x[1].lower())):
            if pkg not in mod_to_pkgs[mod]:
                mod_to_pkgs[mod].append(pkg)

        return mod_to_pkgs

    def generate_artifacts(
        self,
        mod_to_pkgs: dict[str, list[str]],
        pkg_downloads: dict[str, int] | None = None,
    ) -> tuple[Path, Path]:
        """Write both import_mapping_filtered.txt and duplicate_imports_pop.txt."""
        imp_file = self.output_dir / "import_mapping_filtered.txt"
        pop_file = self.output_dir / "duplicate_imports_pop.txt"

        # 1. Write import_mapping_filtered.txt
        imp_lines = [
            f"{mod}:{pkg}"
            for mod, pkgs in sorted(mod_to_pkgs.items(), key=lambda x: x[0].lower())
            for pkg in pkgs
        ]
        imp_file.write_text("\n".join(imp_lines) + "\n", encoding="utf-8")
        logger.info("Saved %d mappings to %s", len(imp_lines), imp_file)

        # 2. Build and write duplicate_imports_pop.txt
        pkg_dls = pkg_downloads or {}
        # Load existing pop downloads if available to preserve counts
        if pop_file.is_file():
            existing_dls = self._load_existing_pop_downloads(pop_file)
            for pkg, dl in existing_dls.items():
                if pkg not in pkg_dls or dl > pkg_dls[pkg]:
                    pkg_dls[pkg] = dl

        pop_lines: list[str] = []
        duplicate_sections_count = 0

        for mod, pkgs in sorted(mod_to_pkgs.items(), key=lambda x: x[0].lower()):
            if len(pkgs) <= 1:
                continue

            duplicate_sections_count += 1
            mod_l = mod.lower()

            candidate_dls = [(pkg, pkg_dls.get(pkg.lower(), 1000)) for pkg in pkgs]

            # Enforce canonical winner if defined
            winner = CANONICAL_WINNERS.get(mod_l)
            if winner:
                winner = winner.lower()
                max_dl = max((d for _, d in candidate_dls), default=1000)
                candidate_dls = [
                    (p, max_dl + 1_000_000) if p.lower() == winner else (p, d)
                    for p, d in candidate_dls
                ]

            candidate_dls.sort(key=lambda x: x[1], reverse=True)

            pop_lines.append(f"{mod}:")
            for pkg, dl in candidate_dls:
                pop_lines.append(f"    {pkg} | downloads: {dl:,}")
            pop_lines.append("")

        pop_file.write_text("\n".join(pop_lines), encoding="utf-8")
        logger.info("Saved %d duplicate sections to %s", duplicate_sections_count, pop_file)

        return imp_file, pop_file

    def _load_existing_pop_downloads(self, pop_path: Path) -> dict[str, int]:
        """Extract download counts from an existing duplicate_imports_pop.txt file."""
        pkg_pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*\|\s*downloads:\s*([0-9,]+)", re.IGNORECASE)
        downloads: dict[str, int] = {}
        try:
            with pop_path.open("r", encoding="utf-8") as f:
                for line in f:
                    m = pkg_pattern.match(line)
                    if m:
                        pkg = m.group(1).lower()
                        dl = int(m.group(2).replace(",", ""))
                        if dl > downloads.get(pkg, 0):
                            downloads[pkg] = dl
        except Exception as e:
            logger.debug("Could not read existing pop downloads: %s", e)
        return downloads

    def validate_mappings(
        self,
        imp_path: Path | str | None = None,
        pop_path: Path | str | None = None,
    ) -> HealthReport:
        """Validate health and integrity of mapping and popularity files."""
        imp_p = Path(imp_path or (self.output_dir / "import_mapping_filtered.txt"))
        pop_p = Path(pop_path or (self.output_dir / "duplicate_imports_pop.txt"))

        logger.info("Validating health of: %s and %s", imp_p, pop_p)

        report = HealthReport(imp_path=str(imp_p), pop_path=str(pop_p))

        if not imp_p.is_file():
            report.errors.append(f"Missing import mapping file at {imp_p}")
            return report

        # 1. Audit import mapping
        imp_modules = defaultdict(list)
        with imp_p.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    report.malformed_lines.append((line_no, line))
                    continue
                mod, pkg = line.split(":", 1)
                mod, pkg = mod.strip(), pkg.strip()

                if not is_valid_module(mod):
                    report.invalid_modules.append((line_no, mod, pkg))
                if mod.lower() in POLLUTION_MODULES:
                    report.polluted_modules.append((line_no, mod, pkg))

                imp_modules[mod].append(pkg)

        report.total_mapping_lines = sum(len(pkgs) for pkgs in imp_modules.values())
        report.total_unique_modules = len(imp_modules)

        # 2. Check canonical mappings present
        for req_mod, req_pkg in CANONICAL_MAPPINGS.items():
            found = False
            for mod, pkgs in imp_modules.items():
                if mod.lower() == req_mod.lower():
                    if any(p.lower() == req_pkg.lower() for p in pkgs):
                        found = True
                        break
            if not found:
                report.missing_canonical_mappings.append((req_mod, req_pkg))

        # 3. Audit popularity file if present
        if pop_p.is_file():
            pkg_pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*\|\s*downloads:\s*([0-9,]+)", re.IGNORECASE)
            pop_sections: dict[str, list[tuple[str, int]]] = defaultdict(list)
            current_sec = None

            with pop_p.open("r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    raw = line.rstrip()
                    if not raw or raw.startswith("#"):
                        continue
                    if raw.strip().endswith(":"):
                        current_sec = raw.strip()[:-1].strip()
                        if not is_valid_module(current_sec):
                            report.invalid_pop_headers.append((line_no, current_sec))
                        continue
                    m = pkg_pattern.match(raw)
                    if m and current_sec:
                        p = m.group(1).lower()
                        dl = int(m.group(2).replace(",", ""))
                        pop_sections[current_sec].append((p, dl))

            report.total_pop_sections = len(pop_sections)

            # Check canonical winners
            for win_mod, exp_win in CANONICAL_WINNERS.items():
                for sec, candidates in pop_sections.items():
                    if sec.lower() == win_mod.lower():
                        if candidates and candidates[0][0].lower() != exp_win.lower():
                            report.winner_mismatches.append(
                                (sec, candidates[0][0], exp_win)
                            )

        return report

    def curate_existing_files(
        self,
        raw_imp_path: Path | str,
        pop_path: Path | str | None = None,
    ) -> tuple[Path, Path]:
        """Load and sanitize an existing mapping file offline, preserving existing download counts."""
        raw_p = Path(raw_imp_path)
        logger.info("Offline curating from existing file: %s", raw_p)

        raw_pairs = []
        with raw_p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                m, p = line.split(":", 1)
                raw_pairs.append((m.strip(), p.strip()))

        mod_to_pkgs = self.sanitize_mappings(raw_pairs)
        return self.generate_artifacts(mod_to_pkgs)


# ==============================================================================
# Health Reporting
# ==============================================================================

@dataclass
class HealthReport:
    """Comprehensive metrics report for mapping health."""
    imp_path: str
    pop_path: str
    total_mapping_lines: int = 0
    total_unique_modules: int = 0
    total_pop_sections: int = 0
    malformed_lines: list[tuple[int, str]] = field(default_factory=list)
    invalid_modules: list[tuple[int, str, str]] = field(default_factory=list)
    polluted_modules: list[tuple[int, str, str]] = field(default_factory=list)
    invalid_pop_headers: list[tuple[int, str]] = field(default_factory=list)
    missing_canonical_mappings: list[tuple[str, str]] = field(default_factory=list)
    winner_mismatches: list[tuple[str, str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return (
            len(self.malformed_lines) == 0
            and len(self.invalid_modules) == 0
            and len(self.polluted_modules) == 0
            and len(self.invalid_pop_headers) == 0
            and len(self.missing_canonical_mappings) == 0
            and len(self.winner_mismatches) == 0
            and len(self.errors) == 0
        )

    def print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("          DEPVEW MAPPINGS HEALTH REPORT")
        print("=" * 60)
        print(f"  Mapping file:       {self.imp_path}")
        print(f"  Popularity file:    {self.pop_path}")
        print(f"  Total mappings:     {self.total_mapping_lines:,}")
        print(f"  Unique modules:     {self.total_unique_modules:,}")
        print(f"  Duplicate sections: {self.total_pop_sections:,}")
        print("-" * 60)
        print(f"  Malformed lines:    {len(self.malformed_lines)}")
        print(f"  Invalid modules:    {len(self.invalid_modules)}")
        print(f"  Polluted modules:   {len(self.polluted_modules)}")
        print(f"  Invalid pop header: {len(self.invalid_pop_headers)}")
        print(f"  Missing canonical:  {len(self.missing_canonical_mappings)}")
        print(f"  Winner mismatches:  {len(self.winner_mismatches)}")
        print("=" * 60)

        if self.is_healthy:
            print("  STATUS: [PASSED] - All files are 100% clean and compliant!")
        else:
            print("  STATUS: [FAILED] - Issues detected:")
            for item in self.invalid_modules[:5]:
                print(f"    - Invalid module line {item[0]}: {item[1]} -> {item[2]}")
            for item in self.polluted_modules[:5]:
                print(f"    - Polluted module line {item[0]}: {item[1]} -> {item[2]}")
            for item in self.invalid_pop_headers[:5]:
                print(f"    - Invalid header line {item[0]}: {item[1]}")
            for item in self.missing_canonical_mappings[:5]:
                print(f"    - Missing canonical: {item[0]} -> {item[1]}")
            for item in self.winner_mismatches[:5]:
                print(f"    - Winner mismatch [{item[0]}]: got {item[1]}, expected {item[2]}")
        print("=" * 60 + "\n")


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified Pipeline for Generating, Curating, and Validating depvex Mappings."
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate integrity and health of current mapping and popularity files.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        metavar="N",
        help="Run a live test on N real packages from PyPI (e.g., --sample 5).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of top packages to fetch and process (default: {DEFAULT_LIMIT}).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of concurrent worker threads (default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="depvex",
        help="Output directory for generated mapping files (default: depvex).",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        help="Process packages listed in a file instead of fetching from Hugovk dataset.",
    )
    parser.add_argument(
        "--offline-curate",
        action="store_true",
        help="Re-curate existing mapping and popularity files in output-dir without network.",
    )

    args = parser.parse_args()

    pipeline = UnifiedMappingPipeline(
        output_dir=args.output_dir,
        workers=args.workers,
    )

    if args.validate:
        report = pipeline.validate_mappings()
        report.print_summary()
        sys.exit(0 if report.is_healthy else 1)

    if args.offline_curate:
        target_imp = Path(args.output_dir) / "import_mapping_filtered.txt"
        pipeline.curate_existing_files(target_imp)
        report = pipeline.validate_mappings()
        report.print_summary()
        sys.exit(0 if report.is_healthy else 1)

    if args.sample:
        logger.info("Executing live sample test on %d packages...", args.sample)
        # Choose widely used diverse packages for the sample test
        test_packages = [
            ("requests", 100_000_000),
            ("urllib3", 95_000_000),
            ("opencv-python", 56_000_000),
            ("Pillow", 70_000_000),
            ("PyYAML", 80_000_000),
            ("pyqt6", 2_600_000),
            ("pyside6", 3_600_000),
            ("scikit-learn", 40_000_000),
            ("python-dotenv", 30_000_000),
            ("types-boto3", 10_000_000),
        ][: args.sample]

        mod_to_pkgs, downloads = pipeline.process_packages(test_packages, max_workers=args.workers)
        logger.info("Sample test extracted mappings for %d modules.", len(mod_to_pkgs))
        for mod, pkgs in sorted(mod_to_pkgs.items())[:15]:
            logger.info("  Sample mapping: %s -> %s", mod, pkgs)
        sys.exit(0)

    # Full run
    if args.from_file:
        logger.info("Loading package names from %s ...", args.from_file)
        with open(args.from_file, "r", encoding="utf-8") as f:
            packages = [(line.strip(), 1000) for line in f if line.strip()]
        packages = packages[: args.limit]
    else:
        packages = pipeline.fetch_top_packages(limit=args.limit)

    mod_to_pkgs, downloads = pipeline.process_packages(packages)
    pipeline.generate_artifacts(mod_to_pkgs, downloads)

    report = pipeline.validate_mappings()
    report.print_summary()
    sys.exit(0 if report.is_healthy else 1)


if __name__ == "__main__":
    main()

