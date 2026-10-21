import importlib.util
import logging
import os
import re
import sys
import time
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path
from typing import Any

import requests
from packaging.specifiers import SpecifierSet
from packaging.version import Version

IMPORT_MAPPING_FILE = Path(__file__).resolve().parent / "import_mapping_filtered.txt"
POPULARITY_MAPPING_FILE = Path(__file__).resolve().parent / "duplicate_imports_pop.txt"

from depvex.parser import DynamicImportFinding, ImportExtractor, ImportRecord, ParseResult
from depvex.utils.read_config import project_config
from depvex.utils.read_yaml_config import read_yaml_config

_log = logging.getLogger("depvex")

_PEP503_SEPARATORS = re.compile(r"[-_.]+")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_PASS_THROUGH_PREFIXES = ("-", "git+", "hg+", "svn+", "bzr+", "http://", "https://", "file:", "./", "../", "/")

# Import roots that are shared by many independently published distributions.
# ``google.cloud.storage`` and ``google.auth`` live in different packages, so the
# first path segment alone cannot identify the dependency.
ALWAYS_NAMESPACE_ROOTS = frozenset(
    {"google", "azure", "opentelemetry", "zope", "sphinxcontrib", "backports", "jaraco", "ruamel", "nvidia", "awslabs"}
)

# Minimum number of dotted segments that make up a distribution name by convention.
# ``google.cloud.storage`` -> ``google-cloud-storage`` needs three; two would match
# the ``google-cloud`` meta package.
_NAMESPACE_MIN_PARTS = {"google": 3}

# Distributions that only aggregate other packages and are never the right answer
# for a specific import.
_NAMESPACE_META_PACKAGES = frozenset(
    {"google-cloud", "azure-storage", "azure-mgmt", "azure-ai", "azure-cognitiveservices", "azure-communication"}
)

# Imports whose distribution name does not follow the ``a.b.c`` -> ``a-b-c`` convention.
NAMESPACE_OVERRIDES: dict[str, str] = {
    "google.protobuf": "protobuf",
    "google.auth": "google-auth",
    "google.oauth2": "google-auth",
    "google.api_core": "google-api-core",
    "google.api": "googleapis-common-protos",
    "google.rpc": "googleapis-common-protos",
    "google.type": "googleapis-common-protos",
    "google.longrunning": "googleapis-common-protos",
    "google.genai": "google-genai",
    "google.generativeai": "google-generativeai",
    "google.adk": "google-adk",
    "google.appengine": "appengine-python-standard",
    "opentelemetry.trace": "opentelemetry-api",
    "opentelemetry.metrics": "opentelemetry-api",
    "opentelemetry.context": "opentelemetry-api",
    "opentelemetry.baggage": "opentelemetry-api",
    "opentelemetry.propagate": "opentelemetry-api",
    "opentelemetry.propagators": "opentelemetry-api",
    "opentelemetry.semconv": "opentelemetry-semantic-conventions",
}


def canonical_package_name(name: str) -> str:
    """Normalize a distribution name the way pip does (PEP 503)."""
    return _PEP503_SEPARATORS.sub("-", name.strip()).lower()


def is_pass_through_line(line: str) -> bool:
    """True for requirements-file lines that are not plain ``name[specifier]`` entries.

    Options (``-r``, ``-c``, ``-e``, ``--index-url``) and direct VCS/URL/path
    requirements are owned by the user: depvex keeps them and never reports
    them as stale.
    """
    stripped = line.strip()
    return stripped.startswith(_PASS_THROUGH_PREFIXES)


def requirement_name(entry: str) -> str:
    """Return the PEP 503 distribution name of a requirements line, or ``""`` when it has none."""
    if not entry or is_pass_through_line(entry) or entry.strip().startswith("#"):
        return ""
    match = _REQUIREMENT_NAME.match(entry)
    return canonical_package_name(match.group(1)) if match else ""


@dataclass(frozen=True)
class DependencyScopes:
    """Dependencies classified by their runtime, test-only, and notebook usage.

    A package imported by both application and test code remains a runtime
    dependency. ``dev_imports`` therefore contains only test-exclusive imports.
    """

    runtime_imports: set[str]
    dev_imports: set[str]
    notebook_imports: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class DynamicImportWarning:
    """A dynamic import whose module target cannot be determined statically."""

    file_path: str
    loader: str
    line_number: int


@dataclass(frozen=True)
class ParseWarning:
    """A source file Python's parser rejected; its imports were recovered line by line."""

    file_path: str
    error: str


def _resolve_file_path(filename: str, configured_var_name: str | None = None) -> Path:
    if configured_var_name and configured_var_name in globals():
        configured_path = globals()[configured_var_name]
        if configured_path is not None:
            path = Path(configured_path)
            if path.is_file():
                return path

    module_path = Path(__file__).resolve()
    for candidate in (
        module_path.parent / filename,
        module_path.parents[1] / filename,
    ):
        if candidate.is_file():
            return candidate

    return module_path.parent / filename


def _resolve_import_mapping_path() -> Path:
    return _resolve_file_path("import_mapping_filtered.txt", "IMPORT_MAPPING_FILE")


def _resolve_popularity_mapping_path() -> Path:
    return _resolve_file_path("duplicate_imports_pop.txt", "POPULARITY_MAPPING_FILE")


class DependencyResolver:
    CAPTIVE_PORTAL_URLS: list[str] = getattr(
        project_config,
        "CAPTIVE_PORTAL_URLS",
        ["http://connectivitycheck.gstatic.com/generate_204"],
    )

    def __init__(
        self,
        parser: ImportExtractor | None = None,
        root: str = ".",
        ignore_dirs: Iterable[str] | None = None,
    ) -> None:
        self.parser = parser or ImportExtractor()
        self.root = os.path.abspath(root)
        self.python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

        yaml_config = read_yaml_config(start_dir=self.root)
        self.MICRO_SERVICE_FOLDERS: list[str] = getattr(yaml_config, "micro_servi_folders", [])
        self.IGNORE_DIRS: set[str] = set(getattr(yaml_config, "ignore_dirs", []))
        self.IGNORE_DIRS.update(directory for directory in (ignore_dirs or []) if directory)
        self.IGNORE_PACKAGES: set[str] = {
            self._normalize_module_name(package)
            for package in getattr(yaml_config, "ignore_packages", [])
            if isinstance(package, str) and self._normalize_module_name(package)
        }
        self.DYNAMIC_IMPORTS: set[str] = {
            self._normalize_module_name(module_name)
            for module_name in getattr(yaml_config, "dynamic_imports", [])
            if isinstance(module_name, str) and self._normalize_module_name(module_name)
        }
        self.NOTEBOOKS_TARGET: str = getattr(yaml_config, "notebooks_target", "requirements-notebooks.txt")
        optional_mode = str(getattr(yaml_config, "optional_imports", "include")).lower()
        self.OPTIONAL_IMPORTS: str = optional_mode if optional_mode in {"include", "exclude"} else "include"
        self.dynamic_import_warnings: list[DynamicImportWarning] = []
        self.parse_warnings: list[ParseWarning] = []
        self.optional_imports: set[str] = set()
        self._namespace_roots: dict[str, bool] = {}
        self._known_packages: dict[str, str] | None = None

        try:
            self.top_level_distributions = packages_distributions()
        except Exception:
            self.top_level_distributions = {}

        self.IMPORT_MAPPING = self._load_import_mapping_file()
        self.POPULARITY_MAPPING = self._load_popularity_mapping_file()

        self._dir_entries: dict[Path, set[str]] = {}
        self._self_names: set[str] = set()
        self.local_modules = self._index_local_modules(self.root)

    def should_merge_notebooks_into_runtime(self) -> bool:
        target = getattr(self, "NOTEBOOKS_TARGET", "requirements-notebooks.txt")
        return target in {"requirements.txt", "./requirements.txt"}

    def get_notebooks_target_path(self, root: str) -> str:
        target = getattr(self, "NOTEBOOKS_TARGET", "requirements-notebooks.txt") or "requirements-notebooks.txt"
        return os.path.join(root, target) if not os.path.isabs(target) else target

    def has_notebooks(self, root: str, exclude_dirs: set[str] | None = None) -> bool:
        return any(True for _ in self._walk_notebook_files(root, exclude_dirs=exclude_dirs))

    _SKIP_DIRS = frozenset(
        {".git", "__pycache__", ".venv", "venv", "env", "node_modules", "build", "dist", "site-packages"}
    )
    _SOURCE_ROOT_DIRS = ("src", "lib", "python")

    def _index_local_modules(self, root_dir: str) -> set[str]:
        """Index the project's own importable modules.

        Besides the flat set of dotted names used by :meth:`is_local_import`,
        this records, per directory, which modules and packages a file in (or
        below) that directory can import without installing anything. That is
        what makes sibling modules (``scripts/run.py`` importing ``helper``)
        and ``src/`` layouts (``src/pkg`` imported as ``pkg``) resolve as local
        code instead of being mistaken for PyPI packages.
        """
        local_mods: set[str] = set()
        root_path = Path(root_dir).resolve()
        skip_dirs = set(self._SKIP_DIRS) | self.IGNORE_DIRS

        module_stems: dict[Path, set[str]] = {}
        child_dirs: dict[Path, set[str]] = {}
        python_dirs: set[Path] = set()

        for dirpath, dirnames, filenames in os.walk(root_path):
            current = Path(dirpath)
            try:
                rel_dir = current.relative_to(root_path)
            except ValueError:
                continue

            dirnames[:] = [
                name
                for name in dirnames
                if name not in skip_dirs
                and not name.startswith(".")
                and not name.endswith(".egg-info")
                and not self._is_ignored_dir("/".join((*rel_dir.parts, name)))
            ]

            stems = {
                Path(name).stem.lower()
                for name in filenames
                if name.endswith(".py") and not name.startswith(".") and name != "__init__.py"
            }
            module_stems[current] = stems
            child_dirs[current] = {name.lower() for name in dirnames}

            if stems or "__init__.py" in filenames:
                python_dirs.add(current)
                python_dirs.update(parent for parent in current.parents if root_path in (parent, *parent.parents))

            for filename in filenames:
                if not filename.endswith(".py") or filename.startswith("."):
                    continue
                parts = [*rel_dir.parts, Path(filename).stem]
                if parts[-1] == "__init__":
                    parts.pop()
                for i in range(1, len(parts) + 1):
                    local_mods.add(".".join(parts[:i]).lower())

        self._dir_entries = {
            directory: stems | {name for name in child_dirs[directory] if (directory / name) in python_dirs}
            for directory, stems in module_stems.items()
            if directory in python_dirs
        }

        local_mods.update(self._dir_entries.get(root_path, set()))
        for source_root in self._SOURCE_ROOT_DIRS:
            local_mods.update(self._dir_entries.get(root_path / source_root, set()))

        for entry in root_path.iterdir():
            if entry.name not in skip_dirs and not entry.name.startswith(".") and entry.is_file():
                local_mods.add(entry.name.lower())
                if entry.suffix == ".py":
                    local_mods.add(entry.stem.lower())

        # A scan started inside a package directory still imports that package by name.
        self._self_names = {root_path.name.lower()} if (root_path / "__init__.py").is_file() else set()
        local_mods.update(self._self_names)

        self._index_root = root_path
        _log.debug("indexed %d local module paths/roots in project", len(local_mods))
        return local_mods

    def _importable_names(self, directory: Path) -> set[str]:
        """Names importable from *directory*: its own modules plus ``src``/``lib`` layouts below it."""
        names = set(self._dir_entries.get(directory, ()))
        for source_root in self._SOURCE_ROOT_DIRS:
            names.update(self._dir_entries.get(directory / source_root, ()))
        return names

    def _is_local_to_file(self, module_name: str, file_path: str) -> bool:
        """True when *file_path* can import *module_name* from the project itself.

        Mirrors how Python and test runners resolve imports: a module is
        visible from its own directory and from the directories above it, up to
        the project root.
        """
        top_level = module_name.split(".", 1)[0].lower()
        if not top_level or top_level in self._self_names:
            return True

        directory = Path(os.path.abspath(file_path)).parent
        root_path = getattr(self, "_index_root", None)
        while True:
            if top_level in self._importable_names(directory):
                return True
            if directory == root_path or directory.parent == directory:
                return False
            directory = directory.parent

    def is_local_import(self, module_name: str) -> bool:
        if not module_name:
            return True

        normalized = self._normalize_module_name(module_name)

        if module_name.startswith("."):
            return True

        if normalized in self.local_modules:
            return True

        root_module = normalized.split(".")[0]
        if root_module in self.local_modules:
            return True

        return False

    def is_stdlib_module(self, module_name: str) -> bool:
        normalized = self._normalize_module_name(module_name).split(".")[0]

        if hasattr(sys, "stdlib_module_names"):
            return normalized in sys.stdlib_module_names

        stdlib_fallback = {
            "os",
            "sys",
            "re",
            "time",
            "pathlib",
            "typing",
            "collections",
            "functools",
            "importlib",
            "json",
            "ast",
            "unittest",
            "tempfile",
            "math",
            "random",
            "subprocess",
            "shutil",
            "logging",
            "threading",
        }
        return normalized in stdlib_fallback

    def _is_ignored_dir(self, rel_path: str) -> bool:
        if not self.IGNORE_DIRS:
            return False
        rel_path = rel_path.replace(os.sep, "/")
        name = os.path.basename(rel_path)
        return name in self.IGNORE_DIRS or rel_path in self.IGNORE_DIRS

    def _is_ignored_package(self, module_name: str) -> bool:
        module = self._normalize_module_name(module_name)

        if self.is_local_import(module_name) or self.is_stdlib_module(module_name):
            return True

        package = self._normalize_module_name(self._module_to_package_name(module_name))
        top_level = module.split(".")[0]
        return (
            module in self.IGNORE_PACKAGES
            or top_level in self.IGNORE_PACKAGES
            or package in self.IGNORE_PACKAGES
            or canonical_package_name(package) in {canonical_package_name(p) for p in self.IGNORE_PACKAGES}
        )

    def internet_check(self, timeout: int = 3) -> bool:
        if requests is None:
            return False

        for url in self.CAPTIVE_PORTAL_URLS:
            try:
                response = requests.get(url, timeout=timeout)
                if response.status_code == 204:
                    return True
            except requests.RequestException:
                pass
        return False

    def is_installed(self, module_name: str) -> bool:
        try:
            return importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError, AttributeError):
            # find_spec imports parent packages of a dotted name and can fail on broken ones.
            return False

    @lru_cache(maxsize=512)
    def get_local_version(self, module_name: str) -> str | None:
        try:
            return distribution(module_name).version
        except PackageNotFoundError:
            return None

    @lru_cache(maxsize=512)
    def get_pypi_version(self, module_name: str) -> str | None:
        if requests is None:
            return None

        try:
            url = f"https://pypi.org/pypi/{module_name}/json"
            response = requests.get(url, timeout=3)
            response.raise_for_status()
            version = response.json()["info"]["version"]
            return version if isinstance(version, str) else None
        except (requests.RequestException, KeyError, TypeError, ValueError):
            return None

    def resolve(self, module_name: str, has_net: bool) -> str:
        normalized_module = self._normalize_module_name(module_name)
        if not normalized_module:
            _log.debug(f"resolve skipped empty module for {module_name!r}")
            return ""

        if self.is_local_import(module_name) or self.is_stdlib_module(module_name):
            _log.debug(f"skipping local/stdlib module={normalized_module}")
            return ""

        _log.debug(f"resolving module={normalized_module} has_net={has_net}")
        mapped = self._mapped_package_name(normalized_module)
        if mapped:
            _log.debug(f"module={normalized_module} mapped to package={mapped}")
            package_name = mapped
        elif self.is_installed(normalized_module):
            _log.debug(f"module={normalized_module} is installed; using installed module name")
            package_name = self._module_to_package_name(normalized_module, use_mapping=False)
        else:
            package_name = self._module_to_package_name(normalized_module, use_mapping=True)
            _log.debug(f"module={normalized_module} has no mapping and is not installed; using fallback package={package_name}")

        version = self.get_local_version(package_name)
        if version:
            _log.debug(f"package={package_name} has local version={version}")
            return f"{package_name}=={version}"

        if has_net:
            latest_version = self.get_pypi_version(package_name)
            if latest_version:
                _log.debug(f"package={package_name} resolved latest version={latest_version}")
                return f"{package_name}=={latest_version}"
            _log.debug(f"package={package_name} has no network version; using package name")
            return package_name

        _log.debug(f"package={package_name} returned without version info")
        return package_name

    def _module_to_package_name(self, module_name: str, use_mapping: bool = True) -> str:
        normalized = self._normalize_module_name(module_name)
        if not normalized:
            return normalized

        candidate = self.top_level_distributions.get(normalized)
        if candidate:
            return candidate[0]

        if use_mapping:
            mapped = self._mapped_package_name(normalized)
            if mapped:
                return mapped

        return normalized

    def find_compatible_version(self, package: str, python_version: str) -> dict[str, Any]:
        url = f"https://pypi.org/pypi/{package}/json"

        response: requests.Response = requests.get(url, timeout=5)
        response.raise_for_status()

        data = response.json()

        compatible_versions = []

        for version_string, files in data["releases"].items():
            if not files:
                continue

            try:
                version = Version(version_string)
            except Exception:
                continue

            if all(file.get("yanked", False) for file in files):
                continue

            for file in files:
                requires_python = file.get("requires_python")

                if not requires_python:
                    continue

                try:
                    specifier = SpecifierSet(requires_python)

                    if Version(python_version) in specifier:
                        compatible_versions.append(version)
                        break

                except Exception:
                    continue

        if not compatible_versions:
            return {
                "compatible": False,
                "latest_compatible": None,
            }

        latest = max(compatible_versions)

        return {
            "compatible": True,
            "latest_compatible": str(latest),
        }

    def _normalize_module_name(self, module_name: str) -> str:
        name = module_name.strip()
        if not name or name.startswith("#"):
            return ""

        name = re.split(r"\s+#", name, maxsplit=1)[0].strip()
        match = re.match(r"([A-Za-z0-9_.-]+)", name)
        if not match:
            return ""
        return match.group(1).lower()

    def _load_import_mapping_file(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        mapping_path = _resolve_import_mapping_path()
        _log.debug(f"import mapping path={mapping_path}")
        if not mapping_path.is_file():
            _log.debug(f"import mapping file does not exist at {mapping_path}")
            return mapping

        _log.debug(f"import mapping file exists at {mapping_path}")
        try:
            with mapping_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    parts = line.split(":", 1)
                    if len(parts) != 2:
                        _log.debug(f"skipping malformed mapping line {line_number}: {line!r}")
                        continue

                    module_name = self._normalize_module_name(parts[0])
                    package_name = self._normalize_module_name(parts[1])
                    if module_name and package_name:
                        mapping.setdefault(module_name, []).append(package_name)
        except (OSError, UnicodeDecodeError) as exc:
            _log.debug(f"failed to read import mapping file: {exc}")
            return {}

        _log.debug(f"loaded {len(mapping)} mapping entries from {mapping_path}")
        return mapping

    def _load_popularity_mapping_file(self) -> dict[str, str]:
        popularity_map: dict[str, str] = {}
        pop_path = _resolve_popularity_mapping_path()
        if not pop_path.is_file():
            _log.debug(f"popularity file does not exist at {pop_path}")
            return popularity_map

        current_module = None
        max_downloads = -1
        best_package = None

        pkg_pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*\|\s*downloads:\s*([0-9,]+)", re.IGNORECASE)

        try:
            with pop_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw_line = line.rstrip()
                    if not raw_line or raw_line.startswith("#"):
                        continue

                    if raw_line.strip().endswith(":"):
                        if current_module and best_package:
                            popularity_map[current_module] = best_package

                        current_module = self._normalize_module_name(raw_line.strip().rstrip(":"))
                        max_downloads = -1
                        best_package = None
                        continue

                    match = pkg_pattern.match(raw_line)
                    if match and current_module:
                        pkg_name = self._normalize_module_name(match.group(1))
                        downloads_str = match.group(2).replace(",", "")
                        try:
                            downloads = int(downloads_str)
                        except ValueError:
                            downloads = 0

                        if downloads > max_downloads:
                            max_downloads = downloads
                            best_package = pkg_name

                if current_module and best_package:
                    popularity_map[current_module] = best_package

        except (OSError, UnicodeDecodeError) as exc:
            _log.debug(f"failed to read popularity mapping file: {exc}")
            return {}

        _log.debug(f"loaded {len(popularity_map)} popular choices from {pop_path}")
        return popularity_map

    def _known_package_names(self) -> dict[str, str]:
        """Every distribution name the import mapping knows, keyed by its PEP 503 form."""
        if self._known_packages is None:
            self._known_packages = {
                canonical_package_name(package): package
                for packages in self.IMPORT_MAPPING.values()
                for package in packages
            }
        return self._known_packages

    def _is_namespace_root(self, top_level: str) -> bool:
        """True when many distributions share *top_level* as their import root (``google``, ``azure``...)."""
        name = top_level.lower()
        if name in ALWAYS_NAMESPACE_ROOTS:
            return True

        cached = self._namespace_roots.get(name)
        if cached is not None:
            return cached

        candidates = self.IMPORT_MAPPING.get(name, [])
        prefixes = (f"{name}-", f"{name}.", f"{name}_")
        is_root = (
            len(candidates) >= 3
            and sum(candidate.startswith(prefixes) for candidate in candidates) / len(candidates) >= 0.8
            and canonical_package_name(name) not in self._known_package_names()
        )
        self._namespace_roots[name] = is_root
        return is_root

    def _namespace_match(self, dotted_name: str) -> tuple[str, str] | None:
        """Resolve a dotted import to ``(matched_import_prefix, distribution)``.

        Curated overrides win; otherwise the longest prefix whose hyphen-joined
        form is a known distribution is used (``azure.storage.blob.aio`` ->
        ``azure-storage-blob``). Returns ``None`` when nothing is known, so the
        caller can fall back to the top-level mapping.
        """
        parts = [part for part in dotted_name.split(".") if part]
        if len(parts) < 2:
            return None
        lowered = [part.lower() for part in parts]

        for length in range(len(parts), 0, -1):
            override = NAMESPACE_OVERRIDES.get(".".join(lowered[:length]))
            if override:
                return ".".join(parts[:length]), override

        known = self._known_package_names()
        minimum = _NAMESPACE_MIN_PARTS.get(lowered[0], 2)
        for length in range(len(parts), minimum - 1, -1):
            canonical = canonical_package_name("-".join(lowered[:length]))
            package = known.get(canonical)
            if package and canonical not in _NAMESPACE_META_PACKAGES:
                return ".".join(parts[:length]), package
        return None

    def _canonical_import_names(self, record: ImportRecord) -> list[str]:
        """Names to resolve for one import statement.

        Ordinary imports resolve by their top-level package. For namespace
        roots the matched dotted prefix is kept so the right distribution can
        be chosen: ``from google.cloud import storage, bigquery`` yields
        ``google.cloud.storage`` and ``google.cloud.bigquery``.
        """
        if not self._is_namespace_root(record.top_level):
            return [record.top_level]

        candidates = record.candidates()
        matched: list[str] = []
        for candidate in candidates[:-1] or candidates:
            match = self._namespace_match(candidate)
            if match and match[0] not in matched:
                matched.append(match[0])
        if not matched:
            match = self._namespace_match(record.module)
            if match:
                matched.append(match[0])
        return matched or [record.top_level]

    def _mapped_package_name(self, module_name: str) -> str | None:
        normalized = self._normalize_module_name(module_name)
        if not normalized:
            _log.debug("cannot map empty module name")
            return None

        if "." in normalized:
            match = self._namespace_match(normalized)
            if match:
                return self._normalize_module_name(match[1])
            normalized = normalized.split(".", 1)[0]

        candidates = self.IMPORT_MAPPING.get(normalized)
        if not candidates:
            _log.debug(f"no mapping found for module={normalized}")
            return None

        if len(candidates) == 1:
            result = candidates[0]
            _log.debug(f"mapping for module={normalized} -> {result}")
            return result

        popular_choice = self.POPULARITY_MAPPING.get(normalized)
        if popular_choice and popular_choice in candidates:
            _log.debug(f"popularity map for module={normalized} -> {popular_choice}")
            return popular_choice

        for candidate in candidates:
            if candidate == normalized:
                _log.debug(f"mapping for module={normalized} -> {candidate}")
                return candidate

        result = candidates[0]
        _log.debug(f"mapping for module={normalized} -> {result} (first candidate)")
        return result

    def _read_existing_requirements(self, path: str) -> list[str]:
        """Plain requirement entries in *path*; comments, options and URL/VCS lines are not entries."""
        if not os.path.exists(path):
            _log.debug("requirements file does not exist at %s", path)
            return []

        try:
            with open(path, "r", encoding="utf-8") as handle:
                return [
                    line.strip()
                    for line in handle
                    if line.strip() and not line.strip().startswith("#") and not is_pass_through_line(line)
                ]
        except (OSError, UnicodeDecodeError) as exc:
            _log.debug("failed to read requirements file %s: %s", path, exc)
            return []

    @staticmethod
    def _requirements_file_layout(path: str) -> tuple[list[str], list[str], dict[str, list[str]]]:
        """Split an existing requirements file into the parts depvex must not destroy.

        Returns ``(header, options, comments)``: the leading comment block, the
        option / URL / VCS lines (``-r base.txt``, ``--index-url ...``), and the
        comment lines that sit directly above each requirement, keyed by name.
        """
        header: list[str] = []
        options: list[str] = []
        comments: dict[str, list[str]] = {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = [line.rstrip("\r\n") for line in handle]
        except (OSError, UnicodeDecodeError):
            return header, options, comments

        pending: list[str] = []
        seen_requirement = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if not seen_requirement:
                    header.extend([*pending, ""])
                pending = []
            elif stripped.startswith("#"):
                pending.append(stripped)
            elif is_pass_through_line(stripped):
                options.extend([*pending, stripped])
                pending = []
            else:
                seen_requirement = True
                name = requirement_name(stripped)
                if name:
                    comments[name] = pending
                pending = []

        while header and not header[-1]:
            header.pop()
        return header, options, comments

    def write_req(self, lines: Iterable[str], path: str = "requirements.txt") -> None:
        """Write *lines* sorted, keeping the user's header, options and per-package comments."""
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        valid_lines = [line for line in lines if line]
        header, options, comments = self._requirements_file_layout(path)

        output: list[str] = []
        if header:
            output.extend([*header, ""])
        output.extend(options)
        for line in sorted(set(valid_lines)):
            output.extend(comments.get(requirement_name(line), []))
            output.append(line)

        try:
            with open(path, "w", encoding="utf-8") as handle:
                for line in output:
                    handle.write(line + "\n")
        except OSError as exc:
            _log.debug("failed to write requirements file %s: %s", path, exc)

    def _get_imports_for_file(self, file_path: str) -> tuple[str, ...]:
        """Top-level third-party module names imported by *file_path*."""
        return tuple(sorted({record.top_level for record in self._parse_file(file_path).records}))

    def _get_dynamic_imports_for_file(self, file_path: str) -> tuple[DynamicImportFinding, ...]:
        """Return runtime import-loader calls in *file_path*."""
        return tuple(self._parse_file(file_path).dynamic_findings)

    def _parse_file(self, file_path: str) -> ParseResult:
        """Parse one source file or notebook, cached until the file changes."""
        try:
            cache_key = (file_path, os.stat(file_path).st_mtime_ns)
        except OSError as exc:
            _log.debug("failed to inspect %s: %s", file_path, exc)
            return ParseResult(error=str(exc))
        return self._parse_file_cached(cache_key)

    @lru_cache(maxsize=2048)
    def _parse_file_cached(self, cache_key: tuple[str, int]) -> ParseResult:
        file_path, _ = cache_key
        try:
            with open(file_path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            _log.debug("failed to read %s: %s", file_path, exc)
            return ParseResult(error=str(exc))

        if file_path.endswith(".ipynb"):
            return self.parser.parse_notebook(raw.decode("utf-8", errors="replace"))
        return self.parser.parse_source(raw)

    def _file_dependencies(self, file_path: str) -> list[tuple[str, bool]]:
        """``(name, optional)`` pairs *file_path* needs from outside the project."""
        dependencies: list[tuple[str, bool]] = []
        for record in self._parse_file(file_path).records:
            for name in self._canonical_import_names(record):
                if not self._is_local_to_file(name, file_path):
                    dependencies.append((name, record.optional))
        return dependencies

    def _get_active_service_folders(self, root: str) -> list[str]:
        return [folder for folder in self.MICRO_SERVICE_FOLDERS if os.path.isdir(os.path.join(root, folder))]

    def discover_imports(self, root: str, exclude_dirs: set[str] | None = None) -> set[str]:
        """Return runtime imports for backward-compatible callers."""
        return self.discover_import_scopes(root, exclude_dirs).runtime_imports

    def discover_import_scopes(self, root: str, exclude_dirs: set[str] | None = None) -> DependencyScopes:
        """Discover imports and separate application imports from test-only and notebook imports."""
        runtime_imports: set[str] = set()
        test_imports: set[str] = set()
        notebook_imports: set[str] = set()
        required: set[str] = set()
        self.dynamic_import_warnings = []
        self.parse_warnings = []

        def collect(file_path: str, target: set[str]) -> None:
            for name, optional in self._file_dependencies(file_path):
                target.add(name)
                if not optional:
                    required.add(name)
            parsed = self._parse_file(file_path)
            if parsed.used_fallback:
                self.parse_warnings.append(ParseWarning(file_path, parsed.error or "syntax error"))

        for file_path in self._walk_python_files(root, exclude_dirs=exclude_dirs):
            collect(file_path, test_imports if self._is_test_file(file_path, root) else runtime_imports)
            self._record_dynamic_import_warnings(file_path)

        for file_path in self._walk_notebook_files(root, exclude_dirs=exclude_dirs):
            collect(file_path, notebook_imports)

        runtime_imports.update(self.DYNAMIC_IMPORTS)
        required.update(self.DYNAMIC_IMPORTS)

        if self.should_merge_notebooks_into_runtime():
            runtime_imports.update(notebook_imports)
            notebook_imports.clear()

        filtered_runtime = {module for module in runtime_imports if not self._is_ignored_package(module)}
        filtered_dev = {module for module in test_imports - filtered_runtime if not self._is_ignored_package(module)}
        filtered_notebooks = {module for module in notebook_imports if not self._is_ignored_package(module)}

        # Imports that only ever appear inside ``try/except ImportError`` are optional features.
        self.optional_imports = {module for module in filtered_runtime if module not in required}
        if self.OPTIONAL_IMPORTS == "exclude":
            filtered_runtime -= self.optional_imports
        return DependencyScopes(filtered_runtime, filtered_dev, filtered_notebooks)

    @staticmethod
    def _is_test_file(file_path: str, root: str) -> bool:
        """Identify conventional Python test files without relying on pytest."""
        relative_path = Path(file_path).resolve().relative_to(Path(root).resolve())
        filename = relative_path.name
        return (
            any(part in {"test", "tests"} for part in relative_path.parts[:-1])
            or filename == "conftest.py"
            or filename.startswith("test_")
            or filename.endswith("_test.py")
        )

    def _record_dynamic_import_warnings(self, file_path: str) -> None:
        """Store warnings for computed import targets found in *file_path*."""
        for finding in self._get_dynamic_imports_for_file(file_path):
            if finding.module_name is None or finding.loader in {"load_plugin", "load_entry_point"}:
                self.dynamic_import_warnings.append(
                    DynamicImportWarning(file_path, finding.loader, finding.line_number)
                )

    def print_parse_warnings(self) -> None:
        """Report files Python could not parse and whose imports were recovered heuristically."""
        for warning in self.parse_warnings:
            relative = os.path.relpath(warning.file_path, self.root)
            print(
                f"[depvex] Warning: {relative} could not be parsed ({warning.error}); "
                "its imports were recovered line by line and may be incomplete."
            )

    def print_dynamic_import_warnings(self) -> None:
        """Print actionable warnings for imports that static analysis cannot resolve."""
        for warning in self.dynamic_import_warnings:
            print(
                "[depvex] Warning: dynamic import target cannot be resolved "
                f"({warning.loader} at {warning.file_path}:{warning.line_number}). "
                "Add its module name to dynamic_imports in depvex.yaml."
            )

    def _walk_python_files(self, root: str, exclude_dirs: set[str] | None = None) -> Iterator[str]:
        exclude_dirs = exclude_dirs or set()
        base_skip = {".git", "__pycache__", ".venv", "venv", "node_modules"}
        root_abs = os.path.abspath(root)

        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root_abs)

            if os.path.abspath(dirpath) == root_abs:
                dirnames[:] = [d for d in dirnames if d not in base_skip and d not in exclude_dirs]
            else:
                dirnames[:] = [d for d in dirnames if d not in base_skip]

            dirnames[:] = [d for d in dirnames if not self._is_ignored_dir(d if rel_dir == "." else f"{rel_dir}/{d}")]

            for filename in filenames:
                if filename.endswith(".py") and not filename.startswith("."):
                    yield os.path.join(dirpath, filename)

    def _walk_notebook_files(self, root: str, exclude_dirs: set[str] | None = None) -> Iterator[str]:
        exclude_dirs = exclude_dirs or set()
        base_skip = {".git", "__pycache__", ".venv", "venv", "node_modules", ".ipynb_checkpoints"}
        root_abs = os.path.abspath(root)

        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root_abs)

            if os.path.abspath(dirpath) == root_abs:
                dirnames[:] = [d for d in dirnames if d not in base_skip and d not in exclude_dirs]
            else:
                dirnames[:] = [d for d in dirnames if d not in base_skip]

            dirnames[:] = [d for d in dirnames if not self._is_ignored_dir(d if rel_dir == "." else f"{rel_dir}/{d}")]

            for filename in filenames:
                if filename.endswith(".ipynb") and not filename.startswith("."):
                    yield os.path.join(dirpath, filename)

    def requirements_for(
        self,
        root: str,
        output_path: str | None = None,
        prune_stale: bool = True,
        exclude_dirs: set[str] | None = None,
        scope: str = "runtime",
    ) -> list[str]:
        # Keep the runtime path behind ``discover_imports`` for public API and
        # test-double compatibility. Development and notebook dependencies need the richer
        # scoped result because they are scoped.
        if scope == "runtime":
            discovered = self.discover_imports(root, exclude_dirs=exclude_dirs)
        elif scope == "dev":
            discovered = self.discover_import_scopes(root, exclude_dirs=exclude_dirs).dev_imports
        elif scope == "notebooks":
            discovered = self.discover_import_scopes(root, exclude_dirs=exclude_dirs).notebook_imports
        else:
            raise ValueError("scope must be one of 'runtime', 'dev', or 'notebooks'")

        if output_path is None:
            if scope == "notebooks":
                output_path = self.get_notebooks_target_path(root)
            else:
                output_path = os.path.join(root, "requirements.txt")

        requirements: list[str] = []
        has_net = self.internet_check()

        if prune_stale and os.path.exists(output_path):
            existing_entries = self._read_existing_requirements(output_path)
            existing_by_name = {requirement_name(entry): entry for entry in existing_entries if requirement_name(entry)}

            for module_name in sorted(discovered):
                if not module_name:
                    continue

                normalized_package = canonical_package_name(self._module_to_package_name(module_name))
                if not normalized_package:
                    continue

                if normalized_package in existing_by_name:
                    requirements.append(existing_by_name[normalized_package])
                else:
                    res = self.resolve(module_name, has_net)
                    if res:
                        requirements.append(res)

            return requirements

        for module_name in sorted(discovered):
            if module_name:
                res = self.resolve(module_name, has_net)
                if res:
                    requirements.append(res)

        return requirements

    def _rebuild_single(
        self,
        root: str,
        output_path: str | None = None,
        prune_stale: bool = True,
        exclude_dirs: set[str] | None = None,
    ) -> list[str]:
        requirements = self.requirements_for(root, output_path, prune_stale, exclude_dirs)
        self.write_req(requirements, path=output_path or os.path.join(root, "requirements.txt"))
        return requirements

    def read_pyproject_dependencies(self, path: str) -> list[str]:
        """Read runtime dependencies from the ``[project]`` table."""
        return self._read_pyproject_dependency_list(path, "dependencies")

    def read_pyproject_optional_dependencies(self, path: str, group: str = "dev") -> list[str]:
        """Read one ``[project.optional-dependencies]`` group, such as ``dev``."""
        return self._read_pyproject_dependency_list(path, group, optional_group=True)

    @staticmethod
    def _read_pyproject_dependency_list(path: str, key: str, optional_group: bool = False) -> list[str]:
        try:
            with open(path, "rb") as handle:
                project = tomllib.load(handle).get("project", {})
        except (OSError, tomllib.TOMLDecodeError):
            return []

        dependencies = project.get("optional-dependencies", {}).get(key, []) if optional_group else project.get(key, [])
        return (
            [dependency for dependency in dependencies if isinstance(dependency, str)]
            if isinstance(dependencies, list)
            else []
        )

    def write_pyproject_dependencies(self, path: str, dependencies: Iterable[str]) -> None:
        """Replace ``[project].dependencies`` while preserving other TOML content."""
        self._write_pyproject_dependency_list(path, dependencies, "dependencies")

    def write_pyproject_optional_dependencies(self, path: str, dependencies: Iterable[str], group: str = "dev") -> None:
        """Replace an optional dependency group, creating its TOML table when needed."""
        self._write_pyproject_dependency_list(path, dependencies, group, optional_group=True)

    @staticmethod
    def _write_pyproject_dependency_list(
        path: str, dependencies: Iterable[str], key: str, optional_group: bool = False
    ) -> None:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()

        dependency_lines = "\n".join(f'    "{dependency}",' for dependency in sorted(set(dependencies)))
        replacement = f"{key} = [\n{dependency_lines}\n]"
        table_name = "project.optional-dependencies" if optional_group else "project"
        table_match = re.search(rf"(?ms)^\[{re.escape(table_name)}\]\n(?P<body>.*?)(?=^\[|\Z)", content)

        if table_match is None:
            if optional_group:
                updated_content = f"{content.rstrip()}\n\n[{table_name}]\n{replacement}\n"
            else:
                raise ValueError(f"{path} does not contain a [project] table")
        else:
            table_body = table_match.group("body")
            updated_body, replacements = re.subn(
                rf"(?ms)^{re.escape(key)}\s*=\s*\[.*?\]", replacement, table_body, count=1
            )
            if replacements == 0:
                updated_body = f"{table_body.rstrip()}\n\n{replacement}\n"
            updated_content = f"{content[:table_match.start('body')]}{updated_body}{content[table_match.end('body'):]}"

        with open(path, "w", encoding="utf-8") as handle:
            handle.write(updated_content)

    def rebuild_requirements(
        self, root: str = ".", output_path: str | None = None, prune_stale: bool = True
    ) -> dict[str, list[str]] | list[str]:
        service_folders = self._get_active_service_folders(root)

        if not service_folders or output_path is not None:
            return self._rebuild_single(root, output_path, prune_stale)

        results: dict[str, list[str]] = {}

        for service in service_folders:
            service_root = os.path.join(root, service)
            service_output = os.path.join(service_root, "requirements.txt")
            results[service] = self._rebuild_single(service_root, service_output, prune_stale)

        results["__root__"] = self._rebuild_single(
            root,
            os.path.join(root, "requirements.txt"),
            prune_stale,
            exclude_dirs=set(service_folders),
        )
        return results

    def rebuild_notebooks(
        self, root: str = ".", exclude_dirs: set[str] | None = None, prune_stale: bool = True
    ) -> list[str] | None:
        if self.should_merge_notebooks_into_runtime():
            return None

        target_path = self.get_notebooks_target_path(root)
        if not self.has_notebooks(root, exclude_dirs=exclude_dirs) and not os.path.exists(target_path):
            return None

        requirements = self.requirements_for(
            root, target_path, prune_stale=prune_stale, exclude_dirs=exclude_dirs, scope="notebooks"
        )
        self.write_req(requirements, path=target_path)
        return requirements

    def monitor_project(self, module_list: Iterable[str], interval: int = 2) -> None:
        last_req: list[str] | None = None

        while True:
            has_net = self.internet_check()
            requirements: list[str] = []

            for module_name in module_list:
                if self.is_installed(module_name) and not self.is_local_import(module_name):
                    res = self.resolve(module_name, has_net)
                    if res:
                        requirements.append(res)

            if requirements != last_req:
                print("\n[depvex] REQUIREMENTS UPDATED")
                for requirement in requirements:
                    print(" ", requirement)

                self.write_req(requirements)
                last_req = requirements

            time.sleep(interval)
