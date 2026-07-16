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
- Ignores modules from the Python standard library.
- Skips imports annotated with `# ignore depvex`.
- Recursively scans `.py` files, excluding `.git`, `__pycache__`, `.venv`,
  `venv`, and `node_modules`.
- Supports additional ignored directories through `depvex.yaml`.
- Maps installed import modules to their distribution names when metadata is
  available (for example, `yaml` to `PyYAML`).
- Resolves a version from installed package metadata without launching `pip` or
  another subprocess. If it is unavailable locally and internet access is
  detected, it queries PyPI; otherwise it writes the package name without a
  version.
- Preserves an existing requirements entry when the same package is still
  imported, and removes entries for imports that disappeared.
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
```

| Command | Purpose | Result |
| --- | --- | --- |
| `depvex --scan [path]` | Scan once. | Creates or updates requirements files. |
| `depvex --check [path]` | Verify scanned dependencies. | Returns `0` when current and `1` when a requirements file is missing or differs. |
| `depvex --watch [path]` | Scan once, then watch the path. | Updates affected requirements files after the debounce delay. |

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
  `micro_servi_folders`.
- `config.json` / `depvex.json`: `CAPTIVE_PORTAL_URLS` for the connectivity
  check and `debounce_seconds` for watch mode.

The JSON `micro_servi_folders` value is not currently read for service
discovery; use YAML for that setting.

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

At present, `check` compares its newly resolved entries with the file contents.
Its output reports only that the file is out of date; it does not yet list the
missing, stale, or changed entries. Use `scan`, inspect the resulting diff, and
commit the intended requirements changes.

## Limitations

- Only static imports are detected; dynamic imports and dependencies loaded
  from configuration are outside the analysis.
- Import names and distribution names are not always identical. Accurate
  mapping depends on installed distribution metadata.
- The PyPI fallback needs network access and can reflect a newer version than
  the one you intend to pin.
- Syntax or decoding errors in a scanned Python file are skipped.
- Watch mode currently reacts to creation and modification events, not every
  possible filesystem operation.

## Roadmap

The next milestones are:

1. Use one shared calculation for scan and check so CI cannot fail merely
   because dependency resolution happened in a different environment.
2. Print an actionable check diff: missing, stale, and changed entries per
   service.
3. Make microservice configuration explicit and consistent, including a
   correctly named `micro_service_folders` key and documented migration.
4. Add automated coverage for single-project, microservice, watch, and CI
   workflows.
