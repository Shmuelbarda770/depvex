import importlib.util
import json
import os
import re
import subprocess
import time
import urllib.request
from functools import lru_cache

try:
    import requests  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - defensive fallback
    requests = None

from depvex.parser import ImportExtractor


class DependencyResolver:
    CAPTIVE_PORTAL_URLS = [
        "http://connectivitycheck.gstatic.com/generate_204",
        "http://clients3.google.com/generate_204",
    ]

    def __init__(self, parser: ImportExtractor | None = None) -> None:
        self.parser = parser or ImportExtractor()

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
    def get_local_version(self, module_name: str)-> str | None:
        try:
            result = subprocess.check_output(["pip", "show", module_name], text=True)
            for line in result.splitlines():
                if line.startswith("Version:"):
                    return line.split(":", 1)[1].strip()
        except subprocess.SubprocessError:
            return None
        return None

    @lru_cache(maxsize=256)
    def get_pypi_version(self, module_name: str) -> str | None:
        try:
            url = f"https://pypi.org/pypi/{module_name}/json"
            with urllib.request.urlopen(url, timeout=3) as response:
                data = json.load(response)
            return data["info"]["version"]
        except (OSError, KeyError, ValueError):
            return None

    def resolve(self, module_name: str, has_net: bool) -> str:
        version = self.get_local_version(module_name)

        if version:
            return f"{module_name}=={version}"

        if has_net:
            latest_version = self.get_pypi_version(module_name)
            if latest_version:
                return f"{module_name}=={latest_version}"
            return module_name

        return module_name

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

    def write_req(self, lines, path: str = "requirements.txt") -> None:
        with open(path, "w", encoding="utf-8") as handle:
            for line in sorted(set(lines)):
                handle.write(line + "\n")

    def _get_imports_for_file(self, file_path: str) -> tuple[str, ...]:
        try:
            stat = os.stat(file_path)
            cache_key = (file_path, stat.st_mtime_ns)
            return self._get_imports_for_file_cached(cache_key)
        except (OSError, SyntaxError):
            return ()

    @lru_cache(maxsize=256)
    def _get_imports_for_file_cached(self, cache_key: tuple[str, int]) -> tuple[str, ...]:
        file_path, _ = cache_key
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                return tuple(self.parser.extract_imports(handle.read()))
        except (OSError, SyntaxError):
            return ()

    def rebuild_requirements(
        self, root: str = ".", output_path: str | None = None, prune_stale: bool = True
    ) -> list[str]:
        discovered = set()

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                directory
                for directory in dirnames
                if directory not in {".git", "__pycache__", ".venv", "venv", "node_modules"}
            ]

            for filename in filenames:
                if not filename.endswith(".py"):
                    continue

                file_path = os.path.join(dirpath, filename)
                try:
                    discovered.update(self._get_imports_for_file(file_path))
                except (OSError, SyntaxError):
                    continue

        if output_path is None:
            output_path = os.path.join(root, "requirements.txt")

        requirements = []
        has_net = self.internet_check()

        if prune_stale and os.path.exists(output_path):
            existing_entries = self._read_existing_requirements(output_path)
            existing_by_name = {
                self._normalize_module_name(entry): entry
                for entry in existing_entries
                if self._normalize_module_name(entry)
            }

            current_normalized_names = {
                self._normalize_module_name(module_name)
                for module_name in discovered
                if self._normalize_module_name(module_name)
            }

            for module_name in sorted(discovered):
                if not module_name:
                    continue

                normalized = self._normalize_module_name(module_name)
                if not normalized:
                    continue

                if normalized in existing_by_name and normalized in current_normalized_names:
                    requirements.append(existing_by_name[normalized])
                else:
                    requirements.append(self.resolve(module_name, has_net))

            for existing_name in sorted(existing_by_name):
                if existing_name not in current_normalized_names:
                    continue

            self.write_req(requirements, path=output_path)
            return requirements

        for module_name in sorted(discovered):
            if module_name:
                requirements.append(self.resolve(module_name, has_net))

        self.write_req(requirements, path=output_path)
        return requirements

    def monitor_project(self, module_list, interval: int = 2) -> None:
        last_req = None

        while True:
            has_net = self.internet_check()
            requirements = []

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
