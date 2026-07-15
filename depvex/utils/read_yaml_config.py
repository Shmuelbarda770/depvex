from pathlib import Path
from types import SimpleNamespace

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - defensive fallback
    yaml = None

YAML_FILENAMES = ("depvex.yaml", "depvex.yml")


def _find_yaml_upwards(start: Path) -> Path | None:
    current = start.resolve()

    while True:
        for filename in YAML_FILENAMES:
            candidate = current / filename
            if candidate.is_file():
                return candidate

        if current.parent == current:  # הגענו לשורש הדיסק
            return None

        current = current.parent


def read_yaml_config(start_dir: str = ".") -> SimpleNamespace:
    if yaml is None:
        print("[depvex] pyyaml is not installed. Run `pip install pyyaml` to use depvex.yaml.")
        return SimpleNamespace()

    target_path = _find_yaml_upwards(Path(start_dir))

    if target_path is None:
        return SimpleNamespace()

    try:
        with open(target_path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as e:
        print(f"[depvex] Error parsing {target_path}: {e}")
        return SimpleNamespace()
    except OSError as e:
        print(f"[depvex] Could not read {target_path}: {e}")
        return SimpleNamespace()

    if not isinstance(data, dict):
        print(f"[depvex] {target_path} must contain a YAML mapping at the top level (key: value pairs).")
        return SimpleNamespace()

    return SimpleNamespace(**data)