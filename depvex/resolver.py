import importlib.util
import os
import re
import time
import tomllib
from collections.abc import Iterable, Iterator
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from typing import Any

requests: Any | None = None
try:
    import requests as requests_module  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - defensive fallback
    pass
else:
    requests = requests_module

from depvex.parser import ImportExtractor  # ignore depvex
from depvex.utils.read_config import project_config  # ignore depvex
from depvex.utils.read_yaml_config import read_yaml_config  # ignore depvex


class DependencyResolver:
    CAPTIVE_PORTAL_URLS: list[str] = getattr(
        project_config,
        "CAPTIVE_PORTAL_URLS",
        ["http://connectivitycheck.gstatic.com/generate_204"],
    )

    def __init__(self, parser: ImportExtractor | None = None, root: str = ".") -> None:
        self.parser = parser or ImportExtractor()
        self.root = root

        yaml_config = read_yaml_config(start_dir=root)
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

    def _is_ignored_dir(self, rel_path: str) -> bool:
        if not self.IGNORE_DIRS:
            return False
        rel_path = rel_path.replace(os.sep, "/")
        name = os.path.basename(rel_path)
        return name in self.IGNORE_DIRS or rel_path in self.IGNORE_DIRS

    def _is_ignored_package(self, module_name: str) -> bool:
        module = self._normalize_module_name(module_name)
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

    @lru_cache(maxsize=256)
    def get_local_version(self, module_name: str) -> str | None:
        try:
            return distribution(module_name).version
        except PackageNotFoundError:
            return None

    @lru_cache(maxsize=256)
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
        package_name = self._module_to_package_name(module_name)
        version = self.get_local_version(package_name)

        if version:
            return f"{package_name}=={version}"

        if has_net:
            latest_version = self.get_pypi_version(package_name)
            if latest_version:
                return f"{package_name}=={latest_version}"
            return package_name

        return package_name

    def _module_to_package_name(self, module_name: str) -> str:
        normalized = self._normalize_module_name(module_name)
        if not normalized:
            return normalized

        candidate = self.top_level_distributions.get(normalized)
        if candidate:
            return candidate[0]

        return normalized

    def _normalize_module_name(self, module_name: str) -> str:
        name = module_name.strip()
        if not name or name.startswith("#"):
            return ""

        name = re.split(r"\s+#", name, maxsplit=1)[0].strip()
        match = re.match(r"([A-Za-z0-9_.-]+)", name)
        if not match:
            return ""
        return match.group(1).lower()

    def _read_existing_requirements(self, path: str) -> list[str]:
        if not os.path.exists(path):
            return []

        with open(path, "r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip() and not line.strip().startswith("#")]

    def write_req(self, lines: Iterable[str], path: str = "requirements.txt") -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(path, "w", encoding="utf-8") as handle:
            for line in sorted(set(lines)):
                handle.write(line + "\n")

    def _get_imports_for_file(self, file_path: str) -> tuple[str, ...]:
        try:
            stat = os.stat(file_path)
            cache_key = (file_path, stat.st_mtime_ns)
            return self._get_imports_for_file_cached(cache_key)
        except (OSError, SyntaxError, UnicodeDecodeError):
            return ()

    @lru_cache(maxsize=256)
    def _get_imports_for_file_cached(self, cache_key: tuple[str, int]) -> tuple[str, ...]:
        file_path, _ = cache_key
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                return tuple(self.parser.extract_imports(handle.read()))
        except (OSError, SyntaxError, UnicodeDecodeError):
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
                    requirements.append(self.resolve(module_name, has_net))

            return requirements

        for module_name in sorted(discovered):
            if module_name:
                requirements.append(self.resolve(module_name, has_net))

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
                if self.is_installed(module_name):
                    requirements.append(self.resolve(module_name, has_net))

            if requirements != last_req:
                print("\n[depvex] REQUIREMENTS UPDATED")
                for requirement in requirements:
                    print(" ", requirement)

                self.write_req(requirements)
                last_req = requirements

            time.sleep(interval)
