import ast
import io
import json
import re
import sys
import tokenize
from dataclasses import dataclass, field

# ``except ImportError`` (or a tuple containing it) marks a guarded, optional import.
_IMPORT_ERROR_NAMES = frozenset({"ImportError", "ModuleNotFoundError"})

# Module-level assignments whose string values name importable modules.
_CONFIG_MODULE_VARIABLES = frozenset({"pytest_plugins", "INSTALLED_APPS"})

_DOTTED_NAME_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_FALLBACK_FROM_RE = re.compile(r"^\s*from\s+(?P<module>\.*[\w.]*)\s+import\s+(?P<names>[^#]+)")
_FALLBACK_IMPORT_RE = re.compile(r"^\s*import\s+(?P<body>[^#]+)")


@dataclass(frozen=True)
class DynamicImportFinding:
    """A dynamic import expression discovered while parsing one Python file.

    ``module_name`` is populated only when the import target is a string
    literal. Non-literal targets need an explicit YAML declaration because
    their value cannot be known safely through static analysis.
    """

    loader: str
    line_number: int
    module_name: str | None = None
    optional: bool = False


@dataclass(frozen=True)
class ImportRecord:
    """One third-party import, keeping the full dotted path that was written.

    ``module`` is always absolute (relative imports are never recorded).
    ``names`` holds the imported names of a ``from module import a, b``
    statement. Both are needed to tell apart distributions that share a
    top-level package, such as ``google.cloud.storage`` and ``google.auth``.
    ``kind`` is ``static`` for ordinary statements, ``dynamic`` for literal
    ``import_module`` calls, ``config`` for names listed in settings such as
    ``INSTALLED_APPS``, and ``fallback`` when the file could not be parsed and
    the import was recovered heuristically.
    """

    module: str
    names: tuple[str, ...] = ()
    line_number: int = 0
    optional: bool = False
    kind: str = "static"

    @property
    def top_level(self) -> str:
        return self.module.split(".", 1)[0]

    def candidates(self) -> list[str]:
        """Dotted names this statement may refer to, most specific first."""
        found = [f"{self.module}.{name}" for name in self.names if name and name != "*"]
        found.append(self.module)
        return found


@dataclass
class ParseResult:
    """Everything depvex learns from one source file."""

    records: list[ImportRecord] = field(default_factory=list)
    dynamic_findings: list[DynamicImportFinding] = field(default_factory=list)
    used_fallback: bool = False
    error: str | None = None


class ImportExtractor:
    def __init__(self) -> None:
        self.stdlib_modules = set(getattr(sys, "stdlib_module_names", set())) | set(sys.builtin_module_names)

    # ------------------------------------------------------------------ helpers

    def _is_third_party(self, module_name: str) -> bool:
        top_level = module_name.split(".", 1)[0]
        return bool(top_level) and top_level not in self.stdlib_modules

    def _should_ignore(self, node: ast.AST, source_lines: list[str]) -> bool:
        """Honour ``# ignore depvex`` anywhere on the lines a statement spans."""
        first = getattr(node, "lineno", None)
        if first is None:
            return False

        last = getattr(node, "end_lineno", None) or first
        for line_number in range(first, last + 1):
            if line_number - 1 < len(source_lines) and "ignore depvex" in source_lines[line_number - 1].lower():
                return True
        return False

    @staticmethod
    def _is_type_checking_condition(node: ast.AST) -> bool:
        if isinstance(node, ast.Name) and node.id == "TYPE_CHECKING":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING":
            return True
        return False

    def _get_type_checking_nodes(self, tree: ast.AST) -> set[ast.AST]:
        type_checking_nodes: set[ast.AST] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                if self._is_type_checking_condition(node.test):
                    for stmt in node.body:
                        type_checking_nodes.update(ast.walk(stmt))
                elif (
                    isinstance(node.test, ast.UnaryOp)
                    and isinstance(node.test.op, ast.Not)
                    and self._is_type_checking_condition(node.test.operand)
                ):
                    for stmt in node.orelse:
                        type_checking_nodes.update(ast.walk(stmt))
        return type_checking_nodes

    @staticmethod
    def _handles_import_error(handler: ast.ExceptHandler) -> bool:
        def names_import_error(expr: ast.AST | None) -> bool:
            if isinstance(expr, ast.Name):
                return expr.id in _IMPORT_ERROR_NAMES
            if isinstance(expr, ast.Attribute):
                return expr.attr in _IMPORT_ERROR_NAMES
            if isinstance(expr, ast.Tuple):
                return any(names_import_error(element) for element in expr.elts)
            return False

        return names_import_error(handler.type)

    def _get_optional_nodes(self, tree: ast.AST) -> set[ast.AST]:
        """Nodes inside ``try``/``except ImportError`` guards.

        Both the guarded body and the handler bodies count: in the common
        ``try: import ujson as json`` / ``except ImportError: import simplejson as json``
        pattern the program works with either, so neither is a hard requirement.
        """
        optional_nodes: set[ast.AST] = set()
        try_types = tuple(t for t in (ast.Try, getattr(ast, "TryStar", None)) if t is not None)

        for node in ast.walk(tree):
            if isinstance(node, try_types) and any(self._handles_import_error(h) for h in node.handlers):
                for stmt in node.body:
                    optional_nodes.update(ast.walk(stmt))
                for handler in node.handlers:
                    for stmt in handler.body:
                        optional_nodes.update(ast.walk(stmt))
        return optional_nodes

    @staticmethod
    def _decode(source: str | bytes) -> str:
        """Decode source bytes using the PEP 263 cookie or BOM, never raising."""
        if isinstance(source, str):
            return source
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
        except (SyntaxError, LookupError):
            encoding = "utf-8"
        try:
            return source.decode(encoding, errors="replace")
        except LookupError:
            return source.decode("utf-8", errors="replace")

    # ----------------------------------------------------------------- notebooks

    @staticmethod
    def extract_notebook_code(content: str) -> str:
        """Extract executable Python code from a Jupyter Notebook JSON string.

        IPython magic commands (lines starting with '%' or '%%'), shell commands
        (lines starting with '!'), and interactive help queries (e.g. 'obj?')
        are converted to comments so they don't produce AST syntax errors.
        """
        try:
            notebook_data = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return ""

        if not isinstance(notebook_data, dict):
            return ""

        cells = notebook_data.get("cells", [])
        if not isinstance(cells, list):
            return ""

        code_lines: list[str] = []
        for cell in cells:
            if not isinstance(cell, dict) or cell.get("cell_type") != "code":
                continue

            source = cell.get("source", [])
            if isinstance(source, str):
                raw_lines = source.splitlines()
            elif isinstance(source, list):
                raw_lines = [str(line).rstrip("\r\n") for line in source]
            else:
                raw_lines = []

            for line in raw_lines:
                stripped = line.strip()
                if stripped.startswith(("%", "!", "?")) or stripped.endswith("?"):
                    code_lines.append(f"# [depvex-notebook-filter] {line}")
                else:
                    code_lines.append(line)

            code_lines.append("")

        return "\n".join(code_lines)

    def parse_notebook(self, content: str) -> ParseResult:
        """Parse a notebook's code cells, recovering imports even from broken cells."""
        clean_code = self.extract_notebook_code(content)
        if not clean_code.strip():
            return ParseResult()
        return self.parse_source(clean_code)

    def extract_notebook_imports(self, content: str) -> list[str]:
        """Extract third-party imports from a Jupyter Notebook JSON string."""
        return sorted({record.top_level for record in self.parse_notebook(content).records})

    # ------------------------------------------------------------------ parsing

    def parse_source(self, source: str | bytes) -> ParseResult:
        """Parse *source* without ever raising.

        Python 2 files, files with a stray syntax error, and partially edited
        files still contribute their imports: when ``ast`` rejects the source
        the import statements are recovered line by line and the result is
        flagged with ``used_fallback`` so callers can warn about it.
        """
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError) as exc:
            text = self._decode(source)
            return ParseResult(
                records=self._extract_fallback_records(text),
                used_fallback=True,
                error=f"{type(exc).__name__}: {exc}",
            )

        text = self._decode(source)
        return ParseResult(
            records=self.extract_records(text, tree=tree),
            dynamic_findings=self.extract_dynamic_imports(text, tree=tree),
        )

    def extract_records(self, code: str, tree: ast.AST | None = None) -> list[ImportRecord]:
        """Return every third-party import in *code*; raises ``SyntaxError`` on invalid source."""
        parsed_tree = tree or ast.parse(code)
        source_lines = code.splitlines()
        type_checking_nodes = self._get_type_checking_nodes(parsed_tree)
        optional_nodes = self._get_optional_nodes(parsed_tree)
        records: list[ImportRecord] = []

        for node in ast.walk(parsed_tree):
            if node in type_checking_nodes or self._should_ignore(node, source_lines):
                continue

            if isinstance(node, ast.Import):
                for alias in node.names:
                    if self._is_third_party(alias.name):
                        records.append(ImportRecord(alias.name, (), node.lineno, node in optional_nodes))
            elif isinstance(node, ast.ImportFrom):
                # ``from . import x`` and ``from .pkg import y`` always refer to the project itself.
                if node.level or not node.module or not self._is_third_party(node.module):
                    continue
                names = tuple(alias.name for alias in node.names)
                records.append(ImportRecord(node.module, names, node.lineno, node in optional_nodes))

        for finding in self.extract_dynamic_imports(code, tree=parsed_tree):
            if finding.loader not in {"import_module", "__import__"} or not finding.module_name:
                continue
            if finding.module_name.startswith(".") or not self._is_third_party(finding.module_name):
                continue
            records.append(
                ImportRecord(finding.module_name, (), finding.line_number, finding.optional, kind="dynamic")
            )

        records.extend(self._extract_config_records(parsed_tree, source_lines, type_checking_nodes))
        return records

    def extract_imports(self, code: str) -> list[str]:
        """Return third-party top-level imports, including statically-known dynamic imports."""
        return sorted({record.top_level for record in self.extract_records(code)})

    def extract_dynamic_imports(self, code: str, tree: ast.AST | None = None) -> list[DynamicImportFinding]:
        """Find calls that load modules at runtime.

        Literal targets are safe to include in dependency discovery. Calls
        whose target is computed are returned as findings so the caller can
        warn and ask for a ``dynamic_imports`` YAML entry.
        """
        parsed_tree = tree or ast.parse(code)
        source_lines = code.splitlines()
        type_checking_nodes = self._get_type_checking_nodes(parsed_tree)
        optional_nodes = self._get_optional_nodes(parsed_tree)
        findings: list[DynamicImportFinding] = []

        for node in ast.walk(parsed_tree):
            if node in type_checking_nodes or not isinstance(node, ast.Call) or self._should_ignore(node, source_lines):
                continue

            loader = self._dynamic_loader_name(node.func)
            if loader is None:
                continue

            target = node.args[0] if node.args else None
            module_name = target.value if isinstance(target, ast.Constant) and isinstance(target.value, str) else None
            findings.append(DynamicImportFinding(loader, node.lineno, module_name, node in optional_nodes))

        return findings

    @staticmethod
    def _dynamic_loader_name(function: ast.AST) -> str | None:
        """Return the supported dynamic-loader name represented by *function*."""
        if isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}:
            return function.id
        if isinstance(function, ast.Attribute) and function.attr in {
            "import_module",
            "load_plugin",
            "load_entry_point",
        }:
            return function.attr
        return None

    def _extract_config_records(
        self, tree: ast.AST, source_lines: list[str], type_checking_nodes: set[ast.AST]
    ) -> list[ImportRecord]:
        """Find modules named in ``pytest_plugins`` and Django's ``INSTALLED_APPS``.

        These are imported by the framework rather than by an import
        statement, so a plain AST import scan never sees them. Only
        module-level assignments of string literals are considered.
        """
        records: list[ImportRecord] = []
        body = getattr(tree, "body", [])

        for node in body:
            if node in type_checking_nodes or self._should_ignore(node, source_lines):
                continue

            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.AugAssign):
                targets, value = [node.target], node.value
            else:
                continue

            if not any(isinstance(t, ast.Name) and t.id in _CONFIG_MODULE_VARIABLES for t in targets):
                continue

            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                entries = [value.value]
            elif isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                entries = [
                    element.value
                    for element in value.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                ]
            else:
                continue

            for entry in entries:
                if _DOTTED_NAME_RE.match(entry) and self._is_third_party(entry):
                    records.append(ImportRecord(entry, (), node.lineno, kind="config"))
        return records

    # ---------------------------------------------------------------- fallback

    def _extract_fallback_records(self, text: str) -> list[ImportRecord]:
        """Recover import statements line by line from source ``ast`` cannot parse."""
        records: list[ImportRecord] = []
        open_docstring: str | None = None

        for line_number, line in enumerate(text.splitlines(), start=1):
            inside_docstring = open_docstring is not None
            for delimiter in ('"""', "'''"):
                if line.count(delimiter) % 2 == 1:
                    if open_docstring == delimiter:
                        open_docstring = None
                    elif open_docstring is None:
                        open_docstring = delimiter
            if inside_docstring:
                continue

            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "ignore depvex" in line.lower():
                continue

            for statement in stripped.split(";"):
                from_match = _FALLBACK_FROM_RE.match(statement)
                if from_match:
                    module = from_match.group("module")
                    if not module or module.startswith(".") or not self._is_third_party(module):
                        continue
                    names = tuple(
                        part.strip().strip("()").split(" as ")[0].strip()
                        for part in from_match.group("names").split(",")
                        if part.strip().strip("()")
                    )
                    names = tuple(name for name in names if _DOTTED_NAME_RE.match(name) or name == "*")
                    if _DOTTED_NAME_RE.match(module):
                        records.append(ImportRecord(module, names, line_number, kind="fallback"))
                    continue

                import_match = _FALLBACK_IMPORT_RE.match(statement)
                if import_match:
                    for part in import_match.group("body").split(","):
                        pieces = part.strip().split()
                        if pieces and _DOTTED_NAME_RE.match(pieces[0]) and self._is_third_party(pieces[0]):
                            records.append(ImportRecord(pieces[0], (), line_number, kind="fallback"))
        return records
