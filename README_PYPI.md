# depvex

`depvex` generates and checks `requirements.txt` files from static Python
imports. It is designed for a single Python application and for repositories
that contain multiple independently managed services.

## Core capabilities

- AST-based detection of `import` and `from ... import ...` statements.
- Standard-library filtering and per-import opt-out with `# ignore depvex`.
- Skips imports inside `if TYPE_CHECKING:` blocks to avoid adding type-only imports.
- Recursive discovery across `.py` and Jupyter Notebook (`.ipynb`) files.
- Automatically isolates notebook dependencies into `requirements-notebooks.txt` (or merges into `requirements.txt` via `notebooks_target`).
- Built-in directory exclusions plus YAML `ignore_dirs` and `ignore_packages` support.
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
- Compatibility analysis for one package or an entire requirements-style file.

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
- Multiple commands can be selected together, for example
  `depvex --scan --check --report .`; they run sequentially by default.
- Use `--parallel` and optionally `--jobs N` to run one command concurrently
  across independent project paths, for example
  `depvex --parallel --scan ./service-a ./service-b`. Use
  `--continue-on-error` to continue sequential execution after a failed
  command.
- Do not combine `--parallel` with multiple commands on the same path;
  commands such as `scan` and `check` depend on one another and must run
  sequentially.
- Use `--ignore-dir DIR` to add a directory exclusion from the CLI. Repeat the
  option for multiple directories; CLI exclusions are combined with YAML
  `ignore_dirs` values.
- `--watch` cannot be combined with other commands because it is long-running.

Use `--pyproject` with `--scan` to write, or with `--check` to verify,
`[project].dependencies` in an existing `pyproject.toml`.

### Compatibility analysis

Check a package before installing it, including its direct and transitive
dependencies:

```bash
depvex --compatibility "fastapi>=0.115" .
depvex --compatibility "fastapi>=0.115" --format json .
```

To check every requirement in a requirements-style file, use
`--compatibility-file`:

```bash
depvex --compatibility-file requirements.txt .
depvex --compatibility-file install.txt --format json .
```

Blank lines and comments are ignored, as are `-r`, `--`, `-e`, and `git+`
entries. The analyzer checks each remaining requirement against the project's
existing requirements without installing packages or modifying files.

This is more than an installed-package check. `pip show PACKAGE` tells you
which version is installed in the current environment; Depvex checks whether
the version you request satisfies the project's declared constraints. For
example, if `requirements.txt` contains `requests==2.34.2`, checking
`requests==2.34.1` correctly reports a conflict, even if `requests` is already
installed, because the exact version pin is not satisfied.

The analyzer also follows direct and transitive dependencies. A package can
have no direct conflict while one of its dependencies conflicts with a package
already pinned by the project. This makes the command useful as a pre-install
review before modifying the environment or committing a new requirements
file.

By default, package metadata may be resolved from PyPI. Add `--no-network` to
use only locally installed metadata. A confirmed conflict returns exit code
`1`, an uncertain result returns `2`, and `0` means all checked requirements
are compatible.

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
