import json
from pathlib import Path
from types import SimpleNamespace

CONFIG_FILENAMES = ("config.json", "depvex.json")


def _find_config_upwards(start: Path) -> Path | None:

    current = start.resolve()

    while True:
        for filename in CONFIG_FILENAMES:
            candidate = current / filename
            if candidate.is_file():
                return candidate

        if current.parent == current:  # הגענו לשורש (/ או C:\)
            return None

        current = current.parent


def read_config(file_path: str | None = None, start_dir: str | None = None) -> SimpleNamespace:
    """
    קורא קובץ קונפיג JSON ומחזיר אותו כ-SimpleNamespace.

    אם file_path נשלח במפורש - נקרא בדיוק אותו (התנהגות ישנה, שימושי לטסטים).
    אחרת - מחפש config.json / depvex.json החל מ-start_dir (ברירת מחדל: cwd)
    ועולה למעלה בעץ התיקיות עד שמוצא או מגיע לשורש.

    Args:
        file_path (str | None): נתיב מפורש לקובץ קונפיג. אם None - מחפש אוטומטית.
        start_dir (str | None): תיקיית התחלה לחיפוש. ברירת מחדל: תיקיית העבודה הנוכחית.
    """
    if file_path is not None:
        target_path = Path(file_path)
    else:
        target_path = _find_config_upwards(Path(start_dir or "."))

    if target_path is None:
        print("[depvex] No config.json found in this directory or any parent directory. Using defaults.")
        return SimpleNamespace()

    try:
        with open(target_path, "r", encoding="utf-8") as file:
            config: dict = json.load(file)
            return json.loads(json.dumps(config), object_hook=lambda d: SimpleNamespace(**d))
    except FileNotFoundError:
        print(f"Configuration file not found: {target_path}")
        return SimpleNamespace()
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from the configuration file: {e}")
        return SimpleNamespace()
    except Exception as e:
        print(f"An unexpected error occurred while reading the configuration file: {e}")
        return SimpleNamespace()


project_config = read_config()
