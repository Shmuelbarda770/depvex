"""Writers and auto-detection for popular Python package manager formats.

Supported output formats:
  - requirements  →  requirements.txt     (pip / uv)
  - pyproject     →  pyproject.toml       (Poetry / PDM / uv / PEP 517)
  - pipfile       →  Pipfile              (Pipenv)
  - conda         →  environment.yml      (conda / mamba)
"""

import os
import re
from collections.abc import Iterable

_RE_PKG_SPLIT = re.compile(r"[><=!~\s;]")
_RE_PIN_VERSION = re.compile(r"==([^\s,;]+)")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _pkg_name(entry: str) -> str:
    """'flask==3.1.0' → 'flask'"""
    return _RE_PKG_SPLIT.split(entry.strip(), maxsplit=1)[0]


def _pkg_version(entry: str) -> str | None:
    """'flask==3.1.0' → '3.1.0', 'flask' → None"""
    m = _RE_PIN_VERSION.search(entry)
    return m.group(1) if m else None


def _ensure_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def _unique_entries(lines: Iterable[str]) -> dict[str, str | None]:
    """Return {package_name: version_or_None}, deduplicating by name."""
    result: dict[str, str | None] = {}
    for entry in lines:
        name = _pkg_name(entry)
        if name:
            result[name] = _pkg_version(entry)
    return result


# ── requirements.txt ──────────────────────────────────────────────────────────


def write_requirements_txt(lines: Iterable[str], path: str) -> None:
    """Write standard pip / uv requirements.txt."""
    _ensure_dir(path)
    sorted_lines = sorted(set(lines))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted_lines) + "\n" if sorted_lines else "")


def read_requirements_txt(path: str) -> list[str]:
    """Read non-comment, non-empty lines from requirements.txt."""
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]


# ── pyproject.toml [project.dependencies] ────────────────────────────────────


def write_pyproject_deps(lines: Iterable[str], path: str) -> None:
    """Update ``[project.dependencies]`` in an existing pyproject.toml or create one.

    Version pins use ``>=`` (safe minimum) rather than ``==``, which is
    the correct style for a project's direct-dependency declarations.
    """
    unique = _unique_entries(lines)
    entries = sorted(
        f"{name}>={version}" if version else name
        for name, version in unique.items()
    )
    deps_text = "dependencies = [\n" + "".join(f'  "{e}",\n' for e in entries) + "]\n"

    if not os.path.isfile(path):
        _ensure_dir(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write('[project]\nname = "my-project"\nversion = "0.1.0"\n\n')
            f.write(deps_text)
        return

    content = open(path, "r", encoding="utf-8").read()

    # Replace existing block if present
    updated = re.sub(
        r"(?m)^dependencies\s*=\s*\[.*?\]",
        deps_text.rstrip("\n"),
        content,
        flags=re.DOTALL,
    )
    if updated == content:
        # Insert after [project] header, before the next section
        updated = re.sub(
            r"(\[project\][^\[]*?)(\n\[)",
            lambda m: m.group(1) + deps_text + m.group(2),
            content,
            count=1,
            flags=re.DOTALL,
        )
    if updated == content:
        # No following section — append at end
        updated = content.rstrip("\n") + "\n\n" + deps_text

    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)


def read_pyproject_deps(path: str) -> list[str]:
    """Read declared dependencies from ``[project.dependencies]`` in pyproject.toml."""
    if not os.path.isfile(path):
        return []
    content = open(path, "r", encoding="utf-8").read()
    m = re.search(r"(?m)^dependencies\s*=\s*\[(.*?)\]", content, re.DOTALL)
    if not m:
        return []
    raw = m.group(1)
    return [
        item.strip().strip('"').strip("'")
        for item in raw.split(",")
        if item.strip().strip('"').strip("'")
    ]


# ── Pipfile [packages] ────────────────────────────────────────────────────────


def write_pipfile(lines: Iterable[str], path: str) -> None:
    """Write or update the ``[packages]`` section of a Pipfile."""
    unique = _unique_entries(lines)
    pkg_lines = "".join(
        f'{name} = "{version}"\n' if version else f'{name} = "*"\n'
        for name, version in sorted(unique.items())
    )
    packages_block = f"[packages]\n{pkg_lines}"

    if not os.path.isfile(path):
        _ensure_dir(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write('[[source]]\nurl = "https://pypi.org/simple"\nverify_ssl = true\nname = "pypi"\n\n')
            f.write(packages_block + "\n\n[dev-packages]\n")
        return

    content = open(path, "r", encoding="utf-8").read()
    updated = re.sub(
        r"\[packages\][^\[]*",
        packages_block + "\n\n",
        content,
        flags=re.DOTALL,
    )
    if updated == content:
        updated = content.rstrip("\n") + "\n\n" + packages_block

    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)


def read_pipfile_deps(path: str) -> list[str]:
    """Read ``[packages]`` entries from a Pipfile as requirement strings."""
    if not os.path.isfile(path):
        return []
    content = open(path, "r", encoding="utf-8").read()
    m = re.search(r"\[packages\](.*?)(?:\[|\Z)", content, re.DOTALL)
    if not m:
        return []
    result: list[str] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # name = "3.1.0"  or  name = "*"
        parts = line.split("=", 1)
        name = parts[0].strip()
        if len(parts) == 2:
            version = parts[1].strip().strip('"').strip("'")
            result.append(f"{name}=={version}" if version != "*" else name)
        else:
            result.append(name)
    return result


# ── conda environment.yml ─────────────────────────────────────────────────────


def write_conda_env(lines: Iterable[str], path: str, env_name: str | None = None) -> None:
    """Write or overwrite a conda ``environment.yml`` with pip dependencies."""
    if env_name is None:
        env_name = os.path.basename(os.path.dirname(os.path.abspath(path))) or "myenv"
    sorted_lines = sorted(set(lines))
    _ensure_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"name: {env_name}\n"
            "channels:\n"
            "  - defaults\n"
            "  - conda-forge\n"
            "dependencies:\n"
            "  - python>=3.11\n"
            "  - pip\n"
            "  - pip:\n"
        )
        for line in sorted_lines:
            f.write(f"    - {line}\n")


def read_conda_env_deps(path: str) -> list[str]:
    """Read pip dependencies from a conda ``environment.yml``."""
    if not os.path.isfile(path):
        return []
    content = open(path, "r", encoding="utf-8").read()
    m = re.search(r"- pip:\n((?:    - .+\n?)*)", content)
    if not m:
        return []
    result: list[str] = []
    for line in m.group(1).splitlines():
        dep = line.strip().lstrip("- ").strip()
        if dep:
            result.append(dep)
    return result


# ── Auto-detection ────────────────────────────────────────────────────────────


def detect_output_format(root: str) -> str:
    """Detect which package manager the project is using.

    Returns one of: ``"requirements"``, ``"pyproject"``, ``"pipfile"``, ``"conda"``.

    Detection order (most specific first):
      1. Pipfile / Pipfile.lock → pipfile
      2. environment.yml        → conda
      3. pyproject.toml with [tool.poetry] or [project] → pyproject
      4. fallback               → requirements
    """
    if os.path.isfile(os.path.join(root, "Pipfile")) or os.path.isfile(os.path.join(root, "Pipfile.lock")):
        return "pipfile"
    if os.path.isfile(os.path.join(root, "environment.yml")):
        return "conda"
    if os.path.isfile(os.path.join(root, "pyproject.toml")):
        try:
            content = open(os.path.join(root, "pyproject.toml"), "r", encoding="utf-8").read()
            if "[tool.poetry]" in content or "[project]" in content:
                return "pyproject"
        except OSError:
            pass
    return "requirements"


# ── Dispatch tables ───────────────────────────────────────────────────────────


_WRITERS = {
    "requirements": write_requirements_txt,
    "pyproject": write_pyproject_deps,
    "pipfile": write_pipfile,
    "conda": write_conda_env,
}

_READERS = {
    "requirements": read_requirements_txt,
    "pyproject": read_pyproject_deps,
    "pipfile": read_pipfile_deps,
    "conda": read_conda_env_deps,
}

OUTPUT_FILENAMES = {
    "requirements": "requirements.txt",
    "pyproject": "pyproject.toml",
    "pipfile": "Pipfile",
    "conda": "environment.yml",
}

VALID_FORMATS = frozenset(_WRITERS)


def write_deps(lines: Iterable[str], path: str, fmt: str = "requirements") -> None:
    """Write *lines* to *path* using the writer for *fmt*."""
    _WRITERS.get(fmt, write_requirements_txt)(list(lines), path)


def read_deps(path: str, fmt: str = "requirements") -> list[str]:
    """Read existing dependencies from *path* using the reader for *fmt*."""
    return _READERS.get(fmt, read_requirements_txt)(path)


def output_filename(fmt: str) -> str:
    """Return the canonical output filename for *fmt*."""
    return OUTPUT_FILENAMES.get(fmt, "requirements.txt")
