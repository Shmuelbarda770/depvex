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

## System requirements

- Python 3.11+
- pip
- Internet access (optional, only needed if you want to query PyPI for versions)

---

## Installation

It is recommended to install `depvex` globally or within your project's virtual environment using `pip`:

```bash
pip install depvex

```

---

## Detailed Usage Guide

Once installed, `depvex` becomes available as a command-line utility in your terminal. You should navigate to your Python project's root directory and run one of the following commands:

### 1. The `scan` command: One-time Generation

Use this command when you want to manually create or update your `requirements.txt` right now.

**Command:**

```bash
depvex --scan .
```

*(You can replace `.` with any specific folder path, e.g., `depvex --scan ./src`)*

**What it does in detail:**

* Recursively searches the target folder for all `.py` files.
* Skips known non-project folders (`.venv`, `node_modules`, `.git`, etc.).
* Reads the code and identifies all `import X` and `from Y import Z` statements.
* Filters out Python's built-in libraries (like `os`, `sys`, `math`).
* Determines the correct version for the remaining third-party libraries by checking your local environment or querying PyPI.
* Writes the final list into `requirements.txt` in that directory, removing any old packages you no longer use.

### 2. The `watch` command: Continuous Auto-Updating

Use this command during active development so you never have to think about dependencies again.

**Command:**

```bash
depvex --watch .
```

**What it does in detail:**

* Starts a long-running background process in your terminal.
* Monitors your project files in real-time.
* The moment you save, create, or delete a `.py` file, `depvex` instantly detects the change.
* It silently re-runs the scanning process and updates `requirements.txt` on the fly.
**Best Workflow:** Open a dedicated terminal tab, activate your virtual environment, run `depvex --watch .`, and leave it running in the background while you write code in your IDE. To stop it, simply press `Ctrl+C`.

### 3. The `check` command: Verification (Great for CI/CD)

Use this command to verify that `requirements.txt` is up-to-date without actually modifying it.

**Command:**

```bash
depvex --check .
```

**What it does in detail:**

* Performs the exact same import analysis as the `scan` command.
* Compares the results with the current contents of `requirements.txt`.
* **Does not modify any files.**
* If the requirements match the code, it exits with a success code (`0`). If there are missing or outdated entries, it alerts you and exits with an error code.
**Best Workflow:** Add this to your GitHub Actions, GitLab CI, or pre-commit hooks to ensure developers don't accidentally push code without updating the dependencies.

---

## Example

Suppose you have a Python file like this:

```python
import requests
import os
from pathlib import Path

```

After running `depvex --scan .`, `requirements.txt` may be updated to something like:

```txt
requests==2.32.3

```

*(Notice that `os` and `pathlib` are ignored because they are standard Python libraries)*

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

## Important notes

* The tool is based on static analysis of imports, so it does not always detect dynamic imports perfectly.
* If a module is not installed and the network is unavailable, it will write only the module name without a version.
* This is a useful tool for generating an initial requirements.txt, but it is not a full replacement for strict dependency lock tools such as `poetry` or `uv` in highly complex projects.

---

## Common issues

### No depvex command found

Ensure that your Python environment's `bin` or `Scripts` directory is in your system's PATH. If you installed it in a virtual environment, make sure the environment is activated (`source .venv/bin/activate` on Mac/Linux, or `.venv\Scripts\activate` on Windows).

### requirements.txt is not updated

Check whether:

* the file you changed is a `.py` file.
* there is no syntax error in your code preventing the parser from reading it.
* you are running watch mode from the correct project root.

### No version is shown for a module

This usually happens when:

* the module is not installed locally.
* there is no internet access.
* PyPI did not return version information for the module (sometimes the import name differs from the PyPI package name).

---

## Summary

Depvex is a simple and useful tool for generating requirements.txt automatically from Python source code, especially in fast-moving development environments.

If you want to get started quickly, open your terminal and run:

```bash
pip install depvex
depvex --watch .
```