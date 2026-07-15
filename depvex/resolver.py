import importlib.util
from importlib.metadata import packages_distributions
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
from depvex.utils.read_config import project_config
from depvex.utils.read_yaml_config import read_yaml_config


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

        try:
            self.top_level_distributions = packages_distributions()
        except Exception:
            self.top_level_distributions = {}

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

    def write_req(self, lines, path: str = "requirements.txt") -> None:
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
        """מחזיר את תיקיות המיקרו-שירות שמוגדרות ב-depvex.yaml וגם קיימות בפועל תחת root."""
        return [
            folder
            for folder in self.MICRO_SERVICE_FOLDERS
            if os.path.isdir(os.path.join(root, folder))
        ]

    def _walk_python_files(self, root: str, exclude_dirs: set[str] | None = None):
        exclude_dirs = exclude_dirs or set()
        base_skip = {".git", "__pycache__", ".venv", "venv", "node_modules"}
        root_abs = os.path.abspath(root)

        for dirpath, dirnames, filenames in os.walk(root):
            if os.path.abspath(dirpath) == root_abs:
                dirnames[:] = [d for d in dirnames if d not in base_skip and d not in exclude_dirs]
            else:
                dirnames[:] = [d for d in dirnames if d not in base_skip]

            for filename in filenames:
                if filename.endswith(".py") and not filename.startswith("."):
                    yield os.path.join(dirpath, filename)

    def _rebuild_single(
        self,
        root: str,
        output_path: str | None = None,
        prune_stale: bool = True,
        exclude_dirs: set[str] | None = None,
    ) -> list[str]:
        discovered = set()

        for file_path in self._walk_python_files(root, exclude_dirs=exclude_dirs):
            try:
                discovered.update(self._get_imports_for_file(file_path))
            except (OSError, SyntaxError, UnicodeDecodeError):
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
                self._normalize_module_name(self._module_to_package_name(module_name))
                for module_name in discovered
                if self._normalize_module_name(self._module_to_package_name(module_name))
            }

            for module_name in sorted(discovered):
                if not module_name:
                    continue

                normalized_package = self._normalize_module_name(self._module_to_package_name(module_name))
                if not normalized_package:
                    continue

                if normalized_package in existing_by_name and normalized_package in current_normalized_names:
                    requirements.append(existing_by_name[normalized_package])
                else:
                    requirements.append(self.resolve(module_name, has_net))

            self.write_req(requirements, path=output_path)
            return requirements

        for module_name in sorted(discovered):
            if module_name:
                requirements.append(self.resolve(module_name, has_net))

        self.write_req(requirements, path=output_path)
        return requirements

    def rebuild_requirements(
        self, root: str = ".", output_path: str | None = None, prune_stale: bool = True
    ) -> dict[str, list[str]] | list[str]:
        """
        בונה מחדש את requirements.txt לפרויקט ב-root.

        אם micro_servi_folders מוגדר ב-depvex.yaml (מוגדר בזמן יצירת ה-resolver,
        לפי הroot שהועבר לו) וקיימת בפועל לפחות תיקייה אחת מהרשימה תחת root,
        כל תיקייה כזו נחשבת מיקרו-שירות עצמאי ומקבלת requirements.txt משלה.
        כל מה שנשאר מחוץ לתיקיות השירות ממשיך לקובץ הגלובלי (root).
        אם output_path נשלח במפורש - זו קריאה ל"קובץ יחיד" קלאסי, בלי פיצול.
        """
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