import json
from types import SimpleNamespace

def read_config(file_path: str) -> SimpleNamespace:
    """
    Reads a JSON configuration file and returns its contents as a SimpleNamespace.

    Args:
        file_path (str): The path to the JSON configuration file.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            config: dict = json.load(file)
            return json.loads(json.dumps(config), object_hook=lambda d: SimpleNamespace(**d))
    except FileNotFoundError:
        print(f"Configuration file not found: {file_path}")
        return {}
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from the configuration file: {e}")
        return {}
    except Exception as e:
        print(f"An unexpected error occurred while reading the configuration file: {e}")
        return {}

project_config = read_config("config.json")