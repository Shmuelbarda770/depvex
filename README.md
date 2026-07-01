# depvex

Depvex is a small Python tool that helps manage a project's dependencies based on the imports used in its Python source code.

It performs three main tasks:

1. Scans Python files in the project and detects third-party imports.
2. Creates or updates the requirements.txt file based on the modules found.
3. Watches the project for changes and re-runs the scan automatically.

---

## What it does

This tool is especially useful when you want to generate requirements.txt quickly and accurately without writing it manually.

It works like this:

- Reads all .py files in the project
- Extracts imports that are not part of the Python standard library
- Tries to resolve each module into a requirements entry
  - If the module is installed locally, it uses the locally installed version
  - If it is not installed and the network is available, it checks the latest version on PyPI
  - Otherwise, it writes just the module name without a version

---

## Main features

- Automatic detection of Python imports
- Creation and updating of requirements.txt
- Watch mode support for automatic re-scanning on file changes
- Removes stale requirements entries when an import disappears from the project and is no longer used anywhere
- Ignores directories such as .git, __pycache__, .venv, venv, and node_modules
- Supports version resolution from local installs or PyPI

---

## Project structure

- cli.py – CLI entry point
- parser.py – Extracts imports from Python code
- resolver.py – Resolves modules into requirements entries
- watcher.py – Watches files for changes

---

## System requirements

- Python 3.11
- pip
- Internet access (optional, only needed if you want to query PyPI for versions)

---

## Installation

### 1) Clone the project

```bash
git clone <repository-url>
cd depix
```

### 2) Create a virtual environment (recommended)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

### 3) Install the project

```bash
python3.11 -m pip install --upgrade pip
python3.11 -m pip install -e .
```

This will also install the required dependencies, including:

- watchdog
- requests
- typer

---

## How to run it

### Basic mode: one-time scan

If you want to create or update requirements.txt based on the code currently in the project, run:

```bash
depvex watch .
```

This will start watching your code for changes and update requirements.txt whenever a Python file changes.

### Running against another path

```bash
depvex watch ./my-project
```

---

## Example

Suppose you have a Python file like this:

```python
import requests
import os
from pathlib import Path
```

After running depvex, requirements.txt may be updated to something like:

```txt
requests==2.32.3
```

The exact output depends on the locally installed version or the latest version available on PyPI.

---

## How watch mode works

In this mode:

- The tool listens for changes in files ending in .py
- When it detects a change, it re-scans the project
- It regenerates requirements.txt

This is especially useful during development when you add or remove imports frequently.

---

## requirements.txt format

The tool writes entries in the form:

```txt
package-name==1.2.3
```

or, if no version is found:

```txt
package-name
```

---

## Packaging and publishing to PyPI

The project includes a GitHub Actions workflow that publishes it to PyPI when a tag starting with v is pushed.

To use this, you need:

- a secret named PYPI_TOKEN
- a tag in the following format:

```bash
git tag v0.0.1
git push origin v0.0.1
```

---

## Important notes

- The tool is based on static analysis of imports, so it does not always detect dynamic imports perfectly.
- If a module is not installed and the network is unavailable, it will write only the module name without a version.
- This is a useful tool for generating an initial requirements.txt, but it is not a full replacement for dependency management tools such as poetry or uv in large projects.

---

## Common issues

### No depvex command found

The most common cause is that the project was not installed with pip install -e .

### requirements.txt is not updated

Check whether:

- the file you changed is a .py file
- there is no syntax error in your code
- you are running watch mode from the correct project root

### No version is shown for a module

This usually happens when:

- the module is not installed locally
- there is no internet access
- PyPI did not return version information for the module

---

## Summary

Depvex is a simple and useful tool for generating requirements.txt automatically from Python source code, especially in fast-moving development environments.

If you want to get started quickly, run:

```bash
python3.11 -m pip install -e .
depvex watch .
```
