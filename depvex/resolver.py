import importlib.util
import os
import re
import sys
import time
import tomllib
from collections.abc import Iterable, Iterator
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path
from typing import Any
import requests
from packaging.specifiers import SpecifierSet
from packaging.version import Version

IMPORT_MAPPING_FILE = Path(__file__).resolve().parent / "import_mapping_filtered.txt"
POPULARITY_MAPPING_FILE = Path(__file__).resolve().parent / "duplicate_imports_pop.txt"

from depvex.parser import ImportExtractor 
from depvex.utils.read_config import project_config
from depvex.utils.read_yaml_config import read_yaml_config


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

    def __init__(self, parser: ImportExtractor | None = None, root: str = ".") -> None:
        self.parser = parser or ImportExtractor()
        self.root = os.path.abspath(root)
        self.python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

        yaml_config = read_yaml_config(start_dir=self.root)
        self.MICRO_SERVICE_FOLDERS: list[str] = getattr(yaml_config, "micro_servi_folders", [])
        self.IGNORE_DIRS: set[str] = set(getattr(yaml_config, "ignore_dirs", []))
        self.IGNORE_PACKAGES: set[str] = {
            self._normalize_module_name(package)
            for package in getattr(yaml_config, "ignore_packages", [])
            if isinstance(package, str) and self._normalize_module_name(package)
        }

        try:
            self.top_level_distributions = packages_distributions()
        except Exception:
            self.top_level_distributions = {}

        self.IMPORT_MAPPING = self._load_import_mapping_file()
        self.POPULARITY_MAPPING = self._load_popularity_mapping_file()
        
        self.local_modules = self._index_local_modules(self.root)

    def _index_local_modules(self, root_dir: str) -> set[str]:

        local_mods = set()
        root_path = Path(root_dir).resolve()

        skip_dirs = {".git", "__pycache__", ".venv", "venv", "env", "node_modules", "build", "dist", "site-packages"}
        skip_dirs.update(self.IGNORE_DIRS)

        for path in root_path.rglob("*.py"):
            try:
                rel_path = path.relative_to(root_path)
            except ValueError:
                continue

            if any(part in skip_dirs or part.startswith(".") for part in rel_path.parts):
                continue

            parts = list(rel_path.with_suffix("").parts)
            if not parts:
                continue

            if parts[-1] == "__init__":
                parts.pop()

            if parts:
                local_mods.add(".".join(parts).lower())
                
                for i in range(1, len(parts) + 1):
                    local_mods.add(".".join(parts[:i]).lower())

        for entry in root_path.iterdir():
            if entry.name not in skip_dirs and not entry.name.startswith("."):
                local_mods.add(entry.name.lower())
                if entry.is_file() and entry.suffix == ".py":
                    local_mods.add(entry.stem.lower())

        print(f"[depvex][debug] indexed {len(local_mods)} local module paths/roots in project")
        return local_mods

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
            "os", "sys", "re", "time", "pathlib", "typing", "collections",
            "functools", "importlib", "json", "ast", "unittest", "tempfile",
            "math", "random", "subprocess", "shutil", "logging", "threading"
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
        return module in self.IGNORE_PACKAGES or package in self.IGNORE_PACKAGES

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
        return importlib.util.find_spec(module_name) is not None

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
            print(f"[depvex][debug] resolve skipped empty module for {module_name!r}")
            return ""

        if self.is_local_import(module_name) or self.is_stdlib_module(module_name):
            print(f"[depvex][debug] skipping local/stdlib module={normalized_module}")
            return ""

        print(f"[depvex][debug] resolving module={normalized_module} has_net={has_net}")
        mapped = self._mapped_package_name(normalized_module)
        if mapped:
            print(f"[depvex][debug] module={normalized_module} mapped to package={mapped}")
            package_name = mapped
        elif self.is_installed(normalized_module):
            print(f"[depvex][debug] module={normalized_module} is installed; using installed module name")
            package_name = self._module_to_package_name(normalized_module, use_mapping=False)
        else:
            package_name = self._module_to_package_name(normalized_module, use_mapping=True)
            print(
                f"[depvex][debug] module={normalized_module} has no mapping and is not installed; using fallback package={package_name}"
            )

        version = self.get_local_version(package_name)
        if version:
            print(f"[depvex][debug] package={package_name} has local version={version}")
            return f"{package_name}=={version}"

        if has_net:
            latest_version = self.get_pypi_version(package_name)
            if latest_version:
                print(f"[depvex][debug] package={package_name} resolved latest version={latest_version}")
                return f"{package_name}=={latest_version}"
            print(f"[depvex][debug] package={package_name} has no network version; using package name")
            return package_name

        print(f"[depvex][debug] package={package_name} returned without version info")
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
        print(f"[depvex][debug] import mapping path={mapping_path}")
        if not mapping_path.is_file():
            print(f"[depvex][debug] import mapping file does not exist at {mapping_path}")
            return mapping

        print(f"[depvex][debug] import mapping file exists at {mapping_path}")
        try:
            with mapping_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    parts = line.split(":", 1)
                    if len(parts) != 2:
                        print(f"[depvex][debug] skipping malformed mapping line {line_number}: {line!r}")
                        continue

                    module_name = self._normalize_module_name(parts[0])
                    package_name = self._normalize_module_name(parts[1])
                    if module_name and package_name:
                        mapping.setdefault(module_name, []).append(package_name)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[depvex][debug] failed to read import mapping file: {exc}")
            return {}

        print(f"[depvex][debug] loaded {len(mapping)} mapping entries from {mapping_path}")
        return mapping

    def _load_popularity_mapping_file(self) -> dict[str, str]:
        popularity_map: dict[str, str] = {}
        pop_path = _resolve_popularity_mapping_path()
        if not pop_path.is_file():
            print(f"[depvex][debug] popularity file does not exist at {pop_path}")
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
            print(f"[depvex][debug] failed to read popularity mapping file: {exc}")
            return {}

        print(f"[depvex][debug] loaded {len(popularity_map)} popular choices from {pop_path}")
        return popularity_map

    def _mapped_package_name(self, module_name: str) -> str | None:
        normalized = self._normalize_module_name(module_name)
        if not normalized:
            print(f"[depvex][debug] cannot map empty module name")
            return None

        candidates = self.IMPORT_MAPPING.get(normalized)
        if not candidates:
            print(f"[depvex][debug] no mapping found for module={normalized}")
            return None

        if len(candidates) == 1:
            result = candidates[0]
            print(f"[depvex][debug] mapping for module={normalized} -> {result}")
            return result

        popular_choice = self.POPULARITY_MAPPING.get(normalized)
        if popular_choice and popular_choice in candidates:
            print(f"[depvex][debug] popularity map for module={normalized} -> {popular_choice}")
            return popular_choice

        for candidate in candidates:
            if candidate == normalized:
                print(f"[depvex][debug] mapping for module={normalized} -> {candidate}")
                return candidate

        result = candidates[0]
        print(f"[depvex][debug] mapping for module={normalized} -> {result} (first candidate)")
        return result

    def _read_existing_requirements(self, path: str) -> list[str]:
        if not os.path.exists(path):
            print(f"[depvex][debug] requirements file does not exist at {path}")
            return []

        try:
            with open(path, "r", encoding="utf-8") as handle:
                return [line.strip() for line in handle if line.strip() and not line.strip().startswith("#")]
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[depvex][debug] failed to read requirements file {path}: {exc}")
            return []

    def write_req(self, lines: Iterable[str], path: str = "requirements.txt") -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        valid_lines = [line for line in lines if line]
        try:
            with open(path, "w", encoding="utf-8") as handle:
                for line in sorted(set(valid_lines)):
                    handle.write(line + "\n")
        except OSError as exc:
            print(f"[depvex][debug] failed to write requirements file {path}: {exc}")

    def _get_imports_for_file(self, file_path: str) -> tuple[str, ...]:
        try:
            stat = os.stat(file_path)
            cache_key = (file_path, stat.st_mtime_ns)
            return self._get_imports_for_file_cached(cache_key)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            print(f"[depvex][debug] failed to inspect imports for {file_path}: {exc}")
            return ()

    @lru_cache(maxsize=512)
    def _get_imports_for_file_cached(self, cache_key: tuple[str, int]) -> tuple[str, ...]:
        file_path, _ = cache_key
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                return tuple(self.parser.extract_imports(handle.read()))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            print(f"[depvex][debug] failed to parse imports from {file_path}: {exc}")
            return ()

    def _get_active_service_folders(self, root: str) -> list[str]:
        return [folder for folder in self.MICRO_SERVICE_FOLDERS if os.path.isdir(os.path.join(root, folder))]

    def discover_imports(self, root: str, exclude_dirs: set[str] | None = None) -> set[str]:
        discovered: set[str] = set()
        for file_path in self._walk_python_files(root, exclude_dirs=exclude_dirs):
            discovered.update(self._get_imports_for_file(file_path))
        return {module_name for module_name in discovered if not self._is_ignored_package(module_name)}

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

    def requirements_for(
        self,
        root: str,
        output_path: str | None = None,
        prune_stale: bool = True,
        exclude_dirs: set[str] | None = None,
    ) -> list[str]:
        discovered = self.discover_imports(root, exclude_dirs=exclude_dirs)

        if output_path is None:
            output_path = os.path.join(root, "requirements.txt")

        requirements: list[str] = []
        has_net = self.internet_check()

        if prune_stale and os.path.exists(output_path):
            existing_entries = self._read_existing_requirements(output_path)
            existing_by_name = {
                self._normalize_module_name(entry): entry
                for entry in existing_entries
                if self._normalize_module_name(entry)
            }

            for module_name in sorted(discovered):
                if not module_name:
                    continue

                normalized_package = self._normalize_module_name(self._module_to_package_name(module_name))
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
        try:
            with open(path, "rb") as handle:
                project = tomllib.load(handle).get("project", {})
        except (OSError, tomllib.TOMLDecodeError):
            return []

        dependencies = project.get("dependencies", [])
        return (
            [dependency for dependency in dependencies if isinstance(dependency, str)]
            if isinstance(dependencies, list)
            else []
        )

    def write_pyproject_dependencies(self, path: str, dependencies: Iterable[str]) -> None:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()

        project_match = re.search(r"(?ms)^\[project\]\n(?P<body>.*?)(?=^\[|\Z)", content)
        if project_match is None:
            raise ValueError(f"{path} does not contain a [project] table")

        dependency_lines = "\n".join(f'    "{dependency}",' for dependency in sorted(set(dependencies)))
        replacement = f"dependencies = [\n{dependency_lines}\n]"
        project_body = project_match.group("body")
        updated_body, replacements = re.subn(r"(?ms)^dependencies\s*=\s*\[.*?\]", replacement, project_body, count=1)
        if replacements == 0:
            updated_body = f"{project_body.rstrip()}\n\n{replacement}\n"

        updated_content = f"{content[:project_match.start('body')]}{updated_body}{content[project_match.end('body'):]}"
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