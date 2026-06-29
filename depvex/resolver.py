import importlib.util
import subprocess
import urllib.request
import json
import os
import requests
import time

from depvex.parser import extract_imports


CAPTIVE_PORTAL_URLS = [
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://clients3.google.com/generate_204",
]


# -------------------------
# Internet check
# -------------------------
def internet_check(timeout=3):
    for url in CAPTIVE_PORTAL_URLS:
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 204:
                return True
        except requests.RequestException:
            pass
    return False


# -------------------------
# Local / installed check
# -------------------------
def is_installed(module_name):
    return importlib.util.find_spec(module_name) is not None


# -------------------------
# Get local pip version
# -------------------------
def get_local_version(module_name):
    try:
        result = subprocess.check_output(
            ["pip", "show", module_name],
            text=True
        )
        for line in result.splitlines():
            if line.startswith("Version:"):
                return line.split(":")[1].strip()
    except:
        return None
    return None


# -------------------------
# Get PyPI version
# -------------------------
def get_pypi_version(module_name):
    try:
        url = f"https://pypi.org/pypi/{module_name}/json"
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.load(r)
        return data["info"]["version"]
    except:
        return None


# -------------------------
# Resolve dependency
# -------------------------
def resolve(module_name, has_net):
    version = get_local_version(module_name)

    # installed locally → pin version
    if version:
        return f"{module_name}=={version}"

    # not installed but internet → latest PyPI
    if has_net:
        v = get_pypi_version(module_name)
        if v:
            return f"{module_name}=={v}"
        return module_name

    # offline fallback
    return module_name


# -------------------------
# Write requirements file
# -------------------------
def write_req(lines, path="requirements.txt"):
    with open(path, "w") as f:
        for l in sorted(set(lines)):
            f.write(l + "\n")


# -------------------------
# Rebuild requirements from project imports
# -------------------------
def rebuild_requirements(root=".", output_path=None):
    discovered = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".venv", "venv", "node_modules"}]

        for filename in filenames:
            if not filename.endswith(".py"):
                continue

            file_path = os.path.join(dirpath, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as handle:
                    discovered.update(extract_imports(handle.read()))
            except (OSError, SyntaxError):
                continue

    if output_path is None:
        output_path = os.path.join(root, "requirements.txt")

    req = []
    has_net = internet_check()
    for module_name in sorted(discovered):
        if module_name:
            req.append(resolve(module_name, has_net))

    write_req(req, path=output_path)
    return req


# -------------------------
# Core monitor engine
# -------------------------
def monitor_project(module_list, interval=2):
    last_req = None

    while True:
        has_net = internet_check()

        req = []

        for m in module_list:
            if is_installed(m):
                req.append(resolve(m, has_net))

        if req != last_req:
            print("\n[depvex] REQUIREMENTS UPDATED")
            for r in req:
                print(" ", r)

            write_req(req)
            last_req = req

        time.sleep(interval)