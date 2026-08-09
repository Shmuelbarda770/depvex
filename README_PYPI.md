# depvex

`depvex` generates and checks `requirements.txt` files from static Python
imports. It is designed for a single Python application and for repositories
that contain multiple independently managed services.

## Core capabilities

- AST-based detection of `import` and `from ... import ...` statements.
- Standard-library filtering and per-import opt-out with `# ignore depvex`.
- Recursive Python-file discovery with built-in ignored directories and YAML
  `ignore_dirs` and `ignore_packages` support.
- Import-module to installed-distribution mapping through `importlib.metadata`.
- Version lookup from installed metadata without subprocesses, followed by an
  optional PyPI fallback.
- Existing dependency pins are retained when the package remains in use;
  stale entries are removed during a scan.
- One-time scan, CI-oriented check, and debounced filesystem watch mode.
- Per-service requirements files plus a root requirements file for
  microservice repositories.
- Detailed check output for missing, stale, and changed entries.
- Optional `[project].dependencies` sync/check and service dependency reports.

## Install

Requires Python 3.11+.

```bash
python -m pip install depvex
```

Install PyYAML separately to enable `depvex.yaml` or `depvex.yml`:

```bash
python -m pip install PyYAML
```

## Intended command interface

```bash
depvex --scan .
depvex --check .
depvex --watch .
depvex --report .
```

- `--scan` updates requirements files.
- `--check` returns exit code `0` when requirements files match the detected
  imports and `1` otherwise.
- `--watch` performs an initial scan and updates files after created or
  modified Python-file events.
- `--report` prints dependencies per service and shared dependencies without
  changing files.

Use `--pyproject` with `--scan` to write, or with `--check` to verify,
`[project].dependencies` in an existing `pyproject.toml`.

### Version note

The new source interface above is not compatible with older published
releases, which use positional commands such as `depvex check .`. Install and
invoke one version consistently in CI.

## Single application

Without configured services, scanning a directory creates or updates its
single `requirements.txt`:

```bash
depvex --scan ./my-app
```

## Microservices

Define direct child service folders in `depvex.yaml`:

```yaml
micro_servi_folders:
  - api
  - worker

ignore_dirs:
  - tests

ignore_packages:
  - pytest
  - depvex
```

Scanning the repository produces:

```text
repository/
├── api/requirements.txt
├── worker/requirements.txt
└── requirements.txt
```

The root file covers Python files outside `api` and `worker`; service-only
imports are excluded from it. Watch mode rescans only the affected service
when a file changes below a configured service folder.

The current key spelling is `micro_servi_folders`, and service configuration is
read from YAML only. `config.json` is used for `CAPTIVE_PORTAL_URLS` and
`debounce_seconds`, not service discovery.

## CI

Install the target project's dependencies and the checked-out project before
checking, so package metadata is available and the local code is executed:

```yaml
- run: |
    python -m pip install -r requirements.txt
    python -m pip install .
- run: depvex --check .
```

`check` reports missing, stale, and changed dependency entries. Run `scan`,
review the resulting diff, and commit expected changes.

## Scope and roadmap

Depvex does not resolve dynamic imports or replace a lockfile manager. Future
work includes a canonical microservice configuration key and broader automated
coverage for single-project, microservice, watch, and CI scenarios.
