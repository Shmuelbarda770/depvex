import ast
import sys
from dataclasses import dataclass


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


class ImportExtractor:
    def __init__(self) -> None:
        self.stdlib_modules = set(getattr(sys, "stdlib_module_names", set())) | set(sys.builtin_module_names)

    def _should_ignore(self, node: ast.AST, source_lines: list[str]) -> bool:
        line_number = getattr(node, "lineno", None)
        if line_number is None:
            return False

        line_text = source_lines[line_number - 1] if line_number - 1 < len(source_lines) else ""
        lowered = line_text.lower()
        return "ignore depvex" in lowered

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

    def extract_imports(self, code: str) -> list[str]:
        """Return third-party imports, including statically-known dynamic imports."""
        tree = ast.parse(code)
        source_lines = code.splitlines()
        type_checking_nodes = self._get_type_checking_nodes(tree)
        imports = set()

        for node in ast.walk(tree):
            if node in type_checking_nodes or self._should_ignore(node, source_lines):
                continue

            if isinstance(node, ast.Import):
                for import_name in node.names:
                    name = import_name.name.split(".")[0]
                    if name not in self.stdlib_modules:
                        imports.add(name)

            if isinstance(node, ast.ImportFrom) and node.module:
                name = node.module.split(".")[0]
                if name not in self.stdlib_modules:
                    imports.add(name)

        for finding in self.extract_dynamic_imports(code, tree=tree):
            if finding.loader in {"import_module", "__import__"} and finding.module_name:
                imports.add(finding.module_name.split(".")[0])

        return sorted(imports)

    def extract_dynamic_imports(self, code: str, tree: ast.AST | None = None) -> list[DynamicImportFinding]:
        """Find calls that load modules at runtime.

        Literal targets are safe to include in dependency discovery. Calls
        whose target is computed are returned as findings so the caller can
        warn and ask for a ``dynamic_imports`` YAML entry.
        """
        parsed_tree = tree or ast.parse(code)
        source_lines = code.splitlines()
        type_checking_nodes = self._get_type_checking_nodes(parsed_tree)
        findings: list[DynamicImportFinding] = []

        for node in ast.walk(parsed_tree):
            if node in type_checking_nodes or not isinstance(node, ast.Call) or self._should_ignore(node, source_lines):
                continue

            loader = self._dynamic_loader_name(node.func)
            if loader is None:
                continue

            target = node.args[0] if node.args else None
            module_name = target.value if isinstance(target, ast.Constant) and isinstance(target.value, str) else None
            findings.append(DynamicImportFinding(loader, node.lineno, module_name))

        return findings

    @staticmethod
    def _dynamic_loader_name(function: ast.AST) -> str | None:
        """Return the supported dynamic-loader name represented by *function*."""
        if isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}:
            return function.id
        if isinstance(function, ast.Attribute) and function.attr in {"import_module", "load_plugin", "load_entry_point"}:
            return function.attr
        return None
