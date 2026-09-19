# depvex

`depvex` discovers third-party Python imports and maintains `requirements.txt`
files from the source code that uses them. It is intended to make dependency
files easier to review and keep current in both a single application and a
repository containing several services.

## Goals

- Generate a clear dependency list from static Python imports.
- Keep existing version pins when a dependency is still used.
- Detect stale dependency files before merging a change.
- Support one root application and independently deployable services in the
  same repository.
- Provide a watch mode for local development without requiring a shell command
  for every edit.

`depvex` is a dependency-file helper, not a lockfile or environment manager.
For reproducible resolution across platforms, use it together with a tool such
as pip-tools, Poetry, or uv.

## What it does

- Parses `import` and `from ... import ...` statements with Python's AST.
- Detects literal `importlib.import_module(...)` and `__import__(...)` calls.
  Computed module targets, plugin loaders, and entry-point loaders produce an
  actionable warning instead of an unsafe guess.
- Ignores modules from the Python standard library.
- Skips imports annotated with `# ignore depvex`.
- Skips imports inside `if TYPE_CHECKING:` and `if typing.TYPE_CHECKING:` blocks so type-only imports do not pollute production requirements.
- Recursively scans `.py` and `.ipynb` files, excluding `.git`, `__pycache__`, `.venv`,
  `venv`, `node_modules`, and `.ipynb_checkpoints`.
- Automatically filters IPython magic commands (`%`, `%%`), shell commands (`!`), and help queries (`?`) when parsing Jupyter notebooks.
- Generates `requirements-notebooks.txt` by default to isolate notebook dependencies, or merges them into `requirements.txt` when configured in `depvex.yaml`.
- Supports additional ignored directories through `depvex.yaml`.
- Supports `ignore_packages` in YAML, matching either an import module or its
  resolved distribution name.
- Maps installed import modules to their distribution names when metadata is
  available (for example, `yaml` to `PyYAML`).
- Uses `import_mapping_filtered.txt` as a fallback when an import is not
  installed locally, mapping it to a known distribution before optionally
  querying PyPI.
- Resolves a version from installed package metadata without launching `pip` or
  another subprocess. If it is unavailable locally and internet access is
  detected, it queries PyPI; otherwise it writes the package name without a
  version.
- Preserves an existing requirements entry when the same package is still
  imported, and removes entries for imports that disappeared.
- Uses the same dependency calculation for `scan` and `check`.
- Explains failed checks with missing, stale, and changed requirement entries.
- Can explicitly sync and verify `[project].dependencies` in `pyproject.toml`.
- Reports dependencies by service and identifies dependencies shared by groups.
- Watches created and modified Python files and debounces repeated events.
- Uses colour only when the terminal supports it; set `NO_COLOR=1` to disable
  colour explicitly.

## Installation

Requires Python 3.11 or newer.

```bash
python -m pip install -e .
```

YAML configuration is optional. Install PyYAML when using `depvex.yaml` or
`depvex.yml`:

```bash
python -m pip install PyYAML
```

For a published release, install the package from PyPI instead of editable
mode.

## Commands

The new CLI interface uses option-style commands:

```bash
depvex --scan .
depvex --check .
depvex --watch .
depvex --report .
depvex --diff .
```

Multiple commands can be selected in one invocation. They run in the order
listed by default, and the process returns the highest exit code encountered:

```bash
depvex --scan --check --report .
```

Use `--parallel` to run one command concurrently across independent project
paths, and `--jobs` to limit the number of workers. Use
`--continue-on-error` to keep sequential execution going after a failed
command. `--watch` is intentionally exclusive because it is a long-running
command:

```bash
depvex --parallel --jobs 3 --scan ./service-a ./service-b
depvex --scan --check --continue-on-error .
depvex --scan --ignore-dir test_project .
```

Do not combine `--parallel` with multiple commands on the same path; commands
such as `scan` and `check` depend on one another and must run sequentially.
Use `--ignore-dir DIR` more than once to add CLI-only exclusions; these are
combined with `ignore_dirs` from `depvex.yaml`.

| Command | Purpose | Result |
| --- | --- | --- |
| `depvex --scan [path]` | Scan once. | Creates or updates requirements files. |
| `depvex --check [path]` | Verify scanned dependencies. | Returns `0` when current and `1` when a requirements file is missing or differs. |
| `depvex --watch [path]` | Scan once, then watch the path. | Updates affected requirements files after the debounce delay. |
| `depvex --report [path]` | Inspect dependency ownership. | Lists root/service dependencies and shared packages without writing files. |
| `depvex --diff [path]` | Preview changes. | Prints colour-coded missing, stale, and changed dependencies without writing files; exits `1` when changes exist. |

`-v`, `-V`, and `--version` continue to print the installed version.

Add `--pyproject` to `--scan` or `--check` to sync or verify the selected
directory's `[project].dependencies` list:

```bash
depvex --scan --pyproject .
depvex --check --pyproject .
```

`path` defaults to the current directory.

The option-style interface is intentionally incompatible with older published
releases that use positional commands such as `depvex check .`. CI must install
and execute one chosen version consistently.

## Single-application repository

With no configured service folders, `depvex` scans the selected directory and
writes one file:

```text
my-app/
├── app.py
└── requirements.txt
```

Run:

```bash
depvex --scan my-app
```

## Microservices repository

Service support is enabled through `depvex.yaml` (or `depvex.yml`). The current
configuration key is intentionally documented exactly as implemented:
`micro_servi_folders`.

```yaml
micro_servi_folders:
  - api
  - worker

ignore_dirs:
  - tests
  - generated

ignore_packages:
  - pytest
  - depvex

# Dependencies loaded dynamically from configuration or plugin names.
# These are import-module names, not pip requirement strings.
dynamic_imports:
  - celery
```

Given this structure:

```text
platform/
├── api/
│   ├── main.py
│   └── requirements.txt
├── worker/
│   ├── jobs.py
│   └── requirements.txt
├── shared_script.py
└── requirements.txt
```

`depvex --scan platform` scans `api` and `worker` separately, writes a
`requirements.txt` inside each service, and writes a root
`platform/requirements.txt` for Python files outside the named service
folders. The root scan excludes those direct child service folders, avoiding
duplication of service-only dependencies.

In watch mode, a change under a named service rescans only that service; a
change outside them rescans the root. Service folders must currently be direct
children of the scanned root.

### Configuration sources

- `depvex.yaml` / `depvex.yml`: `ignore_dirs` and
  `micro_servi_folders`, plus `ignore_packages` and `dynamic_imports`.
- `config.json` / `depvex.json`: `CAPTIVE_PORTAL_URLS` for the connectivity
  check and `debounce_seconds` for watch mode.

The JSON `micro_servi_folders` value is not currently read for service
discovery; use YAML for that setting.

### Dynamic and development dependencies

Depvex includes a dynamic import when its target is a string literal, such as
`importlib.import_module("celery")`. If a target is computed at runtime (for
example from a plugin or configuration value), it prints a warning with the
file and line number. Add the module explicitly under `dynamic_imports` in
`depvex.yaml` to include it reliably.

Imports found only in conventional test paths (`tests/`, `test/`, `test_*.py`,
`*_test.py`, and `conftest.py`) are excluded from `requirements.txt`. When
running `--scan --pyproject`, they are synchronized to
`[project.optional-dependencies].dev`. A dependency used in both application
and test code remains a normal runtime dependency.

### Type-checking imports

Imports enclosed in `if TYPE_CHECKING:` or `if typing.TYPE_CHECKING:` blocks
are skipped automatically. This prevents type-hint-only dependencies (or
types imported solely to prevent circular dependencies) from inflating
production `requirements.txt` files or triggering unexpected check failures.

### Jupyter Notebooks (.ipynb)

`depvex` parses Jupyter Notebooks (`.ipynb`) and extracts third-party imports while ignoring IPython magic commands (`%`, `%%`), shell commands (`!`), and interactive help queries (`?`).

- **Default behavior**: When notebooks are detected, their dependencies are written to a dedicated `requirements-notebooks.txt` file at the project root. This keeps production `requirements.txt` clean from data science and exploration libraries.
- **Merge into `requirements.txt`**: To combine all dependencies into a single file for the entire project, specify `notebooks_target` in `depvex.yaml`:
  ```yaml
  notebooks_target: "requirements.txt"
  ```
- **Custom target file**: You can also route notebook dependencies to another path (e.g. `notebooks_target: "notebooks/requirements.txt"`).

## CI usage

Install the project being checked and its declared dependencies before running
the check. This supplies package metadata required for import-to-distribution
mapping and ensures CI executes the checkout rather than an unrelated PyPI
release.

```yaml
- name: Install project and dependencies
  run: |
    python -m pip install -r requirements.txt
    python -m pip install .

- name: Verify dependency files
  run: depvex --check .
```

When a check fails, it prints each missing, stale, and version-changed entry.
Use `scan`, inspect the resulting diff, and commit the intended requirements
changes. Add `--pyproject` when the project dependency list is also part of the
reviewed source of truth.

## Limitations

- Only static imports are detected; dynamic imports and dependencies loaded
  from configuration are outside the analysis.
- Import names and distribution names are not always identical. Accurate
  mapping depends on installed distribution metadata or a maintained import
  mapping fallback.
- The `import_mapping_filtered.txt` fallback is continuously refined; keep it
  updated with new mappings for best results.
- The PyPI fallback needs network access and can reflect a newer version than
  the one you intend to pin.
- Syntax or decoding errors in a scanned Python file are skipped.
- Watch mode currently reacts to creation and modification events, not every
  possible filesystem operation.

## Roadmap

The next milestones are:

1. Make microservice configuration explicit and consistent, including a
   correctly named `micro_service_folders` key and documented migration.
2. Add automated coverage for single-project, microservice, watch, and CI
   workflows.
