"""Readers for popular Python lock file formats.

Supported:
  - uv.lock     (TOML - Astral uv)
  - poetry.lock (TOML - Poetry)
  - Pipfile.lock (JSON - Pipenv)
  - pdm.lock    (TOML - PDM)
"""
import json
import os
import re
import tomllib
from collections.abc import Callable


class LockFileReader:
    """Read pinned package versions from supported Python lock files."""

    _NORMALIZE_RE = re.compile(r"[-_.]+")

    _LOCK_FILES: tuple[tuple[str, Callable[[str], dict[str, str]]], ...] = (
        ("uv.lock", "_read_toml_packages"),
        ("poetry.lock", "_read_toml_packages"),
        ("Pipfile.lock", "_read_pipfile_lock"),
        ("pdm.lock", "_read_toml_packages"),
    )

    def __init__(self, root: str):
        self.root = root

    def read(self) -> tuple[dict[str, str], str | None]:
        """Read versions from the first supported lock file found.

        Priority:
            uv.lock > poetry.lock > Pipfile.lock > pdm.lock

        Returns:
            A tuple of:
                - package versions mapped by normalized package name
                - detected lock file name, or None
        """
        for filename, reader_name in self._LOCK_FILES:
            path = os.path.join(self.root, filename)

            if not os.path.isfile(path):
                continue

            reader = getattr(self, reader_name)
            versions = reader(path)

            if versions:
                return versions, filename

        return {}, None

    @classmethod
    def _normalize_package_name(cls, name: str) -> str:
        """Normalize a package name according to Python package naming rules."""
        return cls._NORMALIZE_RE.sub("-", name).lower()

    @classmethod
    def _read_toml_packages(cls, path: str) -> dict[str, str]:
        """Read [[package]] entries from a TOML lock file."""
        try:
            with open(path, "rb") as file:
                data = tomllib.load(file)

            return {
                cls._normalize_package_name(package["name"]): str(package["version"])
                for package in data.get("package", [])
                if package.get("name") and package.get("version")
            }

        except (OSError, tomllib.TOMLDecodeError, TypeError, KeyError):
            return {}

    @classmethod
    def _read_pipfile_lock(cls, path: str) -> dict[str, str]:
        """Read package versions from Pipfile.lock."""
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)

            result: dict[str, str] = {}

            for section in ("default", "develop"):
                packages = data.get(section, {})

                for name, info in packages.items():
                    if name.startswith("_"):
                        continue

                    version = info.get("version", "").lstrip("=")

                    if version:
                        result[cls._normalize_package_name(name)] = version

            return result

        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            return {}